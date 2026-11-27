"""Walk-forward HAR variance forecasts with separate close-to-open inputs."""

from datetime import timedelta
from math import isfinite, log, sqrt
from statistics import mean

from market_risk import model_float, signed
from optionsdesk import NEW_YORK, digest, identifier, seconds_between, timestamp, utc
from price_limits import whole_contracts


FLOOR = 1e-12
WINDOWS = (1, 5, 22)


def session_series(raw, archive=None):
    if archive is not None:
        lag = whole_contracts(archive["availability_lag_seconds"], 0, 86400)
        downloaded = utc(archive["downloaded_at"])
        identifier(archive["source"])
    sessions = []
    for row in raw:
        opening, closing = (utc(row[key]) for key in ("open_at", "close_at"))
        if archive is None:
            received = utc(row["received_at"])
        else:
            if "received_at" in row or closing > downloaded:
                raise ValueError("archive sessions need completed market times and no prospective receipt field")
            received = utc((timestamp(closing) + timedelta(seconds=lag)).isoformat())
        day = timestamp(opening).astimezone(NEW_YORK).date().isoformat()
        duration = seconds_between(closing, opening)
        prices = [model_float(signed(price)) for price in row["five_minute_prices"]]
        if (not 0 < duration <= 86400 or duration % 300 or len(prices) != duration // 300 + 1
            or min(prices, default=0) <= 0 or closing > received
            or timestamp(closing).astimezone(NEW_YORK).date().isoformat() != day):
            raise ValueError("sessions require a complete regular five-minute price grid and a post-close receipt")
        if sessions and (day <= sessions[-1]["day"] or row["previous_session"] != sessions[-1]["day"]):
            raise ValueError("supply increasing consecutive exchange sessions")
        intraday = sum((log(current) - log(previous)) ** 2 for previous, current in zip(prices, prices[1:]))
        overnight = (log(prices[0]) - log(sessions[-1]["close_price"])) ** 2 if sessions else None
        available = max(received, sessions[-1]["received"]) if sessions else received
        sessions.append({"day": day, "open_at": opening, "received": received, "close_price": prices[-1],
                         "available": available, "intraday": intraday, "overnight": overnight})
    return sessions[1:]


def fit_ridge(training, feature_key, penalty):
    designs = [row[feature_key] for row in training]
    columns = list(zip(*designs, strict=True))
    centers = [mean(column) for column in columns]
    scales = [max(FLOOR, sqrt(mean((value - center) ** 2 for value in column)))
              for column, center in zip(columns, centers, strict=True)]

    def standardize(vector):
        return [(value - center) / scale for value, center, scale in zip(vector, centers, scales, strict=True)]

    target_mean = mean(row["target"] for row in training)
    normalized = [standardize(vector) for vector in designs]
    x = list(zip(*normalized, strict=True))
    target = [row["target"] - target_mean for row in training]
    system = [[mean(a * b for a, b in zip(col, other, strict=True)) + (penalty if i == j else 0)
               for j, other in enumerate(x)] + [mean(a * b for a, b in zip(col, target, strict=True))]
              for i, col in enumerate(x)]
    for col in range(len(x)):
        pivot = max(range(col, len(x)), key=lambda index: abs(system[index][col]))
        system[col], system[pivot] = system[pivot], system[col]
        scale = system[col][col]
        system[col] = [value / scale for value in system[col]]
        for index, row in enumerate(system):
            if index != col:
                system[index] = [a - row[col] * b for a, b in zip(row, system[col], strict=True)]
    coefficients = [row[-1] for row in system]
    return lambda features: max(FLOOR, target_mean + sum(
        weight * value for weight, value in zip(coefficients, standardize(features), strict=True)
    ))


def compare(raw):
    archive = raw.get("archive")
    sessions = session_series(raw["sessions"], archive)
    horizon = whole_contracts(raw["horizon_sessions"], 1, 22)
    minimum = whole_contracts(raw["min_training_windows"], 8, 100000)
    penalty = model_float(signed(raw["ridge_penalty"]))
    if not 1e-6 <= penalty <= 100:
        raise ValueError("ridge penalty must be between 0.000001 and 100")
    weights = []
    if incumbent := raw.get("incumbent_power_law"):
        lookback = whole_contracts(incumbent["lookback_sessions"], 1, 100000)
        exponent = model_float(signed(incumbent["exponent"]))
        if not -1 < exponent < 0:
            raise ValueError("power-law exponent must be between -1 and 0")
        weights = [(index + 1) ** exponent for index in range(lookback)]
    daily = [row["intraday"] + row["overnight"] for row in sessions]
    observations = []
    for end in range(max(WINDOWS) - 1, len(sessions) - horizon, horizon):
        history = sessions[end - max(WINDOWS) + 1:end + 1]
        target_rows = sessions[end + 1:end + horizon + 1]
        consumed = max(row["available"] for row in history)
        received = sessions[end]["received"]
        if consumed > received or received >= target_rows[0]["open_at"]:
            continue
        observations.append({
            "decision_at": received, "session": sessions[end]["day"],
            "label_available_at": max(row["available"] for row in target_rows),
            "target_through": target_rows[-1]["day"],
            "target": mean(daily[end + 1:end + horizon + 1]),
            "har": [mean(daily[end - window + 1:end + 1]) for window in WINDOWS],
            "har_overnight": [mean(row[key] for row in history[-window:])
                              for key in ("intraday", "overnight") for window in WINDOWS],
            "recent_mean": max(FLOOR, mean(daily[end - 21:end + 1])),
            "power_law": (max(FLOOR, sum(weight * daily[end - lag] for lag, weight in enumerate(weights)) / sum(weights))
                          if weights and end + 1 >= len(weights) and
                          max(mark["available"] for mark in sessions[end - len(weights) + 1:end + 1]) <= received else None),
        })
    tests = []
    for index, row in enumerate(observations):
        past = [prior for prior in observations[:index] if prior["label_available_at"] < row["decision_at"]]
        if row["label_available_at"] <= row["decision_at"] or len(past) < minimum or weights and row["power_law"] is None:
            continue
        forecasts = {name: fit_ridge(past, name, penalty)(row[name]) for name in ("har", "har_overnight")}
        forecasts["recent_mean"] = row["recent_mean"]
        if weights:
            forecasts["power_law"] = row["power_law"]
        loss = {name: log(value) + row["target"] / value for name, value in forecasts.items()}
        if not all(isfinite(value) for value in loss.values()):
            raise ValueError("variance comparison produced nonfinite losses")
        tests.append({**{key: row[key] for key in ("session", "decision_at", "target_through", "target")},
                      "training_windows": len(past), "forecasts": forecasts, "qlike": loss})
    paired = {}
    if tests:
        for name in tests[0]["qlike"]:
            if name == "har_overnight":
                continue
            differences = [row["qlike"]["har_overnight"] - row["qlike"][name] for row in tests]
            paired[name] = {"wins": sum(value < 0 for value in differences), "mean_difference": mean(differences),
                            "mean_excluding_best_window": (sum(differences) - min(differences)) / (len(differences) - 1)
                            if len(differences) > 1 else None}
    return {"basis": "archived bars with assumed availability" if archive is not None else "session prices with supplied receipts",
            "input_sha256": digest(raw), "horizon_sessions": horizon, "complete_test_windows": len(tests),
            "archive": archive, "variance_sessions": len(sessions),
            "status": "scored_historical_research" if tests else "insufficient_windows",
            "mean_qlike": {name: mean(row["qlike"][name] for row in tests) for name in tests[0]["qlike"]} if tests else None,
            "paired_overnight_comparisons": paired, "outcomes": tests}
