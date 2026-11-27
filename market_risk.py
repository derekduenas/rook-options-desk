"""Quote diagnostics and deterministic scenario valuation. No order routing."""
from collections import defaultdict
from datetime import date, datetime, time, timezone
from decimal import Decimal
from itertools import pairwise
from math import erfc, exp, isfinite, log, sqrt

from catalyst import quote_status
from optionsdesk import NEW_YORK, contract, decimal, identifier, money, seconds_between, utc

SECONDS_PER_YEAR = 365 * 24 * 60 * 60
MODEL = "European Black–Scholes–Merton with continuous carry and ACT/365"


def signed(value):
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("expected a finite number")
    return number


def model_float(value):
    number = float(value)
    if not isfinite(number):
        raise ValueError("value is outside the pricing model's numeric range")
    return number


def choice(value, valid, field):
    if value not in valid:
        raise ValueError(f"invalid {field}: {value!r}")
    return value


def payoff(spot, strike, right):
    return max(spot - strike if right == "C" else strike - spot, 0)


def cdf(value):
    return .5 * erfc(-value / sqrt(2))


def option_value(spot, strike, years, rate, dividend_yield, vol, right):
    """Value per share in the European model, including the zero-volatility limit."""
    if years == 0:
        return payoff(spot, strike, right)
    try:
        stock_pv = spot * exp(-dividend_yield * years)
        strike_pv = strike * exp(-rate * years)
    except OverflowError:
        raise ValueError("discount factor is outside the pricing model's numeric range") from None
    scale = vol * sqrt(years)
    if not all(map(isfinite, (stock_pv, strike_pv, scale))):
        raise ValueError("inputs are outside the pricing model's numeric range")
    if not scale or not spot:
        return payoff(stock_pv, strike_pv, right)
    d1 = (log(spot) - log(strike) + (rate - dividend_yield) * years) / scale + scale / 2
    d2 = d1 - scale
    if right == "C":
        return max(stock_pv * cdf(d1) - strike_pv * cdf(d2), 0.0)
    return max(strike_pv * cdf(-d2) - stock_pv * cdf(-d1), 0.0)


def implied_vol(price, spot, strike, years, rate, dividend_yield, right):
    low_value = option_value(spot, strike, years, rate, dividend_yield, 0, right)
    tolerance = 1e-10 * max(1, price)
    if price < low_value - tolerance:
        return None
    if price <= low_value + tolerance:
        return 0.0
    low, high = 0.0, 16.0
    if price >= option_value(spot, strike, years, rate, dividend_yield, high, right):
        return None
    for _ in range(64):
        mid = (low + high) / 2
        if option_value(spot, strike, years, rate, dividend_yield, mid, right) < price:
            low = mid
        else:
            high = mid
    return (low + high) / 2


def years_between(later, earlier):
    return model_float(seconds_between(later, earlier)) / SECONDS_PER_YEAR


def named_observation(journal, kind, instrument, cutoff, source, feed):
    return next((row for row in journal.observations(kind, instrument, cutoff)
                 if (row["source"], row["feed"]) == (source, feed)), None)


def default_close(day):
    return utc(datetime.combine(day, time(16), NEW_YORK).isoformat())


