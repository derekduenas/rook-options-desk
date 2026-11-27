"""Measure preregistered earnings cohorts using the evidence recorded by each cutoff."""

from collections import Counter, defaultdict
from datetime import timedelta
from decimal import Decimal

from catalyst import quote_status
from earnings import PHASES, STRUCTURES, costs, executable_mark, list_experiments, market_snapshot, now_utc, spread_width
from optionsdesk import NEW_YORK, decimal, money, seconds_between, timestamp, utc


MIN_INTERVAL_DAYS = 20
MARKOUT_SECONDS = (1, 5, 10, 60, 300)
REPLAY_PAIRS = (
    ("outright", "debit_spread"),
    ("outright", "underlying"),
    ("debit_spread", "underlying"),
    ("outright", "cash"),
    ("debit_spread", "cash"),
)
COVERAGE_KEYS = (
    "scheduled", "pending", "missed", "frozen", "eligible", "excluded", "late",
    "release_unconfirmed", "pre_after_release", "passes", "eligible_passes", "paper_decisions",
    "replay_pending", "replay_missing", "replay_complete", "replay_partial", "replay_cash_only",
    "replay_no_market_outcomes", "excluded_paper_attempts",
)


def mean_dollars(values):
    return money(sum(values) / len(values)) if values else None


def mean_seconds(values):
    return round(float(sum(values) / len(values)), 3) if values else None


def percentage(numerator, denominator):
    return money(Decimal(numerator) * 100 / denominator) if denominator else None


def estimate(samples):
    """Equal-event mean and approximate interval clustered by New York release date."""
    clusters = defaultdict(list)
    for day, value in samples:
        clusters[day].append(value)
    count = len(samples)
    days = len(clusters)
    if not count:
        return None, None, 0
    mean = sum(value for _, value in samples) / count
    interval = None
    if days >= MIN_INTERVAL_DAYS:
        clustered_squares = sum((sum(values) - mean * len(values)) ** 2 for values in clusters.values())
        variance = clustered_squares * days / ((days - 1) * count ** 2)
        radius = Decimal("1.96") * variance.sqrt()
        interval = [money(mean - radius), money(mean + radius)]
    return money(mean), interval, days


def comparison(left_name, right_name, left, right):
    matched = left.keys() & right.keys()
    samples = [(left[event]["day"], left[event]["pnl"] - right[event]["pnl"])
               for event in sorted(matched)]
    average, interval, days = estimate(samples)
    return {"left": left_name, "right": right_name, "paired": len(matched),
            "left_only": len(left) - len(matched), "right_only": len(right) - len(matched),
            "sample_days": days, "mean_delta_dollars": average, "interval_95_dollars": interval}


def replay_structure(samples, eligible, due, entry_blockers):
    values = samples.values()
    average, interval, days = estimate([(row["day"], row["pnl"]) for row in values])
    return {"eligible": eligible, "observed": len(samples), "missing": due - len(samples),
            "pending": eligible - due, "entry_blockers": dict(entry_blockers),
            "sample_days": days, "mean_net_pnl_dollars": average,
            "interval_95_dollars": interval,
            "mean_entry_cost_dollars": mean_dollars([row["entry_cost"] for row in values]),
            "mean_hold_seconds": mean_seconds([row["hold"] for row in values if row["hold"] is not None])}


def cached_snapshot(journal, protocol, event, cutoff, evidence_cache):
    key = event["event_id"], cutoff
    if key not in evidence_cache:
        evidence_cache[key] = market_snapshot(journal, protocol, event, cutoff)
    return evidence_cache[key]


def option_slippage(journal, protocol, event, fill, evidence_cache):
    """Positive values are adverse; quotes must cover the reported fill quantity."""
    if fill["structure"] == "underlying":
        return None, None
    multiplier, _ = costs(protocol, fill["structure"])
    differences = []
    for time_field, price_field, opening in (("entry_at", "entry_net_price", True),
                                             ("exit_at", "exit_net_price", False)):
        cutoff = fill[time_field]
        evidence = cached_snapshot(journal, protocol, event, cutoff, evidence_cache)
        status, mark, depth = executable_mark(protocol, event, cutoff, evidence,
                                               fill["structure"], opening=opening)
        if status != "eligible" or depth is None or depth < fill["quantity"]:
            differences.append(None)
            continue
        actual = Decimal(fill[price_field])
        shortfall = actual - mark if opening else mark - actual
        differences.append(shortfall * multiplier * fill["quantity"])
    return tuple(differences)


