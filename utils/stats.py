#!/usr/bin/env python3
"""
WEATHER EDGE — Shared Statistical Engine

Vectorized Gaussian KDE, Silverman bandwidth estimation, and ensemble
member weighting. Used by edge_scanner_v2.py and proxy_arb_engine.py
so the math lives in exactly one place.

All functions are pure (no side effects, no file I/O) and importable
without pulling in the full scanner or calibration modules.
"""

from __future__ import annotations

import numpy as np

__all__ = [
    "kde_probability",
    "silverman_bandwidth",
    "_detect_bimodal",
    "build_member_weights",
]


def kde_probability(
    members: list[float],
    low: float,
    high: float,
    bandwidth: float | None = None,
    n_points: int = 200,
    weights: list[float] | None = None,
    min_bandwidth: float = 0.3,
) -> float:
    """Compute bracket probability using Gaussian KDE (numpy-vectorized).

    Smooths discrete ensemble members into a continuous PDF, then integrates
    over [low, high) using the trapezoidal rule.

    Args:
        members       : Ensemble temperature values (°F for weather, % for CPI).
        low, high     : Bracket bounds — the integration window.
        bandwidth     : Fixed kernel bandwidth. If None, Silverman's rule is applied.
        n_points      : Grid density for numerical integration (default 200).
        weights       : Per-member model weights (e.g., AIFS = 1.30×). If provided,
                        must be the same length as members.
        min_bandwidth : Floor to prevent under-smoothing. Use 0.3 for weather (°F),
                        0.005 for CPI (percentage-scale).
    """
    if not members or len(members) < 2:
        return 0.0

    m = np.asarray(members, dtype=np.float64)

    if bandwidth is None:
        std = np.std(m, ddof=1)
        if std == 0:
            return 1.0 if low <= m[0] < high else 0.0
        bandwidth = 1.06 * std * len(m) ** (-0.2)
    bandwidth = max(min_bandwidth, bandwidth)

    range_low = max(low, float(m.min()) - 4 * bandwidth)
    range_high = min(high, float(m.max()) + 4 * bandwidth)

    if range_low >= range_high:
        return 1.0 if low <= float(m.min()) and high >= float(m.max()) else 0.0

    x_grid = np.linspace(range_low, range_high, n_points + 1)
    z = (x_grid[:, np.newaxis] - m[np.newaxis, :]) / bandwidth
    kernel_vals = np.exp(-0.5 * z * z) / (bandwidth * np.sqrt(2 * np.pi))

    if weights is not None and len(weights) == len(members):
        w = np.asarray(weights, dtype=np.float64)
        w_total = w.sum()
        w = w / w_total if w_total > 0 else np.ones_like(w) / len(w)
        density = (kernel_vals * w[np.newaxis, :]).sum(axis=1)
    else:
        density = kernel_vals.mean(axis=1)

    _trapz = getattr(np, "trapezoid", None) or np.trapz
    return min(1.0, max(0.0, float(_trapz(density, x_grid))))


def _detect_bimodal(members_sorted: np.ndarray, gap_threshold: float = 3.0) -> bool:
    """Detect bimodality via gap analysis on the middle 60% of the distribution.

    Catches the common AIFS/IFS split where one model clusters ~35°F and
    the other ~31°F — the inter-cluster gap is far larger than intra-cluster spacing.
    """
    n = len(members_sorted)
    if n < 20:
        return False

    lo_idx = int(n * 0.2)
    hi_idx = int(n * 0.8)
    middle = members_sorted[lo_idx:hi_idx]

    if len(middle) < 5:
        return False

    gaps = np.diff(middle)
    max_gap = float(gaps.max())
    median_gap = float(np.median(gaps))

    return max_gap >= gap_threshold and (median_gap == 0 or max_gap >= 1.5 * median_gap)


def silverman_bandwidth(
    members: list[float],
    min_bandwidth: float = 0.3,
    bandwidth_factor: float = 1.0,
) -> float:
    """Silverman's rule of thumb with bimodal correction.

    When the ensemble splits into two clusters (AIFS vs IFS disagree by 3+°F),
    Silverman over-smooths. In that case bandwidth is reduced by 40% to
    preserve both peaks.

    Args:
        members          : Ensemble member values.
        min_bandwidth    : Floor to prevent under-smoothing (0.3°F for weather).
        bandwidth_factor : Multiplicative calibration factor from backtest data.
                           Pass the module-level _BANDWIDTH_FACTOR from the scanner.
                           Defaults to 1.0 (no correction).
    """
    if len(members) < 2:
        return 1.0
    m = np.asarray(members, dtype=np.float64)
    m_sorted = np.sort(m)
    std = float(np.std(m, ddof=1))
    bw = 1.06 * std * len(m) ** (-0.2)

    if _detect_bimodal(m_sorted):
        bw *= 0.6

    bw *= bandwidth_factor
    return max(min_bandwidth, bw)


def build_member_weights(models: list) -> tuple[list[float], list[float]]:
    """Build parallel (members, weights) arrays for weighted KDE.

    Accepts any list of objects with `.members` (list[float]) and `.weight` (float).
    Returns sorted members with corresponding per-member weights.
    """
    pairs: list[tuple[float, float]] = []
    for mg in models:
        if not getattr(mg, "members", []):
            continue
        w = getattr(mg, "weight", 1.0)
        for val in mg.members:
            pairs.append((val, w))

    if not pairs:
        return [], []

    pairs.sort(key=lambda x: x[0])
    members = [p[0] for p in pairs]
    weights = [p[1] for p in pairs]
    return members, weights
