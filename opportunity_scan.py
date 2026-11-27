"""Read-only regular-session contract scanner over journaled market quotes."""

from contextlib import closing
from decimal import Decimal
import json
import sqlite3

from market_scan import liquidity, usable
from optionsdesk import NEW_YORK, Journal, contract, decimal, money, seconds_between, timestamp, utc
from price_limits import whole_contracts
from robinhood_discovery import DISCOVERY_FILE, report as discovery_report


SETTINGS_FILE = "opportunity-scan.json"


def from_feed(journal, kind, code, cutoff, feed):
    return next((row for row in journal.observations(kind, code, cutoff)
                 if [row["source"], row["feed"]] == feed), None)


def scan(journal, raw, *, as_of):
    cutoff = utc(as_of)
    market = [(utc(item["open_at"]), utc(item["close_at"]), item["previous_session"])
              for item in raw["market_sessions"]]
    if (not market or any(start >= end for start, end, _ in market)
        or any(left[1] >= right[0] for left, right in zip(market, market[1:]))):
        raise ValueError("supply ordered, non-overlapping market sessions")
    feeds = {key: raw[key] for key in ("option_feed", "stock_feed")}
    if any(not isinstance(feed, list) or len(feed) != 2 or not all(isinstance(v, str) and v for v in feed)
           for feed in feeds.values()):
        raise ValueError("explicit provider and feed are required")
    current = next((item for item in market if item[0] <= cutoff < item[1]), None)
    upcoming = next((item[0] for item in market if item[0] > cutoff), None)
    report = {"as_of": cutoff, "status": "market_closed" if upcoming else "calendar_coverage_ended",
              "feeds": feeds, "next_open_at": upcoming, "leads": [], "excluded": [], "tested": 0,
              "basis": "journaled quotes and a simple directional watchlist rule"}
    if not current:
        return report
    opened, closes, prior_day = current
    report["closes_at"] = closes
    age = decimal(raw["max_quote_age_seconds"])
    skew = decimal(raw["max_quote_skew_seconds"])
    budget = decimal(raw["one_lot_budget_dollars"])
    fees = decimal(raw["round_trip_fee_dollars"])
    minimum = decimal(raw["min_underlying_move_pct"])
    whole_contracts(raw["liquidity_limits"]["min_size_per_side"], 1, 10**6)
    if not age or not budget:
        raise ValueError("the quote age allowance and loss budget must be positive")

    stock = {symbol: from_feed(journal, "underlying_trade", symbol, cutoff, feeds["stock_feed"])
             for symbol in {term["symbol"] for term in raw["contracts"]}}
    seen = set()
    ranked = []
    for item in raw["contracts"]:
        symbol, right = item["symbol"], item["right"]
        if right not in {"C", "P"}:
            raise ValueError("option right must be C or P")
        code = contract(symbol, item["expiry"], item["strike"], right)
        if code in seen:
            raise ValueError("duplicate scanner contract")
        seen.add(code)
        option = from_feed(journal, "option_quote", code, cutoff, feeds["option_feed"])
        price = stock[symbol]
        prior = price.get("prior_close") if price else None
        reasons = []
        last_entry = utc(item["last_entry_at"])
        if (cutoff >= last_entry or timestamp(last_entry).astimezone(NEW_YORK).date().isoformat() > item["expiry"]
            or utc(item["known_at"]) > cutoff):
            reasons.append("entry_cutoff_reached_or_unavailable")
        if feeds["option_feed"] == ["alpaca", "indicative"]:
            reasons.append("indicative_option_feed")
        if not option:
            reasons.append("option_quote_missing")
        book = None
        reserve = None
        if option:
            book, = liquidity({**raw, "option_quotes": [{**option, "contract": code}]}, cutoff)
            reasons.extend(book["reasons"])
            if option["observed_at"] < opened:
                reasons.append("option_quote_before_session")
            if option.get("adjusted") or decimal(option["multiplier"]) != 100:
                reasons.append("unsupported_deliverable")
            reserve = Decimal(book["ask"]) * 100 + fees
            if reserve > budget:
                reasons.append("one_lot_exceeds_budget")
        move = None
        if not price or price["session"] != "regular" or not opened <= price["observed_at"] or not usable(price, cutoff, age):
            reasons.append("underlying_print_missing_or_stale")
        elif (not prior or prior.get("date") != prior_day or prior.get("interpolated") is not False
              or not decimal(prior.get("price") or 0)):
            reasons.append("prior_regular_close_unverified")
        else:
            move = (decimal(price["price"]) / decimal(prior["price"]) - 1) * 100
            if abs(move) < minimum or move == 0 or (move > 0) != (right == "C"):
                reasons.append("directional_rule_inactive")
            if option and abs(seconds_between(option["observed_at"], price["observed_at"])) > skew:
                reasons.append("stock_option_quotes_out_of_sync")
        row = {"contract": code, "symbol": symbol, "right": right, "reasons": reasons,
               "underlying_move_pct": float(move) if move is not None else None,
               "one_lot_reserve_dollars": money(reserve) if reserve is not None else None,
               "ask": book["ask"] if book else None, "bid": book["bid"] if book else None,
               "spread_pct_of_mid": book["spread_pct_of_mid"] if book else None}
        if reasons:
            report["excluded"].append(row)
        else:
            ranked.append((row["spread_pct_of_mid"], -abs(move), code, row))
    report["leads"] = [item[-1] for item in sorted(ranked)]
    report["tested"] = len(seen)
    report["status"] = "watchlist_available" if ranked else "no_eligible_watchlist_leads"
    return report


def read_scan(db, *, as_of):
    path = db.parent / SETTINGS_FILE
    if not path.is_file():
        return None
    raw = json.loads(path.read_text())
    with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("BEGIN")
        result = scan(Journal(connection, initialize=False), raw, as_of=as_of)
    discovery = db.parent / DISCOVERY_FILE
    if discovery.is_file():
        result["discovery"] = discovery_report(json.loads(discovery.read_text()), as_of=as_of)
    return result