def surface_node(spec, quote, underlying, underlying_status, context, as_of, skew):
    expiry_at = spec["expiry_at"]
    years = years_between(expiry_at, as_of)
    status = quote_status(context, [quote], version=2)
    row = {**spec, "quote": quote, "quote_status": status, "years_to_expiry": years,
           "model_status": status, "iv": None, "prices": None, "forward_log_moneyness": None}
    if status != "fresh":
        return row
    if underlying_status != "fresh":
        return {**row, "model_status": underlying_status + "_underlying"}
    if abs(seconds_between(quote["observed_at"], underlying["observed_at"])) > skew:
        return {**row, "model_status": "unsynchronized_underlying"}
    spot, strike = model_float(underlying["price"]), model_float(spec["strike"])
    rate, dividend_yield = context["rate"], context["dividend_yield"]
    log_moneyness = log(strike) - log(spot) - (rate - dividend_yield) * years
    bid, ask = decimal(quote["bid"]), decimal(quote["ask"])
    prices = {"bid": bid, "mid": (bid + ask) / 2, "ask": ask}
    ivs = {side: implied_vol(model_float(price), spot, strike, years, rate, dividend_yield, spec["right"])
           for side, price in prices.items()}
    return {**row, "model_status": "computed" if all(vol is not None for vol in ivs.values()) else "model_bounds",
            "iv": ivs, "prices": {side: str(price) for side, price in prices.items()},
            "forward_log_moneyness": log_moneyness}


def timestamp_span(rows):
    times = [row["quote"]["observed_at"] for row in rows]
    return seconds_between(max(times), min(times))


def strike_diagnostics(slices, skew):
    findings, checks, skipped = [], 0, 0
    for (_, right), rows in slices.items():
        for low, high in pairwise(rows):
            if timestamp_span([low, high]) > skew:
                skipped += 1
                continue
            checks += 1
            richer, cheaper = (low, high) if right == "C" else (high, low)
            if Decimal(richer["prices"]["mid"]) < Decimal(cheaper["prices"]["mid"]):
                findings.append({"kind": "strike_monotonicity", "instruments": [low["instrument"], high["instrument"]],
                                 "quote_band_violation": Decimal(richer["prices"]["ask"]) < Decimal(cheaper["prices"]["bid"])})
        for low, mid, high in zip(rows, rows[1:], rows[2:]):
            if timestamp_span([low, mid, high]) > skew:
                skipped += 1
                continue
            checks += 1
            left = Decimal(mid["strike"]) - Decimal(low["strike"])
            right_gap = Decimal(high["strike"]) - Decimal(mid["strike"])
            span = left + right_gap
            midpoint_violation = Decimal(mid["prices"]["mid"]) * span > (
                Decimal(low["prices"]["mid"]) * right_gap + Decimal(high["prices"]["mid"]) * left)
            if midpoint_violation:
                findings.append({"kind": "strike_convexity", "instruments": [r["instrument"] for r in (low, mid, high)],
                                 "quote_band_violation": Decimal(mid["prices"]["bid"]) * span > (
                                     Decimal(low["prices"]["ask"]) * right_gap + Decimal(high["prices"]["ask"]) * left)})
    return {"checks": checks, "skipped_for_skew": skipped, "findings": findings}


def variance_at(rows, moneyness, side):
    for low, high in pairwise(rows):
        left, right = low["forward_log_moneyness"], high["forward_log_moneyness"]
        if left <= moneyness <= right:
            a = low["iv"][side] ** 2 * low["years_to_expiry"]
            b = high["iv"][side] ** 2 * high["years_to_expiry"]
            return a + (b - a) * (moneyness - left) / (right - left)
    raise ValueError("calendar diagnostic must be inside observed strike ranges")


def calendar_diagnostics(slices, skew, style):
    grouped = defaultdict(list)
    for (expiry_at, right), rows in sorted(slices.items()):
        valid = [row for row in rows if row["model_status"] == "computed"]
        if len(valid) >= 2:
            grouped[right].append((expiry_at, valid))
    findings, checks, skipped = [], 0, 0
    for right, maturities in grouped.items():
        for (near_at, near), (far_at, far) in pairwise(maturities):
            start = max(near[0]["forward_log_moneyness"], far[0]["forward_log_moneyness"])
            end = min(near[-1]["forward_log_moneyness"], far[-1]["forward_log_moneyness"])
            if start > end:
                continue
            if timestamp_span(near + far) > skew:
                skipped += 1
                continue
            grid = sorted({r["forward_log_moneyness"] for r in near + far
                           if start <= r["forward_log_moneyness"] <= end})
            for point in grid:
                checks += 1
                near_variance, far_variance = (variance_at(rows, point, "mid") for rows in (near, far))
                if near_variance > far_variance + 1e-10:
                    findings.append({"kind": "calendar_total_variance", "right": right, "near_expiry_at": near_at,
                                     "far_expiry_at": far_at, "forward_log_moneyness": point,
                                     "near_total_variance": near_variance, "far_total_variance": far_variance,
                                     "quote_band_violation": variance_at(near, point, "bid") >
                                     variance_at(far, point, "ask") + 1e-10})
    return {"basis": "European model diagnostic" if style == "European" else "American quotes in European approximation",
            "checks": checks, "skipped_for_skew": skipped, "findings": findings}


