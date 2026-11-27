#!/usr/bin/env python3
"""Record timestamped options research and replay bounded call-spread decisions."""

import argparse
from contextlib import closing
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from time import sleep
from zoneinfo import ZoneInfo

from providers import normalize_mcp


VERSION = 2
CENT = Decimal("0.01")
KINDS = {"option_quote", "underlying_trade", "open_interest"}
ISO_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.(\d{1,9}))?(?:Z|[+-]\d{2}:\d{2})")
NEW_YORK = ZoneInfo("America/New_York")


def timestamp(value):
    value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("timestamps must include a time zone")
    return value.astimezone(timezone.utc)


def utc(value):
    match = ISO_TIME.fullmatch(value)
    if not match:
        raise ValueError("expected an ISO 8601 timestamp with a time zone and at most nine fractional digits")
    fraction = (match.group(1) or "").ljust(9, "0")
    return f"{timestamp(value):%Y-%m-%dT%H:%M:%S}.{fraction}+00:00"


def seconds_between(later, earlier):
    """Return elapsed seconds for two canonical UTC timestamps without losing precision."""
    elapsed = timestamp(later) - timestamp(earlier)
    whole_seconds = elapsed.days * 86400 + elapsed.seconds
    trailing_ns = int(later.split(".")[1][6:9]) - int(earlier.split(".")[1][6:9])
    return Decimal(whole_seconds) + Decimal(elapsed.microseconds).scaleb(-6) + Decimal(trailing_ns).scaleb(-9)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def decimal(value):
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError(f"expected a finite nonnegative number, got {value!r}")
    return result


def money(value):
    return str(value.quantize(CENT))