def markout_mid(journal, protocol, event, fill, cutoff, evidence_cache, *, after=None):
    evidence = cached_snapshot(journal, protocol, event, cutoff, evidence_cache)
    spread = fill["structure"] == "debit_spread"
    quotes = evidence["options"] if spread else evidence["options"][:1]
    rules = {**protocol, "as_of": cutoff,
             "max_quote_age_seconds": min(Decimal(1), decimal(protocol["max_quote_age_seconds"])),
             "max_leg_skew_seconds": min(Decimal(1), decimal(protocol["max_leg_skew_seconds"]))}
    status = quote_status(rules, quotes, version=2)
    if status != "fresh":
        return None, status
    if after is not None and any(row["observed_at"] <= after for row in quotes):
        return None, "no_post_fill_quote"
    midpoints = [(decimal(row["bid"]) + decimal(row["ask"])) / 2 for row in quotes]
    mark = midpoints[0] - midpoints[1] if spread else midpoints[0]
    if spread and not 0 <= mark <= spread_width(event):
        return None, "spread_mid_outside_payoff_bounds"
    return mark, None


def option_markouts(journal, protocol, event, fill, as_of, evidence_cache):
    if fill["structure"] == "underlying":
        return {}
    entry = fill["entry_at"]
    fraction = entry.split(".")[1].split("+")[0]
    multiplier, _ = costs(protocol, fill["structure"])
    base, base_reason = markout_mid(journal, protocol, event, fill, entry, evidence_cache)
    rows = {}
    for seconds in MARKOUT_SECONDS:
        when = timestamp(entry) + timedelta(seconds=seconds)
        cutoff = f"{when:%Y-%m-%dT%H:%M:%S}.{fraction}+00:00"
        if cutoff > as_of or cutoff >= fill["exit_at"]:
            continue
        after, after_reason = markout_mid(journal, protocol, event, fill, cutoff, evidence_cache, after=entry)
        fill_mark = Decimal(fill["entry_net_price"])
        scale = multiplier * fill["quantity"]
        rows[seconds] = (
            (after - base) * scale if base is not None and after is not None else None,
            (after - fill_mark) * scale if after is not None else None,
            f"followup_{after_reason}" if after_reason else f"entry_{base_reason}" if base_reason else None,
        )
    return rows