def surface_report(journal, raw):
    as_of = utc(raw["as_of"])
    if as_of > utc(datetime.now(timezone.utc).isoformat()):
        raise ValueError("surface cutoff cannot be in the future")
    symbol = identifier(raw["symbol"]).upper()
    max_age = decimal(raw["max_quote_age_seconds"])
    skew = decimal(raw["max_quote_skew_seconds"])
    quote_source, quote_feed, underlying_source, underlying_feed = (
        identifier(raw[key]) for key in ("quote_source", "quote_feed", "underlying_source", "underlying_feed"))
    rate = model_float(signed(raw["rate"]))
    dividend_yield = model_float(signed(raw["dividend_yield"]))
    style = choice(raw.get("exercise_style", "American"), {"American", "European"}, "exercise style")
    context = {"as_of": as_of, "quote_source": quote_source, "quote_feed": quote_feed,
               "max_quote_age_seconds": max_age, "max_leg_skew_seconds": skew,
               "rate": rate, "dividend_yield": dividend_yield}
    specs = {}
    closing_times = {}
    for item in raw["contracts"]:
        day = date.fromisoformat(item["expiry"])
        expiry_at = utc(item["expiry_at"]) if "expiry_at" in item else default_close(day)
        if as_of >= expiry_at or datetime.fromisoformat(expiry_at).astimezone(NEW_YORK).date() != day:
            raise ValueError("expiration timestamp must be after the cutoff on the listed New York expiry date")
        if closing_times.setdefault(day, expiry_at) != expiry_at:
            raise ValueError("one symbol requires a consistent expiration timestamp per listed date")
        right = choice(item["right"], {"C", "P"}, "right")
        strike = decimal(item["strike"])
        instrument = contract(symbol, day.isoformat(), strike, right)
        if instrument in specs:
            raise ValueError(f"duplicate surface contract: {instrument}")
        specs[instrument] = {"instrument": instrument, "expiry": day.isoformat(), "expiry_at": expiry_at,
                             "right": right, "strike": str(strike)}
    if not specs:
        raise ValueError("list at least one surface contract")

    def observations():
        return (named_observation(journal, "underlying_trade", symbol, as_of, underlying_source, underlying_feed),
                {instrument: named_observation(journal, "option_quote", instrument, as_of, quote_source, quote_feed)
                 for instrument in specs})

    if journal.db.in_transaction:
        underlying, quotes = observations()
    else:
        with journal.db:
            journal.db.execute("BEGIN")
            underlying, quotes = observations()
    underlying_status = ("missing" if underlying is None else "stale"
                         if seconds_between(as_of, underlying["observed_at"]) > max_age else "fresh")
    nodes = [surface_node(spec, quotes[spec["instrument"]], underlying, underlying_status, context, as_of, skew)
             for spec in sorted(specs.values(), key=lambda s: (
                              s["expiry_at"], s["right"], Decimal(s["strike"])))]
    slices = defaultdict(list)
    for row in nodes:
        if row["prices"] is not None:
            slices[row["expiry_at"], row["right"]].append(row)
    report = {"symbol": symbol, "as_of": as_of, "model": MODEL, "exercise_style": style,
            "rate": rate, "dividend_yield": dividend_yield, "underlying": underlying,
            "quote_source": quote_source, "quote_feed": quote_feed,
            "underlying_source": underlying_source, "underlying_feed": underlying_feed,
            "underlying_status": underlying_status, "nodes": nodes, "diagnostics": {
                "strike": strike_diagnostics(slices, skew), "calendar": calendar_diagnostics(slices, skew, style)},
            "notes": ["Underlyings use the observed last trade. Bid/mid/ask IVs are per quote, annualized fractions.",
                      "Calendar checks use linear total-variance interpolation on overlapping forward log-moneyness.",
                      "Sparse and asynchronous quotes limit diagnostic coverage. A finding is a model diagnostic.",
                      "Default expiration cutoff is 4:00 p.m. New York time. Supply explicit cutoffs for other terms.",
                      "The approximation omits discrete dividends, stock borrow effects and early exercise."]}
    if raw.get("event_variance") is not None:
        from advanced_risk import event_variance
        report["event_variance"] = event_variance(report, raw["event_variance"])
    return report


