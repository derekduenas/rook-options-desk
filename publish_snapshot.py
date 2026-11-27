"""Build a hostable read model from explicitly selected Options Desk decisions."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from shutil import copyfile
import sqlite3
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from dashboard import ROOT, terminal_snapshot

SCHEMA_VERSION = 1
ASSETS = ("terminal.css", "terminal.js", "workspace.js")
RELEASE_FILES = (*ASSETS, "index.html", "desk.json")
SNAPSHOT_FIELDS = {"schema_version", "mode", "generated_at", "observations", "sources", "decisions", "workspace_market"}
SOURCE_FIELDS = ("source", "feed", "captures", "last_received_at")


def fields(record, names):
    selected = {name: record[name] for name in names if name in record}
    if any(value is not None and not isinstance(value, (str, int, float, bool)) for value in selected.values()):
        raise ValueError("publication fields must contain scalar values")
    return selected


def project_decision(decision):
    result = {
        "decision_id": decision["decision_id"],
        "plan": fields(decision["plan"], ("name", "symbol", "expiry", "long_strike", "short_strike")),
        "analysis": fields(decision["analysis"], ("quote_status", "decision_status",
            "synthetic_ask_debit_dollars", "standalone_max_loss_dollars",
            "standalone_max_profit_dollars", "expiration_breakeven_stock_price")),
        "review": None,
    }
    result["analysis"]["scenarios"] = [
        fields(row, ("name", "terminal_stock_price", "pnl_dollars"))
        for row in decision["analysis"].get("scenarios", [])
    ]
    review = decision["review"]
    if review:
        result["review"] = fields(review, ("analyst", "as_of", "verdict", "summary", "invalidation"))
        result["review"]["evidence"] = [
            fields(item, ("title", "claim", "source", "status", "available_at"))
            for item in review["evidence"]
        ]
        for item in result["review"]["evidence"]:
            source = urlsplit(item["source"])
            if source.username is not None or source.password is not None:
                raise ValueError("publication evidence links cannot include embedded credentials")
        result["review"]["comparison"] = fields(review["comparison"], (
            "status", "reason", "baseline", "enhanced", "delta_dollars"
        ))
    return result


def project_market(market):
    result = fields(market, ("as_of", "discovery_received_at", "option_feed",
                            "unpartitioned_matches", "has_discovery_discrepancy"))
    result["market_rows"] = [fields(row, ("symbol", "last", "change_pct", "options_volume", "received_at"))
                             for row in market["market_rows"]]
    result["contracts"] = [
        {**fields(row, ("code", "symbol", "expiry", "strike", "right")),
         "quote": fields(row["quote"], ("bid", "ask", "bid_size", "ask_size", "observed_at", "implied_volatility"))
         if row["quote"] is not None else None}
        for row in market["contracts"]
    ]
    result["history"] = {
        symbol: [list(fields({"date": date, "close": close}, ("date", "close")).values()) for date, close in points]
        for symbol, points in market["history"].items()
    }
    return result


def published_snapshot(snapshot, decision_ids, generated_at):
    decisions = {decision["decision_id"]: decision for decision in snapshot["decisions"]}
    missing = set(decision_ids) - decisions.keys()
    if missing:
        raise ValueError(f"Unknown decision IDs: {', '.join(sorted(missing))}")
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "published_snapshot",
        "generated_at": generated_at,
        "observations": snapshot["observations"],
        "sources": [fields(source, SOURCE_FIELDS) for source in snapshot["sources"]],
        "decisions": [project_decision(decisions[decision_id]) for decision_id in dict.fromkeys(decision_ids)],
        **({"workspace_market": project_market(snapshot["workspace_market"])} if snapshot.get("workspace_market") else {}),
    }


def validate_publication(snapshot):
    if (not isinstance(snapshot, dict) or snapshot.get("mode") != "published_snapshot"
            or snapshot.get("schema_version") != SCHEMA_VERSION):
        raise ValueError("expected a versioned publication snapshot")
    if set(snapshot) - SNAPSHOT_FIELDS:
        raise ValueError("snapshot contains nonpublication fields")
    if not isinstance(snapshot.get("decisions"), list) or not isinstance(snapshot.get("sources", []), list):
        raise ValueError("snapshot is missing valid publication records")
    fields(snapshot, SNAPSHOT_FIELDS - {"sources", "decisions", "workspace_market"})
    try:
        if "workspace_market" in snapshot and project_market(snapshot["workspace_market"]) != snapshot["workspace_market"]:
            raise ValueError("snapshot contains nonpublication market fields")
        for source in snapshot.get("sources", []):
            if fields(source, SOURCE_FIELDS) != source:
                raise ValueError("snapshot contains nonpublication source fields")
        for decision in snapshot["decisions"]:
            fields(decision, ("decision_id",))
            if project_decision(decision) != decision:
                raise ValueError("snapshot contains nonpublication decision fields")
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError("snapshot is missing valid publication records") from error


def write_publication(output, snapshot):
    validate_publication(snapshot)
    if output.is_symlink():
        raise ValueError("the publication directory must not be a symbolic link")
    output = output.resolve()
    if any(output.is_relative_to((ROOT / directory).resolve()) for directory in ("terminal", "data")):
        raise ValueError("choose a separate publication directory")
    payload = json.dumps(snapshot, indent=2, allow_nan=False) + "\n"
    output.mkdir(parents=True, exist_ok=True)
    unexpected = sorted(path.name for path in output.iterdir()
                        if path.name not in RELEASE_FILES or path.is_symlink() or not path.is_file())
    if unexpected:
        raise ValueError(f"unexpected publication entries: {', '.join(unexpected)}")

    assets = ROOT / "terminal"
    with TemporaryDirectory(dir=output.parent) as staging:
        staged = Path(staging)
        for asset in ASSETS:
            copyfile(assets / asset, staged / asset)
        html = (assets / "index.html").read_text(encoding="utf-8")
        (staged / "index.html").write_text(html.replace('<html lang="en">',
            '<html lang="en" data-desk-endpoint="desk.json">', 1), encoding="utf-8")
        (staged / "desk.json").write_text(payload, encoding="utf-8")
        for name in RELEASE_FILES:
            (staged / name).replace(output / name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "journal.sqlite3")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--decision", action="append", required=True)
    parser.add_argument("--workspace", action="store_true", help="include the market-only terminal snapshot")
    args = parser.parse_args()
    try:
        if args.db.resolve().is_relative_to(args.out.resolve()):
            raise ValueError("the publication directory must be separate from the source journal")
        selected = terminal_snapshot(args.db, include_studies=False, decision_ids=args.decision,
                                     include_workspace=args.workspace)
        snapshot = published_snapshot(selected, args.decision, datetime.now(timezone.utc).isoformat())
        write_publication(args.out, snapshot)
    except (OSError, sqlite3.Error, ValueError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
