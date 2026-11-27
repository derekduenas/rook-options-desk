"""Local option sensitivities and exact attribution within the desk pricing model."""

from collections import defaultdict
from decimal import Decimal
from itertools import permutations, product
from math import exp, isfinite, log, pi, sqrt

from market_risk import cdf, model_float, option_value, years_between
from optionsdesk import decimal, money, seconds_between, utc


def option_greeks(spot, strike, years, rate, yield_rate, iv, right):
    if min(spot, years, iv) <= 0:
        return None
    root_time = sqrt(years)
    scale = iv * root_time
    if not scale:
        return None
    d1 = (log(spot) - log(strike) + (rate - yield_rate) * years) / scale + scale / 2
    d2 = d1 - scale
    if not all(map(isfinite, (d1, d2))):
        return None
    discount = exp(-yield_rate * years)
    density = exp(-d1 * d1 / 2) / sqrt(2 * pi) * discount
    delta = discount * (cdf(d1) if right == "C" else -cdf(-d1))
    gamma = density / spot / scale
    vega = spot * density * root_time
    mark = option_value(spot, strike, years, rate, yield_rate, iv, right)
    theta = rate * mark - (rate - yield_rate) * spot * delta - (iv * spot)**2 * gamma / 2
    d1_dyears = ((rate - yield_rate) / iv + iv / 2) / root_time - d1 / (2 * years)
    greeks = {
        "delta_shares": delta,
        "gamma_delta_per_dollar": gamma,
        "gamma_pnl_one_pct_move_dollars": gamma * (0.01 * spot)**2 / 2,
        "theta_dollars_per_calendar_day": theta / 365,
        "vega_dollars_per_iv_point": vega / 100,
        "vanna_delta_per_iv_point": -density * d2 / iv / 100,
        "volga_dollars_per_iv_point_squared": vega * d1 * d2 / iv / 10000,
        "charm_delta_per_calendar_day": (yield_rate * delta - density * d1_dyears) / 365,
        "speed_gamma_per_dollar": -gamma / spot * (1 + d1 / scale),
        "zomma_gamma_per_iv_point": gamma * (d1 * d2 - 1) / iv / 100,
        "color_gamma_per_calendar_day": gamma * (yield_rate + d1 * d1_dyears + 1 / (2 * years)) / 365,
    }
    return greeks if all(map(isfinite, greeks.values())) else None


def exact_attribution(value, start, end):
    """Average marginal repricing over all orders of spot, IV and time changes."""
    marks = {states: Decimal(str(value(*(end[i] if state else start[i] for i, state in enumerate(states)))))
             for states in product((0, 1), repeat=3)}
    contributions = [Decimal(0)] * 3
    for order in permutations(range(3)):
        flags = [0, 0, 0]
        for axis in order:
            before = marks[tuple(flags)]
            flags[axis] = 1
            contributions[axis] += marks[tuple(flags)] - before
    total = marks[1, 1, 1] - marks[0, 0, 0]
    # Assign any decimal-context remainder to time to retain exact additivity.
    spot, vol = (contributions[i] / 6 for i in range(2))
    return {"spot": spot, "implied_volatility": vol, "time": total - spot - vol, "mark_change": total}


