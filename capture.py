"""Read-only scheduled market capture and separately labeled historical replay."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from http.client import HTTPSConnection, HTTPException
import json
import os
from urllib.parse import quote, urlencode

from earnings import (PHASES, STRUCTURES, costs, exit_outcome, list_experiments, market_snapshot, normalize_event,
                      normalize_protocol, now_utc, price_entry, resolve_entry)
from catalyst import quote_status
from optionsdesk import (NEW_YORK, canonical, contract, decimal, digest, identifier, money, seconds_between,
                         timestamp, utc, validate_quote)


ALPACA_DATA = "data.alpaca.markets"
ALPACA_PAPER = "paper-api.alpaca.markets"
ALPACA_LIVE = "api.alpaca.markets"
MASSIVE_DATA = "api.massive.com"
CAPTURE_STAGES = (*PHASES, "exit")
MAX_WINDOW = Decimal(45)
ATTEMPT_SPACING_SECONDS = 5
IN_PROGRESS_LEASE_SECONDS = 15


def init_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS desk_capture_attempts (
        event_id TEXT NOT NULL REFERENCES earnings_events(id), stage TEXT NOT NULL,
        attempted_at TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY (event_id, stage, attempted_at)
    )""")


def contracts(event):
    return [contract(event["symbol"], event["expiry"], event[key], event["right"])
            for key in ("long_strike", "short_strike")]


def connector_requirements(protocol, credentials):
    status = {}
    for kind, supported in (("quote", {"opra"}), ("underlying", {"sip", "iex"})):
        provider = protocol[kind + "_source"], protocol[kind + "_feed"]
        if provider[0] != "alpaca" or provider[1] not in supported:
            status[kind] = "connector_import_required"
        else:
            status[kind] = "configured" if credentials else "credentials_required"
    return status


def credentials_from_environment():
    key, secret = os.getenv("APCA_API_KEY_ID"), os.getenv("APCA_API_SECRET_KEY")
    if not (key and secret):
        return None
    mode = os.getenv("OPTIONSDESK_ALPACA_CONTRACT_MODE", "paper")
    if mode not in {"paper", "live"}:
        raise ValueError("OPTIONSDESK_ALPACA_CONTRACT_MODE must be paper or live")
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}, ALPACA_PAPER if mode == "paper" else ALPACA_LIVE


