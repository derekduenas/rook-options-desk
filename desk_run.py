"""Run and record the local desk's research checks without changing the journal."""

import argparse
from collections import Counter
from contextlib import closing
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
from tempfile import NamedTemporaryFile

from capture import capture_status, credentials_from_environment
from candidate_review import candidate_review
from candidate_sizing import size_report
from catalyst import latest_studies
from desk_engines import catalyst_engine, directional_engine, volatility_engine
from earnings import list_experiments, now_utc
from forecast import forecast_report
from expression_review import expression_report
from exit_replay import exit_report
from hedge_replay import hedge_replay
from market_risk import portfolio_report
from market_scan import scan_report
from optionsdesk import Journal, canonical, digest, money
from price_limits import price_limit_report
from scorecard import cohort_report


ROOT = Path(__file__).resolve().parent
RUN_FILE = "research-run.json"
ENGINE_INPUTS = {
    "directional": {"closes", "tape_plan", "screener"},
    "volatility": {"closes", "volatility_plan", "overnight_variance", "regime_diagnostics"},
    "catalyst": {"closes", "direction_plan", "option_overlay"},
}


def journal_checks(db, as_of):
    with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("BEGIN")
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("journal integrity check failed")
        journal = Journal(connection, initialize=False)
        status = journal.status()
        for decision_id in status["decisions"]:
            journal.replay(decision_id)
        experiments = list_experiments(journal)
        events = {(event["symbol"], event["event_at"]) for exp in experiments for event in exp["events"]}
        studies = latest_studies(journal)
        kinds = Counter(
            row["study"]["catalyst"].get("event_type", "earnings" if
            (row["study"]["symbol"], row["study"]["catalyst"]["event_at"]) in events else "unclassified")
            for row in studies
        )
        tasks = capture_status(journal, as_of=as_of, auth=credentials_from_environment())["tasks"]
        cohorts = cohort_report(journal, as_of=as_of)["cohorts"]
        forecasts = forecast_report(journal, {"as_of": as_of, "phase": "post", "target": "stock_up"})["reports"]
    return {
        "integrity": "passed", "observations": status["observations"], "decisions_replayed": len(status["decisions"]),
        "unique_earnings_events": len(events), "catalyst_studies": len(studies),
        "catalyst_studies_by_type": dict(kinds),
        "cutoffs_by_status": dict(Counter(row["status"] for row in tasks)),
        "next_cutoff": tasks_next(tasks),
        "eligible_prospective_phases": sum(row["coverage"]["eligible"] for row in cohorts),
        "scored_post_release_forecasts": sum(row["status"] == "scored_research" for row in forecasts),
    }


def tasks_next(tasks):
    row = next((row for row in tasks if row["status"] in {"scheduled", "capture_window"}), None)
    return {key: row[key] for key in ("symbol", "stage", "cutoff", "transport")} if row else None


def input_paths(db, root):
    return {
        "price_limits": root / "examples/price-limits.json",
        "portfolio": root / "examples/advanced-risk.json",
        "candidate_review": root / "examples/candidate-review.json",
        "candidate_sizing": root / "examples/candidate-sizing.json",
        "expression_review": root / "examples/expression-review.json",
        "exit_replay": root / "examples/exit-replay.json",
        "hedge_replay": root / "examples/hedge-replay.json",
        "closes": db.parent / "earnings-relative-strength-2026-input.json",
        "direction_plan": db.parent / "plans/earnings-relative-strength-2026.json",
        "tape_plan": db.parent / "plans/directional-tape-benchmark.json",
        "volatility_plan": db.parent / "plans/volatility-power-law-challenger.json",
        "overnight_variance": db.parent / "overnight-variance-input.json",
        "regime_diagnostics": db.parent / "jump-regime-input.json",
        "option_overlay": db.parent / "eight-name-earnings-eod-cohort.json",
        "screener": db.parent / "tradealgo-2026-11-27-morning-audit.csv",
        "market_scan": db.parent / "market-scan-input.json",
    }


def read_inputs(paths):
    payloads = {name: path.read_bytes() for name, path in paths.items() if path.is_file()}
    hashes = {name: hashlib.sha256(value).hexdigest() for name, value in payloads.items()}
    data = {name: json.loads(value) for name, value in payloads.items() if name != "screener"}
    return data, payloads.get("screener"), hashes