def risk_details(underlyings, positions, scenarios, as_of):
    position_rows, symbol_totals, unavailable = [], defaultdict(lambda: defaultdict(float)), set()
    attributed = {row["name"]: {"spot": Decimal(0), "implied_volatility": Decimal(0),
                                "time": Decimal(0), "mark_change": Decimal(0), "unattributed_positions": []}
                  for row in scenarios}
    for index, position in enumerate(positions, 1):
        symbol, qty = position["symbol"], position["quantity"]
        context = underlyings[symbol]
        spot = model_float(context["spot"])
        if position["kind"] == "stock":
            greeks = {"delta_shares": qty}
            for scenario in scenarios:
                change = qty * (scenario["spots"][symbol] - context["spot"])
                attributed[scenario["name"]]["spot"] += change
                attributed[scenario["name"]]["mark_change"] += change
        else:
            strike, iv = model_float(position["strike"]), model_float(position["iv"])
            expiry, right = position["expiry_at"], position["right"]
            years = years_between(expiry, as_of)
            rate, carry = context["rate"], context["dividend_yield"]
            greeks = option_greeks(spot, strike, years, rate, carry, iv, right)
            qty *= 100
            if greeks is not None:
                greeks = {key: model_float(value * qty) for key, value in greeks.items()}

            for scenario in scenarios:
                totals = attributed[scenario["name"]]
                if scenario["at"] >= expiry:
                    totals["unattributed_positions"].append(index)
                    continue
                new_iv = position["iv"] + scenario["iv_shifts"].get(symbol, 0) + scenario["leg_iv_shifts"].get(index, 0)
                end = (model_float(scenario["spots"][symbol]), model_float(new_iv),
                       years_between(expiry, scenario["at"]))
                changes = exact_attribution(lambda s, vol, t: option_value(s, strike, t, rate, carry, vol, right),
                                            (spot, iv, years), end)
                for key, value in changes.items():
                    totals[key] += qty * value
        if greeks is None:
            unavailable.add(symbol)
        else:
            for key, value in greeks.items():
                symbol_totals[symbol][key] += value
        position_rows.append({"position": index, "symbol": symbol, "greeks": greeks})
    for row in attributed.values():
        if row["unattributed_positions"]:
            row.update({key: None for key in ("spot", "implied_volatility", "time", "mark_change")})
        else:
            total, spot, vol = (Decimal(money(row[key])) for key in ("mark_change", "spot", "implied_volatility"))
            row.update(mark_change=money(total), spot=money(spot), implied_volatility=money(vol),
                       time=money(total - spot - vol))
    return {"positions": position_rows,
            "by_underlying": {symbol: None if symbol in unavailable else dict(symbol_totals[symbol])
                              for symbol in underlyings},
            "attribution_before_exit_cost_dollars": attributed}


def event_variance(surface, spec):
    """Compare three matched-moneyness total-variance nodes around one event."""
    cutoff, event = surface["as_of"], utc(spec["event_at"])
    by_contract = {row["instrument"]: row for row in surface["nodes"]}
    ids = [spec[key] for key in ("before", "after", "farther")]
    if len(set(ids)) != 3 or any(code not in by_contract for code in ids):
        raise ValueError("event variance needs three distinct contracts in the surface")
    before, after, farther = rows = [by_contract[code] for code in ids]
    if not cutoff < before["expiry_at"] < event < after["expiry_at"] < farther["expiry_at"]:
        raise ValueError("expiration times must bracket the single event with two expiries after it")
    report = {"event_at": event, "contracts": ids, "status": "unavailable", "event_variance": None,
              "assumption": "constant background variance across the three expiries and one event in the bracket"}
    if len({row["right"] for row in rows}) != 1:
        return {**report, "reason": "different_option_rights"}
    if any(row["model_status"] != "computed" for row in rows):
        return {**report, "reason": "ineligible_surface_nodes"}
    times = [node["quote"]["observed_at"] for node in rows]
    if seconds_between(max(times), min(times)) > decimal(spec["max_quote_skew_seconds"]):
        return {**report, "reason": "unsynchronized_expiries"}
    moneyness = [node["forward_log_moneyness"] for node in rows]
    if max(moneyness) - min(moneyness) > model_float(decimal(spec["max_log_moneyness_gap"])):
        return {**report, "reason": "moneyness_mismatch"}
    t0, t1, t2 = [node["years_to_expiry"] for node in rows]
    if not 0 < t0 < t1 < t2:
        return {**report, "reason": "variance_clock_resolution"}
    ratio = (t1 - t0) / (t2 - t1)
    w0, w1, w2 = [{side: vol**2 * t for side, vol in node["iv"].items()}
                  for node, t in zip(rows, (t0, t1, t2))]
    estimate = w1["mid"] - w0["mid"] - ratio * (w2["mid"] - w1["mid"])
    band = ((1 + ratio) * w1["bid"] - w0["ask"] - ratio * w2["ask"],
            (1 + ratio) * w1["ask"] - w0["bid"] - ratio * w2["bid"])
    if not w0["mid"] <= w1["mid"] <= w2["mid"]:
        return {**report, "reason": "negative_forward_variance"}
    return {**report, "status": "estimated" if band[0] > 0 else "inconclusive_at_quote_bounds",
            "event_variance": estimate, "event_variance_bid_ask_bounds": band,
            "implied_event_standard_deviation_pct": 100 * sqrt(estimate) if estimate >= 0 else None}