def market_get(host, path, params, headers):
    target = path + ("?" + urlencode(params) if params else "")
    connection = HTTPSConnection(host, timeout=4)
    try:
        connection.request("GET", target, headers={"Accept": "application/json", **headers})
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError(f"{host} returned HTTP {response.status}")
        body = response.read(1_000_001)
        if len(body) > 1_000_000:
            raise ValueError("market response exceeded the size limit")
        result = json.loads(body)
        if not isinstance(result, dict):
            raise ValueError(f"{host} returned an invalid market response")
        return result
    except (OSError, HTTPException, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError(f"{host} market data request failed ({type(error).__name__})") from None
    finally:
        connection.close()


def option_reference(host, code, symbol, headers):
    raw = market_get(host, f"/v2/options/contracts/{quote(code, safe='')}", {}, headers)
    deliverables = raw.get("deliverables", [])
    if (raw.get("symbol") != code or raw.get("root_symbol") != symbol or raw.get("underlying_symbol") != symbol
            or decimal(raw.get("multiplier", 0)) != 100 or decimal(raw.get("size", 0)) != 100
            or not isinstance(deliverables, list) or len(deliverables) != 1):
        raise ValueError(f"unverified standard deliverable for {code}")
    delivery = deliverables[0]
    if (delivery.get("type") != "equity" or delivery.get("symbol") != symbol
            or decimal(delivery.get("amount", 0)) != 100 or delivery.get("delayed_settlement") is not False):
        raise ValueError(f"unverified standard deliverable for {code}")


def pull_options(protocol, event, auth):
    headers, contract_host = auth
    codes = contracts(event)
    verified, problems = [], {}
    with ThreadPoolExecutor(max_workers=len(codes)) as pool:
        checks = {code: pool.submit(option_reference, contract_host, code, event["symbol"], headers) for code in codes}
        for code, check in checks.items():
            try:
                check.result()
            except (ValueError, TypeError, AttributeError, KeyError, ArithmeticError) as error:
                problems[code] = str(error)
            else:
                verified.append(code)
    feed = protocol["quote_feed"]
    raw = (market_get(ALPACA_DATA, "/v1beta1/options/quotes/latest",
                      {"symbols": ",".join(verified), "feed": feed}, headers) if verified else {})
    receipt = now_utc()
    observations = []
    quotes = raw.get("quotes") or {}
    for code in verified:
        option = quotes.get(code)
        if not option:
            problems[code] = "quote unavailable"
            continue
        try:
            row = {"kind": "option_quote", "instrument": code, "observed_at": utc(option["t"]),
                   "bid": option["bp"], "ask": option["ap"], "bid_size": option["bs"],
                   "ask_size": option["as"], "multiplier": 100, "adjusted": False}
            validate_quote(row)
            if row["observed_at"] > receipt:
                raise ValueError("quote is newer than its receipt")
        except (ValueError, TypeError, KeyError, ArithmeticError) as error:
            problems[code] = str(error)
        else:
            observations.append(row)
    return {"source": "alpaca", "feed": feed, "received_at": receipt, "observations": observations,
            "problems": problems}


def pull_trade(protocol, event, auth):
    headers, _ = auth
    ticker, feed = event["symbol"], protocol["underlying_feed"]
    raw = market_get(ALPACA_DATA, "/v2/stocks/trades/latest", {"symbols": ticker, "feed": feed}, headers)
    receipt = now_utc()
    trade = (raw.get("trades") or {}).get(ticker)
    rows = [{"kind": "underlying_trade", "instrument": ticker, "observed_at": trade["t"], "price": trade["p"],
             "session": "provider_trade"}] if trade else []
    return {"source": "alpaca", "feed": feed, "received_at": receipt, "observations": rows}


def stage_rows(journal, experiment, event, stage, as_of):
    protocol = experiment["protocol"]
    cutoff = event[stage + "_at"]
    window = min(MAX_WINDOW, decimal(protocol["max_quote_age_seconds"]))
    mode_at = min(cutoff, as_of)
    delay = seconds_between(cutoff, as_of)
    capture = market_snapshot(journal, protocol, event, mode_at)
    spec = {**protocol, "as_of": mode_at}
    codes = contracts(event)
    quote_legs = [{"contract": code, "status": quote_status(spec, [row], version=2),
                   "observed_at": row["observed_at"] if row else None, "received_at": row["received_at"] if row else None}
                  for code, row in zip(codes, capture["options"], strict=True)]
    fresh_legs = sum(row["status"] == "fresh" for row in quote_legs)
    pair_status = quote_status(spec, capture["options"], version=2)
    synchronized = pair_status == "fresh"
    underlying = capture["underlying"]
    stock_status = "missing"
    if underlying:
        stock_status = ("fresh" if seconds_between(mode_at, underlying["observed_at"]) <=
                        decimal(protocol["max_quote_age_seconds"]) else "stale")
    fresh_stock = stock_status == "fresh"
    if delay > window:
        state = "scheduled"
    elif delay > 0:
        state = "capture_window"
    elif synchronized and fresh_stock:
        state = "captured"
    else:
        state = "partial" if fresh_legs or fresh_stock else "missing"
    attempt = journal.db.execute("""SELECT attempted_at, result FROM desk_capture_attempts
        WHERE event_id = ? AND stage = ? AND attempted_at <= ? ORDER BY attempted_at DESC LIMIT 1""",
        (event["event_id"], stage, as_of)).fetchone()
    last_attempt = {"at": attempt[0], **json.loads(attempt[1])} if attempt else None
    if last_attempt and last_attempt.get("finished_at", as_of) > as_of:
        last_attempt = {"at": attempt[0], "status": "in_progress"}
    return {"experiment_id": experiment["experiment_id"], "event_id": event["event_id"], "symbol": event["symbol"],
            "stage": stage, "cutoff": cutoff, "status": state, "option_contracts": codes,
            "capture_window_seconds": str(window), "evidence_as_of": mode_at,
            "feeds": {"quote": [protocol["quote_source"], protocol["quote_feed"]],
                      "underlying": [protocol["underlying_source"], protocol["underlying_feed"]]},
            "quote_legs": quote_legs, "option_pair_status": pair_status, "underlying_status": stock_status,
            "fresh_option_legs": fresh_legs, "synchronized": synchronized, "fresh_underlying": fresh_stock,
            "last_attempt": last_attempt}


def capture_status(journal, *, as_of=None, auth=None, symbol=None):
    as_of = utc(as_of) if as_of else now_utc()
    registered = list_experiments(journal)
    tasks = []
    for experiment in registered:
        for event in experiment["events"]:
            if event["registered_at"] > as_of or (symbol is not None and event["symbol"] != symbol):
                continue
            for stage in CAPTURE_STAGES:
                row = stage_rows(journal, experiment, event, stage, as_of)
                row["transport"] = connector_requirements(experiment["protocol"], auth)
                tasks.append(row)
    return {"as_of": as_of, "tasks": sorted(tasks, key=lambda task: (task["cutoff"], task["event_id"]))}


def claim_attempt(journal, event, stage, window):
    with journal.db:
        journal.db.execute("BEGIN IMMEDIATE")
        now = now_utc()
        if not 0 < seconds_between(event[stage + "_at"], now) <= window:
            return None
        previous = journal.db.execute("""SELECT attempted_at, result FROM desk_capture_attempts
            WHERE event_id = ? AND stage = ? ORDER BY attempted_at DESC LIMIT 1""",
            (event["event_id"], stage)).fetchone()
        if previous:
            spacing = (IN_PROGRESS_LEASE_SECONDS if json.loads(previous[1]).get("status") == "in_progress"
                       else ATTEMPT_SPACING_SECONDS)
            if seconds_between(now, previous[0]) < spacing:
                return None
        journal.db.execute("INSERT INTO desk_capture_attempts VALUES (?, ?, ?, ?)",
                           (event["event_id"], stage, now, canonical({"status": "in_progress"})))
    return now


def capture_once(journal, auth):
    now = now_utc()
    attempted = []
    for experiment in list_experiments(journal):
        protocol = experiment["protocol"]
        transport = connector_requirements(protocol, auth)
        work = {kind: pull for kind, pull in (("quote", pull_options), ("underlying", pull_trade))
                if transport[kind] == "configured"}
        if not work:
            continue
        window = min(MAX_WINDOW, decimal(protocol["max_quote_age_seconds"]))
        for event in experiment["events"]:
            for stage in CAPTURE_STAGES:
                if not 0 < seconds_between(event[stage + "_at"], now_utc()) <= window:
                    continue
                started = claim_attempt(journal, event, stage, window)
                if started is None:
                    continue
                records = {}
                with ThreadPoolExecutor(max_workers=len(work)) as pool:
                    futures = {kind: pool.submit(pull, protocol, event, auth) for kind, pull in work.items()}
                    for kind, future in futures.items():
                        try:
                            bundle = future.result()
                            records[kind] = journal.record(bundle) if bundle["observations"] else {"status": "empty_response"}
                            if bundle.get("problems"):
                                records[kind].update(status="partial_response" if bundle["observations"] else "error",
                                                     problems=bundle["problems"])
                        except (ValueError, TypeError, AttributeError, KeyError, ArithmeticError) as error:
                            records[kind] = {"status": "error", "reason": str(error)}
                details = {"status": "finished", "finished_at": now_utc(), "feeds": records}
                with journal.db:
                    journal.db.execute("UPDATE desk_capture_attempts SET result = ? WHERE event_id = ? AND stage = ? "
                                       "AND attempted_at = ?", (canonical(details), event["event_id"], stage, started))
                attempted.append({"event_id": event["event_id"], "stage": stage, "at": started, **details})
    return {"as_of": now, "attempts": attempted}


def reconcile_due(journal):
    now = now_utc()
    pending = [entry["entry_id"] for experiment in list_experiments(journal) for event in experiment["events"]
               if event["exit_at"] <= now for entry in event["entries"] if entry["replay"] is None]
    results = [resolve_entry(journal, entry_id) for entry_id in pending]
    return {"as_of": now, "replays": [{"entry_id": row["entry_id"], "replay_id": row["replay_id"],
                                      "status_by_structure": {outcome["structure"]: outcome["status"]
                                                              for outcome in row["outcomes"]}}
                                     for row in results]}


def affordability_preflight(journal, raw):
    protocol = normalize_protocol(raw["protocol"])
    event = normalize_event(raw["event"], protocol)
    moment = now_utc()
    cutoff = utc(raw["as_of"]) if raw.get("as_of") else moment
    if cutoff > moment:
        raise ValueError("preflight cutoff cannot be in the future")
    evidence = market_snapshot(journal, protocol, event, cutoff)
    candidates = []
    for kind in ("outright", "debit_spread"):
        entry = price_entry(protocol, event, cutoff, evidence, kind)
        quote_quality = entry["status"]
        if quote_quality == "stale":
            rows = evidence["options"] if kind == "debit_spread" else evidence["options"][:1]
            reference_time = max(row["observed_at"] for row in rows)
            entry = price_entry(protocol, event, reference_time, evidence, kind)
        mark = entry["entry_net_price"]
        multiplier, fee = costs(protocol, kind)
        funded = decimal(mark) * multiplier + 2 * fee if mark is not None else None
        headroom = decimal(protocol["risk_budget_dollars"]) - funded if funded is not None else None
        candidates.append({**entry, "quote_quality_at_cutoff": quote_quality,
                           "one_lot_reserved_dollars": money(funded) if funded is not None else None,
                           "one_lot_headroom_dollars": money(headroom) if headroom is not None else None})
    return {"basis": "affordability_preview_only", "as_of": cutoff, "symbol": event["symbol"],
            "risk_budget_dollars": protocol["risk_budget_dollars"], "candidates": candidates, "evidence": evidence}


def archive_strike_selection(raw, event):
    if raw is None:
        return None
    as_of = utc(raw["as_of"])
    increment = decimal(raw["strike_increment"])
    width = decimal(raw["spread_width"])
    if event["right"] != "C" or not increment or not width:
        raise ValueError("historical strike selection requires a call and positive strike increment and spread width")
    if not event["known_at"] <= as_of <= event["pre_at"]:
        raise ValueError("historical strike selection must occur between the known event and the pre cutoff")
    return {"as_of": as_of, "strike_increment": str(increment), "spread_width": str(width)}


def archive_replay(raw):
    """Use explicitly retrospective market-time observations without altering the prospective journal."""
    protocol = normalize_protocol(raw["protocol"])
    event = normalize_event(raw["event"], protocol)
    source = identifier(raw["archive_source"])
    if utc(event["exit_at"]) > now_utc():
        raise ValueError("historical replay requires a completed exit")
    selection = archive_strike_selection(raw.get("strike_selection"), event)
    first_option_time = selection["as_of"] if selection else ""
    codes = contracts(event)
    ordered = {}
    for observation in raw["observations"]:
        row = {**observation, "observed_at": utc(observation["observed_at"])}
        instrument, kind = row["instrument"], row["kind"]
        if kind == "option_quote" and instrument in codes:
            validate_quote(row)
        elif kind == "underlying_trade" and instrument == event["symbol"]:
            if not decimal(row["price"]):
                raise ValueError("archive underlying price must be positive")
        else:
            raise ValueError("archive rows must match the registered instruments and supported kinds")
        key = kind, instrument, row["observed_at"]
        if key in ordered and ordered[key] != row:
            raise ValueError("conflicting historical observations share a timestamp")
        ordered[key] = row

    def snapshot(cutoff):
        latest = {}
        for (kind, instrument, observed), row in sorted(ordered.items()):
            if observed <= cutoff and (kind != "option_quote" or first_option_time <= observed):
                latest[kind, instrument] = row
        return {"options": [latest.get(("option_quote", code)) for code in codes],
                "underlying": latest.get(("underlying_trade", event["symbol"]))}

    exit_snapshot = snapshot(event["exit_at"])
    report = {"basis": "historical_market_time_only", "archive_source": source,
              "archive_reference": identifier(raw["archive_reference"]), "source_digest": digest(raw),
              "protocol": protocol, "event": event, "exit_evidence": exit_snapshot, "phases": []}
    if selection:
        trade = snapshot(selection["as_of"])["underlying"]
        if trade is None or seconds_between(selection["as_of"], trade["observed_at"]) > decimal(
                protocol["max_quote_age_seconds"]):
            raise ValueError("historical strike selection requires a fresh underlying trade at or before its cutoff")
        increment = decimal(selection["strike_increment"])
        long_strike = (decimal(trade["price"]) // increment + 1) * increment
        short_strike = long_strike + decimal(selection["spread_width"])
        if (long_strike, short_strike) != (decimal(event["long_strike"]), decimal(event["short_strike"])):
            raise ValueError("historical option strikes do not match the cutoff underlying trade and selection rule")
        report["strike_selection"] = {**selection, "rule": "strict_next_higher_call",
                                      "underlying": trade, "earliest_option_mark_at": first_option_time}
    for phase in PHASES:
        cutoff = event[phase + "_at"]
        evidence = snapshot(cutoff)
        entries = [price_entry(protocol, event, cutoff, evidence, kind) for kind in STRUCTURES]
        report["phases"].append({"phase": phase, "as_of": cutoff, "entry_evidence": evidence, "candidates": entries,
                                 "outcomes": [exit_outcome(protocol, event, exit_snapshot, row) for row in entries]})
    return report


def unix_nanos(value):
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("expected a nonnegative integer SIP timestamp in nanoseconds")
    seconds, fraction = divmod(value, 10**9)
    as_utc = datetime.fromtimestamp(seconds, timezone.utc)
    return f"{as_utc:%Y-%m-%dT%H:%M:%S}.{fraction:09d}+00:00"


def massive_contract_supported(code, symbol, day, headers):
    ticker = "O:" + code
    raw = market_get(MASSIVE_DATA, f"/v3/reference/options/contracts/{quote(ticker, safe=':')}",
                     {"as_of": day}, headers).get("results", {})
    if (raw.get("ticker") != ticker or raw.get("underlying_ticker") != symbol or raw.get("additional_underlyings")
            or decimal(raw.get("shares_per_contract", 0)) != 100):
        raise ValueError(f"unverified historical deliverable for {code} on {day}")


def massive_point(path, cutoff, headers):
    query = {"timestamp.lte": cutoff.replace("+00:00", "Z"), "limit": 1, "sort": "timestamp", "order": "desc"}
    results = market_get(MASSIVE_DATA, path, query, headers).get("results", [])
    if not results:
        return None, None
    row = results[0]
    observed = unix_nanos(row["sip_timestamp"])
    if observed > cutoff:
        raise ValueError("archive provider returned a quote or trade after its requested cutoff")
    return observed, row


def archive_fetch(raw, key):
    """Fetch dated Massive SIP/OPRA evidence for three historical cutoffs."""
    if not key:
        raise ValueError("historical retrieval requires MARKET-data entitlement and MASSIVE_API_KEY")
    protocol = normalize_protocol({**raw["protocol"], "quote_source": "massive", "quote_feed": "opra",
                                   "underlying_source": "massive", "underlying_feed": "sip"})
    event = normalize_event(raw["event"], protocol)
    if event["exit_at"] > now_utc():
        raise ValueError("historical retrieval requires a completed exit")
    selection = archive_strike_selection(raw.get("strike_selection"), event)
    headers = {"Authorization": "Bearer " + key}
    codes = contracts(event)
    cutoffs = [event[stage + "_at"] for stage in CAPTURE_STAGES]
    days = sorted({timestamp(cutoff).astimezone(NEW_YORK).date().isoformat() for cutoff in cutoffs})
    for day in days:
        for code in codes:
            massive_contract_supported(code, event["symbol"], day, headers)
    observations = []
    ticker = event["symbol"]
    options_at_cutoff = dict.fromkeys(cutoffs, codes)
    if selection:
        options_at_cutoff.setdefault(selection["as_of"], ())
    for cutoff, option_codes in options_at_cutoff.items():
        for code in option_codes:
            observed, row = massive_point(f"/v3/quotes/O:{quote(code, safe='')}", cutoff, headers)
            if row:
                observations.append({"kind": "option_quote", "instrument": code, "observed_at": observed,
                                     "multiplier": 100, "bid": row["bid_price"], "ask": row["ask_price"],
                                     "bid_size": row["bid_size"], "ask_size": row["ask_size"],
                                     "sequence_number": row.get("sequence_number")})
        observed, row = massive_point(f"/v3/trades/{quote(ticker, safe='')}", cutoff, headers)
        if row:
            observations.append({"kind": "underlying_trade", "instrument": ticker, "observed_at": observed,
                                 "price": row["price"], "conditions": row.get("conditions", [])})
    report = {"basis": "licensed_vendor_archive", "retrieved_at": now_utc(), "archive_source": "Massive SIP/OPRA",
              "archive_reference": "https://massive.com/docs/rest/options/trades-quotes/quotes", "event": event,
              "protocol": protocol, "observations": observations}
    if selection:
        report["strike_selection"] = selection
    return report