def paper_structure(journal, protocol, candidates, evidence_cache, as_of):
    attempts = [(event, planned, attempt) for event, planned, attempt, _, excluded in candidates
                if attempt is not None and not excluded]
    eligible_after_exit = sum(after_exit for _, _, _, after_exit, _ in candidates)
    attempted_after_exit = sum(after_exit and attempt is not None and not excluded
                               for _, _, attempt, after_exit, excluded in candidates)
    excluded_after_exit = sum(after_exit and excluded for _, _, _, after_exit, excluded in candidates)
    excluded_attempts = sum(excluded for _, _, _, _, excluded in candidates)
    filled = sum(attempt["status"] == "filled" for _, _, attempt in attempts)
    partial = sum(attempt["status"] == "partial" for _, _, attempt in attempts)
    unfilled = sum(attempt["status"] == "unfilled" for _, _, attempt in attempts)
    attempted_quantity = sum(planned["quantity"] for _, planned, _ in attempts)
    filled_quantity = sum(attempt["quantity"] for _, _, attempt in attempts)
    pnl_samples, debit, fees, hold = [], [], [], []
    entry_slippage, exit_slippage, round_trip_slippage = [], [], []
    markouts = {seconds: [] for seconds in MARKOUT_SECONDS}
    for event, _, attempt in attempts:
        if not attempt["quantity"]:
            continue
        if attempt["pnl_dollars"] is not None:
            day = timestamp(event["event_at"]).astimezone(NEW_YORK).date()
            pnl_samples.append((day, Decimal(attempt["pnl_dollars"])))
        multiplier, _ = costs(protocol, attempt["structure"])
        debit.append(Decimal(attempt["entry_net_price"]) * multiplier * attempt["quantity"])
        fees.append(Decimal(attempt["fees_dollars"]))
        hold.append(seconds_between(attempt["exit_at"], attempt["entry_at"]))
        entry, exit_ = option_slippage(journal, protocol, event, attempt, evidence_cache)
        if entry is not None:
            entry_slippage.append(entry)
        if exit_ is not None:
            exit_slippage.append(exit_)
        if entry is not None and exit_ is not None:
            round_trip_slippage.append(entry + exit_)
        for seconds, marks in option_markouts(journal, protocol, event, attempt, as_of, evidence_cache).items():
            markouts[seconds].append(marks)
    average, interval, days = estimate(pnl_samples)
    return {
        "eligible_candidates": len(candidates), "eligible_after_exit": eligible_after_exit,
        "attempted": len(attempts), "attempted_after_exit": attempted_after_exit,
        "excluded_attempts": excluded_attempts, "excluded_after_exit": excluded_after_exit,
        "missing_after_exit": eligible_after_exit - attempted_after_exit - excluded_after_exit,
        "attempt_coverage_pct": percentage(attempted_after_exit, eligible_after_exit),
        "filled": filled, "partial": partial, "unfilled": unfilled,
        "any_fill_rate_pct": percentage(filled + partial, len(attempts)),
        "full_fill_rate_pct": percentage(filled, len(attempts)),
        "attempted_quantity": attempted_quantity, "filled_quantity": filled_quantity,
        "unit_fill_rate_pct": percentage(filled_quantity, attempted_quantity),
        "observed_pnl": len(pnl_samples), "sample_days": days, "mean_net_pnl_dollars": average,
        "interval_95_dollars": interval, "mean_entry_debit_dollars": mean_dollars(debit),
        "mean_fees_dollars": mean_dollars(fees), "mean_hold_seconds": mean_seconds(hold),
        "entry_slippage_samples": len(entry_slippage), "mean_entry_slippage_dollars": mean_dollars(entry_slippage),
        "exit_slippage_samples": len(exit_slippage), "mean_exit_slippage_dollars": mean_dollars(exit_slippage),
        "round_trip_slippage_samples": len(round_trip_slippage),
        "mean_round_trip_slippage_dollars": mean_dollars(round_trip_slippage),
        "entry_markouts": {str(seconds): {
            "eligible_fills": len(rows),
            "midpoint_move_samples": sum(a is not None for a, _, _ in rows),
            "mean_midpoint_move_dollars": mean_dollars([a for a, _, _ in rows if a is not None]),
            "fill_to_midpoint_samples": sum(b is not None for _, b, _ in rows),
            "mean_fill_to_midpoint_dollars": mean_dollars([b for _, b, _ in rows if b is not None]),
            "incomplete_reasons": dict(Counter(reason for _, _, reason in rows if reason)),
        } for seconds, rows in markouts.items()},
    }