def unique_symbols(items):
    normalized = {identifier(symbol).upper(): value for symbol, value in items.items()}
    if len(normalized) != len(items):
        raise ValueError("duplicate underlying symbol")
    return normalized


def position_spec(raw, underlyings, as_of):
    kind = choice(raw["kind"], {"stock", "option"}, "position kind")
    symbol = identifier(raw["symbol"]).upper()
    if symbol not in underlyings:
        raise ValueError(f"position has no underlying inputs: {symbol}")
    quantity = signed(raw["quantity"])
    if not quantity or quantity % 1:
        raise ValueError("position quantity must be a nonzero whole number")
    entry = decimal(raw["entry_price"])
    if not entry:
        raise ValueError("position entry price must be positive")
    row = {"kind": kind, "symbol": symbol, "quantity": int(quantity), "entry_price": entry,
           **{field: decimal(raw.get(field, 0)) for field in (
               "entry_fee_dollars", "exit_fee_dollars", "exit_slippage_per_unit")}}
    quote_fields = {key: decimal(raw[key]) for key in ("bid", "ask", "bid_size", "ask_size") if key in raw}
    if ("bid" in quote_fields) != ("ask" in quote_fields):
        raise ValueError("portfolio quote sensitivity needs both price sides")
    if "bid" in quote_fields and (quote_fields["bid"] > quote_fields["ask"] or not quote_fields["ask"]):
        raise ValueError("portfolio quote sensitivity requires an uncrossed ask above zero")
    if any(quote_fields.get(key, Decimal(0)) % 1 for key in ("bid_size", "ask_size")):
        raise ValueError("displayed quote sizes must be whole units")
    row.update(quote_fields)
    if kind == "option":
        expiry_at = utc(raw["expiry_at"])
        strike = decimal(raw["strike"])
        if expiry_at <= as_of or not strike:
            raise ValueError("option expiration must be after the valuation cutoff and strike must be positive")
        style = choice(raw.get("exercise_style", "American"), {"American", "European"}, "exercise style")
        row.update(expiry_at=expiry_at, strike=strike, iv=decimal(raw["iv"]), exercise_style=style,
                   right=choice(raw["right"], {"C", "P"}, "right"),
                   settlement=choice(raw.get("settlement", "physical"), {"physical", "cash"}, "settlement"))
    return row


