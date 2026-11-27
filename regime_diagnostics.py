"""Compute fixed-parameter jump intensity and correlation concentration diagnostics."""

from math import atan2, cos, exp, expm1, log, sin, sqrt
from statistics import mean

from market_risk import model_float, signed
from optionsdesk import decimal, digest, identifier, seconds_between, utc


def eigenvalues(matrix):
    # Symmetric Jacobi rotations keep the matrix real and preserve the trace.
    matrix = [list(row) for row in matrix]
    n = len(matrix)
    for _ in range(50 * n * n):
        i, j = max(((i, j) for i in range(n) for j in range(i + 1, n)), key=lambda cell: abs(matrix[cell[0]][cell[1]]))
        off = matrix[i][j]
        if abs(off) < 1e-12:
            return [matrix[i][i] for i in range(n)]
        angle = atan2(2 * off, matrix[j][j] - matrix[i][i]) / 2
        c, s = cos(angle), sin(angle)
        a, b = matrix[i][i], matrix[j][j]
        for k in range(n):
            if k != i and k != j:
                left, right = matrix[k][i], matrix[k][j]
                matrix[i][k] = matrix[k][i] = c * left - s * right
                matrix[j][k] = matrix[k][j] = s * left + c * right
        matrix[i][i], matrix[j][j] = c*c*a - 2*s*c*off + s*s*b, s*s*a + 2*s*c*off + c*c*b
        matrix[i][j] = matrix[j][i] = 0.0
    raise ValueError("correlation eigenvalues did not converge")


def correlation_report(rows, assets):
    if len(rows) <= len(assets):
        return {"status": "insufficient_aligned_returns", "observations": len(rows)}
    columns = [[row[asset] for row in rows] for asset in assets]
    standardized = []
    for column in columns:
        scale = max(map(abs, column))
        if not scale:
            return {"status": "constant_return_series", "observations": len(rows)}
        column = [value / scale for value in column]
        average = mean(column)
        deviations = [value - average for value in column]
        norm = sqrt(sum(value * value for value in deviations))
        if not norm:
            return {"status": "constant_return_series", "observations": len(rows)}
        standardized.append([value / norm for value in deviations])
    matrix = [[sum(a * b for a, b in zip(left, right, strict=True)) for right in standardized]
              for left in standardized]
    spectrum = [max(0.0, value) for value in eigenvalues(matrix)]
    trace = sum(spectrum)
    shares = [value / trace for value in spectrum if value]
    entropy = -sum(value * log(value) for value in shares) / log(len(assets))
    pairwise = [matrix[i][j] for i in range(len(assets)) for j in range(i + 1, len(assets))]
    return {"status": "computed", "observations": len(rows), "largest_eigenvalue_share": max(shares),
            "normalized_spectral_entropy": min(1.0, max(0.0, entropy)), "mean_pairwise_correlation": mean(pairwise)}


def report(raw):
    start, cutoff = utc(raw["window_start"]), utc(raw["as_of"])
    horizon, decay = (model_float(decimal(raw[key])) for key in ("forecast_seconds", "decay_seconds"))
    branching = model_float(decimal(raw["branching_ratio"]))
    mu = model_float(decimal(raw["background_jumps_per_second"]))
    if not start < cutoff or not 0 < horizon or not 0 < decay or not 0 <= branching < 1:
        raise ValueError("supply a valid observation window and a subcritical exponential Hawkes kernel")
    assets = [identifier(asset) for asset in raw["assets"]]
    if not 2 <= len(assets) <= 20 or len(set(assets)) != len(assets):
        raise ValueError("supply 2 to 20 distinct assets")

    timestamps = []
    for mark in raw["jumps"]:
        observed, received = utc(mark["observed_at"]), utc(mark["received_at"])
        if not start <= observed <= received <= cutoff or timestamps and observed <= timestamps[-1]:
            raise ValueError("jumps must be ordered and available in the observation window")
        timestamps.append(observed)
    rate = len(timestamps) / float(seconds_between(cutoff, start))
    excess = branching * sum(exp(-float(seconds_between(cutoff, event)) / decay) for event in timestamps) / decay
    # The first-arrival survival uses the decay of the existing intensity until a new jump occurs.
    hazard = mu * horizon - excess * decay * expm1(-horizon / decay)
    rows, previous = [], None
    for row in raw["aligned_returns"]:
        observed, received = utc(row["observed_at"]), utc(row["received_at"])
        if (not start <= observed <= received <= cutoff or previous and observed <= previous
            or set(row["returns"]) != set(assets)):
            raise ValueError("returns require ordered point-in-time rows for every asset")
        previous = observed
        rows.append({asset: model_float(signed(row["returns"][asset])) for asset in assets})
    return {"basis": "supplied point-in-time jump events and aligned returns", "input_sha256": digest(raw),
            "as_of": cutoff, "jump_count": len(timestamps), "horizon_seconds": horizon,
            "fixed_hawkes_intensity_per_minute": (mu + excess) * 60,
            "fixed_hawkes_probability_of_jump": -expm1(-hazard),
            "poisson_count_baseline_probability": -expm1(-rate * horizon),
            "correlation": correlation_report(rows, assets)}
