"""Compare normalized account snapshots without broker access or state changes."""

from market_risk import signed
from optionsdesk import decimal, digest, seconds_between, utc


ORDER_PRICES = {"market": (), "limit": ("limit",), "stop": ("stop",), "stop_limit": ("limit", "stop")}


def snapshot(raw):
    account = raw["account"]
    if not isinstance(account, str) or not account.strip():
        raise ValueError("provide an account identifier")
    observed, received = utc(raw["observed_at"]), utc(raw["received_at"])
    if observed > received:
        raise ValueError("snapshot receipt precedes observation")
    cash = {key: signed(value) for key, value in raw["cash"].items()}
    if not cash:
        raise ValueError("provide at least one cash balance")
    positions = {key: signed(value) for key, value in raw["positions"].items()}
    orders = {}
    for key, order in raw["working_orders"].items():
        remaining = decimal(order["remaining_quantity"])
        kind = order["type"]
        if (not remaining or order["side"] not in {"buy", "sell"} or kind not in ORDER_PRICES
            or not isinstance(order["instrument"], str) or not order["instrument"].strip() or not order["time_in_force"]):
            raise ValueError("supply normalized single-instrument working orders")
        prices = tuple(decimal(order[field]) for field in ORDER_PRICES[kind])
        if any(not price for price in prices):
            raise ValueError("working order prices must be positive")
        orders[key] = (order["instrument"], order["side"], remaining, kind, order["time_in_force"], prices)
    return account, observed, received, {
        "cash": cash, "positions": {key: value for key, value in positions.items() if value}, "working_orders": orders,
    }


def reconcile(raw):
    cutoff = utc(raw["as_of"])
    max_age = decimal(raw["max_age_seconds"])
    max_skew = decimal(raw["max_observation_skew_seconds"])
    broker_account, broker_at, broker_received, broker = snapshot(raw["broker"])
    ledger_account, ledger_at, ledger_received, ledger = snapshot(raw["ledger"])
    reasons = []
    if broker_account != ledger_account:
        reasons.append("account_mismatch")
    for name, at, received in (("broker", broker_at, broker_received), ("ledger", ledger_at, ledger_received)):
        if received > cutoff:
            reasons.append(f"{name}_unavailable_at_cutoff")
        elif seconds_between(cutoff, at) > max_age:
            reasons.append(f"{name}_stale")
    if abs(seconds_between(broker_at, ledger_at)) > max_skew:
        reasons.append("observation_skew")

    # Only compare values when both snapshots describe the same account at a comparable time.
    differences = {
        key: sorted(ref for ref in broker[key].keys() | ledger[key].keys()
                    if broker[key].get(ref) != ledger[key].get(ref))
        for key in broker
    } if not reasons else {}
    if any(differences.values()):
        reasons.append("account_state_difference")
    return {"basis": "supplied_normalized_account_snapshots", "input_sha256": digest(raw), "as_of": cutoff,
            "status": "review_required" if reasons else "matched", "reasons": reasons,
            "differences": differences}
