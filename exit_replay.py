"""Replay two exit policies against timestamped two-sided debit spread package quotes."""

from optionsdesk import decimal, digest, money, seconds_between, utc
from price_limits import whole_contracts


def exit_report(raw):
    entry, scheduled, last = (utc(raw[key]) for key in ("entry_at", "scheduled_exit_at", "last_execution_at"))
    expiry = utc(raw["expiry_at"])
    width, debit = (decimal(raw[key]) for key in ("width", "entry_debit"))
    target, stop = (decimal(raw[key]) for key in ("target_fraction_of_max_profit", "stop_fraction_of_max_loss"))
    age = decimal(raw["max_quote_age_seconds"])
    latency = decimal(raw["latency_seconds"])
    size = whole_contracts(raw["quantity"], 1, 1000000)
    fees = decimal(raw["round_trip_fee_per_contract_dollars"])
    cost, max_profit = debit * 100 + fees, (width - debit) * 100 - fees
    if not (entry < scheduled < last < expiry and max_profit > 0 and 0 < debit < width
            and 0 < target <= 1 and 0 < stop <= 1 and age > 0):
        raise ValueError("invalid exit policy, debit-spread economics, or execution cutoffs")
    books = []
    last_observed = last_received = entry
    for row in raw["package_quotes"]:
        observed, received = utc(row["observed_at"]), utc(row["received_at"])
        bid, ask = decimal(row["bid"]), decimal(row["ask"])
        depth = whole_contracts(row["bid_size"], 0, 10**9)
        if (not last_observed < observed <= received or received <= last_received or
                not 0 <= bid <= min(ask, width) or ask == 0):
            raise ValueError("package books must be positive, uncrossed, and strictly ordered")
        last_observed, last_received = observed, received
        if received <= last and seconds_between(received, observed) <= age:
            books.append((observed, received, bid, depth))

    rows = []
    for policy in ("scheduled_exit", "target_stop_or_scheduled"):
        trigger_at, reason = scheduled, "scheduled"
        if policy != "scheduled_exit":
            for _, received, bid, depth in books:
                if received >= scheduled:
                    break
                if not bid or depth < size:
                    continue
                pnl = bid * 100 - cost
                if pnl >= target * max_profit or pnl <= -stop * cost:
                    trigger_at, reason = received, "target" if pnl >= target * max_profit else "stop"
                    break
        # Trigger and modeled liquidation require separate observations.
        fill = next((book for book in books if book[0] > trigger_at and
                     seconds_between(book[0], trigger_at) >= latency and book[2] > 0 and book[3] >= size), None)
        rows.append({"policy": policy, "trigger": reason, "trigger_at": trigger_at,
                     "status": "modeled_exit" if fill else "unresolved_exit",
                     "fill_observed_at": fill[0] if fill else None, "modeled_credit": str(fill[2]) if fill else None,
                     "net_pnl_dollars": money((fill[2] * 100 - cost) * size) if fill else None})
    return {"basis": "supplied_debit_spread_package_quote_replay", "input_sha256": digest(raw), "policies": rows}
