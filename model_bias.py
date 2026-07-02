#!/usr/bin/env python3
"""
MODEL BIAS — Per-model, per-city rolling error tracker.

Tracks systematic forecast biases across all ensemble models and 5 cities
to enable:
  1. Bias detection (e.g., "ICON runs +2.0F hot for Chicago in winter")
  2. Data-driven weight adjustments (inverse-MAE weighting)
  3. Real-time bias corrections applied before KDE probability computation

Data source: backtest/daily_data.jsonl — records with per_model_means
and actual_high fields, loaded via calibration.load_backtest_records().

Usage:
  python3 model_bias.py                    # Full bias report
  python3 model_bias.py --city NYC         # Single city
  python3 model_bias.py --model ecmwf_aifs025  # Single model
  python3 model_bias.py --corrections      # Print correction values
"""

from __future__ import annotations

import argparse
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from log_setup import get_logger

logger = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent

# Canonical model names and default weights (single source of truth in config.py)
from config import DEFAULT_MODEL_WEIGHTS

TOTAL_DEFAULT_WEIGHT = sum(DEFAULT_MODEL_WEIGHTS.values())

# Minimum records needed before we trust bias statistics
MIN_RECORDS_FOR_BIAS = 5

# Exponential decay factor for rolling bias (per day back)
ROLLING_DECAY = 0.9

# Threshold for flagging significant bias (degrees F)
SIGNIFICANT_BIAS_F = 1.5

# Cities we track
CITIES = ["NYC", "CHI", "DEN", "MIA", "LAX"]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ModelBias:
    """Bias statistics for a single (model, city) combination."""
    model_name: str
    city: str               # City code or "ALL" for aggregate
    sample_count: int
    mae: float              # Mean Absolute Error
    bias: float             # Mean signed error (positive = model runs hot)
    rmse: float
    recent_bias: float      # Last 7 days exponentially-weighted bias
    trend: str              # "warming", "cooling", "stable"
    suggested_weight: float # Based on inverse MAE
    current_weight: float   # From DEFAULT_MODEL_WEIGHTS


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _load_records(city_filter: Optional[str] = None) -> List[dict]:
    """
    Load backtest records that have both per_model_means and actual_high.

    Uses calibration.load_backtest_records() which reads from
    backtest/daily_data.jsonl and deduplicates by (date, city).
    """
    try:
        from calibration import load_backtest_records
        records = load_backtest_records(city_filter)
    except ImportError:
        logger.warning("calibration module not found; loading directly from daily_data.jsonl")
        records = _load_daily_data_fallback(city_filter)

    # Filter to records with per_model_means data
    usable = []
    for r in records:
        if r.get("actual_high") is None:
            continue
        pmm = r.get("per_model_means")
        if not pmm or not isinstance(pmm, dict):
            continue
        usable.append(r)

    return usable


def _load_daily_data_fallback(city_filter: Optional[str] = None) -> List[dict]:
    """Direct JSONL loader when calibration module is unavailable."""
    import json

    data_file = PROJECT_ROOT / "backtest" / "daily_data.jsonl"
    if not data_file.exists():
        return []

    records = []
    seen: set = set()
    with open(data_file) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if r.get("actual_high") is None:
                continue
            if city_filter and r.get("city", "").upper() != city_filter.upper():
                continue
            key = (r.get("date"), r.get("city"))
            if key not in seen:
                seen.add(key)
                records.append(r)
    return records


# ---------------------------------------------------------------------------
# Core computation
# ---------------------------------------------------------------------------

def _extract_errors(records: List[dict]) -> Dict[Tuple[str, str], List[float]]:
    """
    Build mapping of (model, city) -> [signed_errors] from records.

    Signed error = model_forecast - actual_high.
    Positive = model ran hot; negative = model ran cold.
    """
    errors: Dict[Tuple[str, str], List[float]] = defaultdict(list)
    for r in records:
        actual = r["actual_high"]
        city = r.get("city", "UNK").upper()
        pmm = r.get("per_model_means", {})
        for model, forecast in pmm.items():
            if forecast is None:
                continue
            err = forecast - actual
            errors[(model, city)].append(err)
    return errors


