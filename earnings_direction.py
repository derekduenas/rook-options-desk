"""Compare a fixed pre-earnings relative-strength signal with constant directions."""

import argparse
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path

from optionsdesk import decimal, money


def event_rows(plan, evidence):
    window = plan["event_window"]
    signal = plan["signal"]
    benchmark = signal["benchmark"]
    lookback = signal["ticker_lookback_completed_sessions"]
    if type(lookback) is not int or lookback <= 0:
        raise ValueError("lookback must be a positive number of sessions")
    sessions = sorted(evidence["sessions"], key=lambda row: row["day"])
    indices = {row["day"]: index for index, row in enumerate(sessions)}
    if len(indices) != len(sessions):
        raise ValueError("the exchange calendar contains duplicate sessions")

    results, exclusions, seen = [], [], set()
    for symbol in plan["universe"]:
        if not evidence["reports"].get(symbol):
            exclusions.append({"symbol": symbol, "status": "no_report_history"})
        for report in evidence["reports"].get(symbol, []):
            day = report["day"]
            row = {"symbol": symbol, **report}
            if day is None:
                exclusions.append({**row, "status": "undated_report"})
                continue
            if not window["start"] <= day <= window["end"]:
                continue
            key = symbol, day
            if key in seen:
                raise ValueError(f"duplicate event for {symbol} on {day}")
            seen.add(key)
            if not (report["verified"] is True and report["reported"] is True and report["timing"] == "pm"):
                exclusions.append({**row, "status": "unconfirmed_after_close"})
                continue
            i = indices.get(day)
            if i is None or sessions[i]["close_et"] != "16:00:00":
                exclusions.append({**row, "status": "no_standard_close"})
                continue
            if i < lookback + 1 or i + 1 >= len(sessions):
                exclusions.append({**row, "status": "missing_calendar_endpoints"})
                continue

            first = sessions[i - lookback - 1]["day"]
            last = sessions[i - 1]["day"]
            next_day = sessions[i + 1]["day"]
            endpoints = {
                "stock_start": (symbol, first), "stock_end": (symbol, last),
                "benchmark_start": (benchmark, first), "benchmark_end": (benchmark, last),
                "release_close": (symbol, day), "next_session_close": (symbol, next_day),
            }
            values = {name: evidence["closes"].get(ticker, {}).get(session)
                      for name, (ticker, session) in endpoints.items()}
            missing = [name for name, value in values.items() if value is None]
            if missing:
                exclusions.append({**row, "status": "missing_prices", "missing": missing})
                continue
            prices = {name: decimal(value) for name, value in values.items()}
            if any(value <= 0 for value in prices.values()):
                raise ValueError(f"nonpositive endpoint price for {symbol} on {day}")
            stock_ratio = prices["stock_end"] / prices["stock_start"]
            benchmark_ratio = prices["benchmark_end"] / prices["benchmark_start"]
            forward_ratio = prices["next_session_close"] / prices["release_close"]
            results.append({**row, "lookback_start": first, "lookback_end": last, "exit_day": next_day,
                            "closes": values, "stock_return_pct": money(100 * (stock_ratio - 1)),
                            "benchmark_return_pct": money(100 * (benchmark_ratio - 1)),
                            "relative_return_pct": money(100 * (stock_ratio - benchmark_ratio)),
                            "next_close_return_pct": money(100 * (forward_ratio - 1)),
                            "predicted_up": stock_ratio > benchmark_ratio, "actual_up": forward_ratio > 1})
    return sorted(results, key=lambda row: (row["day"], row["symbol"])), exclusions


def score(rows, predictor):
    pairs = [(predictor(row), row["actual_up"]) for row in rows]
    tp, tn = pairs.count((True, True)), pairs.count((False, False))
    fp, fn = pairs.count((True, False)), pairs.count((False, True))
    balanced = (tp / (tp + fn) + tn / (tn + fp)) / 2 if (tp + fn) and (tn + fp) else None
    return {"correct": tp + tn, "accuracy": (tp + tn) / len(rows) if rows else None,
            "balanced_accuracy": balanced, "confusion": {"tp": tp, "tn": tn, "fp": fp, "fn": fn}}


def comparison(rows):
    return {"events": len(rows), "event_days": len({row["day"] for row in rows}),
            "relative_strength": score(rows, lambda row: row["predicted_up"]),
            "always_up": score(rows, lambda row: True),
            "always_down_or_flat": score(rows, lambda row: False)}


def option_overlay(rows, replay, variant):
    signals = {(row["symbol"], row["day"]): row for row in rows}
    structures = ("outright", "debit_spread")
    included, excluded = [], []
    for replay_row in replay["results"]:
        symbol, day = replay_row["symbol"], replay_row["release"]
        signal = signals.get((symbol, day))
        arms = replay_row["variants"][variant]["arms"]
        unresolved = {name: arms[name]["outcome"]["status"] for name in structures if arms[name]["pnl"] is None}
        if signal is None or unresolved:
            excluded.append({"symbol": symbol, "day": day, "signal_available": signal is not None,
                             "unresolved_arms": unresolved})
            continue
        if signal["exit_day"] != replay_row["exit_session"]:
            raise ValueError(f"option replay and signal study disagree on the exit session for {symbol}")
        included.append({"symbol": symbol, "day": day, "predicted_up": signal["predicted_up"],
                         "pnl_dollars": {name: arms[name]["pnl"] for name in structures}})
    totals = {}
    for name in structures:
        all_pnl = sum((Decimal(row["pnl_dollars"][name]) for row in included), Decimal(0))
        signal_pnl = sum((Decimal(row["pnl_dollars"][name]) for row in included if row["predicted_up"]), Decimal(0))
        totals[name] = {"always_on_pnl_dollars": money(all_pnl), "signal_or_cash_pnl_dollars": money(signal_pnl)}
    return {"basis": "already examined close-quote proxy", "variant": variant,
            "up_signals": sum(row["predicted_up"] for row in included), "cash_pnl_dollars": "0.00",
            "included": included, "excluded": excluded, "totals": totals}


def research_report(plan, evidence, replay=None):
    rows, exclusions = event_rows(plan, evidence)
    partitions = {}
    for partition in plan["evaluation"]["time_partitions"]:
        first, last = partition.split(" through ")
        partitions[partition] = comparison([row for row in rows if first <= row["day"] <= last])
    report = {"basis": plan["basis"], "hypothesis": plan["hypothesis"], "sources": evidence["sources"],
              "collected_at": evidence["collected_at"], "summary": comparison(rows),
              "periods": partitions, "events": rows, "exclusions": exclusions,
              "limitations": ["Robinhood pm labels identify the completed reports. Exact release times were not independently verified for the broader cohort.",
                              "Alpaca supplied historical daily prices. These are not point-in-time receipt records or total returns.",
                              "The fixed 2026 cohort is descriptive. Accuracy is event-weighted and events can share dates."]}
    if replay is not None:
        report["option_overlay"] = option_overlay(rows, replay, plan["existing_option_overlay"]["arm"])
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--option-replay", type=Path)
    args = parser.parse_args()
    paths = {"plan": args.plan, "evidence": args.evidence}
    if args.option_replay:
        paths["option_replay"] = args.option_replay
    raw = {key: path.read_bytes() for key, path in paths.items()}
    documents = {key: json.loads(data) for key, data in raw.items()}
    report = research_report(documents["plan"], documents["evidence"], documents.get("option_replay"))
    report["input_sha256"] = {key: sha256(data).hexdigest() for key, data in raw.items()}
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
