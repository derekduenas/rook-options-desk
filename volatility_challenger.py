"""Walk-forward comparison of fixed daily variance forecasts on saved closes."""

import argparse
from hashlib import sha256
import json
from math import isfinite, log
from pathlib import Path
from statistics import mean


def compare(plan, data):
    sessions = sorted(row["day"] for row in data["sessions"])
    if len(sessions) != len(set(sessions)):
        raise ValueError("duplicate market session")
    lookback, horizon = plan["lookback_sessions"], plan["horizon_sessions"]
    decay = plan["ewma_decay"]
    exponent = plan["power_exponent"]
    if lookback < 20 or horizon < 1 or not 0 < decay < 1 or not -1 < exponent < 0:
        raise ValueError("invalid forecast parameters")
    weights = {
        "ewma": [decay**lag for lag in range(lookback)],
        "power_law": [(lag + 1)**exponent for lag in range(lookback)],
        "mean_20": [1.0] * 20,
    }
    outcomes = []
    by_symbol = {}
    for symbol in plan["universe"]:
        prices = [float(data["closes"][symbol][day]) for day in sessions]
        if any(not isfinite(price) or price <= 0 for price in prices):
            raise ValueError(f"invalid closing price for {symbol}")
        squared = [(log(b) - log(a))**2 for a, b in zip(prices, prices[1:])]
        rows = []
        for end in range(lookback, len(squared) - horizon + 1, horizon):
            past = squared[:end][::-1]
            target = mean(squared[end:end + horizon])
            forecasts = {name: max(1e-12, sum(w * r for w, r in zip(curve, past)) / sum(curve))
                         for name, curve in weights.items()}
            qlike = {name: log(value) + target / value for name, value in forecasts.items()}
            rows.append({"symbol": symbol, "as_of_session": sessions[end],
                         "target_through_session": sessions[end + horizon], "target": target,
                         "forecasts": forecasts, "qlike": qlike})
        if not rows:
            raise ValueError("no complete out-of-sample windows")
        outcomes.extend(rows)
        by_symbol[symbol] = {name: mean(row["qlike"][name] for row in rows) for name in weights}
    return {"basis": "historical daily close variance proxy, no options P&L", "plan": plan,
            "symbol_windows": len(outcomes), "distinct_test_windows": len({row["as_of_session"] for row in outcomes}),
            "mean_qlike": {name: mean(row["qlike"][name] for row in outcomes) for name in weights},
            "by_symbol_mean_qlike": by_symbol, "outcomes": outcomes}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("closes", type=Path)
    args = parser.parse_args()
    plan, data = args.plan.read_bytes(), args.closes.read_bytes()
    report = compare(json.loads(plan), json.loads(data))
    report["input_sha256"] = {"plan": sha256(plan).hexdigest(), "closes": sha256(data).hexdigest()}
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