def _compute_trend(errors: List[float], split_point: int = None) -> str:
    """
    Determine if bias is 'warming', 'cooling', or 'stable'.

    Compares mean bias of the first half vs second half of the error list.
    The list is assumed to be in chronological order.
    """
    if len(errors) < 6:
        return "stable"

    if split_point is None:
        split_point = len(errors) // 2

    first_half = errors[:split_point]
    second_half = errors[split_point:]

    mean_first = sum(first_half) / len(first_half)
    mean_second = sum(second_half) / len(second_half)

    delta = mean_second - mean_first
    if delta > 0.5:
        return "warming"
    elif delta < -0.5:
        return "cooling"
    return "stable"


def compute_rolling_bias(
    records: List[dict],
    model: str,
    city: str,
    window: int = 7,
) -> Tuple[float, float, int]:
    """
    Compute exponentially-weighted moving average of bias for recent records.

    Weight = 0.9^(days_ago) -- recent days matter more.

    Args:
        records: List of calibration records (must have per_model_means, actual_high).
        model: Model name (e.g. 'ecmwf_aifs025').
        city: City code (e.g. 'NYC') or 'ALL' for aggregate.
        window: Number of most-recent records to consider.

    Returns:
        (rolling_bias, rolling_mae, sample_count)
    """
    # Filter and sort by date (most recent last)
    relevant = []
    for r in records:
        if r.get("actual_high") is None:
            continue
        pmm = r.get("per_model_means", {})
        if model not in pmm or pmm[model] is None:
            continue
        r_city = r.get("city", "").upper()
        if city.upper() != "ALL" and r_city != city.upper():
            continue
        relevant.append(r)

    relevant.sort(key=lambda x: x.get("date", ""))

    # Take last `window` records
    recent = relevant[-window:] if len(relevant) > window else relevant
    if not recent:
        return 0.0, 0.0, 0

    weighted_bias = 0.0
    weighted_abs_err = 0.0
    total_weight = 0.0

    n = len(recent)
    for i, r in enumerate(recent):
        days_ago = n - 1 - i  # most recent = 0
        w = ROLLING_DECAY ** days_ago
        err = r["per_model_means"][model] - r["actual_high"]
        weighted_bias += w * err
        weighted_abs_err += w * abs(err)
        total_weight += w

    if total_weight == 0:
        return 0.0, 0.0, 0

    return (
        round(weighted_bias / total_weight, 2),
        round(weighted_abs_err / total_weight, 2),
        n,
    )


def compute_model_biases(
    records: List[dict],
    window_days: int = 30,
) -> List[ModelBias]:
    """
    Compute ModelBias for each (model, city) combination and aggregate.

    Args:
        records: Calibration records with per_model_means and actual_high.
        window_days: Not used for filtering (all records used), but passed to
                     rolling bias for the recent-bias window.

    Returns:
        List of ModelBias objects for every (model, city) pair plus
        (model, "ALL") aggregates.
    """
    errors_by_key = _extract_errors(records)

    # Aggregate errors across cities for each model
    aggregate_errors: Dict[str, List[float]] = defaultdict(list)
    for (model, city), errs in errors_by_key.items():
        aggregate_errors[model].extend(errs)

    results: List[ModelBias] = []

    # First pass: compute per-model MAE for weight suggestions
    model_mae_all: Dict[str, float] = {}
    for model in DEFAULT_MODEL_WEIGHTS:
        agg = aggregate_errors.get(model, [])
        if len(agg) >= MIN_RECORDS_FOR_BIAS:
            model_mae_all[model] = sum(abs(e) for e in agg) / len(agg)

    # Compute suggested weights (inverse MAE, normalized to TOTAL_DEFAULT_WEIGHT)
    suggested_weights_all = _inverse_mae_weights(model_mae_all)

    # Per-city MAE for per-city weight suggestions
    city_model_mae: Dict[str, Dict[str, float]] = defaultdict(dict)
    for (model, city), errs in errors_by_key.items():
        if len(errs) >= MIN_RECORDS_FOR_BIAS:
            city_model_mae[city][model] = sum(abs(e) for e in errs) / len(errs)

    # Build ModelBias objects for each (model, city)
    for (model, city), errs in sorted(errors_by_key.items()):
        if len(errs) < MIN_RECORDS_FOR_BIAS:
            continue

        mae = sum(abs(e) for e in errs) / len(errs)
        bias = sum(errs) / len(errs)
        rmse = math.sqrt(sum(e * e for e in errs) / len(errs))

        # Per-city suggested weight
        city_weights = _inverse_mae_weights(city_model_mae.get(city, {}))
        suggested_w = city_weights.get(model, DEFAULT_MODEL_WEIGHTS.get(model, 1.0))

        rolling_bias, _, _ = compute_rolling_bias(records, model, city, window=7)
        trend = _compute_trend(errs)

        results.append(ModelBias(
            model_name=model,
            city=city,
            sample_count=len(errs),
            mae=round(mae, 2),
            bias=round(bias, 2),
            rmse=round(rmse, 2),
            recent_bias=rolling_bias,
            trend=trend,
            suggested_weight=round(suggested_w, 3),
            current_weight=DEFAULT_MODEL_WEIGHTS.get(model, 1.0),
        ))

    # Aggregate (model, "ALL")
    for model in DEFAULT_MODEL_WEIGHTS:
        agg = aggregate_errors.get(model, [])
        if len(agg) < MIN_RECORDS_FOR_BIAS:
            continue

        mae = sum(abs(e) for e in agg) / len(agg)
        bias = sum(agg) / len(agg)
        rmse = math.sqrt(sum(e * e for e in agg) / len(agg))

        rolling_bias, _, _ = compute_rolling_bias(records, model, "ALL", window=7)
        trend = _compute_trend(agg)

        results.append(ModelBias(
            model_name=model,
            city="ALL",
            sample_count=len(agg),
            mae=round(mae, 2),
            bias=round(bias, 2),
            rmse=round(rmse, 2),
            recent_bias=rolling_bias,
            trend=trend,
            suggested_weight=round(suggested_weights_all.get(model, DEFAULT_MODEL_WEIGHTS.get(model, 1.0)), 3),
            current_weight=DEFAULT_MODEL_WEIGHTS.get(model, 1.0),
        ))

    return results