def summarize_phase(journal, experiment, phase, as_of):
    protocol = experiment["protocol"]
    coverage = dict.fromkeys(COVERAGE_KEYS, 0)
    replay_samples = {structure: {} for structure in STRUCTURES}
    eligible_counts = dict.fromkeys(STRUCTURES, 0)
    due_counts = dict.fromkeys(STRUCTURES, 0)
    entry_blockers = {structure: defaultdict(int) for structure in STRUCTURES}
    paper_candidates = {structure: [] for structure in STRUCTURES if structure != "cash"}
    for event in experiment["events"]:
        if event["registered_at"] > as_of:
            continue
        coverage["scheduled"] += 1
        entries = {row["phase"]: row for row in event["entries"] if row["recorded_at"] <= as_of}
        entry = entries.get(phase)
        cutoff = event[phase + "_at"]
        if entry is None:
            deadline = event["event_at"] if phase == "pre" else event["exit_at"]
            missed = as_of >= deadline or seconds_between(as_of, cutoff) > decimal(protocol["max_record_delay_seconds"])
            coverage["missed" if missed else "pending"] += 1
            continue

        coverage["frozen"] += 1
        coverage["passes"] += entry["action"] == "pass"
        attempts = {row["structure"]: row for row in entry["paper"] if row["recorded_at"] <= as_of}
        classification = entry["classification"]
        if phase == "pre" and "post" not in entries:
            classification = entry.get("recorded_classification", classification)
        if classification != "prospective":
            coverage["excluded"] += 1
            coverage["excluded_paper_attempts"] += len(attempts)
            if classification in ("late", "release_unconfirmed", "pre_after_release"):
                coverage[classification] += 1
            continue

        coverage["eligible"] += 1
        coverage["eligible_passes"] += entry["action"] == "pass"
        coverage["paper_decisions"] += entry["action"] == "paper"
        after_exit = as_of >= event["exit_at"]
        replay = entry["replay"]
        if replay is not None and replay["recorded_at"] > as_of:
            replay = None
        outcomes = {row["structure"]: row for row in replay["outcomes"]} if replay else {}
        excluded_attempts = {
            structure for structure, attempt in attempts.items()
            if attempt.get("classification") == "pre_after_release"
            and attempt["classification_effective_at"] <= as_of
        }
        coverage["excluded_paper_attempts"] += len(excluded_attempts)
        complete = True
        market_eligible = market_observed = False
        for candidate in entry["candidates"]:
            structure = candidate["structure"]
            if candidate["status"] != "eligible" or (not candidate["quantity"] and structure != "cash"):
                entry_blockers[structure][candidate["status"]] += 1
                continue
            market_eligible |= structure != "cash"
            eligible_counts[structure] += 1
            due_counts[structure] += after_exit
            if entry["action"] == "paper" and structure != "cash":
                paper_candidates[structure].append((event, candidate, attempts.get(structure), after_exit,
                                                    structure in excluded_attempts))
            outcome = outcomes.get(structure)
            if outcome is None or outcome["pnl_dollars"] is None:
                complete = False
                continue
            market_observed |= structure != "cash"
            replay_samples[structure][event["event_id"]] = {
                "day": timestamp(event["event_at"]).astimezone(NEW_YORK).date(),
                "pnl": Decimal(outcome["pnl_dollars"]), "entry_cost": Decimal(candidate["entry_cost_dollars"]),
                "hold": seconds_between(event["exit_at"], cutoff) if structure != "cash" else None,
            }
        if not after_exit:
            coverage["replay_pending"] += 1
        elif replay is None:
            coverage["replay_missing"] += 1
        else:
            coverage["replay_no_market_outcomes"] += not market_observed
            if not market_eligible:
                coverage["replay_cash_only"] += 1
            else:
                coverage["replay_complete" if complete else "replay_partial"] += 1

    evidence_cache = {}
    return {"experiment_id": experiment["experiment_id"], "name": protocol["name"], "phase": phase,
            "coverage": coverage,
            "replay": {"structures": {structure: replay_structure(replay_samples[structure], eligible_counts[structure],
                                                                  due_counts[structure], entry_blockers[structure])
                                      for structure in STRUCTURES},
                       "comparisons": [comparison(left, right, replay_samples[left], replay_samples[right])
                                       for left, right in REPLAY_PAIRS]},
                "paper": {"structures": {structure: paper_structure(journal, protocol, rows, evidence_cache, as_of)
                                     for structure, rows in paper_candidates.items()}}}, replay_samples


def _report(journal, as_of):
    cohorts, phase_comparisons = [], []
    for experiment in list_experiments(journal):
        if experiment["registered_at"] > as_of:
            continue
        phase_samples = {}
        for phase in PHASES:
            cohort, phase_samples[phase] = summarize_phase(journal, experiment, phase, as_of)
            cohorts.append(cohort)
        for structure in STRUCTURES:
            phase_comparisons.append({"experiment_id": experiment["experiment_id"], "structure": structure,
                                      **comparison("pre", "post", phase_samples["pre"][structure],
                                                   phase_samples["post"][structure])})
    return {"as_of": as_of, "cohorts": cohorts, "phase_comparisons": phase_comparisons,
            "methodology": {"minimum_interval_event_days": MIN_INTERVAL_DAYS,
                            "interval": "Approximate 95% interval clustered by New York event date",
                            "paired_delta": "Left minus right on matched events",
                            "replay_sample": "Prospective frozen decisions, including passes",
                            "paper_sample": "Prospective paper decisions with eligible sized candidates",
                            "paper_unit_denominator": "Frozen candidate units across eligible recorded attempts",
                            "slippage": "Positive is adverse versus option quotes at the fill time and size",
                            "entry_markout": "Positive is favorable for the long position. Midpoints are indicative, "
                            "spread marks use leg mids, and quotes must be at most one second old."}}


def cohort_report(journal, *, as_of=None):
    """Return a consistent report using only records visible at the chosen time."""
    cutoff = now_utc() if as_of is None else utc(as_of)
    if journal.db.in_transaction:
        return _report(journal, cutoff)
    with journal.db:
        journal.db.execute("BEGIN")
        return _report(journal, cutoff)
