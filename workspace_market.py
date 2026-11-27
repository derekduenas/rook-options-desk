"""Build the terminal's compact market-only read model."""

import json

from opportunity_scan import SETTINGS_FILE, from_feed
from optionsdesk import contract, utc
from robinhood_discovery import DISCOVERY_FILE, report


def read_workspace(journal, directory, cutoff):
    discovery_path = directory / DISCOVERY_FILE
    settings_path = directory / SETTINGS_FILE
    history_path = directory / "terminal-price-history.json"
    if not discovery_path.is_file() and not settings_path.is_file():
        return None
    settings = json.loads(settings_path.read_text()) if settings_path.is_file() else {}
    discovery = report(json.loads(discovery_path.read_text()), as_of=cutoff, include_universe=True
                       ) if discovery_path.is_file() else {}
    terms = []
    for row in settings.get("contracts", []):
        code = contract(row["symbol"], row["expiry"], row["strike"], row["right"])
        if utc(row["known_at"]) > cutoff:
            continue
        quote = from_feed(journal, "option_quote", code, cutoff, settings["option_feed"])
        terms.append({"code": code, **{key: row[key] for key in ("symbol", "expiry", "strike", "right")},
                      "quote": ({key: quote.get(key) for key in ("bid", "ask", "bid_size", "ask_size",
                                 "observed_at", "implied_volatility")} if quote else None)})
    history = json.loads(history_path.read_text()) if history_path.is_file() else {}
    return {"as_of": cutoff, "discovery_received_at": discovery.get("last_received_at"),
            "market_rows": discovery.get("universe", []), "contracts": terms, "history": history,
            "option_feed": " / ".join(settings.get("option_feed", [])),
            "unpartitioned_matches": discovery.get("unpartitioned_matches"),
            "has_discovery_discrepancy": discovery.get("count_discrepancy", False)}
