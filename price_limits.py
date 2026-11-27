"""Evaluate research limit prices against joint fill and liquidation scenarios."""

from decimal import Decimal

from market_risk import signed
from optionsdesk import NEW_YORK, contract, decimal, digest, identifier, money, seconds_between, timestamp, utc


def whole_contracts(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("invalid whole-contract quantity")
    return value


def expected_shortfall(loss_and_mass, tail_mass):
    """Average the worst outcomes, taking fractional mass at the boundary."""
    remaining, loss_sum = tail_mass, Decimal(0)
    for loss, mass in sorted(loss_and_mass, reverse=True):
        used = min(remaining, mass)
        loss_sum += loss * used
        remaining -= used
        if not remaining:
            break
    return loss_sum / tail_mass


def score_distribution(raw, price, quantity, fees, exit_debit_cap, payout_cap, tail, *, wealth=None):
    identifier(raw["basis"])
    weighted_losses = []
    mass = filled_mass = expected_pnl = expected_quantity = Decimal(0)
    expected_log = Decimal(0) if wealth is not None else None
    for outcome in raw["outcomes"]:
        weight = decimal(outcome["probability"])
        filled = whole_contracts(outcome["filled_contracts"], 0, quantity)
        proceeds = signed(outcome["exit_dollars_per_contract"])
        if not filled and proceeds != 0:
            raise ValueError("unfilled outcomes must have zero proceeds")
        if proceeds < -exit_debit_cap or payout_cap is not None and proceeds > payout_cap:
            raise ValueError("exit proceeds exceed the supplied risk bounds")
        pnl = filled * (proceeds - price * 100 - fees)
        if expected_log is not None and weight:
            terminal_wealth = wealth + pnl
            expected_log = (expected_log + weight * (terminal_wealth / wealth).ln()
                            if terminal_wealth > 0 else None)
        mass += weight
        expected_pnl += weight * pnl
        expected_quantity += weight * filled
        if filled and weight:
            filled_mass += weight
            weighted_losses.append((-pnl, weight))
    if mass != 1:
        raise ValueError("each limit-price distribution must sum to one")
    return {"expected_net_dollars": expected_pnl, "fill_probability": filled_mass,
            "expected_filled_contracts": expected_quantity,
            "expected_net_dollars_given_fill": expected_pnl / filled_mass if filled_mass else None,
            "tail_loss_dollars_given_fill": max(Decimal(0), expected_shortfall(weighted_losses, filled_mass * tail))
            if filled_mass else None,
            **({"expected_log_wealth_growth": expected_log} if wealth is not None else {})}


def book_checks(raw, cutoff, max_age, spread):
    observed, received = utc(raw["observed_at"]), utc(raw["received_at"])
    bid, ask = decimal(raw["bid"]), decimal(raw["ask"])
    size = whole_contracts(raw["ask_size"], 0, 10**9)
    basis = raw["basis"]
    if basis not in {"single_contract", "package", "synthetic_legs"}:
        raise ValueError("unknown quote basis")
    if spread != (basis in {"package", "synthetic_legs"}):
        raise ValueError("book basis does not match the option structure")
    if bid > ask or ask == 0:
        raise ValueError("book must have a positive ask and must not be crossed")
    if not observed <= received <= cutoff:
        return ask, size, "quote_unavailable_at_cutoff"
    if seconds_between(cutoff, observed) > max_age:
        return ask, size, "stale_quote"
    return ask, size, None


def price_limit_report(raw, *, wealth=None):
    cutoff = utc(raw["as_of"])
    option = raw["contract"]
    symbol, expiry, right = identifier(option["symbol"]), option["expiry"], option["right"]
    if right not in {"C", "P"}:
        raise ValueError("right must be C or P")
    long_code = contract(symbol, expiry, option["long_strike"], right)
    cutoff_day = timestamp(cutoff).astimezone(NEW_YORK).date().isoformat()
    if expiry < cutoff_day:
        raise ValueError("contract expiration precedes the decision date")
    if expiry == cutoff_day and "expiry_at" not in option:
        raise ValueError("same-day expiration requires the exact contract cutoff in expiry_at")
    if "expiry_at" in option:
        expiry_at = utc(option["expiry_at"])
        if timestamp(expiry_at).astimezone(NEW_YORK).date().isoformat() != expiry or expiry_at <= cutoff:
            raise ValueError("the contract cutoff must follow the decision on its listed expiration date")
    wing = option.get("short_strike")
    short_code, payout_cap = None, None
    if wing is not None:
        short_code = contract(symbol, expiry, wing, right)
        width = (decimal(wing) - decimal(option["long_strike"])) * (1 if right == "C" else -1)
        if width <= 0:
            raise ValueError("the short strike must cap the same-direction debit spread")
        payout_cap = width * 100
    quantity = whole_contracts(raw["quantity"], 1, 10**6)
    fees = decimal(raw["round_trip_fees_per_contract_dollars"])
    exit_debit = decimal(raw["exit_debit_bound_per_contract_dollars"])
    limits = raw["limits"]
    max_loss = decimal(limits["max_loss_dollars"])
    max_tail = decimal(limits["max_tail_loss_given_fill_dollars"])
    min_net = decimal(limits["min_expected_net_dollars"])
    min_fill = decimal(limits["min_fill_probability"])
    tail = decimal(limits["tail_probability"])
    tick = decimal(raw["price_tick"])
    if not 0 < min_fill <= 1 or not 0 < tail <= 1 or not tick or not max_loss or not min_net:
        raise ValueError("positive tick, loss budget, minimum net and probability limits are required")
    ask, depth, invalid_book = book_checks(raw["quote"], cutoff, decimal(limits["max_quote_age_seconds"]), wing is not None)
    reports, seen, accepted = [], set(), []
    required_models = None
    for row in raw["price_grid"]:
        price = decimal(row["limit"])
        if not price or price % tick or price in seen or payout_cap is not None and price * 100 >= payout_cap:
            raise ValueError("use distinct positive tick-aligned limits within the structure's payoff bound")
        seen.add(price)
        models = row["models"]
        if not {"baseline", "adverse_selection"} <= models.keys():
            raise ValueError("include baseline and adverse_selection distributions at each price")
        if required_models is not None and models.keys() != required_models:
            raise ValueError("compare every price against the same set of models")
        required_models = models.keys()
        if any(utc(model["known_at"]) > cutoff for model in models.values()):
            raise ValueError("model distribution was unavailable at the decision cutoff")
        estimates = {name: score_distribution(model, price, quantity, fees, exit_debit, payout_cap, tail, wealth=wealth)
                     for name, model in models.items()}
        least_net = min(model["expected_net_dollars"] for model in estimates.values())
        least_fill = min(model["fill_probability"] for model in estimates.values())
        worst_tail = max((model["tail_loss_dollars_given_fill"] for model in estimates.values()
                          if model["tail_loss_dollars_given_fill"] is not None), default=None)
        gross_risk = quantity * (100 * price + fees + exit_debit)
        reasons = [invalid_book] if invalid_book else []
        if raw["quote"]["basis"] == "synthetic_legs":
            reasons.append("no_package_price")
        if price >= ask and quantity > depth:
            reasons.append("insufficient_ask_depth")
        if gross_risk > max_loss:
            reasons.append("loss_budget")
        if least_net < min_net:
            reasons.append("expected_net_below_minimum")
        if least_fill < min_fill:
            reasons.append("fill_probability_below_minimum")
        if worst_tail is None or worst_tail > max_tail:
            reasons.append("tail_limit")
        if not reasons:
            accepted.append((least_net, price))
        reports.append({
            "limit": str(price), "status": "passes_model_constraints" if not reasons else "rejected",
            "reasons": reasons, "modeled_loss_bound_dollars": money(gross_risk),
            "worst_model_expected_net_dollars": money(least_net),
            "minimum_model_fill_probability": str(least_fill),
            "maximum_model_tail_loss_given_fill_dollars": money(worst_tail) if worst_tail is not None else None,
            "models": {name: {key: str(value) if value is not None else None for key, value in estimate.items()}
                       for name, estimate in estimates.items()},
        })
    if not reports:
        raise ValueError("provide at least one limit price")
    return {"basis": "research_with_supplied_distributions", "input_sha256": digest(raw), "as_of": cutoff,
            "long_contract": long_code, "short_contract": short_code, "quantity": quantity,
            "highest_passing_tested_limit": str(max(price for _, price in accepted)) if accepted else None,
            "tested_limit_with_best_worst_model_expectancy": str(max(accepted, key=lambda x: (x[0], -x[1]))[1])
            if accepted else None,
            "prices": reports,
            "limitations": ["Fill and exit distributions are supplied assumptions. The observed book does not establish fills.",
                            "All modeled fills pay their tested limit. Fees apply per filled contract and unfilled paths cost zero.",
                            "The loss bound assumes standard contracts and the specified maximum exit debit. "
                            "The engine does not price assignment, operational losses or portfolio exposure."]}
