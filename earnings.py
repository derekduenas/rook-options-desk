"""Preregister earnings decisions and reconcile quote replay and paper outcomes."""

from datetime import date, datetime, timezone
from decimal import Decimal
import json
from urllib.parse import urlsplit

from catalyst import leg_quotes, quote_status
from optionsdesk import NEW_YORK, canonical, contract, decimal, digest, identifier, money, seconds_between, timestamp, utc


VERSION = 1
PHASES = ("pre", "post")
STRUCTURES = ("outright", "debit_spread", "underlying", "cash")


def now_utc():
    return utc(datetime.now(timezone.utc).isoformat())


def source_url(value):
    value = identifier(value)
    location = urlsplit(value)
    if location.scheme != "https" or not location.hostname or location.username or location.password:
        raise ValueError("event evidence requires an HTTPS source URL")
    return value


def read_frozen(row, id_field):
    row_id, document = row
    frozen = json.loads(document)
    if digest(frozen) != row_id:
        raise ValueError(f"{id_field} failed its integrity check")
    return {id_field: row_id, **frozen}


def lookup(journal, table, row_id, id_field):
    row = journal.db.execute(f"SELECT id, frozen FROM {table} WHERE id = ?", (row_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown {id_field}: {row_id}")
    return read_frozen(row, id_field)


def normalize_protocol(raw):
    protocol = {key: identifier(raw[key]) for key in (
        "name", "quote_source", "quote_feed", "underlying_source", "underlying_feed")}
    if not isinstance(raw["universe"], list):
        raise ValueError("universe must be a list of ticker symbols")
    protocol["universe"] = sorted({identifier(symbol).upper() for symbol in raw["universe"]})
    if not protocol["universe"]:
        raise ValueError("register a fixed, nonempty universe")
    for key in ("risk_budget_dollars", "max_quote_age_seconds", "max_leg_skew_seconds", "max_record_delay_seconds",
                "option_fee_per_leg_dollars", "equity_fee_dollars"):
        protocol[key] = format(decimal(raw[key]).normalize(), "f")
    if decimal(protocol["risk_budget_dollars"]) == 0:
        raise ValueError("risk budget must be positive")
    return protocol


def normalize_event(raw, protocol):
    event = {key: utc(raw[key]) for key in ("known_at", "pre_at", "event_at", "post_at", "exit_at")}
    event.update(symbol=identifier(raw["symbol"]).upper(), title=identifier(raw["title"]),
                 source=source_url(raw["source"]), expiry=date.fromisoformat(raw["expiry"]).isoformat(),
                 right=raw["right"])
    if event["symbol"] not in protocol["universe"]:
        raise ValueError("event is outside the registered universe")
    if not event["known_at"] < event["pre_at"] < event["event_at"] < event["post_at"] < event["exit_at"]:
        raise ValueError("the cutoffs must bracket the scheduled event and precede the exit")
    if date.fromisoformat(event["expiry"]) < timestamp(event["exit_at"]).astimezone(NEW_YORK).date():
        raise ValueError("exit cutoff cannot be after the option expiration date")
    if event["right"] not in {"C", "P"}:
        raise ValueError("right must be C or P")
    for key in ("long_strike", "short_strike"):
        event[key] = format(decimal(raw[key]).normalize(), "f")
        contract(event["symbol"], event["expiry"], event[key], event["right"])
    width = decimal(event["short_strike"]) - decimal(event["long_strike"])
    if (width if event["right"] == "C" else -width) <= 0:
        raise ValueError("short strike must cap the same-direction debit spread")
    return event


def add_event(journal, experiment, raw, registered_at):
    event = normalize_event(raw, experiment["protocol"])
    experiment_id = experiment["experiment_id"]
    row = journal.db.execute("SELECT id, frozen FROM earnings_events WHERE experiment_id = ? AND symbol = ? "
                             "AND event_at = ?", (experiment_id, event["symbol"], event["event_at"])).fetchone()
    if row:
        existing = read_frozen(row, "event_id")
        if any(existing[key] != value for key, value in event.items()):
            raise ValueError("this event already has frozen terms")
        return existing
    if not event["known_at"] <= registered_at < event["pre_at"]:
        raise ValueError("event must be known and registered before its first cutoff")
    frozen = {**event, "registered_at": registered_at, "experiment_id": experiment_id}
    event_id = digest(frozen)
    journal.db.execute("INSERT INTO earnings_events VALUES (?, ?, ?, ?, ?)",
                       (event_id, experiment_id, event["symbol"], event["event_at"], canonical(frozen)))
    return {"event_id": event_id, **frozen}


def register_experiment(journal, raw):
    protocol = normalize_protocol(raw)
    key = digest({"version": VERSION, "protocol": protocol})
    with journal.db:
        journal.db.execute("BEGIN IMMEDIATE")
        registered_at = now_utc()
        row = journal.db.execute("SELECT id, frozen FROM earnings_experiments WHERE protocol_key = ?", (key,)).fetchone()
        if row:
            experiment = read_frozen(row, "experiment_id")
        else:
            frozen = {"version": VERSION, "protocol": protocol, "registered_at": registered_at}
            experiment_id = digest(frozen)
            journal.db.execute("INSERT INTO earnings_experiments VALUES (?, ?, ?)",
                               (experiment_id, key, canonical(frozen)))
            experiment = {"experiment_id": experiment_id, **frozen}
        events = [add_event(journal, experiment, event, registered_at) for event in raw.get("events", [])]
    return {**experiment, "events": events}


def schedule_event(journal, raw):
    with journal.db:
        journal.db.execute("BEGIN IMMEDIATE")
        experiment = lookup(journal, "earnings_experiments", raw["experiment_id"], "experiment_id")
        return add_event(journal, experiment, raw, now_utc())


def applicable_release(raw, phase, cutoff, event_at):
    if raw is None:
        return None
    if phase != "post":
        raise ValueError("release confirmation belongs to the post-earnings phase")
    release = {"source": source_url(raw["source"]), "published_at": utc(raw["published_at"]),
               "first_seen_at": utc(raw["first_seen_at"])}
    if not release["published_at"] <= release["first_seen_at"] <= cutoff:
        raise ValueError("release evidence was not available by the post cutoff")
    if timestamp(release["published_at"]).astimezone(NEW_YORK).date() < timestamp(event_at).astimezone(NEW_YORK).date():
        raise ValueError("release publication predates the scheduled event date")
    return release


def market_snapshot(journal, protocol, event, cutoff):
    spec = {**protocol, **event, "as_of": cutoff}
    underlying = next((row for row in journal.observations("underlying_trade", event["symbol"], cutoff)
                       if (row["source"], row["feed"]) == (protocol["underlying_source"], protocol["underlying_feed"])), None)
    return {"options": leg_quotes(journal, spec), "underlying": underlying}


def spread_width(event):
    return abs(decimal(event["long_strike"]) - decimal(event["short_strike"]))


def executable_mark(protocol, event, cutoff, evidence, structure, *, opening, release_at=None):
    if structure == "underlying":
        trade = evidence["underlying"]
        if trade is None:
            return "missing", None, None
        if seconds_between(cutoff, trade["observed_at"]) > decimal(protocol["max_quote_age_seconds"]):
            return "stale", None, None
        if release_at is not None and trade["observed_at"] < release_at:
            return "pre_release_underlying", None, None
        return "eligible", decimal(trade["price"]), None
    spread = structure == "debit_spread"
    quotes = evidence["options"] if spread else evidence["options"][:1]
    status = quote_status({**protocol, "as_of": cutoff}, quotes, version=2)
    if status != "fresh":
        return status, None, None
    if release_at is not None and any(quote["observed_at"] < release_at for quote in quotes):
        return "pre_release_quote", None, None
    long_side, short_side = ("ask", "bid") if opening else ("bid", "ask")
    price = decimal(quotes[0][long_side])
    depth = int(decimal(quotes[0][long_side + "_size"]))
    if spread:
        price -= decimal(quotes[1][short_side])
        depth = min(depth, int(decimal(quotes[1][short_side + "_size"])))
        width = spread_width(event)
        if opening and not 0 < price < width:
            return "invalid_debit", None, None
        if not opening and price > width:
            return "inconsistent_quote", None, None
    return "eligible", price, depth


def costs(protocol, structure):
    if structure == "underlying":
        return 1, decimal(protocol["equity_fee_dollars"])
    return 100, decimal(protocol["option_fee_per_leg_dollars"]) * (2 if structure == "debit_spread" else 1)


def price_entry(protocol, event, cutoff, evidence, structure, *, release_at=None):
    row = {"structure": structure, "quantity": 0, "entry_net_price": None, "entry_cost_dollars": None}
    if structure == "cash":
        return {**row, "status": "eligible", "entry_cost_dollars": "0.00"}
    status, mark, depth = executable_mark(protocol, event, cutoff, evidence, structure,
                                          opening=True, release_at=release_at)
    if status != "eligible":
        return {**row, "status": status}
    multiplier, fee = costs(protocol, structure)
    budget = decimal(protocol["risk_budget_dollars"])
    # Option fees are per contract; the equity fee is a flat charge per order.
    if structure == "underlying":
        quantity = max(0, int((budget - 2 * fee) // mark))
        entry_cost = quantity * mark + fee
    else:
        quantity = min(depth, int(budget // (mark * multiplier + 2 * fee)))
        entry_cost = quantity * (mark * multiplier + fee)
    return {**row, "status": "eligible" if quantity else "risk_budget", "quantity": quantity,
            "entry_net_price": str(mark), "entry_cost_dollars": money(entry_cost) if quantity else "0.00"}


def record_entry(journal, raw):
    phase, action = raw["phase"], raw["action"]
    if phase not in PHASES or action not in {"paper", "pass"}:
        raise ValueError("entry requires pre or post phase and paper or pass action")
    with journal.db:
        journal.db.execute("BEGIN IMMEDIATE")
        event = lookup(journal, "earnings_events", raw["event_id"], "event_id")
        if event["experiment_id"] != raw["experiment_id"]:
            raise ValueError("event belongs to another experiment")
        cutoff = event[phase + "_at"]
        request = {"action": action, "reason": identifier(raw["reason"]),
                   "release": applicable_release(raw.get("release"), phase, cutoff, event["event_at"])}
        row = journal.db.execute("SELECT id, frozen FROM earnings_entries WHERE event_id = ? AND phase = ?",
                                 (event["event_id"], phase)).fetchone()
        if row:
            entry = read_frozen(row, "entry_id")
            if any(entry[key] != value for key, value in request.items()):
                raise ValueError("this phase already has a frozen entry")
            return entry
        recorded_at = now_utc()
        if recorded_at < cutoff:
            raise ValueError("entry cannot be recorded before its scheduled cutoff")
        experiment = lookup(journal, "earnings_experiments", event["experiment_id"], "experiment_id")
        protocol = experiment["protocol"]
        deadline = event["event_at"] if phase == "pre" else event["exit_at"]
        if recorded_at >= deadline or seconds_between(recorded_at, cutoff) > decimal(protocol["max_record_delay_seconds"]):
            classification = "late"
        else:
            classification = "release_unconfirmed" if phase == "post" and request["release"] is None else "prospective"
        evidence = market_snapshot(journal, protocol, event, cutoff)
        release_at = request["release"]["published_at"] if request["release"] else None
        frozen = {**request, "event_id": event["event_id"], "phase": phase, "as_of": cutoff, "recorded_at": recorded_at,
                  "classification": classification, "evidence": evidence,
                  "candidates": [price_entry(protocol, event, cutoff, evidence, kind, release_at=release_at)
                                 for kind in STRUCTURES]}
        entry_id = digest(frozen)
        journal.db.execute("INSERT INTO earnings_entries VALUES (?, ?, ?, ?)",
                           (entry_id, event["event_id"], phase, canonical(frozen)))
    return {"entry_id": entry_id, **frozen}


def exit_outcome(protocol, event, evidence, candidate):
    structure, quantity = candidate["structure"], candidate["quantity"]
    row = {"structure": structure, "quantity": quantity, "status": candidate["status"], "pnl_dollars": None}
    if structure == "cash":
        return {**row, "status": "modeled", "pnl_dollars": "0.00"}
    if not quantity:
        return row
    status, mark, depth = executable_mark(protocol, event, event["exit_at"], evidence, structure, opening=False)
    if status != "eligible":
        return {**row, "status": status + "_exit"}
    if depth is not None and depth < quantity:
        return {**row, "status": "insufficient_exit_size"}
    multiplier, fee = costs(protocol, structure)
    fee_total = 2 * fee * (1 if structure == "underlying" else quantity)
    pnl = (mark - Decimal(candidate["entry_net_price"])) * multiplier * quantity - fee_total
    return {**row, "status": "modeled", "exit_net_price": str(mark), "pnl_dollars": money(pnl)}


def resolve_entry(journal, entry_id):
    with journal.db:
        journal.db.execute("BEGIN IMMEDIATE")
        previous = journal.db.execute("SELECT id, frozen FROM earnings_replays WHERE entry_id = ?", (entry_id,)).fetchone()
        if previous:
            return read_frozen(previous, "replay_id")
        entry = lookup(journal, "earnings_entries", entry_id, "entry_id")
        event = lookup(journal, "earnings_events", entry["event_id"], "event_id")
        recorded_at = now_utc()
        if recorded_at < event["exit_at"]:
            raise ValueError("quote replay cannot resolve before its fixed exit")
        protocol = lookup(journal, "earnings_experiments", event["experiment_id"], "experiment_id")["protocol"]
        evidence = market_snapshot(journal, protocol, event, event["exit_at"])
        frozen = {"entry_id": entry_id, "as_of": event["exit_at"], "recorded_at": recorded_at, "evidence": evidence,
                  "outcomes": [exit_outcome(protocol, event, evidence, candidate) for candidate in entry["candidates"]]}
        replay_id = digest(frozen)
        journal.db.execute("INSERT INTO earnings_replays VALUES (?, ?, ?)", (replay_id, entry_id, canonical(frozen)))
    return {"replay_id": replay_id, **frozen}


def paper_payload(raw, entry, event, protocol, recorded_at, *, retry=False):
    structure, status = raw["structure"], raw["status"]
    if structure not in STRUCTURES[:-1] or status not in {"filled", "partial", "unfilled"}:
        raise ValueError("paper execution requires a candidate and filled, partial, or unfilled status")
    candidate = next(row for row in entry["candidates"] if row["structure"] == structure)
    if entry["action"] != "paper" or entry["classification"] != "prospective" or not candidate["quantity"]:
        raise ValueError("paper execution requires a prospective, sized candidate")
    attempt = utc(raw["attempted_at"])
    if not entry["recorded_at"] <= attempt < event["exit_at"] or attempt > recorded_at:
        raise ValueError("paper attempt is outside the recorded entry window")
    if entry["phase"] == "pre" and attempt >= event["event_at"]:
        raise ValueError("pre-earnings attempt must precede the scheduled event")
    fill = {"entry_id": entry["entry_id"], "structure": structure, "status": status,
            "source": identifier(raw["source"]), "reference": identifier(raw["reference"]), "attempted_at": attempt,
            "pnl_dollars": None, "quantity": 0}
    if status == "unfilled":
        fill_fields = ("entry_at", "exit_at", "entry_net_price", "exit_net_price")
        has_fill_details = any(raw.get(key) is not None for key in fill_fields)
        if not retry and (has_fill_details or decimal(raw.get("quantity") or 0) or decimal(raw.get("fees_dollars") or 0)):
            raise ValueError("an unfilled attempt cannot include a fill or fill fees")
        return fill
    quantity = decimal(raw["quantity"])
    if quantity % 1 or not 0 < quantity <= candidate["quantity"] or (
            (status == "filled") != (quantity == candidate["quantity"])):
        raise ValueError("paper quantity does not match the frozen size or fill status")
    entry_at, exit_at = utc(raw["entry_at"]), utc(raw["exit_at"])
    if not attempt <= entry_at < exit_at <= min(event["exit_at"], recorded_at):
        raise ValueError("paper fill timestamps are outside the experiment window")
    if entry["phase"] == "pre" and entry_at >= event["event_at"]:
        raise ValueError("pre-earnings fill must precede the scheduled event")
    price, exit_price, fees = decimal(raw["entry_net_price"]), Decimal(str(raw["exit_net_price"])), decimal(raw["fees_dollars"])
    if not price or not exit_price.is_finite() or (exit_price < 0 and structure != "debit_spread"):
        raise ValueError("invalid paper execution price")
    if not retry and structure == "debit_spread" and price >= spread_width(event):
        raise ValueError("paper debit must be below the registered strike width")
    multiplier, _ = costs(protocol, structure)
    if price * multiplier * quantity + fees > decimal(protocol["risk_budget_dollars"]):
        raise ValueError("paper entry and fees exceed the frozen risk budget")
    pnl = (exit_price - price) * multiplier * quantity - fees
    return {**fill, "quantity": int(quantity), "entry_at": entry_at, "exit_at": exit_at,
            "entry_net_price": str(price), "exit_net_price": str(exit_price), "fees_dollars": str(fees),
            "pnl_dollars": money(pnl)}


def record_paper(journal, raw):
    with journal.db:
        journal.db.execute("BEGIN IMMEDIATE")
        entry = lookup(journal, "earnings_entries", raw["entry_id"], "entry_id")
        event = lookup(journal, "earnings_events", entry["event_id"], "event_id")
        protocol = lookup(journal, "earnings_experiments", event["experiment_id"], "experiment_id")["protocol"]
        recorded_at = now_utc()
        row = journal.db.execute("SELECT id, frozen FROM earnings_paper WHERE entry_id = ? AND structure = ?",
                                 (entry["entry_id"], raw["structure"])).fetchone()
        # Apply new validation to new records while recognizing retries of immutable prior records.
        fill = paper_payload(raw, entry, event, protocol, recorded_at, retry=row is not None)
        if row:
            existing = read_frozen(row, "paper_id")
            previous = {key: existing.get(key) for key in fill}
            for key in ("entry_net_price", "exit_net_price", "fees_dollars"):
                if key in fill and previous[key] is not None:
                    previous[key] = Decimal(previous[key])
                    fill[key] = Decimal(fill[key])
            if previous != fill:
                raise ValueError("this candidate already has a frozen paper outcome")
            return existing
        frozen = {**fill, "recorded_at": recorded_at}
        paper_id = digest(frozen)
        journal.db.execute("INSERT INTO earnings_paper VALUES (?, ?, ?, ?, ?, ?)",
                           (paper_id, entry["entry_id"], fill["structure"], fill["source"], fill["reference"], canonical(frozen)))
    return {"paper_id": paper_id, **frozen}


def list_experiments(journal):
    experiments = [read_frozen(row, "experiment_id") for row in journal.db.execute(
        "SELECT id, frozen FROM earnings_experiments ORDER BY rowid DESC")]
    for experiment in experiments:
        experiment["events"] = [read_frozen(row, "event_id") for row in journal.db.execute(
            "SELECT id, frozen FROM earnings_events WHERE experiment_id = ? ORDER BY event_at, symbol",
            (experiment["experiment_id"],))]
        for event in experiment["events"]:
            entries = event["entries"] = [read_frozen(row, "entry_id") for row in journal.db.execute(
                "SELECT id, frozen FROM earnings_entries WHERE event_id = ? ORDER BY phase DESC", (event["event_id"],))]
            post = next((entry for entry in entries if entry["phase"] == "post" and entry["release"]), None)
            for entry in entries:
                entry_id = entry["entry_id"]
                row = journal.db.execute("SELECT id, frozen FROM earnings_replays WHERE entry_id = ?", (entry_id,)).fetchone()
                entry["replay"] = read_frozen(row, "replay_id") if row else None
                entry["paper"] = [read_frozen(row, "paper_id") for row in journal.db.execute(
                    "SELECT id, frozen FROM earnings_paper WHERE entry_id = ? ORDER BY rowid", (entry_id,))]
                # Keep the original record intact and expose eligibility using confirmed release timing.
                if entry["phase"] == "pre" and post:
                    release_at = post["release"]["published_at"]
                    if release_at <= entry["recorded_at"]:
                        entry["recorded_classification"] = entry["classification"]
                        entry["classification"] = "pre_after_release"
                    for fill in entry["paper"]:
                        if release_at <= fill.get("entry_at", fill["attempted_at"]):
                            fill.update(classification="pre_after_release", classification_effective_at=post["recorded_at"])
    return experiments