# ---------------------------------------------------------------------------
# Bias correction
# ---------------------------------------------------------------------------

def get_bias_correction(
    model: str,
    city: str,
    records: Optional[List[dict]] = None,
) -> float:
    """
    Return the recommended correction in degrees F for a model+city combo.

    If ICON runs +2.0F hot for CHI, returns -2.0 (subtract from forecast).
    If insufficient data (<5 records), returns 0.0 (no correction).

    Args:
        model: Model name (e.g. 'ecmwf_aifs025').
        city: City code (e.g. 'NYC').
        records: Pre-loaded records, or None to load from daily_data.jsonl.

    Returns:
        Correction in degrees F (negative = model runs hot, subtract from forecast).
    """
    if records is None:
        records = _load_records(city_filter=None)

    errors: List[float] = []
    for r in records:
        r_city = r.get("city", "").upper()
        if r_city != city.upper():
            continue
        pmm = r.get("per_model_means", {})
        if model not in pmm or pmm[model] is None:
            continue
        actual = r.get("actual_high")
        if actual is None:
            continue
        errors.append(pmm[model] - actual)

    if len(errors) < MIN_RECORDS_FOR_BIAS:
        return 0.0

    mean_bias = sum(errors) / len(errors)
    return round(-mean_bias, 2)


# ---------------------------------------------------------------------------
# Weight suggestions
# ---------------------------------------------------------------------------

def _inverse_mae_weights(model_mae: Dict[str, float]) -> Dict[str, float]:
    """
    Compute weights inversely proportional to MAE.

    Normalized so the total equals TOTAL_DEFAULT_WEIGHT.
    Models not in model_mae get their default weight.
    """
    if not model_mae:
        return dict(DEFAULT_MODEL_WEIGHTS)

    # Inverse MAE for models with data
    inv = {}
    for model, mae in model_mae.items():
        if mae > 0:
            inv[model] = 1.0 / mae

    if not inv:
        return dict(DEFAULT_MODEL_WEIGHTS)

    total_inv = sum(inv.values())

    # Normalize: distribute TOTAL_DEFAULT_WEIGHT proportionally
    weights: Dict[str, float] = {}
    for model in DEFAULT_MODEL_WEIGHTS:
        if model in inv:
            weights[model] = (inv[model] / total_inv) * TOTAL_DEFAULT_WEIGHT
        else:
            weights[model] = DEFAULT_MODEL_WEIGHTS[model]

    return weights