def identifier(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("expected a nonempty string")
    return value.strip()


def contract(symbol, expiry, strike, right):
    strike_code = decimal(strike) * 1000
    if strike_code != strike_code.to_integral_value() or not 0 < strike_code < 100_000_000:
        raise ValueError("strike must be positive and fit the OCC strike code")
    return f"{symbol}{date.fromisoformat(expiry):%y%m%d}{right}{int(strike_code):08d}"


def validate_quote(row):
    bid, ask = decimal(row["bid"]), decimal(row["ask"])
    if bid > ask:
        raise ValueError(f"crossed quote for {row['instrument']}")
    for field in ("bid_size", "ask_size", "multiplier"):
        value = decimal(row[field])
        if value != int(value) or (field == "multiplier" and value == 0):
            raise ValueError(f"invalid {field} for {row['instrument']}")


class Journal:
    def __init__(self, connection, *, initialize=True):
        self.db = connection
        self.db.execute("PRAGMA foreign_keys = ON")
        if not initialize:
            return
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS captures (
                id TEXT PRIMARY KEY, received_at TEXT NOT NULL, source TEXT NOT NULL,
                feed TEXT NOT NULL, document TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS observations (
                capture_id TEXT NOT NULL REFERENCES captures(id),
                kind TEXT NOT NULL, instrument TEXT NOT NULL, observed_at TEXT NOT NULL,
                document TEXT NOT NULL, PRIMARY KEY (capture_id, kind, instrument)
            );
            CREATE INDEX IF NOT EXISTS observations_lookup
                ON observations(kind, instrument, observed_at);
            CREATE TABLE IF NOT EXISTS decisions (
                id TEXT PRIMARY KEY, frozen TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reviews (
                id TEXT PRIMARY KEY, decision_id TEXT NOT NULL REFERENCES decisions(id),
                as_of TEXT NOT NULL, document TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS catalyst_studies (
                id TEXT PRIMARY KEY, frozen TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS earnings_experiments (
                id TEXT PRIMARY KEY, protocol_key TEXT NOT NULL UNIQUE, frozen TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS earnings_events (
                id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL REFERENCES earnings_experiments(id),
                symbol TEXT NOT NULL, event_at TEXT NOT NULL, frozen TEXT NOT NULL,
                UNIQUE (experiment_id, symbol, event_at)
            );
            CREATE TABLE IF NOT EXISTS earnings_entries (
                id TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES earnings_events(id),
                phase TEXT NOT NULL, frozen TEXT NOT NULL, UNIQUE (event_id, phase)
            );
            CREATE TABLE IF NOT EXISTS earnings_replays (
                id TEXT PRIMARY KEY, entry_id TEXT NOT NULL UNIQUE REFERENCES earnings_entries(id),
                frozen TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS earnings_paper (
                id TEXT PRIMARY KEY, entry_id TEXT NOT NULL REFERENCES earnings_entries(id),
                structure TEXT NOT NULL, source TEXT NOT NULL, reference TEXT NOT NULL, frozen TEXT NOT NULL,
                UNIQUE (entry_id, structure), UNIQUE (source, reference)
            );
        """)
        from capture import init_schema as init_capture_schema
        from event_quantiles import initialize as init_event_quantiles

        init_capture_schema(self.db)
        init_event_quantiles(self.db)

    def record(self, bundle):
        received = utc(bundle["received_at"])
        if received > utc(datetime.now(timezone.utc).isoformat()):
            raise ValueError("capture receipt cannot be in the future")
        observations = []
        for original in bundle["observations"]:
            row = dict(original)
            if {"capture_id", "received_at", "source", "feed"} & row.keys():
                raise ValueError("observation cannot override capture metadata")
            row["instrument"] = identifier(row["instrument"])
            kind = row["kind"]
            if kind not in KINDS:
                raise ValueError(f"unknown observation kind {kind!r}")
            row["observed_at"] = utc(row["observed_at"])
            if row["observed_at"] > received:
                raise ValueError("observation is newer than its receipt")
            if kind == "option_quote":
                validate_quote(row)
            elif kind == "underlying_trade":
                if decimal(row["price"]) == 0:
                    raise ValueError("underlying price must be positive")
                identifier(row["session"])
            else:
                day = date.fromisoformat(row["effective_date"])
                if day > timestamp(received).astimezone(NEW_YORK).date() or decimal(row["contracts"]) % 1:
                    raise ValueError("invalid open interest or effective date")
                row["effective_date"] = day.isoformat()
            observations.append(row)
        keys = [(row["kind"], row["instrument"]) for row in observations]
        if not keys or len(set(keys)) != len(keys):
            raise ValueError("a capture needs at least one observation and unique kind/instrument pairs")
        document = {
            "received_at": received,
            "source": identifier(bundle["source"]),
            "feed": identifier(bundle["feed"]),
            "observations": sorted(observations, key=lambda row: (row["kind"], row["instrument"])),
        }
        capture_id = digest(document)
        with self.db:
            inserted = self.db.execute(
                "INSERT OR IGNORE INTO captures VALUES (?, ?, ?, ?, ?)",
                (capture_id, received, document["source"], document["feed"], canonical(document)),
            ).rowcount
            if inserted:
                self.db.executemany(
                    "INSERT INTO observations VALUES (?, ?, ?, ?, ?)",
                    ((capture_id, row["kind"], row["instrument"], row["observed_at"], canonical(row))
                     for row in document["observations"]),
                )
        return {"capture_id": capture_id, "observations": len(observations), "inserted": bool(inserted)}

    def observations(self, kind, instrument, as_of):
        rows = self.db.execute("""
            SELECT c.id, c.received_at, c.source, c.feed, o.document
            FROM observations o JOIN captures c ON c.id = o.capture_id
            WHERE o.kind = ? AND o.instrument = ? AND c.received_at <= ?
              AND o.observed_at <= ?
            ORDER BY o.observed_at DESC, c.received_at DESC, c.id DESC
        """, (kind, instrument, as_of, as_of))
        # Publication order breaks ties within an OI session. A late capture of
        # an older session cannot displace the latest eligible session.
        latest = {}
        for capture_id, received, provider, feed, document in rows:
            key = (provider, feed)
            row = json.loads(document)
            previous = latest.get(key)
            if previous is not None and (kind != "open_interest" or
                    date.fromisoformat(row["effective_date"]) <= date.fromisoformat(previous["effective_date"])):
                continue
            latest[key] = {
                "capture_id": capture_id, "received_at": received, "source": provider,
                "feed": feed, **row,
            }
        return list(latest.values())

    def evaluate(self, plan):
        plan = validate_plan(plan)
        symbol, expiry, as_of = plan["symbol"], plan["expiry"], plan["as_of"]
        symbols = [contract(symbol, expiry, plan[field], "C") for field in ("long_strike", "short_strike")]
        with self.db:
            # Resolve all legs against one database snapshot, even during capture.
            self.db.execute("BEGIN IMMEDIATE")
            evidence = {
                "legs": {code: self.observations("option_quote", code, as_of) for code in symbols},
                "underlying": self.observations("underlying_trade", symbol, as_of),
                "open_interest": {code: self.observations("open_interest", code, as_of) for code in symbols},
            }
            frozen = {"version": VERSION, "plan": plan, "evidence": evidence}
            result = self.render(frozen)
            self.db.execute("INSERT OR IGNORE INTO decisions VALUES (?, ?)",
                            (result["decision_id"], canonical(frozen)))
        return result

    @staticmethod
    def render(frozen):
        version = frozen["version"]
        if version not in (1, VERSION):
            raise ValueError("unsupported frozen decision version")
        return {"decision_id": digest(frozen), **frozen,
                "analysis": analyze(frozen["plan"], frozen["evidence"], version=version)}

    def replay(self, decision_id):
        row = self.db.execute("SELECT frozen FROM decisions WHERE id = ?", (decision_id,)).fetchone()
        if not row:
            raise ValueError(f"decision not found: {decision_id}")
        frozen = json.loads(row[0])
        if digest(frozen) != decision_id:
            raise ValueError("frozen decision failed its integrity check")
        return self.render(frozen)

    def status(self):
        return {
            "sources": [dict(zip(("source", "feed", "captures", "last_received_at"), row))
                        for row in self.db.execute("""
                            SELECT source, feed, count(*), max(received_at)
                            FROM captures GROUP BY source, feed ORDER BY source, feed
                        """)],
            "observations": self.db.execute("SELECT count(*) FROM observations").fetchone()[0],
            "decisions": [row[0] for row in self.db.execute("SELECT id FROM decisions ORDER BY rowid")],
        }

    def inspect(self, instrument, as_of):
        as_of = utc(as_of)
        instrument = identifier(instrument).upper().replace(" ", "")
        return {"instrument": instrument, "as_of": as_of,
                **{kind: self.observations(kind, instrument, as_of) for kind in sorted(KINDS)}}


def validate_plan(original):
    plan = dict(original)
    for field in ("name", "symbol", "quote_source", "quote_feed", "thesis", "invalidation"):
        plan[field] = identifier(plan[field])
    plan["symbol"] = plan["symbol"].upper()
    plan["as_of"] = utc(plan["as_of"])
    if plan["as_of"] > utc(datetime.now(timezone.utc).isoformat()):
        raise ValueError("analysis cutoff cannot be in the future")
    if date.fromisoformat(plan["expiry"]) < timestamp(plan["as_of"]).astimezone(NEW_YORK).date():
        raise ValueError("expiration predates the analysis date")
    lower, upper = (decimal(plan[field]) for field in ("long_strike", "short_strike"))
    if upper <= lower:
        raise ValueError("a long call vertical requires a higher short strike")
    for field in ("max_quote_age_seconds", "max_leg_skew_seconds"):
        if decimal(plan[field]) == 0:
            raise ValueError(f"{field} must be positive")
    decimal(plan.get("fees_dollars", 0))
    decimal(plan.get("required_expected_profit_dollars", 0))
    scenarios = plan["scenarios"]
    if not scenarios:
        raise ValueError("include at least one terminal scenario")
    for scenario in scenarios:
        identifier(scenario["name"])
        decimal(scenario["terminal_stock_price"])
    if len({scenario["name"] for scenario in scenarios}) != len(scenarios):
        raise ValueError("scenario names must be unique")
    if any("probability" in scenario for scenario in scenarios):
        if not all("probability" in scenario for scenario in scenarios):
            raise ValueError("provide probabilities for every scenario or omit all")
        if sum(decimal(scenario["probability"]) for scenario in scenarios) != 1:
            raise ValueError("scenario probabilities must sum to 1")
    if "portfolio" in plan:
        book = plan["portfolio"]
        if book["horizon"] != plan["expiry"]:
            raise ValueError("portfolio scenarios must have the same horizon as the spread")
        if utc(book["as_of"]) != plan["as_of"]:
            raise ValueError("portfolio scenarios must be synchronized with the decision")
        identifier(book["source"])
        if set(book["pnl_dollars"]) != {scenario["name"] for scenario in scenarios}:
            raise ValueError("portfolio P&L must match the decision's scenario names")
        for value in book["pnl_dollars"].values():
            if not Decimal(str(value)).is_finite():
                raise ValueError("portfolio P&L must be finite")
    return plan


def analyze(plan, evidence, *, version=VERSION):
    source = (plan["quote_source"], plan["quote_feed"])
    lower, upper = (decimal(plan[field]) for field in ("long_strike", "short_strike"))
    codes = [contract(plan["symbol"], plan["expiry"], strike, "C") for strike in (lower, upper)]
    legs = [next((row for row in evidence["legs"][code]
                  if (row["source"], row["feed"]) == source), None) for code in codes]
    if any(row is None for row in legs):
        return {"quote_status": "missing", "decision_status": "insufficient_data", "contracts": codes}
    long, short = legs
    if any(decimal(row["multiplier"]) != 100 or row.get("adjusted", False) for row in legs):
        return {"quote_status": "unsupported_deliverable", "decision_status": "insufficient_data"}
    sizes = min(int(decimal(long["ask_size"])), int(decimal(short["bid_size"])))
    two_sided_fields = ("bid", "ask") if version == 1 else ("bid", "ask", "bid_size", "ask_size")
    if any(decimal(row[field]) == 0 for row in legs for field in two_sided_fields) or sizes == 0:
        return {"quote_status": "no_two_sided_size", "decision_status": "insufficient_data"}
    quote_times = [row["observed_at"] for row in legs]
    if version == 1:
        times = [timestamp(value) for value in quote_times]
        as_of = timestamp(plan["as_of"])
        quote_age = max((as_of - time).total_seconds() for time in times)
        skew = abs((times[0] - times[1]).total_seconds())
    else:
        quote_age = seconds_between(plan["as_of"], min(quote_times))
        skew = abs(seconds_between(*quote_times))
    freshness = "fresh"
    if quote_age > decimal(plan["max_quote_age_seconds"]):
        freshness = "stale"
    elif skew > decimal(plan["max_leg_skew_seconds"]):
        freshness = "unsynchronized"
    elif version >= 2 and source == ("alpaca", "indicative"):
        freshness = "indicative"
    multiplier = decimal(long["multiplier"])
    debit = decimal(long["ask"]) - decimal(short["bid"])
    lower_bound = decimal(long["bid"]) - decimal(short["ask"])
    if not 0 < debit < upper - lower:
        return {"quote_status": "invalid_debit", "decision_status": "insufficient_data"}
    fees_per_share = decimal(plan.get("fees_dollars", 0)) / multiplier
    cost = debit * multiplier + decimal(plan.get("fees_dollars", 0))
    max_profit = (upper - lower) * multiplier - cost
    breakeven = money(lower + debit + fees_per_share) if version == 1 or max_profit >= 0 else None
    scenarios = []
    payouts = []
    for scenario in plan["scenarios"]:
        spot = decimal(scenario["terminal_stock_price"])
        payout = min(max(spot - lower, 0), upper - lower) * multiplier
        payouts.append(Decimal(money(payout)) if version == 1 else payout)
        scenarios.append({**scenario, "payout_dollars": money(payout), "pnl_dollars": money(payout - cost)})
    expected = max_debit = clears = None
    if "probability" in scenarios[0]:
        expected_gross = sum(decimal(row["probability"]) * payout for row, payout in zip(scenarios, payouts))
        expected = money(expected_gross - cost)
        ceiling = (expected_gross - decimal(plan.get("fees_dollars", 0))
                   - decimal(plan.get("required_expected_profit_dollars", 0)))
        max_debit = money(ceiling if version == 1 else ceiling.quantize(CENT, rounding=ROUND_FLOOR))
        clears = debit * multiplier <= ceiling
    status = "research_only" if freshness == "fresh" else "reference_only"
    portfolio_impact = {"status": "unknown", "reason": "no synchronized portfolio scenario input"}
    if "portfolio" in plan:
        portfolio_impact = {"status": "scenario_only", "source": plan["portfolio"]["source"], "scenarios": []}
        for scenario in scenarios:
            before = Decimal(str(plan["portfolio"]["pnl_dollars"][scenario["name"]]))
            portfolio_impact["scenarios"].append({"name": scenario["name"], "before_dollars": money(before),
                "after_dollars": money(before + Decimal(scenario["pnl_dollars"]))})
    return {
        "quote_status": freshness,
        "decision_status": status,
        "contracts": codes,
        "oldest_quote_age_seconds": float(quote_age),
        "leg_timestamp_skew_seconds": float(skew),
        "displayed_size_upper_bound": sizes,
        "synthetic_mid_debit_dollars": money((debit + lower_bound) * multiplier / 2),
        "synthetic_ask_debit_dollars": money(debit * multiplier),
        "standalone_max_loss_dollars": money(cost),
        "standalone_max_profit_dollars": money(max_profit),
        "expiration_breakeven_stock_price": breakeven,
        "expected_pnl_dollars": expected,
        "max_debit_at_forecast_dollars": max_debit,
        "forecast_clears_required_profit": clears,
        "portfolio_impact": portfolio_impact,
        "scenarios": scenarios,
    }


def read_json(path):
    if path == "-":
        return json.load(sys.stdin)
    with Path(path).open(encoding="utf-8") as stream:
        return json.load(stream)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path(__file__).parent / "data" / "journal.sqlite3")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("record", help="append an observation batch").add_argument("input")
    commands.add_parser("record-mcp", help="import Robinhood or Alpaca MCP market-data output").add_argument("input")
    commands.add_parser("evaluate", help="freeze a plan and its point-in-time evidence").add_argument("input")
    commands.add_parser("replay", help="recompute a frozen decision").add_argument("decision_id")
    commands.add_parser("review", help="freeze an evidence review alongside a decision").add_argument("input")
    commands.add_parser("study-catalyst", help="freeze an outright vs. debit-spread catalyst study").add_argument("input")
    commands.add_parser("replay-catalyst", help="recompute a frozen catalyst study").add_argument("study_id")
    for name, help_text in (
        ("earnings-register", "register a fixed earnings experiment"),
        ("earnings-schedule", "freeze an upcoming earnings event and cutoffs"),
        ("earnings-entry", "record all candidates at a scheduled earnings cutoff"),
        ("earnings-paper", "record an observed paper round trip or unfilled attempt"),
    ):
        commands.add_parser(name, help=help_text).add_argument("input")
    commands.add_parser("earnings-resolve", help="freeze an exit quote replay").add_argument("entry_id")
    commands.add_parser("earnings-status", help="list the earnings cohort and outcomes")
    commands.add_parser("scorecard", help="report dated cohort coverage, replay, and observed paper outcomes").add_argument("--as-of")
    capture_readiness = commands.add_parser("capture-status", help="show quote capture windows and feed readiness")
    capture_readiness.add_argument("--as-of")
    capture_readiness.add_argument("--symbol", type=str.upper)
    commands.add_parser("reconcile-due", help="freeze unresolved quote replays after their registered exits")
    commands.add_parser("price-limits", help="evaluate a limit grid under joint fill and payoff stress").add_argument("input")
    candidate = commands.add_parser("candidate-review", help="review a tested entry against combined portfolio stress")
    candidate.add_argument("input")
    candidate.add_argument("--require-operational-context", action="store_true",
                           help="require timestamped account, market and contract cutoff evidence")
    sizing = commands.add_parser("candidate-size", help="compare whole-contract sizes with cash under risk and cost stress")
    sizing.add_argument("input")
    sizing.add_argument("--require-operational-context", action="store_true",
                        help="require timestamped account, market and contract cutoff evidence")
    expression = commands.add_parser("expression-review", help="compare tested option expressions and cash")
    expression.add_argument("input")
    expression.add_argument("--require-operational-context", action="store_true",
                            help="require timestamped account, market and contract cutoff evidence")
    commands.add_parser("overnight-variance", help="validate separate overnight and intraday variance features").add_argument("input")
    commands.add_parser("exit-replay", help="compare scheduled and target/stop spread exits").add_argument("input")
    commands.add_parser("regime-diagnostics", help="compare jump intensity and correlation concentration").add_argument("input")
    scanner = commands.add_parser("opportunity-scan", help="screen the configured contracts on incoming journal quotes")
    scanner.add_argument("--as-of", help="replay the scanner at a past cutoff")
    scanner.add_argument("--watch", action="store_true", help="recheck as journal quotes arrive")
    scanner.add_argument("--every", type=int, default=5, help="polling interval in seconds")
    commands.add_parser("hedge-replay", help="compare hedges on a supplied quote path").add_argument("input")
    commands.add_parser("broker-reconcile", help="compare supplied broker and local account snapshots").add_argument("input")
    run_capture = commands.add_parser("capture-run", help="collect eligible read-only Alpaca market data")
    run_capture.add_argument("--watch", action="store_true", help="poll and collect before registered cutoffs")
    for name, help_text in (
        ("affordability-preflight", "screen whether one lot fits before registering a comparison"),
        ("archive-fetch", "fetch licensed market-time records from Massive"),
        ("archive-replay", "replay licensed historical observations outside the prospective ledger"),
        ("iv-surface", "derive an implied-volatility surface and quote diagnostics"),
        ("portfolio-stress", "reprice an explicitly supplied portfolio under shock scenarios"),
        ("graph-import", "append a source document and cite structured evidence claims"),
        ("graph-query", "query evidence at its historical knowledge cutoff"),
        ("event-quantile-register", "freeze paired numerical and evidence forecast recipes"),
        ("event-quantile-seal", "record both event forecasts before the post-release cutoff"),
    ):
        commands.add_parser(name, help=help_text).add_argument("input")
    forecast = commands.add_parser("forecast-validate", help="evaluate a walk-forward earnings model against a base rate")
    forecast.add_argument("--as-of")
    forecast.add_argument("--experiment-id")
    forecast.add_argument("--phase", choices=["pre", "post"], default="pre")
    forecast.add_argument("--target", choices=["stock_up", "outright_profitable", "debit_spread_profitable"],
                          default="debit_spread_profitable")
    commands.add_parser("event-quantile-report", help="score sealed forecasts on matched post-release returns").add_argument("--as-of")
    inspect = commands.add_parser("inspect", help="look up a ticker or OCC option symbol as of a timestamp")
    inspect.add_argument("instrument")
    inspect.add_argument("--as-of", default=datetime.now(timezone.utc).isoformat())
    commands.add_parser("status", help="list recorded sources and decisions")
    args = parser.parse_args(argv)
    try:
        if args.command == "opportunity-scan":
            from earnings import now_utc
            from opportunity_scan import read_scan
            if args.every < 1 or args.watch and args.as_of:
                raise ValueError("watch requires a positive polling interval and the current clock")
            cutoff = utc(args.as_of) if args.as_of else now_utc()
            if cutoff > now_utc():
                raise ValueError("scanner cutoff cannot be in the future")
            while True:
                result = read_scan(args.db, as_of=cutoff)
                if result is None:
                    raise ValueError("provide opportunity-scan.json beside the journal")
                result["mode"] = "historical_replay" if args.as_of else "current"
                print(canonical(result) if args.watch else json.dumps(result, indent=2, sort_keys=True), flush=True)
                if not args.watch:
                    return 0
                sleep(args.every)
                cutoff = now_utc()
        if args.command == "regime-diagnostics":
            from regime_diagnostics import report
            print(json.dumps(report(read_json(args.input)), indent=2, sort_keys=True, allow_nan=False))
            return 0
        if args.command == "exit-replay":
            from exit_replay import exit_report
            print(json.dumps(exit_report(read_json(args.input)), indent=2, sort_keys=True, allow_nan=False))
            return 0
        if args.command == "expression-review":
            from expression_review import expression_report
            print(json.dumps(expression_report(read_json(args.input), require_operational_context=args.require_operational_context),
                             indent=2, sort_keys=True, allow_nan=False))
            return 0
        if args.command == "overnight-variance":
            from overnight_variance import compare
            print(json.dumps(compare(read_json(args.input)), indent=2, sort_keys=True, allow_nan=False))
            return 0
        if args.command == "candidate-size":
            from candidate_sizing import size_report
            print(json.dumps(size_report(read_json(args.input), require_operational_context=args.require_operational_context),
                             indent=2, sort_keys=True, allow_nan=False))
            return 0
        if args.command == "hedge-replay":
            from hedge_replay import hedge_replay
            print(json.dumps(hedge_replay(read_json(args.input)), indent=2, sort_keys=True, allow_nan=False))
            return 0
        if args.command == "broker-reconcile":
            from broker_reconcile import reconcile
            result = reconcile(read_json(args.input))
            print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
            return 0 if result["status"] == "matched" else 1
        if args.command in {"price-limits", "candidate-review"}:
            from candidate_review import candidate_review
            from price_limits import price_limit_report
            supplied = read_json(args.input)
            result = (price_limit_report(supplied) if args.command == "price-limits" else
                      candidate_review(supplied, require_operational_context=args.require_operational_context))
            print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
            if args.command == "candidate-review" and args.require_operational_context:
                return 0 if result["status"] == "passes_research_gates" else 1
            return 0
        args.db.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(args.db)) as connection:
            journal = Journal(connection)
            if args.command == "record":
                result = journal.record(read_json(args.input))
            elif args.command == "record-mcp":
                result = journal.record(normalize_mcp(read_json(args.input)))
            elif args.command == "evaluate":
                result = journal.evaluate(read_json(args.input))
            elif args.command == "replay":
                result = journal.replay(args.decision_id)
            elif args.command == "review":
                from frontier import record_review
                result = record_review(journal, read_json(args.input))
            elif args.command == "study-catalyst":
                from catalyst import freeze_study
                result = freeze_study(journal, read_json(args.input))
            elif args.command == "replay-catalyst":
                from catalyst import replay_study
                result = replay_study(journal, args.study_id)
            elif args.command == "scorecard":
                from scorecard import cohort_report
                result = cohort_report(journal, as_of=args.as_of)
            elif args.command in {"capture-status", "capture-run", "archive-fetch", "archive-replay",
                                  "reconcile-due", "affordability-preflight"}:
                from capture import (affordability_preflight, archive_fetch, archive_replay, capture_once, capture_status,
                                     credentials_from_environment, reconcile_due)

                if args.command == "archive-replay":
                    result = archive_replay(read_json(args.input))
                elif args.command == "archive-fetch":
                    result = archive_fetch(read_json(args.input), os.getenv("MASSIVE_API_KEY"))
                elif args.command == "reconcile-due":
                    result = reconcile_due(journal)
                elif args.command == "affordability-preflight":
                    result = affordability_preflight(journal, read_json(args.input))
                else:
                    auth = credentials_from_environment()
                    if args.command == "capture-status":
                        result = capture_status(journal, as_of=args.as_of, auth=auth, symbol=args.symbol)
                    elif not auth:
                        raise ValueError("scheduled Alpaca capture requires APCA_API_KEY_ID and APCA_API_SECRET_KEY")
                    elif args.watch:
                        while True:
                            tick = capture_once(journal, auth)
                            if tick["attempts"]:
                                print(canonical(tick), flush=True)
                            sleep(2)
                    else:
                        result = capture_once(journal, auth)
            elif args.command in {"iv-surface", "portfolio-stress"}:
                from market_risk import portfolio_report, surface_report

                supplied = read_json(args.input)
                result = surface_report(journal, supplied) if args.command == "iv-surface" else portfolio_report(supplied)
            elif args.command == "forecast-validate":
                from forecast import forecast_report
                result = forecast_report(journal, vars(args))
            elif args.command.startswith("event-quantile-"):
                from event_quantiles import register, report, seal
                if args.command == "event-quantile-report":
                    result = report(journal, args.as_of)
                else:
                    result = (register if args.command == "event-quantile-register" else seal)(journal, read_json(args.input))
            elif args.command in {"graph-import", "graph-query"}:
                from research_graph import graph_as_of, import_evidence

                supplied = read_json(args.input)
                if args.command == "graph-import":
                    result = import_evidence(journal, supplied)
                else:
                    result = graph_as_of(journal, **supplied)
            elif args.command.startswith("earnings-"):
                from earnings import list_experiments, record_entry, record_paper, register_experiment, resolve_entry, schedule_event

                if args.command == "earnings-status":
                    result = list_experiments(journal)
                elif args.command == "earnings-resolve":
                    result = resolve_entry(journal, args.entry_id)
                else:
                    command = {"earnings-register": register_experiment, "earnings-schedule": schedule_event,
                               "earnings-entry": record_entry, "earnings-paper": record_paper}[args.command]
                    result = command(journal, read_json(args.input))
            elif args.command == "inspect":
                result = journal.inspect(args.instrument, args.as_of)
            else:
                result = journal.status()
        print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    except KeyboardInterrupt:
        return 130
    except (ValueError, TypeError, KeyError, OSError, InvalidOperation, sqlite3.Error) as error:
        print(f"optionsdesk: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