def run_engine(name, db, *, root=ROOT):
    paths = {key: path for key, path in input_paths(db, root).items() if key in ENGINE_INPUTS[name] | {"market_scan"}}
    data, screen, hashes = read_inputs(paths)
    if name == "directional":
        result = directional_engine(data, screen)
    elif name == "volatility":
        result = volatility_engine(data)
    else:
        result = catalyst_engine(data, journal_checks(db, now_utc()))
    return {"engine": name, "result": result,
            "intake": scan_report(data["market_scan"], evaluated_at=now_utc()) if "market_scan" in data else None,
            "input_sha256": hashes,
            "missing_inputs": sorted(paths.keys() - hashes.keys())}


def run_desk(db, *, root=ROOT):
    as_of = now_utc()
    journal = journal_checks(db, as_of)
    paths = input_paths(db, root)
    data, screen, hashes = read_inputs(paths)
    directional = directional_engine(data, screen)
    volatility = volatility_engine(data)
    catalyst = catalyst_engine(data, journal)
    history = {key: value for key, value in {
        "direction": catalyst["earnings_benchmark"], "option_overlay": catalyst["option_overlay"],
        "volatility": volatility["benchmark"], "screener": directional["screener"],
    }.items() if value is not None}
    price = price_limit_report(data["price_limits"])
    risk = portfolio_report(data["portfolio"])
    hedge = hedge_replay(data["hedge_replay"]) if "hedge_replay" in data else None
    for row in risk["advanced_risk"]["attribution_before_exit_cost_dollars"].values():
        if row["mark_change"] is not None and money(sum(Decimal(row[key]) for key in
                ("spot", "implied_volatility", "time"))) != row["mark_change"]:
            raise ValueError("portfolio mark attribution does not reconcile")
    report = {
        "schema_version": 1, "generated_at": as_of, "basis": "local_research_run",
        "journal": journal, "historical": history, "missing_inputs": sorted(paths.keys() - hashes.keys()),
        "engines": {"directional": directional, "volatility": volatility, "catalyst": catalyst},
        "intake": scan_report(data["market_scan"], evaluated_at=as_of) if "market_scan" in data else None,
        "synthetic": {
            "price_limits": {key: price[key] for key in ("highest_passing_tested_limit",
                              "tested_limit_with_best_worst_model_expectancy", "prices")},
            "risk_attribution": risk["advanced_risk"]["attribution_before_exit_cost_dollars"],
            "candidate_review": candidate_review(data["candidate_review"]) if "candidate_review" in data else None,
            "candidate_sizing": size_report(data["candidate_sizing"]) if "candidate_sizing" in data else None,
            "expression_review": expression_report(data["expression_review"]) if "expression_review" in data else None,
            "exit_replay": exit_report(data["exit_replay"]) if "exit_replay" in data else None,
            "hedge_replay": (
                [{key: value for key, value in row.items() if key != "attempts"} for row in hedge["policies"]]
                if hedge is not None else None
            ),
        },
        "input_sha256": hashes,
    }
    return {**report, "run_id": digest(report)}


def load_run(path):
    if not path.is_file():
        return None
    report = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise ValueError("invalid desk run")
    run_id = report.pop("run_id", None)
    if report.get("schema_version") != 1 or report.get("basis") != "local_research_run" or digest(report) != run_id:
        raise ValueError("invalid desk run integrity or version")
    return {**report, "run_id": run_id}


def write_run(db, report):
    output = db.parent / RUN_FILE
    protected = [db, *(db.with_name(db.name + suffix) for suffix in ("-wal", "-shm", "-journal"))]
    if any(output.resolve() == path.resolve() or
           (output.exists() and path.exists() and output.samefile(path)) for path in protected):
        raise ValueError("desk report path overlaps the journal")
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent, delete=False) as stream:
        staged = Path(stream.name)
        try:
            stream.write(canonical(report) + "\n")
            stream.close()
            staged.replace(output)
        finally:
            staged.unlink(missing_ok=True)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data/journal.sqlite3")
    parser.add_argument("--engine", choices=ENGINE_INPUTS, help="run only this engine and print its report")
    args = parser.parse_args()
    try:
        if args.engine:
            print(json.dumps(run_engine(args.engine, args.db), indent=2, allow_nan=False))
        else:
            print(write_run(args.db, run_desk(args.db)))
    except (OSError, ValueError, sqlite3.Error) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