def suggest_weights(
    records: List[dict],
) -> Dict[str, dict]:
    """
    Compute weight suggestions based on inverse MAE.

    Returns:
        {
            "aggregate": {"ecmwf_aifs025": 1.35, ...},
            "by_city": {
                "NYC": {"ecmwf_aifs025": 1.40, ...},
                "CHI": {"ecmwf_aifs025": 1.20, ...},
                ...
            }
        }
    """
    errors_by_key = _extract_errors(records)

    # Aggregate MAE per model
    agg_errors: Dict[str, List[float]] = defaultdict(list)
    for (model, city), errs in errors_by_key.items():
        agg_errors[model].extend(errs)

    agg_mae: Dict[str, float] = {}
    for model, errs in agg_errors.items():
        if len(errs) >= MIN_RECORDS_FOR_BIAS:
            agg_mae[model] = sum(abs(e) for e in errs) / len(errs)

    aggregate_weights = _inverse_mae_weights(agg_mae)

    # Per-city MAE and weights
    city_mae: Dict[str, Dict[str, float]] = defaultdict(dict)
    for (model, city), errs in errors_by_key.items():
        if len(errs) >= MIN_RECORDS_FOR_BIAS:
            city_mae[city][model] = sum(abs(e) for e in errs) / len(errs)

    by_city: Dict[str, Dict[str, float]] = {}
    for city in sorted(city_mae):
        by_city[city] = {
            m: round(w, 3)
            for m, w in _inverse_mae_weights(city_mae[city]).items()
        }

    return {
        "aggregate": {m: round(w, 3) for m, w in aggregate_weights.items()},
        "by_city": by_city,
    }


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def generate_bias_report(
    records: Optional[List[dict]] = None,
    city_filter: Optional[str] = None,
    model_filter: Optional[str] = None,
) -> str:
    """
    Generate a human-readable text report of all biases.

    Args:
        records: Pre-loaded records, or None to load from daily_data.jsonl.
        city_filter: Restrict to one city (e.g. 'NYC').
        model_filter: Restrict to one model (e.g. 'ecmwf_aifs025').

    Returns:
        Multi-line string report.
    """
    if records is None:
        records = _load_records(city_filter=city_filter)

    biases = compute_model_biases(records)

    # Apply filters
    if city_filter:
        city_up = city_filter.upper()
        biases = [b for b in biases if b.city == city_up or b.city == "ALL"]
    if model_filter:
        biases = [b for b in biases if b.model_name == model_filter]

    if not biases:
        return "No bias data available. Need records with per_model_means and actual_high."

    lines: List[str] = []
    sep = "=" * 110
    thin_sep = "-" * 110

    lines.append("")
    lines.append(sep)
    lines.append("  MODEL BIAS REPORT")
    lines.append(sep)

    n_records = len(records)
    n_dates = len(set(r.get("date") for r in records))
    n_cities = len(set(r.get("city") for r in records))
    lines.append(f"  Records: {n_records} ({n_dates} unique days, {n_cities} cities)")
    lines.append("")

    # Header
    hdr = (
        f"  {'Model':<18s} {'City':<5s} {'N':>4s}  "
        f"{'MAE':>5s} {'Bias':>6s} {'RMSE':>5s}  "
        f"{'7d Bias':>7s} {'Trend':>8s}  "
        f"{'Cur Wt':>6s} {'Sug Wt':>6s} {'Flag':>4s}"
    )
    lines.append(hdr)
    lines.append(f"  {thin_sep[2:]}")

    # Separate per-city and aggregate
    per_city = [b for b in biases if b.city != "ALL"]
    aggregates = [b for b in biases if b.city == "ALL"]

    significant_biases: List[ModelBias] = []

    for b in sorted(per_city, key=lambda x: (x.model_name, x.city)):
        flag = " ** " if abs(b.bias) >= SIGNIFICANT_BIAS_F else "    "
        if abs(b.bias) >= SIGNIFICANT_BIAS_F:
            significant_biases.append(b)
        line = (
            f"  {b.model_name:<18s} {b.city:<5s} {b.sample_count:>4d}  "
            f"{b.mae:>5.2f} {b.bias:>+6.2f} {b.rmse:>5.2f}  "
            f"{b.recent_bias:>+7.2f} {b.trend:>8s}  "
            f"{b.current_weight:>5.2f}x {b.suggested_weight:>5.3f}x{flag}"
        )
        lines.append(line)

    if aggregates:
        lines.append(f"  {thin_sep[2:]}")
        for b in sorted(aggregates, key=lambda x: x.model_name):
            flag = " ** " if abs(b.bias) >= SIGNIFICANT_BIAS_F else "    "
            if abs(b.bias) >= SIGNIFICANT_BIAS_F:
                significant_biases.append(b)
            line = (
                f"  {b.model_name:<18s} {b.city:<5s} {b.sample_count:>4d}  "
                f"{b.mae:>5.2f} {b.bias:>+6.2f} {b.rmse:>5.2f}  "
                f"{b.recent_bias:>+7.2f} {b.trend:>8s}  "
                f"{b.current_weight:>5.2f}x {b.suggested_weight:>5.3f}x{flag}"
            )
            lines.append(line)

    lines.append(sep)

    # Significant bias warnings
    if significant_biases:
        lines.append("")
        lines.append("  ** SIGNIFICANT BIASES (|bias| >= 1.5F):")
        for b in significant_biases:
            direction = "HOT" if b.bias > 0 else "COLD"
            lines.append(
                f"     {b.model_name} @ {b.city}: {b.bias:+.2f}F ({direction}) "
                f"-- correction: {-b.bias:+.2f}F"
            )
    else:
        lines.append("")
        lines.append("  No significant biases detected (all |bias| < 1.5F).")

    # Weight recommendations
    weight_data = suggest_weights(records)
    agg_w = weight_data["aggregate"]

    lines.append("")
    lines.append("  WEIGHT RECOMMENDATIONS (aggregate):")
    lines.append(f"  {'Model':<18s} {'Current':>8s} {'Suggested':>10s} {'Delta':>8s}")
    lines.append(f"  {'─' * 46}")
    for model in DEFAULT_MODEL_WEIGHTS:
        cur = DEFAULT_MODEL_WEIGHTS[model]
        sug = agg_w.get(model, cur)
        delta = sug - cur
        lines.append(f"  {model:<18s} {cur:>7.2f}x {sug:>9.3f}x {delta:>+7.3f}")

    lines.append(sep)
    lines.append("")

    return "\n".join(lines)