def scenario_spec(raw, underlyings, as_of, positions=()):
    at = utc(raw["at"])
    if at < as_of:
        raise ValueError("scenario timestamp cannot precede the portfolio cutoff")
    spot_inputs = unique_symbols(raw["spots"])
    shift_inputs = unique_symbols(raw.get("iv_shifts", {}))
    option_indices = {str(index) for index, position in enumerate(positions, 1) if position["kind"] == "option"}
    leg_shifts = raw.get("iv_shifts_by_position", {})
    if leg_shifts.keys() - option_indices:
        raise ValueError("position IV shifts must name one-based option positions")
    expiry_inputs = unique_symbols(raw.get("expiry_spots", {}))
    if (spot_inputs.keys() | shift_inputs.keys() | expiry_inputs.keys()) - underlyings.keys():
        raise ValueError("scenario names an unknown underlying")
    expiry_spots = {}
    for symbol, fixings in expiry_inputs.items():
        canonical_fixings = {utc(expiry): decimal(spot) for expiry, spot in fixings.items()}
        if len(canonical_fixings) != len(fixings) or any(not as_of < cutoff <= at for cutoff in canonical_fixings):
            raise ValueError("expiry fixings must be distinct and lie within the scenario horizon")
        expiry_spots[symbol] = canonical_fixings
    return {"name": identifier(raw["name"]), "at": at,
            "spots": {symbol: decimal(spot_inputs.get(symbol, values["spot"])) for symbol, values in underlyings.items()},
            "iv_shifts": {symbol: signed(shift) for symbol, shift in shift_inputs.items()}, "expiry_spots": expiry_spots,
            "leg_iv_shifts": {int(index): signed(shift) for index, shift in leg_shifts.items()}}


def assignment_stress(p, spot, expired):
    if (p["quantity"] >= 0 or p["settlement"] != "physical" or
            (not expired and p["exercise_style"] != "American")):
        return None
    is_call = p["right"] == "C"
    shares = abs(p["quantity"]) * 100 * (-1 if is_call else 1)
    cash = -shares * p["strike"]
    return {"timing": "expiration" if expired else "early_assignment",
            "in_the_money": payoff(spot, p["strike"], p["right"]) > 0,
            "shares_if_assigned": shares, "cash_flow_if_assigned_dollars": money(cash)}


def position_valuation(p, basis, scenario, position_id, *, apply_exit_friction):
    symbol, at = p["symbol"], scenario["at"]
    spot = scenario["spots"][symbol]
    quantity, multiplier = p["quantity"], 1
    mark, method, assignment, volatility = spot, "scenario_spot", None, None
    if p["kind"] == "option":
        multiplier = 100
        expiry_at = p["expiry_at"]
        expired = at >= expiry_at
        if expired:
            fixing = scenario["expiry_spots"].get(symbol, {}).get(expiry_at)
            if fixing is None and at > expiry_at:
                raise ValueError(f"scenario {scenario['name']} needs the {symbol} fixing at {expiry_at}")
            if fixing is not None:
                spot = fixing
            mark, method = payoff(spot, p["strike"], p["right"]), "expiration_intrinsic"
        else:
            underlier = basis[symbol]
            volatility = p["iv"] + scenario["iv_shifts"].get(symbol, 0) + scenario.get("leg_iv_shifts", {}).get(position_id, 0)
            if volatility < 0:
                raise ValueError(f"scenario {scenario['name']} leaves {symbol} with negative volatility")
            mark = Decimal(str(option_value(model_float(spot), model_float(p["strike"]), years_between(expiry_at, at),
                                            underlier["rate"], underlier["dividend_yield"], model_float(volatility), p["right"])))
            method = "european_model"
        assignment = assignment_stress(p, spot, expired)
    units = quantity * multiplier
    value = mark * units
    gross = value - p["entry_price"] * units
    slippage = 0 if method == "expiration_intrinsic" else p["exit_slippage_per_unit"] * abs(units)
    exit_cost = p["exit_fee_dollars"] + slippage if apply_exit_friction else Decimal(0)
    net = gross - p["entry_fee_dollars"] - exit_cost
    return {"position": position_id, "symbol": symbol, "kind": p["kind"], "quantity": quantity,
            "spot_used": str(spot), "method": method, "mark_per_unit": str(mark), "assignment": assignment,
            "iv_used": str(volatility) if volatility is not None else None,
            "value_dollars": money(value), "gross_pnl_dollars": money(gross), "pnl_dollars": money(net),
            "exit_friction_dollars": money(exit_cost)}, (value, gross, net, exit_cost)


