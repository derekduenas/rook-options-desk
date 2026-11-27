"""A chronological, low-parameter benchmark for prospective earnings outcomes."""

from collections import Counter, defaultdict
from decimal import Decimal
from math import exp, isfinite, log, sqrt

from earnings import PHASES, list_experiments, now_utc
from optionsdesk import NEW_YORK, digest, timestamp, utc


VERSION = 2
TRAIN_DAYS = 20
TEST_DAYS = 10
FEATURES = ("long_moneyness", "wing_width", "premium_pct_spot", "relative_option_spread",
            "long_iv", "iv_available")
TARGETS = {"stock_up", "outright_profitable", "debit_spread_profitable"}


def feature_row(event, entry):
    evidence = entry["evidence"]
    quote, trade = (evidence.get("options") or [None])[0], evidence.get("underlying")
    if not quote or not trade:
        return None
    direction = 1 if event["right"] == "C" else -1
    stock, strike, wing = float(trade["price"]), float(event["long_strike"]), float(event["short_strike"])
    bid, ask = float(quote["bid"]), float(quote["ask"])
    midpoint = (bid + ask) / 2
    try:
        iv = float(quote["implied_volatility"])
        if not isfinite(iv) or not 0 < iv <= 10:
            iv = 0.0
    except (KeyError, TypeError, ValueError):
        iv = 0.0
    if not stock or not midpoint:
        return None
    features = (direction * (strike / stock - 1), abs(wing - strike) / stock, midpoint / stock,
                (ask - bid) / midpoint, iv, float(iv > 0))
    return features if all(map(isfinite, features)) else None


def labeled_row(event, entry, arm, as_of):
    replay = entry["replay"]
    if not replay or replay["recorded_at"] > as_of:
        return None, "replay_unavailable"
    outcome = next((row for row in replay["outcomes"] if row["structure"] == arm), None)
    if not outcome or outcome["status"] != "modeled":
        return None, "target_unavailable"
    statuses = {candidate["structure"]: candidate["status"] for candidate in entry["candidates"]}
    accepted = {"eligible", "risk_budget"}
    features = feature_row(event, entry) if {statuses.get("outright"), statuses.get("underlying")} <= accepted else None
    if features is None:
        return None, "feature_unavailable"
    label = (Decimal(outcome["exit_net_price"]) > Decimal(str(entry["evidence"]["underlying"]["price"]))
             if arm == "underlying" else Decimal(outcome["pnl_dollars"]) > 0)
    return {"event_id": event["event_id"], "entry_id": entry["entry_id"], "replay_id": replay["replay_id"],
            "event_day": timestamp(event["event_at"]).astimezone(NEW_YORK).date().isoformat(),
            "cutoff": entry["as_of"], "label_available_at": replay["recorded_at"],
            "features": features, "label": int(label)}, None


def sample_rows(experiment, phase, target, as_of):
    """Keep superseded samples when earlier fits could have used them."""
    rows, gaps = [], Counter()
    arm = "underlying" if target == "stock_up" else target.removesuffix("_profitable")
    for event in experiment["events"]:
        if event["registered_at"] > as_of:
            continue
        entry = next((row for row in event["entries"] if row["phase"] == phase and row["recorded_at"] <= as_of), None)
        if not entry:
            gaps["entry_unavailable"] += 1
            continue
        invalidated_at = None
        classification = entry.get("recorded_classification", entry["classification"])
        if entry["classification"] == "pre_after_release":
            linked = next(row for row in event["entries"] if row["phase"] == "post")
            if linked["recorded_at"] <= as_of:
                invalidated_at = linked["recorded_at"]
        row, gap = labeled_row(event, entry, arm, as_of) if classification == "prospective" else (None, classification)
        if invalidated_at is not None:
            gaps["pre_after_release"] += 1
            if row is None or row["label_available_at"] >= invalidated_at:
                continue
        elif gap is not None:
            gaps[gap] += 1
            continue
        rows.append({**row, "invalidated_at": invalidated_at})
    return rows, dict(gaps)


