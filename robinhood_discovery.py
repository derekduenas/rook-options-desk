"""Normalize and audit partitioned Robinhood market scanner previews."""

from decimal import Decimal

from market_risk import signed
from optionsdesk import decimal, identifier, utc
from price_limits import whole_contracts


DISCOVERY_FILE = "robinhood-market-discovery.json"


def report(raw, *, as_of, include_universe=False):
    cutoff = utc(as_of)
    batches = [batch for batch in raw["previews"] if utc(batch["received_at"]) <= cutoff]
    partition_summary, underlying = [], {}
    missing_fields = duplicates = 0
    for batch in batches:
        preview = batch["result"]
        rows = preview["results"] or []
        matches = whole_contracts(preview["total_items"], 0, 10**7)
        if len(rows) > matches:
            raise ValueError("Robinhood preview returned more rows than reported")
        partition_summary.append({"matches": matches, "returned": len(rows), "received_at": utc(batch["received_at"])})
        for item in rows:
            if item.get("instrument_type") != "EQUITY" or not item.get("ticker"):
                continue
            symbol = identifier(item["ticker"]).upper()
            mapped = item.get("columns") or {}
            if any(mapped.get(key) in {None, ""} for key in ("Last", "% Change", "Options volume")):
                missing_fields += 1
                continue
            last, change, contracts = decimal(mapped["Last"]), signed(mapped["% Change"]), decimal(mapped["Options volume"])
            if not last:
                missing_fields += 1
                continue
            receipt = utc(batch["received_at"])
            if symbol in underlying:
                duplicates += 1
                if underlying[symbol]["received_at"] >= receipt:
                    continue
            underlying[symbol] = {"symbol": symbol, "last": str(last), "change_pct": str(change * 100),
                                  "options_volume": str(contracts), "received_at": receipt}
    total = sum(row["matches"] for row in partition_summary)
    returned = sum(row["returned"] for row in partition_summary)
    root = raw.get("unpartitioned_preview")
    unpartitioned = (whole_contracts(root["matches"], 0, 10**7)
                     if root and utc(root["received_at"]) <= cutoff else None)
    active = sorted(underlying.values(), key=lambda row: (-Decimal(row["options_volume"]), row["symbol"]))
    move = sorted(underlying.values(), key=lambda row: (
        -abs(Decimal(row["change_pct"])), -Decimal(row["options_volume"]), row["symbol"]))
    return {"basis": "Robinhood scanner preview discovery", "matched_in_previews": total, "rows_returned": returned,
            "unpartitioned_matches": unpartitioned,
            "count_discrepancy": unpartitioned is not None and unpartitioned != total,
            "distinct_usable_symbols": len(underlying), "duplicates": duplicates, "missing_fields": missing_fields,
            "any_partition_truncated": any(row["matches"] > row["returned"] for row in partition_summary),
            "last_received_at": max((row["received_at"] for row in partition_summary), default=None),
            "partitions": partition_summary, "most_active": active[:12], "largest_moves": move[:12],
            **({"universe": active} if include_universe else {})}
