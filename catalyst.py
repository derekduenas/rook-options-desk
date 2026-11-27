"""Point-in-time catalyst comparisons and hypothetical account stress tests."""
from bisect import bisect_right
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_FLOOR
from itertools import accumulate
import json
import random
from urllib.parse import urlsplit

from optionsdesk import NEW_YORK, canonical, contract, decimal, digest, identifier, money, seconds_between, timestamp, utc

STUDY_VERSION = 2
MULTIPLIER = Decimal(100)


def integer(value, label, upper):
    value = decimal(value)
    if value % 1 or not 1 <= value <= upper:
        raise ValueError(f"{label} must be a whole number from 1 to {upper}")
    return int(value)


def validate_study(original):
    study = dict(original)
    for field in ("name", "symbol", "universe_name", "quote_source", "quote_feed", "target_scenario"):
        study[field] = identifier(study[field])
    study["symbol"] = study["symbol"].upper()
    study["universe"] = sorted({identifier(symbol).upper() for symbol in study["universe"]})
    if study["symbol"] not in study["universe"]:
        raise ValueError("symbol is outside the declared study universe")
    study["as_of"] = utc(study["as_of"])
    if study["as_of"] > utc(datetime.now(timezone.utc).isoformat()):
        raise ValueError("study cutoff cannot be in the future")
    event = study["catalyst"] = dict(study["catalyst"])
    identifier(event["title"])
    if "event_type" in event and event["event_type"] not in {"earnings", "macro", "corporate", "regulatory", "other"}:
        raise ValueError("catalyst.event_type must be earnings, macro, corporate, regulatory, or other")
    source = urlsplit(event["source"])
    if source.scheme not in {"https", "http"} or not source.netloc:
        raise ValueError("catalyst must cite a web source")
    for field in ("known_at", "event_at"):
        event[field] = utc(event[field])
    if not event["known_at"] <= study["as_of"] < event["event_at"]:
        raise ValueError("catalyst must be known at the pre-event decision cutoff")
    # Require a later trading date so a same-day after-close announcement cannot slip past expiry.
    if date.fromisoformat(study["expiry"]) <= timestamp(event["event_at"]).astimezone(NEW_YORK).date():
        raise ValueError("use an expiry after the catalyst's New York calendar date")
    if study["right"] not in {"C", "P"}:
        raise ValueError("right must be C or P")
    strike = decimal(study["long_strike"])
    wing = decimal(study["short_strike"])
    for level in (strike, wing):
        contract(study["symbol"], study["expiry"], level, study["right"])
    direction = 1 if study["right"] == "C" else -1
    if (wing - strike) * direction <= 0:
        raise ValueError("short strike must cap the tail in the direction of the long option")
    for field in ("max_quote_age_seconds", "max_leg_skew_seconds", "fees_per_leg_dollars",
                  "stress_slippage_per_leg_dollars", "minimum_target_r"):
        decimal(study[field])
    scenarios = study["scenarios"]
    names = [identifier(item["name"]) for item in scenarios]
    if len(names) < 3 or len(set(names)) != len(names) or study["target_scenario"] not in names:
        raise ValueError("include at least three distinct scenarios and identify the target")
    for item in scenarios:
        decimal(item["terminal_stock_price"])
    if any("probability" in item for item in scenarios):
        if not all("probability" in item for item in scenarios):
            raise ValueError("supply probabilities for all scenarios or omit all")
        if sum(decimal(item["probability"]) for item in scenarios) != 1:
            raise ValueError("scenario probabilities must sum to 1")
        identifier(study["forecast_basis"])
    if "account_stress" in study:
        risk = study["account_stress"]
        f, barrier = (decimal(risk[field]) for field in ("risk_fraction", "drawdown_limit"))
        group = integer(risk["simultaneous_positions"], "simultaneous positions", 20)
        integer(risk["loss_streak"], "loss streak", 100)
        if not (0 < f * group < 1 and 0 < barrier < 1):
            raise ValueError("risk, joint exposure, and the drawdown limit must lie between zero and one")
        if "illustrative_equity_dollars" in risk and decimal(risk["illustrative_equity_dollars"]) == 0:
            raise ValueError("illustrative equity must be positive")
    return study