def fit(training):
    columns = list(zip(*(row["features"] for row in training)))
    means = [sum(column) / len(column) for column in columns]
    scales = [max(1e-6, sqrt(sum((value - mean) ** 2 for value in column) / len(column)))
              for column, mean in zip(columns, means)]

    def transform(features):
        return [1.0, *(max(-5, min(5, (value - mean) / scale))
                       for value, mean, scale in zip(features, means, scales))]

    designs = [(transform(row["features"]), row["label"]) for row in training]
    baseline = (1 + sum(row["label"] for row in training)) / (2 + len(training))
    weights = [log(baseline / (1 - baseline)), *([0.0] * len(FEATURES))]

    def probability(vector):
        return 1 / (1 + exp(-max(-20, min(20, sum(weight * value for weight, value in zip(weights, vector))))))

    for _ in range(200):
        errors = [(vector, probability(vector) - label) for vector, label in designs]
        gradients = [sum(vector[index] * error for vector, error in errors) / len(errors)
                     for index in range(len(weights))]
        for index, gradient in enumerate(gradients):
            weights[index] -= 0.05 * (gradient + (0.10 * weights[index] if index else 0))
    return baseline, lambda features: probability(transform(features))


def scores(predictions, key):
    actual = [(row["label"], row[key]) for row in predictions]
    buckets = defaultdict(list)
    for label, probability in actual:
        buckets[min(int(probability * 5), 4)].append((label, probability))
    return {"brier": round(sum((outcome - prob) ** 2 for outcome, prob in actual) / len(actual), 8),
            "log_loss": round(-sum(log(prob if outcome else 1 - prob) for outcome, prob in actual) / len(actual), 8),
            "reliability": [{"probability_interval": [bucket / 5, (bucket + 1) / 5], "observations": len(members),
                             "average_probability": sum(p for _, p in members) / len(members),
                             "observed_frequency": sum(y for y, _ in members) / len(members)}
                            for bucket, members in sorted(buckets.items())]}


def walk_forward(rows):
    grouped = defaultdict(list)
    for row in rows:
        if row.get("invalidated_at") is None:
            grouped[row["event_day"]].append(row)
    predictions = []
    for day in sorted(grouped):
        group = sorted(grouped[day], key=lambda row: row["cutoff"])
        cutoff = group[0]["cutoff"]
        training = [{key: value for key, value in row.items() if key != "invalidated_at"}
                    for row in rows if row["event_day"] < day and row["label_available_at"] < cutoff
                    and (row.get("invalidated_at") is None or row["invalidated_at"] > cutoff)]
        training.sort(key=lambda row: (row["event_day"], row["event_id"]))
        train_days = len({row["event_day"] for row in training})
        if train_days < TRAIN_DAYS or {row["label"] for row in training} != {0, 1}:
            continue
        baseline, estimate = fit(training)
        for row in group:
            predictions.append({key: row[key] for key in ("event_id", "entry_id", "replay_id", "event_day", "label")}
                               | {"baseline_probability": baseline, "model_probability": estimate(row["features"]),
                                  "training_event_days": train_days, "training_manifest": digest(training)})
    return predictions


def forecast_report(journal, raw):
    moment = now_utc()
    as_of = utc(raw["as_of"]) if raw.get("as_of") else moment
    if as_of > moment:
        raise ValueError("forecast validation cutoff cannot be in the future")
    phase, target = raw.get("phase", "pre"), raw.get("target", "debit_spread_profitable")
    if phase not in PHASES or target not in TARGETS:
        raise ValueError("invalid forecast phase or target")
    experiments = [experiment for experiment in list_experiments(journal) if experiment["registered_at"] <= as_of]
    if raw.get("experiment_id"):
        experiments = [experiment for experiment in experiments if experiment["experiment_id"] == raw["experiment_id"]]
        if not experiments:
            raise ValueError("experiment was unavailable at this forecast cutoff")
    reports = []
    for experiment in experiments:
        rows, gaps = sample_rows(experiment, phase, target, as_of)
        eligible = [row for row in rows if row["invalidated_at"] is None]
        predictions = walk_forward(rows)
        days = len({row["event_day"] for row in predictions})
        sufficient = days >= TEST_DAYS
        reports.append({"experiment_id": experiment["experiment_id"], "name": experiment["protocol"]["name"],
                        "status": "scored_research" if sufficient else "insufficient_outcomes", "gaps": gaps,
                        "eligible_labeled_events": len(eligible),
                        "distinct_labeled_days": len({row["event_day"] for row in eligible}),
                        "predicted_test_days": days,
                        "scores": {"baseline": scores(predictions, "baseline_probability"),
                                   "logistic": scores(predictions, "model_probability")} if sufficient else None,
                        "predictions": predictions if sufficient else []})
    return {"as_of": as_of, "version": VERSION, "phase": phase, "target": target, "features": FEATURES,
            "minimum_training_days": TRAIN_DAYS, "minimum_test_days": TEST_DAYS, "reports": reports,
            "basis": "prospective quote replay labels with walk-forward evaluation by event day"}
