"""Compare fixed hedge policies on a timestamped, supplied quote path."""

from decimal import Decimal, ROUND_HALF_EVEN

from advanced_risk import option_greeks
from market_risk import model_float, option_value, payoff, signed, years_between
from optionsdesk import decimal, digest, money, seconds_between, utc


def whole(value, name, *, signed_allowed=False):
    if type(value) is not int or not signed_allowed and value < 0:
        raise ValueError(f"{name} requires a whole number")
    return value


def quote(row, max_age):
    observed, received = utc(row["observed_at"]), utc(row["received_at"])
    bid, ask = decimal(row["bid"]), decimal(row["ask"])
    if not 0 < bid <= ask or not observed <= received or seconds_between(received, observed) > max_age:
        raise ValueError("hedge quotes require a fresh positive uncrossed book")
    return {"observed_at": observed, "received_at": received, "bid": bid, "ask": ask,
            "bid_size": whole(row["bid_size"], "bid size"), "ask_size": whole(row["ask_size"], "ask size"),
            "mid": (bid + ask) / 2}


def position_target(delta, holdings, band, cap):
    neutral = int((-delta).to_integral_value(rounding=ROUND_HALF_EVEN))
    lower, upper = neutral - band, neutral + band
    desired = min(max(holdings, lower), upper)
    return max(-cap, min(desired, cap))


def crossing(book, request, fee):
    buy = request > 0
    price = book["ask"] if buy else book["bid"]
    side_size = book["ask_size"] if buy else book["bid_size"]
    filled = min(abs(request), side_size) * (1 if buy else -1)
    cost = abs(filled) * (abs(price - book["mid"]) + fee)
    return filled, -filled * price - abs(filled) * fee, cost


def simulate(name, band, terms, samples, terminal, settings):
    cash = -terms["premium"] * terms["units"] - terms["entry_fee"]
    holdings = turnover = hedge_fills = partials = unfilled = 0
    hedge_cost = peak = drawdown = Decimal(0)
    order = None
    history = []
    for row in samples:
        decision_at = row["received_at"]
        if order and row["observed_at"] > order["decision_at"] and (
            seconds_between(row["observed_at"], order["decision_at"]) >= settings["latency"]
        ):
            filled, cash_delta, cost = crossing(row, order["shares"], settings["fee"])
            holdings += filled
            cash += cash_delta
            hedge_cost += cost
            turnover += abs(filled)
            hedge_fills += bool(filled)
            partials += bool(filled and filled != order["shares"])
            unfilled += not filled
            history.append({**order, "execution_quote_at": row["observed_at"], "filled_shares": filled})
            order = None
        marked = cash + holdings * row["mid"] + row["option_mark_dollars"]
        peak = max(peak, marked)
        drawdown = max(drawdown, peak - marked)
        if band is not None and order is None:
            target = position_target(row["option_delta_shares"], holdings, band, settings["max_shares"])
            if target != holdings:
                order = {"decision_at": decision_at, "shares": target - holdings}

    terminal_filled, terminal_cash, cost = crossing(terminal, -holdings, settings["fee"])
    closed = holdings + terminal_filled == 0
    gross_payoff = terms["units"] * payoff(terminal["fixing"], terms["strike"], terms["right"])
    net = cash + terminal_cash + gross_payoff if closed else None
    drawdown = max(drawdown, peak - net) if net is not None else drawdown
    return {"policy": name, "band_shares": band, "status": "modeled" if closed else "unpriced_terminal_exit",
            "terminal_pnl_dollars": money(net) if net is not None else None,
            "modeled_hedge_friction_dollars": money(hedge_cost + cost), "shares_traded": turnover + abs(terminal_filled),
            "hedge_fills_before_settlement": hedge_fills, "partial_hedges": partials, "unfilled_hedges": unfilled,
            "pending_at_settlement": int(order is not None), "unclosed_hedge_shares": holdings + terminal_filled,
            "max_sampled_drawdown_dollars": money(drawdown), "attempts": history}


def hedge_replay(raw):
    option = raw["option"]
    expiry = utc(option["expiry_at"])
    quantity = whole(option["quantity"], "option quantity", signed_allowed=True)
    right, strike, premium = option["right"], decimal(option["strike"]), decimal(option["premium"])
    if (option["exercise_style"] != "European" or option["settlement"] != "cash"
        or right not in {"C", "P"} or not quantity or not strike or not premium):
        raise ValueError("supply a nonzero European cash-settled option position with positive strike and premium")
    latency, age = decimal(raw["latency_seconds"]), decimal(raw["max_quote_age_seconds"])
    fees, cap = decimal(raw["hedge_fee_per_share"]), whole(raw["max_abs_stock_shares"], "share cap")
    band = whole(raw["band_shares"], "hedge band")
    if not band or not cap:
        raise ValueError("hedge band and share cap must be positive")
    rate, carry = (model_float(signed(option[key])) for key in ("rate", "dividend_yield"))
    samples = []
    for item in raw["quotes"]:
        row = quote(item, age)
        at = row["received_at"]
        if at >= expiry or samples and (samples[-1]["received_at"] >= at
                                       or samples[-1]["observed_at"] >= row["observed_at"]):
            raise ValueError("hedge quote timestamps must increase and precede expiry")
        if utc(item["iv_known_at"]) > at:
            raise ValueError("hedge IV must be known by the decision time")
        spot, vol = model_float(row["mid"]), model_float(decimal(item["iv"]))
        years = years_between(expiry, at)
        strike_model = model_float(strike)
        greeks = option_greeks(spot, strike_model, years, rate, carry, vol, right)
        if greeks is None:
            raise ValueError("supply a positive usable IV at each hedge decision")
        row["option_delta_shares"] = Decimal(str(greeks["delta_shares"])) * quantity * 100
        row["option_mark_dollars"] = Decimal(str(option_value(spot, strike_model, years, rate, carry, vol, right))) * quantity * 100
        samples.append(row)
    if len(samples) < 2:
        raise ValueError("at least two hedge quotes are required")
    terminal = quote(raw["settlement"], age)
    if terminal["observed_at"] < expiry or seconds_between(terminal["received_at"], expiry) > age:
        raise ValueError("terminal hedge book must be observed at or after the fixing and received within the age allowance")
    terminal["fixing"] = decimal(raw["settlement"]["fixing"])
    terms = {"right": right, "strike": strike, "premium": premium, "units": quantity * 100,
             "entry_fee": decimal(option["entry_fee_dollars"])}
    settings = {"fee": fees, "latency": latency, "max_shares": cap}
    return {"basis": "supplied_path_research_replay", "input_sha256": digest(raw), "quote_count": len(samples),
            "policies": [simulate(name, width, terms, samples, terminal, settings)
                         for name, width in (("unhedged", None), ("delta_rebalance", 0), ("no_trade_band", band))],
            "limitations": [
                "Single European cash-settled option and stock hedge. The disclosed fixing determines option proceeds.",
                "Decisions use received quotes and may fill at a subsequent quote after modeled latency, limited by displayed size.",
                "Unfilled shares at the first eligible quote are canceled. Remaining stock uses terminal displayed liquidity.",
                "Stock borrow, financing, dividends on hedge shares, market impact and queue position are outside the model.",
                "Sampled drawdown excludes any unpriced terminal liquidation."]}