def valuation(underlyings, positions, scenario, *, apply_exit_friction):
    legs = [position_valuation(position, underlyings, scenario, index, apply_exit_friction=apply_exit_friction)
            for index, position in enumerate(positions, 1)]
    value, gross, net, friction = (sum(items) for items in zip(*(totals for _, totals in legs)))
    assignment = defaultdict(lambda: {"shares_to_deliver": 0, "shares_to_receive": 0,
                                     "cash_to_pay": Decimal(0), "cash_to_receive": Decimal(0)})
    for leg, _ in legs:
        exposure = leg["assignment"]
        if exposure and exposure["in_the_money"]:
            row = assignment[leg["symbol"]]
            shares = exposure["shares_if_assigned"]
            source_position = positions[leg["position"] - 1]
            cash = -shares * source_position["strike"]
            row["shares_to_deliver"] += max(-shares, 0)
            row["shares_to_receive"] += max(shares, 0)
            row["cash_to_pay"] += max(-cash, 0)
            row["cash_to_receive"] += max(cash, 0)
    return {"name": scenario["name"], "at": scenario["at"], "positions": [leg for leg, _ in legs],
            "value_dollars": money(value), "gross_pnl_dollars": money(gross), "pnl_dollars": money(net),
            "exit_friction_dollars": money(friction),
            "itm_assignment_exposure": {symbol: {"shares_to_deliver": row["shares_to_deliver"],
                                                  "shares_to_receive": row["shares_to_receive"],
                                                  "cash_to_pay_dollars": money(row["cash_to_pay"]),
                                                  "cash_to_receive_dollars": money(row["cash_to_receive"])}
                                        for symbol, row in assignment.items()}}, net


def execution_sensitivity(positions):
    instruments = defaultdict(list)
    for position in positions:
        key = tuple(position.get(field) for field in (
            "kind", "symbol", "expiry_at", "strike", "right", "exercise_style", "settlement"))
        instruments[key].append(position)
    mid_debit = cross_debit = cross_credit = Decimal(0)
    fully_sized = known_size = bid_positive = True
    for (kind, *_), lots in instruments.items():
        quantity = sum(lot["quantity"] for lot in lots)
        if not quantity:
            continue
        quotes = {tuple(lot.get(field) for field in ("bid", "ask", "bid_size", "ask_size"))
                  for lot in lots if "bid" in lot}
        if len(quotes) != 1:
            return {"status": "conflicting_quotes" if quotes else "missing_quotes"}
        (bid, ask, bid_size, ask_size), = quotes
        units = (100 if kind == "option" else 1) * quantity
        mid_debit += (bid + ask) / 2 * units
        cross_debit += (ask if quantity > 0 else bid) * units
        cross_credit += (bid if quantity > 0 else ask) * units
        sizes = (bid_size, ask_size)
        if not bid:
            bid_positive = False
        if any(size is None for size in sizes):
            known_size = False
        if any(size is not None and size < abs(quantity) for size in sizes):
            fully_sized = False
    return {"status": "no_executable_bid" if not bid_positive else
            "insufficient_displayed_size" if not fully_sized else
            "unknown_displayed_size" if not known_size else "synthetic_quoted_sides",
            "entry_net_debit_mid_dollars": money(mid_debit), "entry_net_debit_cross_dollars": money(cross_debit),
            "exit_net_credit_cross_dollars": money(cross_credit),
            "round_trip_cross_spread_cost_dollars": money(cross_debit - cross_credit)}


