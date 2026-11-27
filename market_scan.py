"""Point-in-time market, news, and liquidity diagnostics shared by the desk."""

from math import log, sqrt
import re
from statistics import mean

from earnings import source_url
from optionsdesk import NEW_YORK, decimal, identifier, seconds_between, timestamp, utc


BREADTH = ("SPY", "QQQ", "IWM", "RSP", "SMH")
NEWS_TAGS = {
    "regulatory": r"antitrust|\bsec\b|regulat|lawsuit|export control|sanction|probe",
    "corporate": r"acqui|merger|buyback|buy-back|share offering|dividend|chief executive",
    "earnings": r"earnings|guidance|financial results|quarterly results",
    "macro": r"\bfed\b|inflation|trade deficit|tariff|treasur|\bgdp\b|payroll",
}


def usable(row, cutoff, age):
    observed, received = utc(row["observed_at"]), utc(row["received_at"])
    return observed <= received <= cutoff and seconds_between(cutoff, observed) <= age


def regime(raw, cutoff, comparative_cutoff):
    market = raw["session"]
    opening, closing = utc(market["open"]), utc(market["close"])
    if closing <= opening:
        raise ValueError("market close must follow its opening")
    date = timestamp(opening).astimezone(NEW_YORK).date().isoformat()
    baseline = market["previous_session"]
    if baseline >= date:
        raise ValueError("previous session must precede the current session")
    prices = [decimal(value) for day, value in sorted(raw["spy_closes"].items()) if day <= baseline]
    if any(not price for price in prices):
        raise ValueError("SPY historical closes must be positive")
    trend = volatility = None
    if baseline in raw["spy_closes"] and len(prices) >= 50:
        sma20, sma50 = mean(prices[-20:]), mean(prices[-50:])
        direction = "up" if prices[-1] > sma20 > sma50 else "down" if prices[-1] < sma20 < sma50 else "mixed"
        trend = {"label": direction, "last_close": str(prices[-1]), "sma20": str(sma20), "sma50": str(sma50)}
    if baseline in raw["spy_closes"] and len(prices) >= 147:
        squared = [(log(b) - log(a))**2 for a, b in zip(prices, prices[1:])]
        values = [sqrt(252 * mean(squared[i - 20:i])) for i in range(len(squared) - 126, len(squared) + 1)]
        score = sum(value <= values[-1] for value in values[:-1]) / 126
        volatility = {"realized_20_session_pct": 100 * values[-1], "historical_percentile": 100 * score,
                      "label": "elevated" if score >= .8 else "subdued" if score <= .2 else "typical"}
    changes = {}
    age = decimal(raw["max_quote_age_seconds"])
    if opening <= cutoff < closing:
        for symbol, row in raw["quotes"].items():
            if (row["previous_session"] == baseline and opening <= utc(row["observed_at"]) <= cutoff
                    and usable(row, cutoff, age)):
                close = decimal(row["previous_close"])
                last = decimal(row["last"])
                if close and last:
                    changes[symbol] = float(100 * (last / close - 1))
    basket = {ticker: changes[ticker] for ticker in BREADTH if ticker in changes}
    up = sum(change > 0 for change in basket.values())
    down = sum(change < 0 for change in basket.values())
    label = "unclassified"
    if trend and len(basket) == len(BREADTH):
        pct_up, pct_down = up / len(basket), down / len(basket)
        label = "broad_rally" if pct_up >= .8 else "broad_selloff" if pct_down >= .8 else "mixed_tape"
        if label == "broad_rally" and trend["label"] == "down":
            label = "countertrend_rally"
        if label == "broad_selloff" and trend["label"] == "up":
            label = "pullback_in_uptrend"
    return {"label": label, "session": "regular_open" if opening <= cutoff < closing else "outside_regular_hours",
            "close_at": closing, "trend": trend, "volatility": volatility, "breadth_up": up,
            "breadth_observed": len(basket), "breadth_expected": len(BREADTH), "stock_changes_pct": changes,
            "snapshot_stale": comparative_cutoff > cutoff and seconds_between(comparative_cutoff, cutoff) > age}


def news_leads(rows, cutoff):
    deduped = {}
    for raw in rows:
        published, received = utc(raw["published_at"]), utc(raw["received_at"])
        if not published <= received <= cutoff or seconds_between(cutoff, published) > 48 * 3600:
            continue
        title = identifier(raw["title"])
        key = re.sub(r"\W+", " ", title.casefold()).strip()
        evidence = raw.get("evidence", "headline_lead")
        if evidence not in {"headline_lead", "primary_release"}:
            raise ValueError("unknown news evidence type")
        item = {"title": title, "source": source_url(raw["source"]), "provider": identifier(raw["provider"]),
                "published_at": published, "first_seen_at": received, "symbols": set(raw["symbols"]),
                "evidence": evidence,
                "tags": [tag for tag, pattern in NEWS_TAGS.items() if re.search(pattern, title, re.I)]}
        previous = deduped.get(key)
        if previous:
            item["symbols"].update(previous["symbols"])
            if previous["evidence"] != evidence:
                if previous["evidence"] == "primary_release":
                    previous["symbols"].update(item["symbols"])
                    continue
                deduped[key] = item
                continue
            if previous["first_seen_at"] < received:
                previous["symbols"].update(item["symbols"])
                continue
        deduped[key] = item
    return [{**row, "symbols": sorted(row["symbols"])} for row in
            sorted(deduped.values(), key=lambda item: (item["published_at"], item["title"]), reverse=True)]


def liquidity(raw, cutoff):
    rows = []
    limits = raw["liquidity_limits"]
    spread_limit, size_floor = decimal(limits["max_spread_pct_of_mid"]), decimal(limits["min_size_per_side"])
    for quote in raw.get("option_quotes", []):
        bid, ask = decimal(quote["bid"]), decimal(quote["ask"])
        size = min(decimal(quote["bid_size"]), decimal(quote["ask_size"]))
        spread = 200 * (ask - bid) / (ask + bid) if 0 < bid <= ask else None
        reasons = []
        if not usable(quote, cutoff, decimal(raw["max_quote_age_seconds"])):
            reasons.append("stale_or_unavailable")
        if spread is None:
            reasons.append("no_valid_two_sided_book")
        elif spread > spread_limit:
            reasons.append("wide_spread")
        if size < size_floor:
            reasons.append("thin_displayed_size")
        if quote.get("broker_cutoff_at") and utc(quote["broker_cutoff_at"]) <= cutoff:
            reasons.append("broker_cutoff_reached")
        rows.append({"contract": identifier(quote["contract"]), "bid": str(bid), "ask": str(ask),
                     "available_per_side": str(size), "spread_pct_of_mid": float(spread) if spread is not None else None,
                     "observed_at": utc(quote["observed_at"]), "reasons": reasons})
    return rows


def scan_report(raw, *, evaluated_at):
    cutoff, evaluated = utc(raw["as_of"]), utc(evaluated_at)
    if cutoff > evaluated:
        raise ValueError("scanner snapshot is newer than the desk run")
    return {"as_of": cutoff, "regime": regime(raw, cutoff, evaluated),
            "news": news_leads(raw["news"], cutoff), "liquidity": liquidity(raw, cutoff),
            "news_coverage": raw["news_coverage"], "max_quote_age_seconds": str(decimal(raw["max_quote_age_seconds"])),
            "basis": "observed intake; regime and liquidity rules are diagnostic"}
