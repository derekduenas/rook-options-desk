"""Independent research engines behind the shared desk runner."""

from collections import Counter
import csv
from decimal import Decimal
from io import StringIO
from statistics import mean

from earnings_direction import research_report
from overnight_variance import compare as overnight_compare
from optionsdesk import decimal
from regime_diagnostics import report as regime_diagnostics
from volatility_challenger import compare


def validate_screen(raw):
    counts = Counter()
    for row in csv.DictReader(StringIO(raw.decode("utf-8"), newline="")):
        strike, ask = Decimal(row["strike"]), Decimal(row["screener_ask"])
        call = row["right"] == "C"
        threshold = strike + ask if call else strike - ask
        hit = (Decimal(row["stock_regular_high"]) >= threshold if call else
               Decimal(row["stock_regular_low"]) <= threshold)
        if threshold != Decimal(row["expiration_breakeven_from_ask"]) or hit != (row["stock_reached_breakeven"] == "yes"):
            raise ValueError("screener stock-breakeven audit does not reconcile")
        pnl = row["hypothetical_one_contract_pnl_at_bid_before_fees"]
        if pnl and (Decimal(row["option_bid"]) - ask) * 100 != Decimal(pnl):
            raise ValueError("screener bid-side P&L does not reconcile")
        counts["rows"] += 1
        counts["stock_breakeven_touches"] += hit
        counts["option_quote_snapshots"] += bool(row["option_quote_as_of"])
    return dict(counts)


def direction_benchmark(plan, evidence):
    days = sorted(row["day"] for row in evidence["sessions"])
    lookback, horizon = plan["lookback_sessions"], plan["horizon_sessions"]
    if (type(lookback) is not int or type(horizon) is not int or min(lookback, horizon) < 1
            or len(days) != len(set(days)) or len(days) <= lookback + horizon):
        raise ValueError("directional research needs distinct sessions and a complete positive forecast horizon")
    benchmark, symbols = plan["benchmark"], plan["universe"]
    if not symbols or len(set(symbols)) != len(symbols) or benchmark in symbols:
        raise ValueError("directional research needs a distinct benchmark and universe")
    ranges = range(lookback, len(days) - horizon, horizon)
    prices = {symbol: [decimal(evidence["closes"][symbol][day]) for day in days] for symbol in [benchmark, *symbols]}
    if any(not price for series in prices.values() for price in series):
        raise ValueError("directional close prices must be positive")
    outcomes = []
    for index in ranges:
        reference = prices[benchmark]
        past_ref = reference[index] / reference[index - lookback]
        forward_ref = reference[index + horizon] / reference[index]
        for symbol in symbols:
            stock = prices[symbol]
            past = stock[index] / stock[index - lookback] - past_ref
            future = stock[index + horizon] / stock[index] - forward_ref
            outcomes.append((past > 0, future))
    positive = [value for signal, value in outcomes if signal]
    return {"basis": "historical regular-session close prices with no event filter",
            "benchmark": benchmark, "lookback_sessions": lookback, "horizon_sessions": horizon,
            "distinct_windows": len(ranges), "symbol_windows": len(outcomes),
            "correct": sum(signal == (actual > 0) for signal, actual in outcomes),
            "always_outperform_correct": sum(actual > 0 for _, actual in outcomes),
            "always_underperform_or_flat_correct": sum(actual <= 0 for _, actual in outcomes),
            "outperform_signals": len(positive),
            "mean_forward_excess_return_pct_when_positive": str(mean(positive) * 100) if positive else None}


def directional_engine(data, screen):
    benchmark = direction_benchmark(data["tape_plan"], data["closes"]) if {"closes", "tape_plan"} <= data.keys() else None
    return {"status": "historical_research" if benchmark or screen else "awaiting_inputs",
            "benchmark": benchmark, "screener": validate_screen(screen) if screen else None}


def volatility_engine(data):
    daily = compare(data["volatility_plan"], data["closes"]) if {"closes", "volatility_plan"} <= data.keys() else None
    overnight = overnight_compare(data["overnight_variance"]) if "overnight_variance" in data else None
    return {"status": "historical_research" if daily or overnight and overnight["complete_test_windows"] else "awaiting_inputs",
            "benchmark": {key: daily[key] for key in ("basis", "symbol_windows", "distinct_test_windows", "mean_qlike")}
            if daily else None,
            "overnight": {key: value for key, value in overnight.items() if key != "outcomes"} if overnight else None,
            "regime_diagnostics": regime_diagnostics(data["regime_diagnostics"]) if "regime_diagnostics" in data else None}


def catalyst_engine(data, journal):
    earnings = overlay = None
    if {"closes", "direction_plan"} <= data.keys():
        report = research_report(data["direction_plan"], data["closes"], data.get("option_overlay"))
        earnings = report["summary"]
        if "option_overlay" in report:
            overlay = {key: report["option_overlay"][key] for key in ("basis", "totals", "included", "excluded")}
    return {"status": "historical_research" if earnings or journal["catalyst_studies"] else "awaiting_inputs",
            "earnings_benchmark": earnings, "option_overlay": overlay, "frozen_catalyst_studies": journal["catalyst_studies"],
            "event_types": journal["catalyst_studies_by_type"], "next_earnings_cutoff": journal["next_cutoff"]}
