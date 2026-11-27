"""Normalize the market-data fields used by the journal."""

def robinhood_options(payload, instruments):
    from optionsdesk import contract

    by_id = {item["id"]: item for item in instruments}
    for result in payload["data"]["results"] or []:
        if not result or not result.get("quote"):
            continue
        quote = result["quote"]
        option = by_id[quote["instrument_id"]]
        right = {"call": "C", "put": "P"}.get(option["type"])
        if right is None:
            raise ValueError(f"unknown Robinhood option type {option['type']!r}")
        root = option.get("root_symbol") or option["chain_symbol"]
        symbol = contract(root, option["expiration_date"], option["strike_price"], right)
        yield {
            "kind": "option_quote", "instrument": symbol, "observed_at": quote["updated_at"],
            "bid": quote["bid_price"], "ask": quote["ask_price"], "bid_size": quote["bid_size"],
            "ask_size": quote["ask_size"], "multiplier": option["trade_value_multiplier"],
            "adjusted": option.get("adjusted", False), "volume": quote["volume"],
            "displayed_open_interest_undated": quote["open_interest"],
            "implied_volatility": quote.get("implied_volatility"),
            "greeks": {key: quote.get(key) for key in ("delta", "gamma", "vega", "theta", "rho")},
            "prior_close": result.get("close"),
        }


def robinhood_stocks(payload):
    from optionsdesk import utc

    for result in payload["data"]["results"] or []:
        if not result or not result.get("quote"):
            continue
        quote = result["quote"]
        if not quote["has_traded"] or quote.get("state", "active") != "active":
            continue
        regular = quote.get("venue_last_trade_time")
        other = quote.get("venue_last_non_reg_trade_time")
        regular_price = quote.get("last_trade_price")
        other_price = quote.get("last_non_reg_trade_price")
        regular_at = utc(regular) if regular and regular_price is not None else None
        other_at = utc(other) if other and other_price is not None else None
        if regular_at is None and other_at is None:
            continue
        is_non_regular = other_at is not None and (regular_at is None or other_at > regular_at)
        yield {
            "kind": "underlying_trade", "instrument": quote["symbol"],
            "observed_at": other_at if is_non_regular else regular_at,
            "price": other_price if is_non_regular else regular_price,
            "session": "non_regular" if is_non_regular else "regular", "prior_close": result.get("close"),
        }


def alpaca_snapshots(payload, instruments):
    from optionsdesk import decimal

    # Older contract envelopes supplied size only. Explicit contract terms take precedence.
    by_symbol = {option["symbol"]: option for option in instruments}
    for symbol, result in payload["snapshots"].items():
        if not result.get("latest_quote"):
            continue
        quote = result["latest_quote"]
        reference = by_symbol[symbol]
        multiplier = reference.get("multiplier")
        if multiplier is None:
            multiplier = reference["size"]
        share_count = reference.get("size")
        adjusted = bool(reference.get("adjusted") or decimal(multiplier) != 100
                        or share_count is not None and decimal(share_count) != 100)
        deliverables = reference.get("deliverables")
        if deliverables is not None:
            adjusted = (adjusted or not isinstance(deliverables, list) or len(deliverables) != 1
                        or not isinstance(deliverables[0], dict)
                        or deliverables[0].get("type") != "equity"
                        or decimal(deliverables[0].get("amount", 0)) != 100
                        or deliverables[0].get("symbol") != reference.get("underlying_symbol")
                        or deliverables[0].get("delayed_settlement", False) is not False)
        yield {
            "kind": "option_quote", "instrument": symbol, "observed_at": quote["timestamp"],
            "bid": quote["bid_price"], "ask": quote["ask_price"], "bid_size": quote["bid_size"],
            "ask_size": quote["ask_size"], "multiplier": multiplier, "adjusted": adjusted,
            "implied_volatility": result.get("implied_volatility"), "greeks": result.get("greeks"),
            "latest_trade": result.get("latest_trade"),
        }


def alpaca_interest(payload, received_at):
    for option in payload["contracts"]:
        if option.get("open_interest") is None or not option.get("open_interest_date"):
            continue
        yield {
            "kind": "open_interest", "instrument": option["symbol"],
            "observed_at": received_at, "effective_date": option["open_interest_date"],
            "contracts": option["open_interest"], "close_price": option.get("close_price"),
            "close_price_date": option.get("close_price_date"),
        }


def normalize_mcp(envelope):
    provider, received_at, payload = (envelope[key] for key in ("provider", "received_at", "payload"))
    if provider == "robinhood_options":
        source, feed = "robinhood", "market_snapshot"
        rows = robinhood_options(payload, envelope["instruments"])
    elif provider == "robinhood_stocks":
        source, feed = "robinhood", "equity_snapshot"
        rows = robinhood_stocks(payload)
    elif provider == "alpaca_snapshots":
        source, feed = "alpaca", payload["request"]["feed"]
        rows = alpaca_snapshots(payload, envelope["instruments"])
    elif provider == "alpaca_contracts":
        source, feed = "alpaca", "contract_reference"
        rows = alpaca_interest(payload, received_at)
    else:
        raise ValueError(f"unknown MCP provider payload {provider!r}")
    return {"source": source, "feed": feed, "received_at": received_at, "observations": list(rows)}
