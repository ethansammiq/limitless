#!/usr/bin/env python3
"""
CALIBRATION ANALYZER — Prediction accuracy and model calibration engine.

Answers the fundamental question: "When the system says confidence 70,
does the prediction actually come true 70% of the time?"

Reads from backtest/calibration_log.jsonl and produces:
  1. Calibration curve (confidence bins vs actual accuracy)
  2. Per-model accuracy report (MAE, bias, RMSE by model family)
  3. Optimal model weight suggestions (inverse-MAE weighting)
  4. Threshold analysis (simulated P&L at various confidence/score gates)
  5. Sigma-accuracy relationship (does tighter spread = better predictions?)
  6. Full human-readable report

Usage:
  python3 calibration_analyzer.py                    # Full report, all data
  python3 calibration_analyzer.py --city NYC         # Filter by city
  python3 calibration_analyzer.py --days 30          # Last N days only
  python3 calibration_analyzer.py --output report.txt  # Save to file
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from log_setup import get_logger

logger = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent
BACKTEST_DIR = PROJECT_ROOT / "backtest"
CALIBRATION_LOG = BACKTEST_DIR / "calibration_log.jsonl"

# Canonical model weights (single source of truth in config.py)
from config import DEFAULT_MODEL_WEIGHTS as CURRENT_MODEL_WEIGHTS

CURRENT_TOTAL_WEIGHT = sum(CURRENT_MODEL_WEIGHTS.values())

ALL_MODELS = list(CURRENT_MODEL_WEIGHTS.keys())


# ---------------------------------------------------------------------------
# Data Loading
# ---------------------------------------------------------------------------

def load_calibration_records(
    city: Optional[str] = None,
    days: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Load calibration records from backtest/calibration_log.jsonl.

    Parameters
    ----------
    city : str, optional
        Filter to a single city code (e.g. "NYC").
    days : int, optional
        Only include records from the last N days.

    Returns
    -------
    List of record dicts, deduplicated by (date, city).
    """
    if not CALIBRATION_LOG.exists():
        logger.warning("Calibration log not found: %s", CALIBRATION_LOG)
        return []

    cutoff_date: Optional[str] = None
    if days is not None:
        cutoff_dt = datetime.now(timezone.utc).date() - timedelta(days=days)
        cutoff_date = cutoff_dt.isoformat()

    records: List[Dict[str, Any]] = []
    with open(CALIBRATION_LOG) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Must have actual outcome to be useful for calibration
            if r.get("actual_high") is None:
                continue

            # City filter
            if city and r.get("city", "").upper() != city.upper():
                continue

            # Date filter
            if cutoff_date and r.get("date", "") < cutoff_date:
                continue

            records.append(r)

    # Deduplicate by (date, city) — keep the last entry if duplicates exist
    seen: Dict[tuple, int] = {}
    for idx, r in enumerate(records):
        key = (r.get("date"), r.get("city"))
        seen[key] = idx

    unique = [records[i] for i in sorted(seen.values())]
    logger.info(
        "Loaded %d calibration records (%d unique city-days)",
        len(records), len(unique),
    )
    return unique


# ---------------------------------------------------------------------------
# 1. Calibration Curve
# ---------------------------------------------------------------------------

