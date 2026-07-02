#!/usr/bin/env python3
"""
CALIBRATION — Data-driven model weight and KDE bandwidth optimization.

Reads backtest/daily_data.jsonl and ensemble snapshots to compute:
  1. Optimal model weights (inverse MAE weighting)
  2. KDE bandwidth correction factor (minimize bracket prediction error)

Called by edge_scanner_v2.py at startup if sufficient data exists.

Usage:
  python3 calibration.py          # Print calibration report
  python3 calibration.py --apply  # Write calibrated params to calibration_cache.json
"""

import json
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
BACKTEST_DIR = PROJECT_ROOT / "backtest"
DAILY_DATA_FILE = BACKTEST_DIR / "daily_data.jsonl"
SNAPSHOT_DIR = BACKTEST_DIR / "ensemble_snapshots"
CACHE_FILE = PROJECT_ROOT / "calibration_cache.json"

# Minimum number of data points required for calibration
MIN_DAYS_FOR_CALIBRATION = 14

# Canonical default weights (single source of truth in config.py) — used as priors
from config import DEFAULT_MODEL_WEIGHTS


def load_backtest_records(city_filter: str = None) -> list:
    """Load all backtest records, optionally filtered by city."""
    if not DAILY_DATA_FILE.exists():
        return []
    records = []
    with open(DAILY_DATA_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                if r.get("actual_high") is None:
                    continue
                if city_filter and r.get("city", "").upper() != city_filter.upper():
                    continue
                records.append(r)
            except json.JSONDecodeError:
                continue
    # Deduplicate by (date, city)
    seen = set()
    unique = []
    for r in records:
        key = (r.get("date"), r.get("city"))
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique


def calibrate_model_weights(records: list = None) -> dict:
    """
    Compute calibrated model weights from backtest MAE.

    Uses inverse-MAE weighting: models with lower error get higher weight.
    Blends 50/50 with prior weights to prevent overfitting on small samples.

    Returns dict of {model_name: calibrated_weight}.
    """
    if records is None:
        records = load_backtest_records()

    if len(records) < MIN_DAYS_FOR_CALIBRATION:
        return {}  # Not enough data

    # Collect per-model errors
    model_errors = defaultdict(list)
    for r in records:
        actual = r.get("actual_high")
        if actual is None:
            continue

        # Try snapshot first, then inline per_model_means
        snap = {}
        snap_path = SNAPSHOT_DIR / f"{r['date']}_{r['city']}.json"
        if snap_path.exists():
            try:
                snap = json.loads(snap_path.read_text())
            except (json.JSONDecodeError, Exception):
                pass

        per_model = snap.get("per_model_means") or r.get("per_model_means", {})
        for model, mean in per_model.items():
            if mean is not None:
                model_errors[model].append(abs(mean - actual))

    if not model_errors:
        return {}

    # Compute MAE per model
    model_mae = {}
    for model, errors in model_errors.items():
        if len(errors) >= 5:  # Need at least 5 data points per model
            model_mae[model] = sum(errors) / len(errors)

    if not model_mae:
        return {}

    # Inverse-MAE weighting (normalized so GFS baseline ≈ 1.0)
    gfs_mae = model_mae.get("gfs_seamless")
    if gfs_mae is None or gfs_mae == 0:
        # Use median MAE as baseline
        baseline = sorted(model_mae.values())[len(model_mae) // 2]
    else:
        baseline = gfs_mae

    data_weights = {}
    for model, mae in model_mae.items():
        if mae > 0:
            data_weights[model] = baseline / mae  # Lower MAE → higher weight

    # Blend with priors (50/50) to prevent overfitting on small samples
    n_days = len(set(r.get("date") for r in records))
    # More data → more trust in data weights (up to 80% at 60+ days)
    data_trust = min(0.80, max(0.30, n_days / 75))

    calibrated = {}
    for model in DEFAULT_MODEL_WEIGHTS:
        prior = DEFAULT_MODEL_WEIGHTS[model]
        data_w = data_weights.get(model)
        if data_w is not None:
            calibrated[model] = round(data_trust * data_w + (1 - data_trust) * prior, 3)
        else:
            calibrated[model] = prior

    return calibrated


def calibrate_bandwidth_factor(records: list = None) -> float:
    """
    Compute bandwidth correction factor from backtest data.

    Analyzes whether Silverman's rule over-smooths or under-smooths by
    checking if the ensemble predicted probability matches actual hit rates.

    Returns a multiplier (0.7 - 1.5) to apply to Silverman bandwidth.
    1.0 = no correction, <1.0 = narrower bandwidth, >1.0 = wider.
    Returns 0.0 if insufficient data.
    """
    if records is None:
        records = load_backtest_records()

    if len(records) < MIN_DAYS_FOR_CALIBRATION:
        return 0.0  # Not enough data

    # Collect (predicted_prob, actual_hit) pairs from snapshots
    predicted = []
    actual = []

    for r in records:
        snap_path = SNAPSHOT_DIR / f"{r['date']}_{r['city']}.json"
        if not snap_path.exists():
            continue
        try:
            snap = json.loads(snap_path.read_text())
        except (json.JSONDecodeError, Exception):
            continue

        opps = snap.get("opportunities", [])
        settlements = {s["ticker"]: s["result"] for s in r.get("settlements", []) if "ticker" in s}

        for opp in opps:
            ticker = opp.get("ticker", "")
            kde = opp.get("kde_prob", 0)
            side = opp.get("side", "yes")

            settled = settlements.get(ticker)
            if settled is None or kde <= 0:
                continue

            if side == "yes":
                predicted.append(kde)
                actual.append(1.0 if settled == "yes" else 0.0)
            else:
                predicted.append(1 - kde)
                actual.append(1.0 if settled == "no" else 0.0)

    if len(predicted) < 20:
        return 0.0

    # Compute calibration: if predicted > actual hit rate, we're over-confident
    # (bandwidth too narrow). If predicted < actual, we're under-confident
    # (bandwidth too wide).
    avg_predicted = sum(predicted) / len(predicted)
    avg_actual = sum(actual) / len(actual)

    if avg_predicted == 0:
        return 1.0

    # Ratio: >1 means we're under-confident → need wider bandwidth
    # <1 means over-confident → need narrower bandwidth
    # Clamped to reasonable range
    ratio = avg_actual / avg_predicted
    factor = max(0.7, min(1.5, ratio))

    return round(factor, 3)


def load_calibration_cache() -> dict:
    """Load cached calibration results if they exist and are recent."""
    if not CACHE_FILE.exists():
        return {}
    try:
        data = json.loads(CACHE_FILE.read_text())
        return data
    except (json.JSONDecodeError, Exception):
        return {}


def save_calibration_cache(weights: dict, bw_factor: float, n_days: int):
    """Save calibration results to cache file."""
    from datetime import datetime
    cache = {
        "calibrated_weights": weights,
        "bandwidth_factor": bw_factor,
        "n_days": n_days,
        "calibrated_at": datetime.now().isoformat(),
    }
    CACHE_FILE.write_text(json.dumps(cache, indent=2))


def get_calibrated_params() -> tuple:
    """
    Get calibrated model weights and bandwidth factor.

    Returns (model_weights: dict, bandwidth_factor: float).
    - model_weights: empty dict if insufficient data (use defaults)
    - bandwidth_factor: 0.0 if insufficient data (use Silverman as-is)
    """
    # Try cache first
    cache = load_calibration_cache()
    if cache.get("calibrated_weights") and cache.get("bandwidth_factor") is not None:
        return cache["calibrated_weights"], cache["bandwidth_factor"]

    # Compute from backtest data
    records = load_backtest_records()
    weights = calibrate_model_weights(records)
    bw_factor = calibrate_bandwidth_factor(records)

    # Cache if we computed anything
    if weights or bw_factor:
        n_days = len(set(r.get("date") for r in records))
        save_calibration_cache(weights, bw_factor, n_days)

    return weights, bw_factor


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Calibration — Model weight and bandwidth optimization")
    parser.add_argument("--apply", action="store_true", help="Save calibrated params to cache")
    parser.add_argument("--city", type=str, default=None, help="Filter by city")
    args = parser.parse_args()

    records = load_backtest_records(args.city)
    n_days = len(set(r.get("date") for r in records))

    print(f"\n{'='*50}")
    print("  CALIBRATION REPORT")
    print(f"{'='*50}")
    print(f"  Records: {len(records)} ({n_days} unique days)")
    print(f"  Min required: {MIN_DAYS_FOR_CALIBRATION} days")

    if n_days < MIN_DAYS_FOR_CALIBRATION:
        print(f"\n  Insufficient data ({n_days}/{MIN_DAYS_FOR_CALIBRATION} days). Using defaults.")
        print(f"{'='*50}\n")
    else:
        weights = calibrate_model_weights(records)
        bw_factor = calibrate_bandwidth_factor(records)

        print("\n  MODEL WEIGHTS (calibrated vs default)")
        print(f"  {'Model':<20s} {'Default':>8s} {'Calibrated':>10s} {'Delta':>8s}")
        print(f"  {'─'*48}")
        for model in DEFAULT_MODEL_WEIGHTS:
            default = DEFAULT_MODEL_WEIGHTS[model]
            cal = weights.get(model, default)
            delta = cal - default
            print(f"  {model:<20s} {default:>7.2f}x {cal:>9.3f}x {delta:>+7.3f}")

        print(f"\n  BANDWIDTH FACTOR: {bw_factor:.3f}x")
        if bw_factor > 1.05:
            print("  → Model is UNDER-CONFIDENT: widening bandwidth")
        elif bw_factor < 0.95:
            print("  → Model is OVER-CONFIDENT: narrowing bandwidth")
        else:
            print("  → Model is well-calibrated")

        if args.apply:
            save_calibration_cache(weights, bw_factor, n_days)
            print(f"\n  Saved to {CACHE_FILE.name}")

        print(f"{'='*50}\n")