def generate_corrections_report(
    records: Optional[List[dict]] = None,
) -> str:
    """
    Generate a concise corrections table for all model+city combos.

    Returns:
        Multi-line string with correction values.
    """
    if records is None:
        records = _load_records()

    lines: List[str] = []
    lines.append("")
    lines.append("=" * 70)
    lines.append("  BIAS CORRECTIONS (add to model forecast)")
    lines.append("=" * 70)
    lines.append(f"  {'Model':<18s} " + "".join(f"{c:>8s}" for c in CITIES) + f"{'ALL':>8s}")
    lines.append(f"  {'-' * 66}")

    for model in DEFAULT_MODEL_WEIGHTS:
        parts = [f"  {model:<18s}"]
        for city in CITIES:
            corr = get_bias_correction(model, city, records)
            parts.append(f"{corr:>+8.2f}" if corr != 0.0 else f"{'---':>8s}")
        # Aggregate correction
        all_errors: List[float] = []
        for r in records:
            pmm = r.get("per_model_means", {})
            if model in pmm and pmm[model] is not None and r.get("actual_high") is not None:
                all_errors.append(pmm[model] - r["actual_high"])
        if len(all_errors) >= MIN_RECORDS_FOR_BIAS:
            agg_corr = round(-sum(all_errors) / len(all_errors), 2)
            parts.append(f"{agg_corr:>+8.2f}")
        else:
            parts.append(f"{'---':>8s}")
        lines.append("".join(parts))

    lines.append("=" * 70)
    lines.append("  Positive = model runs cold (add to warm up)")
    lines.append("  Negative = model runs hot  (subtract to cool down)")
    lines.append("  --- = insufficient data (<5 records)")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Model Bias — Per-model, per-city error tracking"
    )
    parser.add_argument(
        "--city",
        type=str,
        default=None,
        help="Filter by city code (NYC, CHI, DEN, MIA, LAX)",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Filter by model name (ecmwf_aifs025, ecmwf_ifs025, gfs_seamless, ...)",
    )
    parser.add_argument(
        "--corrections",
        action="store_true",
        help="Print only correction values for each model+city",
    )
    args = parser.parse_args()

    records = _load_records(city_filter=args.city if not args.corrections else None)

    if not records:
        print("\nNo usable records found in backtest/daily_data.jsonl")
        print("Need records with both per_model_means and actual_high fields.")
        return

    if args.corrections:
        print(generate_corrections_report(records))
    else:
        print(generate_bias_report(records, city_filter=args.city, model_filter=args.model))


if __name__ == "__main__":
    main()