def leg_quotes(journal, study):
    source = study["quote_source"], study["quote_feed"]
    result = []
    for key in ("long_strike", "short_strike"):
        symbol = contract(study["symbol"], study["expiry"], study[key], study["right"])
        rows = journal.observations("option_quote", symbol, study["as_of"])
        result.append(next((row for row in rows if (row["source"], row["feed"]) == source), None))
    return result


def quote_status(study, quotes, version):
    if any(quote is None for quote in quotes):
        return "missing"
    if any(decimal(q["multiplier"]) != MULTIPLIER or q.get("adjusted") for q in quotes):
        return "unsupported_deliverable"
    for q in quotes:
        if any(decimal(q[side]) <= 0 for side in ("bid", "ask", "bid_size", "ask_size")):
            return "no_two_sided_size"
    times = [q["observed_at"] for q in quotes]
    interval = legacy_seconds_between if version == 1 else seconds_between
    if interval(study["as_of"], min(times)) > decimal(study["max_quote_age_seconds"]):
        return "stale"
    if interval(max(times), min(times)) > decimal(study["max_leg_skew_seconds"]):
        return "unsynchronized"
    if version >= 2 and (study["quote_source"], study["quote_feed"]) == ("alpaca", "indicative"):
        return "indicative"
    return "fresh"


def legacy_seconds_between(later, earlier):
    return (timestamp(later) - timestamp(earlier)).total_seconds()


