"""Preregister and score a paired, prospective post-release forecast experiment."""

from collections import Counter, defaultdict
from decimal import Decimal

from earnings import executable_mark, list_experiments, lookup, now_utc, read_frozen
from market_risk import signed
from optionsdesk import NEW_YORK, canonical, digest, identifier, timestamp, utc
from research_graph import graph_as_of


LEVELS = ("0.10", "0.25", "0.50", "0.75", "0.90")
ARMS = ("numerical", "with_claims")
REVIEW_DAYS = 25


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS event_quantile_protocols (
        experiment_id TEXT PRIMARY KEY REFERENCES earnings_experiments(id),
        id TEXT NOT NULL UNIQUE, frozen TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS event_quantile_forecasts (
        event_id TEXT PRIMARY KEY REFERENCES earnings_events(id),
        id TEXT NOT NULL UNIQUE, frozen TEXT NOT NULL)""")


def register(journal, raw):
    experiment = raw["experiment_id"]
    recipes = {arm: identifier(raw["recipes"][arm]) for arm in ARMS}
    if recipes[ARMS[0]] == recipes[ARMS[1]]:
        raise ValueError("supply distinct, versioned recipes for each forecast arm")
    with journal.db:
        journal.db.execute("BEGIN IMMEDIATE")
        lookup(journal, "earnings_experiments", experiment, "experiment_id")
        previous = journal.db.execute(
            "SELECT id, frozen FROM event_quantile_protocols WHERE experiment_id = ?", (experiment,)
        ).fetchone()
        if previous:
            protocol = read_frozen(previous, "protocol_id")
            if protocol["recipes"] != recipes:
                raise ValueError("this experiment already has frozen quantile recipes")
            return protocol
        frozen = {"experiment_id": experiment, "recipes": recipes, "registered_at": now_utc(), "version": 1}
        protocol_id = digest(frozen)
        journal.db.execute("INSERT INTO event_quantile_protocols VALUES (?, ?, ?)",
                           (experiment, protocol_id, canonical(frozen)))
    return {"protocol_id": protocol_id, **frozen}


def quantiles(raw):
    if set(raw) != set(LEVELS):
        raise ValueError("supply the five fixed quantile levels")
    values = [signed(raw[level]) for level in LEVELS]
    if min(values) < -10000 or values != sorted(values):
        raise ValueError("return quantiles must be ordered and at least -10000 bps")
    return dict(zip(LEVELS, map(str, values), strict=True))


def seal(journal, raw):
    if set(raw["forecasts"]) != set(ARMS):
        raise ValueError("supply both registered forecast arms")
    forecasts = {arm: quantiles(raw["forecasts"][arm]) for arm in ARMS}
    ids = raw["claim_ids"]
    if not isinstance(ids, list) or not ids or len(ids) > 20 or len(set(ids)) != len(ids):
        raise ValueError("supply 1 to 20 distinct graph claim IDs")
    event_id = raw["event_id"]
    with journal.db:
        journal.db.execute("BEGIN IMMEDIATE")
        event = lookup(journal, "earnings_events", event_id, "event_id")
        previous = journal.db.execute(
            "SELECT id, frozen FROM event_quantile_forecasts WHERE event_id = ?", (event_id,)
        ).fetchone()
        if previous:
            receipt = read_frozen(previous, "forecast_id")
            if receipt["forecasts"] != forecasts or receipt["claim_ids"] != sorted(ids):
                raise ValueError("this event already has a sealed forecast pair")
            return receipt
        protocol_row = journal.db.execute(
            "SELECT id, frozen FROM event_quantile_protocols WHERE experiment_id = ?", (event["experiment_id"],)
        ).fetchone()
        if protocol_row is None:
            raise ValueError("register quantile recipes first")
        protocol = read_frozen(protocol_row, "protocol_id")
        now = now_utc()
        if not event["registered_at"] <= now <= event["post_at"]:
            raise ValueError("seal forecasts by the registered post-release cutoff")
        graph = graph_as_of(journal, as_of=now, subject={"type": "event", "key": event_id}, include_model=True)
        edges = {edge["id"]: edge for edge in graph["edges"] if edge["predicate"] == "reports"}
        if not all(key in edges for key in ids):
            raise ValueError("claims must be event-linked reports available when the pair is sealed")
        documents = {edges[key]["document_id"] for key in ids}
        if len(documents) != 1:
            raise ValueError("claims must come from one release")
        source = next(doc for doc in graph["documents"] if doc["id"] in documents)
        if not max(protocol["registered_at"], event["registered_at"]) < source["published_at"] <= now:
            raise ValueError("register the experiment and recipes before the cited release")
        frozen = {"event_id": event_id, "protocol_id": protocol["protocol_id"], "recorded_at": now,
                  "source": source, "claim_ids": sorted(ids), "forecasts": forecasts}
        forecast_id = digest(frozen)
        journal.db.execute("INSERT INTO event_quantile_forecasts VALUES (?, ?, ?)",
                           (event_id, forecast_id, canonical(frozen)))
    return {"forecast_id": forecast_id, **frozen}


def outcome(event, forecast, as_of, protocol):
    if not forecast:
        return "forecast_missing" if event["post_at"] <= as_of else "scheduled", None
    entry = next((row for row in event["entries"] if row["phase"] == "post" and row["recorded_at"] <= as_of), None)
    if not entry:
        return "entry_unavailable", None
    if entry["classification"] != "prospective":
        return entry["classification"], None
    release, source = entry["release"], forecast["source"]
    if (release["source"] != source["source_url"] or release["published_at"] != source["published_at"]
        or release["first_seen_at"] > forecast["recorded_at"]):
        return "release_mismatch", None
    replay = entry["replay"]
    if not replay or replay["recorded_at"] > as_of:
        return "exit_replay_unavailable", None
    enter_status, start, _ = executable_mark(protocol, event, entry["as_of"], entry["evidence"], "underlying",
                                             opening=True, release_at=release["published_at"])
    exit_status, finish, _ = executable_mark(protocol, event, replay["as_of"], replay["evidence"], "underlying", opening=False)
    if enter_status != "eligible" or exit_status != "eligible":
        return f"underlying_{enter_status if enter_status != 'eligible' else exit_status}", None
    if replay["evidence"]["underlying"]["observed_at"] <= entry["evidence"]["underlying"]["observed_at"]:
        return "no_new_exit_trade", None
    return "scored", (finish / start - 1) * 10000


def metrics(rows, arm):
    levels = {}
    for level in LEVELS:
        groups = defaultdict(list)
        for day, realized, forecasts in rows:
            error = realized - signed(forecasts[arm][level])
            tau = Decimal(level)
            groups[day].append((max(tau * error, (tau - 1) * error), error <= 0))
        n = len(groups)
        day_loss = sum(sum(loss for loss, _ in group) / len(group) for group in groups.values()) / n
        day_coverage = sum(Decimal(sum(below for _, below in group)) / len(group) for group in groups.values()) / n
        levels[level] = {"pinball_loss_bps": str(round(day_loss, 6)), "observed_cdf_fraction": str(round(day_coverage, 6))}
    average = sum(Decimal(row["pinball_loss_bps"]) for row in levels.values()) / len(LEVELS)
    return {"mean_pinball_loss_bps": str(round(average, 6)), "quantiles": levels}


def report(journal, as_of=None):
    now = now_utc()
    as_of = utc(as_of) if as_of else now
    if as_of > now:
        raise ValueError("quantile review cutoff cannot be in the future")
    if journal.db.in_transaction:
        return report_snapshot(journal, as_of)
    with journal.db:
        journal.db.execute("BEGIN")
        return report_snapshot(journal, as_of)


def report_snapshot(journal, as_of):
    summary = {"as_of": as_of, "basis": "paired prospective underlying returns, equal weight per local decision day",
               "review_sample_event_days": REVIEW_DAYS, "reports": []}
    if not journal.db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='event_quantile_protocols'"
    ).fetchone():
        return summary
    for experiment in list_experiments(journal):
        if experiment["registered_at"] > as_of:
            continue
        registered = journal.db.execute("SELECT id, frozen FROM event_quantile_protocols WHERE experiment_id = ?",
                                        (experiment["experiment_id"],)).fetchone()
        trial = read_frozen(registered, "protocol_id") if registered else None
        if trial and trial["registered_at"] > as_of:
            trial = None
        counts, rows = Counter(), []
        for event in experiment["events"]:
            if event["registered_at"] > as_of or not trial or event["post_at"] <= trial["registered_at"]:
                continue
            row = journal.db.execute("SELECT id, frozen FROM event_quantile_forecasts WHERE event_id = ?",
                                     (event["event_id"],)).fetchone()
            forecast = read_frozen(row, "forecast_id") if row else None
            if forecast and forecast["recorded_at"] > as_of:
                forecast = None
            state, realized = outcome(event, forecast, as_of, experiment["protocol"])
            counts[state] += 1
            if realized is not None:
                day = timestamp(event["post_at"]).astimezone(NEW_YORK).date().isoformat()
                rows.append((day, realized, forecast["forecasts"]))
        days = len({day for day, _, _ in rows})
        scores = {arm: metrics(rows, arm) for arm in ARMS} if rows else None
        summary["reports"].append({"experiment_id": experiment["experiment_id"], "name": experiment["protocol"]["name"],
                        "protocol_id": trial["protocol_id"] if trial else None,
                        "outcomes": dict(counts), "scored_event_days": days,
                        "status": ("awaiting_protocol" if not trial else
                                   "review_sample_reached" if days >= REVIEW_DAYS else "insufficient_event_days"),
                        "scores": scores,
                        "claims_minus_numerical_loss_bps": str(
                            Decimal(scores["with_claims"]["mean_pinball_loss_bps"]) -
                            Decimal(scores["numerical"]["mean_pinball_loss_bps"])
                        ) if scores else None})
    return summary
