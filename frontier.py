"""Freeze cited analyst reviews and compare forecasts over the same payoff scenarios."""
from datetime import datetime, timezone
from decimal import Decimal
import json
from urllib.parse import urlsplit


def record_review(journal, supplied):
    from optionsdesk import canonical, decimal, digest, identifier, money, seconds_between, utc

    review = dict(supplied)
    decision = journal.replay(review["decision_id"])
    review["as_of"] = utc(review["as_of"])
    recorded_at = utc(datetime.now(timezone.utc).isoformat())
    review["recorded_at"] = recorded_at
    plan = decision["plan"]
    if not plan["as_of"] <= review["as_of"] <= recorded_at:
        raise ValueError("review time must fall between the original decision and now")
    for field in ("analyst", "verdict", "summary", "invalidation"):
        identifier(review[field])
    if review["verdict"] not in {"pass", "watch", "research"}:
        raise ValueError("review verdict must be pass, watch, or research")
    if not review["evidence"]:
        raise ValueError("include at least one cited observation")
    review["evidence"] = [dict(item) for item in review["evidence"]]
    for item in review["evidence"]:
        for field in ("title", "claim", "source"):
            identifier(item[field])
        location = urlsplit(item["source"])
        from_journal = (location.scheme == "journal" and location.netloc == "decision"
                        and location.path == "/" + review["decision_id"])
        if not (location.scheme in {"http", "https"} and location.netloc or from_journal):
            raise ValueError("sources must be HTTP links or refer to this journal decision")
        item["available_at"] = utc(item["available_at"])
        if item["available_at"] > review["as_of"]:
            raise ValueError("review includes evidence that arrived after its cutoff")
        if from_journal and item["available_at"] < plan["as_of"]:
            raise ValueError("journal evidence cannot precede its decision cutoff")
        if item["status"] not in {"observed", "inference", "unknown"}:
            raise ValueError("evidence status must be observed, inference, or unknown")

    analysis = decision["analysis"]
    scenarios = {row["name"]: row for row in analysis.get("scenarios", [])}
    source = (plan["quote_source"], plan["quote_feed"])
    quotes_fresh = False
    if analysis["quote_status"] == "fresh" and source != ("alpaca", "indicative"):
        oldest_quote = min(row["observed_at"] for rows in decision["evidence"]["legs"].values()
                           for row in rows if (row["source"], row["feed"]) == source)
        quotes_fresh = seconds_between(recorded_at, oldest_quote) <= decimal(plan["max_quote_age_seconds"])
    comparison = {"status": "unscored", "reason": "Paired forecast probabilities are absent."}
    if "forecasts" in review:
        forecasts = review["forecasts"]
        if set(forecasts) != {"baseline", "enhanced"} or not scenarios:
            raise ValueError("paired forecasts require a baseline, an enhanced model, and priced scenarios")
        comparison = {"status": "hypothetical" if quotes_fresh else "reference_only"}
        for label, probabilities in forecasts.items():
            if probabilities.keys() != scenarios.keys():
                raise ValueError("forecasts must use the frozen decision's scenario names")
            if sum(decimal(p) for p in probabilities.values()) != 1:
                raise ValueError("each forecast must sum to 1")
            comparison[label] = money(sum(decimal(p) * Decimal(scenarios[key]["pnl_dollars"])
                                          for key, p in probabilities.items()))
        comparison["delta_dollars"] = money(Decimal(comparison["enhanced"]) - Decimal(comparison["baseline"]))
    review["comparison"] = comparison
    review["quote_status"] = "fresh" if quotes_fresh else "reference_only"
    review_id = digest(review)
    with journal.db:
        journal.db.execute("INSERT OR IGNORE INTO reviews VALUES (?, ?, ?, ?)",
                           (review_id, review["decision_id"], review["as_of"], canonical(review)))
    return {"review_id": review_id, **review}


def latest_review(journal, decision_id):
    from optionsdesk import digest
    row = journal.db.execute("SELECT id, document FROM reviews WHERE decision_id = ? "
                             "ORDER BY as_of DESC, rowid DESC LIMIT 1", (decision_id,)).fetchone()
    if row is None:
        return None
    review = json.loads(row[1])
    if digest(review) != row[0]:
        raise ValueError("frozen review failed its integrity check")
    return {"review_id": row[0], **review}