def check_limits(limits, scenarios, current_net, execution):
    if not isinstance(limits, dict):
        raise ValueError("risk_limits must be an object")
    losses = [max(0, current_net - net) for _, net in scenarios]
    funding = [sum((Decimal(row["cash_to_pay_dollars"]) for row in scenario["itm_assignment_exposure"].values()),
                   Decimal(0)) for scenario, _ in scenarios]
    metrics = {"max_loss_from_mark_dollars": max(losses), "max_gross_assignment_cash_dollars": max(funding),
               "max_synthetic_round_trip_cost_dollars": None}
    if execution["status"] == "synthetic_quoted_sides":
        metrics["max_synthetic_round_trip_cost_dollars"] = Decimal(execution["round_trip_cross_spread_cost_dollars"])
    checks = []
    for key, bound in limits.items():
        if key not in metrics:
            raise ValueError(f"unknown portfolio risk limit {key!r}")
        limit, metric = decimal(bound), metrics[key]
        result = "unknown" if metric is None else "breach" if metric > limit else "within_limit"
        checks.append({"limit": key, "assumed_limit_dollars": money(limit), "value_dollars": money(metric) if metric is not None else None,
                       "status": result})
    statuses = {check["status"] for check in checks}
    status = "not_configured" if not checks else "breach" if "breach" in statuses else (
        "unknown" if "unknown" in statuses else "within_limits")
    return {"status": status, "checks": checks}


def portfolio_report(raw):
    as_of = utc(raw["as_of"])
    underlyings = {symbol: {"spot": decimal(row["spot"]), "rate": model_float(signed(row["rate"])),
                            "dividend_yield": model_float(signed(row["dividend_yield"]))}
                   for symbol, row in unique_symbols(raw["underlyings"]).items()}
    positions = [position_spec(row, underlyings, as_of) for row in raw["positions"]]
    scenarios = [scenario_spec(row, underlyings, as_of, positions) for row in raw["scenarios"]]
    if not positions or not scenarios or len({row["name"] for row in scenarios}) != len(scenarios):
        raise ValueError("portfolio needs positions and uniquely named scenarios")
    current, current_net = valuation(underlyings, positions, {
        "name": "current", "at": as_of, "spots": {symbol: row["spot"] for symbol, row in underlyings.items()},
        "iv_shifts": {}, "leg_iv_shifts": {}, "expiry_spots": {},
    }, apply_exit_friction=False)
    results = [valuation(underlyings, positions, row, apply_exit_friction=True) for row in scenarios]
    for row, net in results:
        row["change_from_current_mark_dollars"] = money(net - current_net)
    worst, _ = min(results, key=lambda pair: pair[1])
    scenario_rows = [row for row, _ in results]
    execution = execution_sensitivity(positions)
    report = {"as_of": as_of, "model": MODEL, "current": current, "scenarios": scenario_rows,
            "worst_scenario": {"name": worst["name"], "pnl_dollars": worst["pnl_dollars"]},
            "underlyings": {symbol: {**row, "spot": str(row["spot"])} for symbol, row in underlyings.items()},
            "position_specs": [{key: str(value) if isinstance(value, Decimal) else value
                                for key, value in row.items()} for row in positions],
            "execution_sensitivity": execution,
            "risk_limits": check_limits(raw.get("risk_limits", {}), results, current_net, execution),
            "notes": ["Scenario inputs describe hypothetical positions and marks. Dollar P&L is measured from entry.",
                      "Options assume standard 100-share deliverables. Interim marks use European exercise with continuous carry.",
                      "After expiration, the declared fixing crystallizes intrinsic P&L with no reinvestment or stock carry.",
                      "Net scenario P&L includes entry fees and configured exit friction. Expired options use configured exit fees only.",
                      "Assignment totals assume every in-the-money physical short is assigned. Gross obligations exclude offsets, timing, borrow and margin.",
                      "Assignment can occur before expiration for American contracts. Exact exercise and closing behavior depend on the holder and broker.",
                      "Execution sensitivity crosses supplied single-leg quotes at stated size, before fees. Package fills require actual complex-book evidence."]}
    if raw.get("advanced_greeks") is True:
        from advanced_risk import risk_details
        report["advanced_risk"] = risk_details(underlyings, positions, scenarios, as_of)
    return report