def account_stress(study, outcomes, cost, version):
    risk = study["account_stress"]
    f, barrier = (decimal(risk[key]) for key in ("risk_fraction", "drawdown_limit"))
    group, streak = int(decimal(risk["simultaneous_positions"])), int(decimal(risk["loss_streak"]))
    loss = 1 - (1 - f) ** streak
    joint = f * group
    result = {
        "loss_streak_drawdown_pct": money(loss * 100),
        "simultaneous_full_loss_pct": money(joint * 100),
        "deterministic_stress_breaches_limit": max(loss, joint) >= barrier,
        "simulated_breach_pct": None,
    }
    if "illustrative_equity_dollars" in risk:
        budget = decimal(risk["illustrative_equity_dollars"]) * f
        result.update(illustrative_risk_budget_dollars=money(budget),
                      contracts_within_illustrative_budget=int(budget // cost))
    if version >= 2:
        clustered_loss = 1 - (1 - joint) ** streak
        result.update(repeated_cluster_drawdown_pct=money(clustered_loss * 100),
                      deterministic_stress_breaches_limit=clustered_loss >= barrier,
                      stress_basis="fractional exposure, independent of whole-contract affordability")
        if "probability" in outcomes[0]:
            result.update(simulate_account(risk, outcomes, cost, group, streak, f, barrier))
        return result
    if "probability" not in outcomes[0]:
        return result
    # Common random draws are shared across structures. Within each batch the
    # outcome is shared by all positions, so losers arrive together.
    cumulative = list(accumulate(float(row["probability"]) for row in outcomes))
    cumulative[-1] = 1.0
    returns = [float(Decimal(row["pnl_dollars"]) / cost * joint) for row in outcomes]
    rng = random.Random(71)
    paths, batches = 2000, streak
    terminal = []
    breaches = 0
    for _ in range(paths):
        wealth = peak = 1.0
        breached = False
        for _ in range(batches):
            draw = rng.random()
            index = next(i for i, threshold in enumerate(cumulative) if draw < threshold)
            wealth *= 1 + returns[index]
            peak = max(peak, wealth)
            breached |= 1 - wealth / peak >= float(barrier)
        breaches += breached
        terminal.append(wealth)
    terminal.sort()
    result.update(simulated_breach_pct=money(Decimal(breaches) / paths * 100), simulated_paths=paths,
                  batches_per_path=batches, seed=71,
                  terminal_equity_multiples={key: str(Decimal(str(terminal[int((paths - 1) * q)])).quantize(
                      Decimal("0.001"))) for key, q in (("p05", .05), ("p50", .50), ("p95", .95))})
    return result


def equity_multiple(value):
    if value.adjusted() > 24:
        return format(value, ".3E")
    return str(value.quantize(Decimal("0.001")))


def simulate_account(risk, outcomes, cost, group, batches, fraction, barrier):
    # Common draws across structures, with a shared outcome within each batch.
    cumulative = list(accumulate(decimal(row["probability"]) for row in outcomes))
    initial = decimal(risk.get("illustrative_equity_dollars", 1))
    whole_contracts = "illustrative_equity_dollars" in risk
    unit_cost = Decimal(1) if whole_contracts else cost
    pnl = [row["stressed_pnl"] / unit_cost * group for row in outcomes]
    cash_only = whole_contracts and initial * fraction < cost
    rng = random.Random(71)
    paths = 2000
    terminal = []
    breaches = 0
    for _ in range(paths):
        wealth = peak = initial
        breached = False
        for _ in range(batches):
            index = bisect_right(cumulative, Decimal.from_float(rng.random()))
            budget = wealth * fraction
            exposure = budget // cost if whole_contracts else budget
            wealth += pnl[index] * exposure
            peak = max(peak, wealth)
            breached |= wealth <= peak * (1 - barrier)
        breaches += breached
        terminal.append(wealth / initial)
    terminal.sort()
    return {
        "simulation_status": "cash_only" if cash_only else "hypothetical",
        "simulation_sizing": "whole_contracts" if whole_contracts else "fractional_exposure",
        "simulated_breach_pct": money(Decimal(breaches) / paths * 100),
        "simulated_paths": paths,
        "batches_per_path": batches,
        "seed": 71,
        "terminal_equity_multiples": {
            key: equity_multiple(terminal[int((paths - 1) * q)])
            for key, q in (("p05", .05), ("p50", .50), ("p95", .95))
        },
    }


def price_candidate(study, quotes, spread, version):
    label = "debit_spread" if spread else "outright"
    status = quote_status(study, quotes, version)
    row = {"structure": label, "quote_status": status}
    if status not in {"stale", "unsynchronized", "fresh", "indicative"}:
        return row, None
    base = decimal(quotes[0]["ask"])
    if spread:
        base -= decimal(quotes[1]["bid"])
    width = abs(decimal(study["long_strike"]) - decimal(study["short_strike"])) if spread else None
    if base <= 0 or (spread and base >= width):
        return {**row, "quote_status": "invalid_debit"}, None
    debit = base * MULTIPLIER
    fees = decimal(study["fees_per_leg_dollars"]) * len(quotes)
    friction = decimal(study["stress_slippage_per_leg_dollars"]) * len(quotes)
    cost = debit + fees
    stressed = cost + friction
    anchor = decimal(study["long_strike"])
    direction = 1 if study["right"] == "C" else -1
    outcomes = []
    exact_outcomes = []
    for scenario in study["scenarios"]:
        intrinsic = max((decimal(scenario["terminal_stock_price"]) - anchor) * direction, 0)
        payout = min(intrinsic, width) * MULTIPLIER if spread else intrinsic * MULTIPLIER
        outcomes.append({**scenario, "payout_dollars": money(payout), "pnl_dollars": money(payout - cost),
                         "stressed_pnl_dollars": money(payout - stressed)})
        exact_outcomes.append({**scenario, "payout": payout, "pnl": payout - cost,
                               "stressed_pnl": payout - stressed})
    target_index = next(i for i, s in enumerate(outcomes) if s["name"] == study["target_scenario"])
    target, exact_target = outcomes[target_index], exact_outcomes[target_index]
    target_r = (Decimal(target["pnl_dollars"]) if version == 1 else exact_target["pnl"]) / cost
    stressed_r = (Decimal(target["stressed_pnl_dollars"]) if version == 1 else exact_target["stressed_pnl"]) / stressed
    threshold = decimal(study["minimum_target_r"])
    max_profit = None if width is None and study["right"] == "C" else (
        (anchor if width is None else width) * MULTIPLIER - cost)
    breakeven = anchor + direction * cost / MULTIPLIER
    if version >= 2 and max_profit is not None and max_profit < 0:
        breakeven = None
    payout = Decimal(target["payout_dollars"]) if version == 1 else exact_target["payout"]
    ceiling = max(Decimal(0), payout / (1 + threshold) - fees - friction)
    if version >= 2:
        ceiling = ceiling.quantize(Decimal("0.01"), rounding=ROUND_FLOOR)
    row.update(
        quoted_entry_debit_dollars=money(debit),
        max_loss_dollars=money(cost), target_pnl_dollars=target["pnl_dollars"],
        max_profit_dollars=None if max_profit is None else money(max_profit),
        expiry_breakeven_stock_price=None if breakeven is None else money(breakeven),
        target_r=str(target_r.quantize(Decimal("0.0001"))),
        stressed_target_r=str(stressed_r.quantize(Decimal("0.0001"))),
        max_debit_for_target_r_dollars=money(ceiling),
        target_clears_hurdle=stressed_r >= threshold,
        outcomes=outcomes,
        expected_pnl_dollars=None, stressed_expected_pnl_dollars=None,
    )
    if "probability" in outcomes[0]:
        if version == 1:
            expectation = sum(decimal(s["probability"]) * Decimal(s["pnl_dollars"]) for s in outcomes)
        else:
            expectation = sum(decimal(s["probability"]) * s["pnl"] for s in exact_outcomes)
        row.update(expected_pnl_dollars=money(expectation), stressed_expected_pnl_dollars=money(expectation - friction))
    if "account_stress" in study:
        stressed_outcomes = ([{**s, "pnl_dollars": s["stressed_pnl_dollars"]} for s in outcomes]
                             if version == 1 else exact_outcomes)
        row["account_stress"] = account_stress(study, stressed_outcomes, stressed, version)
    return row, Decimal(row["stressed_target_r"]) if version == 1 else stressed_r


def render_study(frozen):
    version = frozen["version"]
    if version not in {1, STUDY_VERSION}:
        raise ValueError("unsupported catalyst study version")
    study, quotes = frozen["study"], frozen["quotes"]
    priced = [price_candidate(study, quotes[:1], False, version), price_candidate(study, quotes, True, version)]
    candidates = [row for row, _ in priced]
    ranking = sorted(priced, key=lambda candidate: candidate[1], reverse=True) if all(
        score is not None for _, score in priced) else []
    interval = legacy_seconds_between if version == 1 else seconds_between
    recorded_at = frozen["recorded_at"]
    timely = (all(row["quote_status"] == "fresh" for row in candidates)
              and interval(study["catalyst"]["event_at"], recorded_at) > 0
              and all(interval(recorded_at, q["observed_at"]) <= decimal(study["max_quote_age_seconds"])
                      for q in quotes))
    return {"study_id": digest(frozen), **frozen,
            "study_status": "prospective_research" if timely else "reference_only",
            "ranking_basis": "stressed return at the declared target, not a probability forecast",
            "ranked_structures": [row["structure"] for row, _ in ranking], "candidates": candidates}


def freeze_study(journal, original):
    study = validate_study(original)
    with journal.db:
        journal.db.execute("BEGIN IMMEDIATE")
        quotes = leg_quotes(journal, study)
        frozen = {"version": STUDY_VERSION, "study": study, "quotes": quotes,
                  "recorded_at": utc(datetime.now(timezone.utc).isoformat())}
        result = render_study(frozen)
        journal.db.execute("INSERT INTO catalyst_studies VALUES (?, ?)",
                           (result["study_id"], canonical(frozen)))
    return result


def replay_study(journal, study_id):
    row = journal.db.execute("SELECT frozen FROM catalyst_studies WHERE id = ?", (study_id,)).fetchone()
    if not row:
        raise ValueError("unknown catalyst study")
    frozen = json.loads(row[0])
    if digest(frozen) != study_id:
        raise ValueError("frozen catalyst study failed its integrity check")
    return render_study(frozen)


def latest_studies(journal):
    rows = journal.db.execute("SELECT id FROM catalyst_studies ORDER BY rowid DESC")
    return [replay_study(journal, row[0]) for row in rows]