def calibration_curve(
    records: List[Dict[str, Any]],
    bin_size: int = 10,
) -> List[Dict[str, Any]]:
    """Group records by confidence score bins and compute accuracy per bin.

    Parameters
    ----------
    records : list of dicts
        Calibration records with 'confidence_score' and 'prediction_correct'.
    bin_size : int
        Width of each confidence bin (default 10).

    Returns
    -------
    List of dicts: [{"bin": "70-79", "count": 15, "accuracy": 0.60,
                     "avg_confidence": 74.5}]
    """
    bins: Dict[int, List[Dict[str, Any]]] = {}

    for r in records:
        conf = r.get("confidence_score")
        correct = r.get("prediction_correct")
        if conf is None or correct is None:
            continue

        # Determine bin lower bound (e.g. conf=75, bin_size=10 -> bin_start=70)
        bin_start = int(conf // bin_size) * bin_size
        bins.setdefault(bin_start, []).append(r)

    result: List[Dict[str, Any]] = []
    for bin_start in sorted(bins.keys()):
        group = bins[bin_start]
        count = len(group)
        correct_count = sum(1 for r in group if r.get("prediction_correct"))
        accuracy = correct_count / count if count > 0 else 0.0
        avg_conf = sum(r.get("confidence_score", 0) for r in group) / count

        bin_end = bin_start + bin_size - 1
        # Clamp top bin label to 100
        if bin_end >= 100:
            bin_end = 100
        bin_label = f"{bin_start}-{bin_end}"

        result.append({
            "bin": bin_label,
            "count": count,
            "accuracy": round(accuracy, 4),
            "avg_confidence": round(avg_conf, 1),
        })

    return result


# ---------------------------------------------------------------------------
# 2. Model Accuracy Report
# ---------------------------------------------------------------------------

def model_accuracy_report(
    records: List[Dict[str, Any]],
    city: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    """Compute per-model MAE, bias, RMSE, and best/worst city.

    Parameters
    ----------
    records : list of dicts
        Must contain 'model_errors' and 'actual_high'.
    city : str, optional
        Filter to a single city.

    Returns
    -------
    Dict keyed by model name, each with:
        mae, bias, rmse, count, best_city, worst_city
    """
    filtered = records
    if city:
        filtered = [r for r in records if r.get("city", "").upper() == city.upper()]

    # Collect per-model signed errors
    model_errors: Dict[str, List[float]] = {m: [] for m in ALL_MODELS}
    model_city_errors: Dict[str, Dict[str, List[float]]] = {
        m: {} for m in ALL_MODELS
    }

    for r in filtered:
        errors = r.get("model_errors", {})
        city_code = r.get("city", "UNK")

        for model in ALL_MODELS:
            err = errors.get(model)
            if err is not None:
                model_errors[model].append(err)
                model_city_errors[model].setdefault(city_code, []).append(err)

    report: Dict[str, Dict[str, Any]] = {}

    for model in ALL_MODELS:
        errs = model_errors[model]
        if not errs:
            report[model] = {
                "mae": None, "bias": None, "rmse": None, "count": 0,
                "best_city": None, "worst_city": None,
            }
            continue

        abs_errs = [abs(e) for e in errs]
        mae = sum(abs_errs) / len(abs_errs)
        bias = sum(errs) / len(errs)
        rmse = math.sqrt(sum(e * e for e in errs) / len(errs))

        # Best/worst city by MAE
        city_maes: Dict[str, float] = {}
        for c, c_errs in model_city_errors[model].items():
            if c_errs:
                city_maes[c] = sum(abs(e) for e in c_errs) / len(c_errs)

        best_city = min(city_maes, key=city_maes.get) if city_maes else None
        worst_city = max(city_maes, key=city_maes.get) if city_maes else None

        report[model] = {
            "mae": round(mae, 2),
            "bias": round(bias, 2),
            "rmse": round(rmse, 2),
            "count": len(errs),
            "best_city": best_city,
            "worst_city": worst_city,
        }

    return report


# ---------------------------------------------------------------------------
# 3. Optimal Weights
# ---------------------------------------------------------------------------

def optimal_weights(
    records: List[Dict[str, Any]],
) -> Dict[str, float]:
    """Suggest model weights inversely proportional to MAE.

    Normalized so the total sums to ~5.25 (matching current weight total).
    Requires at least 5 data points per model to produce a suggestion.

    Returns
    -------
    Dict like {"ecmwf_aifs025": 1.35, ...}. Empty if insufficient data.
    """
    report = model_accuracy_report(records)

    model_mae: Dict[str, float] = {}
    for model in ALL_MODELS:
        info = report.get(model, {})
        mae = info.get("mae")
        count = info.get("count", 0)
        if mae is not None and mae > 0 and count >= 5:
            model_mae[model] = mae

    if len(model_mae) < 3:
        logger.warning(
            "Insufficient model data for weight optimization (%d models with data)",
            len(model_mae),
        )
        return {}

    # Inverse-MAE: lower error -> higher weight
    inv_mae = {m: 1.0 / mae for m, mae in model_mae.items()}
    inv_sum = sum(inv_mae.values())

    if inv_sum == 0:
        return {}

    # Normalize to target total
    suggested: Dict[str, float] = {}
    for model in ALL_MODELS:
        if model in inv_mae:
            raw = (inv_mae[model] / inv_sum) * CURRENT_TOTAL_WEIGHT
            suggested[model] = round(raw, 2)
        else:
            # Fall back to current default if not enough data
            suggested[model] = CURRENT_MODEL_WEIGHTS[model]

    return suggested


# ---------------------------------------------------------------------------
# 4. Threshold Analysis
# ---------------------------------------------------------------------------

def threshold_analysis(
    records: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Simulate different confidence and trade score thresholds.

    Tests confidence thresholds (50..90 by 5) crossed with trade score
    thresholds (0.40..0.60 by 0.05). For each combo: trade count, win rate,
    average edge.

    Returns
    -------
    List of dicts with: confidence_threshold, score_threshold,
    trade_count, win_rate, avg_edge_cents.
    """
    confidence_thresholds = [50, 55, 60, 65, 70, 75, 80, 85, 90]
    score_thresholds = [0.40, 0.45, 0.50, 0.55, 0.60]

    results: List[Dict[str, Any]] = []

    for conf_thresh in confidence_thresholds:
        for score_thresh in score_thresholds:
            trades: List[Dict[str, Any]] = []
            wins = 0

            for r in records:
                conf = r.get("confidence_score")
                score = r.get("trade_score")
                correct = r.get("prediction_correct")
                edge = r.get("best_edge_cents")

                if conf is None or correct is None:
                    continue

                # Apply confidence gate
                if conf < conf_thresh:
                    continue

                # Apply trade score gate if present
                if score is not None and score < score_thresh:
                    continue

                trades.append(r)
                if correct:
                    wins += 1

            trade_count = len(trades)
            if trade_count == 0:
                results.append({
                    "confidence_threshold": conf_thresh,
                    "score_threshold": score_thresh,
                    "trade_count": 0,
                    "win_rate": 0.0,
                    "avg_edge_cents": 0.0,
                })
                continue

            win_rate = wins / trade_count
            edges = [
                r.get("best_edge_cents", 0) for r in trades
                if r.get("best_edge_cents") is not None
            ]
            avg_edge = sum(edges) / len(edges) if edges else 0.0

            results.append({
                "confidence_threshold": conf_thresh,
                "score_threshold": score_thresh,
                "trade_count": trade_count,
                "win_rate": round(win_rate, 4),
                "avg_edge_cents": round(avg_edge, 1),
            })

    return results


# ---------------------------------------------------------------------------
# 5. Sigma-Accuracy Relationship
# ---------------------------------------------------------------------------

def sigma_accuracy(
    records: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Group records by ensemble standard deviation and measure accuracy.

    Tests hypothesis: tighter ensemble spread -> better predictions.

    Sigma ranges: <1.0, 1.0-1.5, 1.5-2.0, 2.0-3.0, 3.0+

    Returns
    -------
    List of dicts with: sigma_range, count, accuracy, avg_mae.
    """
    sigma_ranges = [
        (0.0, 1.0, "<1.0"),
        (1.0, 1.5, "1.0-1.5"),
        (1.5, 2.0, "1.5-2.0"),
        (2.0, 3.0, "2.0-3.0"),
        (3.0, float("inf"), "3.0+"),
    ]

    buckets: Dict[str, List[Dict[str, Any]]] = {label: [] for _, _, label in sigma_ranges}

    for r in records:
        std = r.get("ensemble_std")
        if std is None:
            continue

        for low, high, label in sigma_ranges:
            if low <= std < high:
                buckets[label].append(r)
                break

    results: List[Dict[str, Any]] = []

    for _, _, label in sigma_ranges:
        group = buckets[label]
        count = len(group)

        if count == 0:
            results.append({
                "sigma_range": label,
                "count": 0,
                "accuracy": 0.0,
                "avg_mae": 0.0,
            })
            continue

        correct_count = sum(
            1 for r in group if r.get("prediction_correct") is True
        )
        accuracy = correct_count / count

        # Average MAE across all model errors for records in this bucket
        all_maes: List[float] = []
        for r in group:
            actual = r.get("actual_high")
            ens_mean = r.get("ensemble_mean")
            if actual is not None and ens_mean is not None:
                all_maes.append(abs(ens_mean - actual))

        avg_mae = sum(all_maes) / len(all_maes) if all_maes else 0.0

        results.append({
            "sigma_range": label,
            "count": count,
            "accuracy": round(accuracy, 4),
            "avg_mae": round(avg_mae, 2),
        })

    return results


# ---------------------------------------------------------------------------
# 6. Report Generation
# ---------------------------------------------------------------------------

def _fmt_pct(value: float) -> str:
    """Format a ratio as a percentage string."""
    return f"{value * 100:.1f}%"


def _fmt_table(headers: List[str], rows: List[List[str]], col_widths: Optional[List[int]] = None) -> str:
    """Build a simple fixed-width text table."""
    if col_widths is None:
        col_widths = [
            max(len(str(h)), max((len(str(cell)) for cell in col), default=0))
            for h, col in zip(headers, zip(*rows))
        ] if rows else [len(h) for h in headers]

    lines: List[str] = []

    # Header
    header_line = "  ".join(h.ljust(w) for h, w in zip(headers, col_widths))
    lines.append(f"  {header_line}")
    lines.append(f"  {'  '.join('-' * w for w in col_widths)}")

    # Rows
    for row in rows:
        row_line = "  ".join(str(cell).ljust(w) for cell, w in zip(row, col_widths))
        lines.append(f"  {row_line}")

    return "\n".join(lines)


def generate_report(
    records: List[Dict[str, Any]],
    output_path: Optional[str] = None,
) -> str:
    """Generate a comprehensive human-readable calibration report.

    Parameters
    ----------
    records : list of dicts
        Calibration records.
    output_path : str, optional
        If given, writes report to this file path.

    Returns
    -------
    The report as a string.
    """
    lines: List[str] = []

    def section(title: str) -> None:
        lines.append("")
        lines.append(f"  {title}")
        lines.append(f"  {'=' * len(title)}")

    # Header
    n_records = len(records)
    dates = sorted(set(r.get("date", "") for r in records))
    cities = sorted(set(r.get("city", "") for r in records))
    n_days = len(dates)

    lines.append("")
    lines.append("=" * 70)
    lines.append("  CALIBRATION ANALYSIS REPORT")
    lines.append("=" * 70)
    lines.append(f"  Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    if dates:
        lines.append(f"  Period:    {dates[0]} to {dates[-1]} ({n_days} days)")
    lines.append(f"  Cities:    {', '.join(cities) if cities else 'None'}")
    lines.append(f"  Records:   {n_records}")

    if n_records == 0:
        lines.append("")
        lines.append("  No calibration data available.")
        lines.append("  Run the scanner with calibration logging enabled to collect data.")
        lines.append("=" * 70)
        report = "\n".join(lines) + "\n"
        if output_path:
            Path(output_path).write_text(report)
            logger.info("Report saved to %s", output_path)
        return report

    # ── 1. Calibration Curve ──
    section("1. CALIBRATION CURVE")
    lines.append("  (Does confidence score match actual accuracy?)")
    lines.append("")

    curve = calibration_curve(records)
    if curve:
        headers = ["Bin", "Count", "Accuracy", "Avg Conf", "Delta"]
        rows = []
        for entry in curve:
            expected = entry["avg_confidence"] / 100.0
            delta = entry["accuracy"] - expected
            delta_str = f"{delta:+.1%}"
            if delta > 0.10:
                delta_str += " (UNDER-CONFIDENT)"
            elif delta < -0.10:
                delta_str += " (OVER-CONFIDENT)"

            rows.append([
                entry["bin"],
                str(entry["count"]),
                _fmt_pct(entry["accuracy"]),
                f"{entry['avg_confidence']:.0f}",
                delta_str,
            ])
        lines.append(_fmt_table(headers, rows, [8, 6, 10, 10, 30]))

        # Key finding
        total_correct = sum(e["count"] * e["accuracy"] for e in curve)
        total_count = sum(e["count"] for e in curve)
        overall_acc = total_correct / total_count if total_count > 0 else 0
        lines.append("")
        lines.append(f"  Overall accuracy: {_fmt_pct(overall_acc)} across {total_count} predictions")
    else:
        lines.append("  No records with confidence scores found.")

    # ── 2. Model Accuracy ──
    section("2. MODEL ACCURACY")
    lines.append("  (Per-model forecast error analysis)")
    lines.append("")

    mar = model_accuracy_report(records)
    if mar:
        headers = ["Model", "MAE", "Bias", "RMSE", "N", "Best", "Worst"]
        rows = []
        # Sort by MAE
        sorted_models = sorted(
            ALL_MODELS,
            key=lambda m: mar[m].get("mae") or 999,
        )
        for model in sorted_models:
            info = mar[model]
            if info["mae"] is None:
                rows.append([model, "N/A", "N/A", "N/A", "0", "-", "-"])
            else:
                bias_str = f"{info['bias']:+.2f}F"
                if info["bias"] > 0.5:
                    bias_str += " (HOT)"
                elif info["bias"] < -0.5:
                    bias_str += " (COLD)"
                rows.append([
                    model,
                    f"{info['mae']:.2f}F",
                    bias_str,
                    f"{info['rmse']:.2f}F",
                    str(info["count"]),
                    info["best_city"] or "-",
                    info["worst_city"] or "-",
                ])
        lines.append(_fmt_table(headers, rows, [20, 8, 16, 8, 6, 6, 6]))
    else:
        lines.append("  No model error data available.")

    # ── 3. Optimal Weights ──
    section("3. OPTIMAL MODEL WEIGHTS")
    lines.append(f"  (Inverse-MAE weighting, normalized to sum={CURRENT_TOTAL_WEIGHT:.2f})")
    lines.append("")

    suggested = optimal_weights(records)
    if suggested:
        headers = ["Model", "Current", "Suggested", "Delta"]
        rows = []
        for model in ALL_MODELS:
            cur = CURRENT_MODEL_WEIGHTS[model]
            sug = suggested.get(model, cur)
            delta = sug - cur
            rows.append([
                model,
                f"{cur:.2f}x",
                f"{sug:.2f}x",
                f"{delta:+.2f}",
            ])
        lines.append(_fmt_table(headers, rows, [20, 10, 10, 10]))

        cur_total = sum(CURRENT_MODEL_WEIGHTS.values())
        sug_total = sum(suggested.values())
        lines.append(f"\n  Current total: {cur_total:.2f}  |  Suggested total: {sug_total:.2f}")
    else:
        lines.append("  Insufficient data for weight optimization (need 5+ records per model).")

    # ── 4. Threshold Analysis ──
    section("4. THRESHOLD ANALYSIS")
    lines.append("  (Win rate and trade count at various confidence/score gates)")
    lines.append("")

    thresh_results = threshold_analysis(records)
    if thresh_results:
        # Show a compact view: one row per confidence threshold at the
        # default score threshold (0.55), plus the full matrix for reference
        lines.append("  Confidence threshold sweep (trade_score >= 0.55):")
        lines.append("")

        headers = ["Conf >=", "Trades", "Win Rate", "Avg Edge"]
        rows = []
        for t in thresh_results:
            if abs(t["score_threshold"] - 0.55) < 0.001:
                rows.append([
                    str(t["confidence_threshold"]),
                    str(t["trade_count"]),
                    _fmt_pct(t["win_rate"]) if t["trade_count"] > 0 else "-",
                    f"{t['avg_edge_cents']:.1f}c" if t["trade_count"] > 0 else "-",
                ])
        lines.append(_fmt_table(headers, rows, [8, 8, 10, 10]))

        # Compact trade score sweep at confidence 70
        lines.append("")
        lines.append("  Trade score sweep (confidence >= 70):")
        lines.append("")
        headers = ["Score >=", "Trades", "Win Rate", "Avg Edge"]
        rows = []
        for t in thresh_results:
            if t["confidence_threshold"] == 70:
                rows.append([
                    f"{t['score_threshold']:.2f}",
                    str(t["trade_count"]),
                    _fmt_pct(t["win_rate"]) if t["trade_count"] > 0 else "-",
                    f"{t['avg_edge_cents']:.1f}c" if t["trade_count"] > 0 else "-",
                ])
        lines.append(_fmt_table(headers, rows, [10, 8, 10, 10]))

        # Find the sweet spot
        best = max(
            (t for t in thresh_results if t["trade_count"] >= 3),
            key=lambda t: t["win_rate"] * math.log(t["trade_count"] + 1),
            default=None,
        )
        if best:
            lines.append("")
            lines.append(
                f"  Best combo: conf>={best['confidence_threshold']} + "
                f"score>={best['score_threshold']:.2f} "
                f"({best['trade_count']} trades, {_fmt_pct(best['win_rate'])} win rate)"
            )
    else:
        lines.append("  No data for threshold analysis.")

    # ── 5. Sigma-Accuracy ──
    section("5. ENSEMBLE SPREAD vs ACCURACY")
    lines.append("  (Does tighter sigma predict better?)")
    lines.append("")

    sigma_results = sigma_accuracy(records)
    if sigma_results and any(s["count"] > 0 for s in sigma_results):
        headers = ["Sigma", "Count", "Accuracy", "Avg MAE"]
        rows = []
        for s in sigma_results:
            if s["count"] > 0:
                rows.append([
                    s["sigma_range"],
                    str(s["count"]),
                    _fmt_pct(s["accuracy"]),
                    f"{s['avg_mae']:.2f}F",
                ])
            else:
                rows.append([s["sigma_range"], "0", "-", "-"])
        lines.append(_fmt_table(headers, rows, [10, 8, 10, 10]))

        # Check hypothesis
        tight = [s for s in sigma_results if s["sigma_range"] in ("<1.0", "1.0-1.5") and s["count"] > 0]
        wide = [s for s in sigma_results if s["sigma_range"] in ("2.0-3.0", "3.0+") and s["count"] > 0]

        if tight and wide:
            tight_acc = sum(s["accuracy"] * s["count"] for s in tight) / sum(s["count"] for s in tight)
            wide_acc = sum(s["accuracy"] * s["count"] for s in wide) / sum(s["count"] for s in wide)
            lines.append("")
            if tight_acc > wide_acc:
                lines.append(
                    f"  Hypothesis CONFIRMED: tight sigma ({_fmt_pct(tight_acc)}) "
                    f"outperforms wide ({_fmt_pct(wide_acc)})"
                )
            else:
                lines.append(
                    f"  Hypothesis REJECTED: wide sigma ({_fmt_pct(wide_acc)}) "
                    f"matches or beats tight ({_fmt_pct(tight_acc)})"
                )
    else:
        lines.append("  No ensemble spread data available.")

    # ── 6. Key Recommendations ──
    section("6. KEY RECOMMENDATIONS")
    lines.append("")
    recommendations = _derive_recommendations(records, curve, mar, suggested, sigma_results)
    for i, rec in enumerate(recommendations, 1):
        lines.append(f"  {i}. {rec}")

    lines.append("")
    lines.append("=" * 70)
    lines.append("")

    report = "\n".join(lines)

    if output_path:
        Path(output_path).write_text(report)
        logger.info("Report saved to %s", output_path)

    return report


def _derive_recommendations(
    records: List[Dict[str, Any]],
    curve: List[Dict[str, Any]],
    mar: Dict[str, Dict[str, Any]],
    suggested: Dict[str, float],
    sigma_results: List[Dict[str, Any]],
) -> List[str]:
    """Derive actionable recommendations from analysis results."""
    recs: List[str] = []

    # Sample size warning
    n = len(records)
    if n < 30:
        recs.append(
            f"SAMPLE SIZE: Only {n} records. Collect 30+ days for "
            f"statistically meaningful conclusions."
        )

    # Calibration curve insights
    if curve:
        high_bins = [e for e in curve if e["avg_confidence"] >= 80 and e["count"] >= 3]
        for hb in high_bins:
            expected = hb["avg_confidence"] / 100.0
            delta = hb["accuracy"] - expected
            if delta < -0.15:
                recs.append(
                    f"OVER-CONFIDENT in bin {hb['bin']}: accuracy {_fmt_pct(hb['accuracy'])} "
                    f"vs expected {_fmt_pct(expected)}. Consider raising confidence threshold."
                )
            elif delta > 0.15:
                recs.append(
                    f"UNDER-CONFIDENT in bin {hb['bin']}: accuracy {_fmt_pct(hb['accuracy'])} "
                    f"vs expected {_fmt_pct(expected)}. Could be more aggressive here."
                )

    # Model weight changes
    if suggested:
        big_changes = []
        for model in ALL_MODELS:
            cur = CURRENT_MODEL_WEIGHTS[model]
            sug = suggested.get(model, cur)
            if abs(sug - cur) > 0.15:
                direction = "increase" if sug > cur else "decrease"
                big_changes.append(f"{model}: {direction} from {cur:.2f} to {sug:.2f}")
        if big_changes:
            recs.append(
                "WEIGHT ADJUSTMENT: Consider updating model weights: "
                + "; ".join(big_changes)
            )

    # Model bias
    if mar:
        for model in ALL_MODELS:
            info = mar.get(model, {})
            bias = info.get("bias")
            if bias is not None and abs(bias) > 1.0:
                direction = "HOT" if bias > 0 else "COLD"
                recs.append(
                    f"MODEL BIAS: {model} runs {direction} by {abs(bias):.1f}F. "
                    f"Consider a bias correction term."
                )

    # Sigma finding
    if sigma_results:
        tight = [s for s in sigma_results if s["sigma_range"] == "<1.0" and s["count"] >= 3]
        if tight and tight[0]["accuracy"] > 0.70:
            recs.append(
                f"HIGH-SIGMA FILTER: Predictions with sigma < 1.0 hit "
                f"{_fmt_pct(tight[0]['accuracy'])}. Focus on tight-spread setups."
            )

    if not recs:
        recs.append("Insufficient data for actionable recommendations. Keep collecting.")

    return recs


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Calibration Analyzer -- prediction accuracy and model calibration",
    )
    parser.add_argument(
        "--city",
        type=str,
        default=None,
        help="Filter by city code (NYC, CHI, DEN, MIA, LAX)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Only include the last N days of data",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Save report to this file path",
    )
    args = parser.parse_args()

    records = load_calibration_records(city=args.city, days=args.days)
    report = generate_report(records, output_path=args.output)

    if not args.output:
        print(report)


if __name__ == "__main__":
    main()
