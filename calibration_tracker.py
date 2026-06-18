#!/usr/bin/env python3
"""
CALIBRATION TRACKER — Prediction-to-outcome loop for model calibration.

Captures enriched snapshots at scan time and links them to settlement outcomes.
This closes the feedback loop between forecasts and reality, enabling:
  - Per-model bias tracking (which models run hot/cold?)
  - Confidence calibration (does 90+ confidence really hit 90%+ of the time?)
  - KDE probability accuracy (do 40% KDE brackets hit ~40%?)
  - Strategy effectiveness (which alpha strategies actually add edge?)

Data flow:
  1. auto_scan.py calls save_calibration_record() after each city scan
  2. backtest_collector.py calls enrich_with_settlement() after settlement
  3. calibration.py reads load_calibration_records() for weight optimization

Storage:
  - Per-record JSON:  backtest/calibration/YYYY-MM-DD_CITY.json
  - Append-only log:  backtest/calibration_log.jsonl  (full timeseries)

Usage:
    from calibration_tracker import save_calibration_record, enrich_with_settlement

    # At scan time:
    save_calibration_record("NYC", scan_result, opps, trade_scores, hours_to_settle)

    # After settlement:
    enrich_with_settlement("2026-02-14", "NYC", actual_high=43.0)

    # For analysis:
    records = load_calibration_records(city_key="NYC", start_date="2026-01-01")
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from log_setup import get_logger

logger = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent
CALIBRATION_DIR = PROJECT_ROOT / "backtest" / "calibration"
CALIBRATION_LOG = PROJECT_ROOT / "backtest" / "calibration_log.jsonl"


# ─── Data Structure ──────────────────────────────────────────────────────────


@dataclass
class CalibrationRecord:
    """Rich snapshot linking a forecast to its eventual outcome.

    Fields populated at scan time are always present.
    Fields populated post-settlement are Optional (None until enriched).
    """

    # ── Identity ──
    date: str                           # YYYY-MM-DD (market date)
    city: str                           # City key (NYC, CHI, DEN, MIA, LAX)
    scan_time: str                      # ISO 8601 timestamp of the scan
    lead_time_hours: float              # Hours to settlement at scan time

    # ── Ensemble statistics ──
    mean: float                         # Weighted ensemble mean (°F)
    std: float                          # Weighted ensemble std dev
    total_count: int                    # Total ensemble members (typically 194)
    kde_bandwidth: float                # KDE bandwidth used (Silverman)
    is_bimodal: bool                    # True if ensemble splits into 2 clusters

    # ── Percentiles ──
    p10: float
    p25: float
    p50: float
    p75: float
    p90: float

    # ── Per-model breakdowns ──
    per_model_means: Dict[str, float]   # {model_name: mean_temp}
    per_model_stds: Dict[str, float]    # {model_name: std_dev}
    per_model_counts: Dict[str, int]    # {model_name: member_count}

    # ── NWS data ──
    nws_forecast_high: float            # Official NWS point forecast
    nws_physics_high: float             # Physics-adjusted high (after penalties)
    nws_current_temp: float             # Observed temp at scan time
    nws_wind_penalty: float             # Wind mixing penalty applied (°F)
    nws_wet_bulb_penalty: float         # Wet bulb penalty applied (°F)
    nws_temp_trend: str                 # "on_track", "running_hot", "running_cold"

    # ── Market data ──
    bracket_prices: Dict[str, Dict[str, Any]]  # {ticker: {yes_bid, yes_ask, volume}}

    # ── Best opportunity metrics ──
    confidence_score: int               # 0-100, best opp's confidence
    trade_score: float                  # 0.0-1.0, best opp's trade score
    trade_score_tradeable: bool         # Whether trade_score passed threshold
    best_bracket: str                   # e.g. "36-37"
    best_kde_prob: float                # KDE probability for best bracket
    best_edge_cents: float              # Edge after fees (cents)
    best_side: str                      # "yes" or "no"
    strategies_active: List[str]        # e.g. ["B:WIND", "E:NWS_DIVERGE"]

    # ── Model agreement ──
    aifs_ifs_divergence_f: float        # |AIFS mean - IFS mean| in °F

    # ── Post-settlement (filled by enrich_with_settlement) ──
    actual_high: Optional[float] = None
    actual_bracket: Optional[str] = None
    prediction_correct: Optional[bool] = None
    model_errors: Optional[Dict[str, float]] = None  # {model: mean - actual}


# ─── Helper Functions ─────────────────────────────────────────────────────────


def _temp_to_bracket(temp: float) -> str:
    """Convert a temperature to its Kalshi bracket display string.

    Kalshi uses 2-degree brackets: "38-39", "40-41", etc.
    Internally, edge_scanner_v2 stores these as [low, low+2) — exclusive
    upper bound — but the display format uses "low-(low+1)".

    The bracket containing temp T has low = floor(T) rounded down to
    nearest even integer.
    """
    floor_t = int(temp) if temp >= 0 else int(temp) - 1
    low = floor_t if floor_t % 2 == 0 else floor_t - 1
    high = low + 1
    return f"{low}-{high}"


def _atomic_write(path: Path, content: str) -> None:
    """Write content to path atomically (write to tmp, then rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=path.parent, prefix=".tmp_", suffix=path.suffix
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, path)
    except Exception:
        # Clean up temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _append_jsonl(path: Path, record: dict) -> None:
    """Append a single JSON record to a JSONL file (atomic append)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, default=str, separators=(",", ":")) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)


def _best_opportunity(opps: list) -> Optional[Any]:
    """Select the best opportunity from a list.

    Best = highest trade_score if available, else highest confidence_score.
    Filters to only opportunities with positive edge.
    """
    if not opps:
        return None

    viable = [o for o in opps if getattr(o, "edge_after_fees", 0) > 0]
    if not viable:
        # Fall back to all opps if none have positive edge
        viable = opps

    # Prefer trade_score, fall back to confidence_score
    def sort_key(o):
        ts = getattr(o, "trade_score", 0) or 0
        cs = getattr(o, "confidence_score", 0) or 0
        return (ts, cs)

    return max(viable, key=sort_key)


# ─── Core API ─────────────────────────────────────────────────────────────────


def save_calibration_record(
    city_key: str,
    scan_result: dict,
    opps: list,
    trade_scores: list,
    hours_to_settlement: float,
) -> Optional[CalibrationRecord]:
    """Save a calibration record from a city scan.

    Parameters
    ----------
    city_key : str
        City code (NYC, CHI, DEN, MIA, LAX).
    scan_result : dict
        The ensemble snapshot dict with keys: mean, std, total_count,
        kde_bandwidth, is_bimodal, p10-p90, per_model_means/stds/counts,
        nws_forecast_high, nws_physics_high, nws_current_temp,
        nws_wind_penalty, nws_wet_bulb_penalty, nws_temp_trend,
        bracket_prices.
    opps : list
        List of Opportunity objects from analyze_opportunities_v2.
    trade_scores : list
        List of TradeScore objects (parallel to opps). May be empty if
        trade scoring was skipped.
    hours_to_settlement : float
        Hours until market settlement at scan time.

    Returns
    -------
    CalibrationRecord or None if save failed.
    """
    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")

    # Find the best opportunity
    best_opp = _best_opportunity(opps)

    # Best opp metrics
    if best_opp is not None:
        # Display format: "38-39". Note: opp.high is exclusive (parse_bracket_range
        # adds +1), so subtract 1 for the display string.
        bracket_high_display = int(best_opp.high) - 1
        best_bracket = f"{int(best_opp.low)}-{bracket_high_display}"
        best_kde_prob = getattr(best_opp, "kde_prob", 0.0)
        best_edge_cents = getattr(best_opp, "edge_after_fees", 0.0) * 100.0
        best_side = getattr(best_opp, "side", "yes")
        confidence_score = int(getattr(best_opp, "confidence_score", 0))
        best_trade_score = getattr(best_opp, "trade_score", 0.0)
        ts_components = getattr(best_opp, "trade_score_components", {})
        trade_score_tradeable = ts_components.get("tradeable", False)
        strategies_active = getattr(best_opp, "strategies", [])
    else:
        best_bracket = ""
        best_kde_prob = 0.0
        best_edge_cents = 0.0
        best_side = ""
        confidence_score = 0
        best_trade_score = 0.0
        trade_score_tradeable = False
        strategies_active = []

    # Per-model data
    per_model_means = scan_result.get("per_model_means", {})
    per_model_stds = scan_result.get("per_model_stds", {})
    per_model_counts = scan_result.get("per_model_counts", {})

    # AIFS vs IFS divergence
    aifs_mean = per_model_means.get("ecmwf_aifs025")
    ifs_mean = per_model_means.get("ecmwf_ifs025")
    if aifs_mean is not None and ifs_mean is not None:
        aifs_ifs_divergence = round(abs(aifs_mean - ifs_mean), 2)
    else:
        aifs_ifs_divergence = 0.0

    record = CalibrationRecord(
        date=today_str,
        city=city_key,
        scan_time=now.isoformat() + "Z",
        lead_time_hours=round(hours_to_settlement, 2),
        # Ensemble
        mean=scan_result.get("mean", 0.0),
        std=scan_result.get("std", 0.0),
        total_count=scan_result.get("total_count", 0),
        kde_bandwidth=scan_result.get("kde_bandwidth", 0.0),
        is_bimodal=scan_result.get("is_bimodal", False),
        # Percentiles
        p10=scan_result.get("p10", 0.0),
        p25=scan_result.get("p25", 0.0),
        p50=scan_result.get("p50", 0.0),
        p75=scan_result.get("p75", 0.0),
        p90=scan_result.get("p90", 0.0),
        # Per-model
        per_model_means=per_model_means,
        per_model_stds=per_model_stds,
        per_model_counts=per_model_counts,
        # NWS
        nws_forecast_high=scan_result.get("nws_forecast_high", 0.0),
        nws_physics_high=scan_result.get("nws_physics_high", 0.0),
        nws_current_temp=scan_result.get("nws_current_temp", 0.0),
        nws_wind_penalty=scan_result.get("nws_wind_penalty", 0.0),
        nws_wet_bulb_penalty=scan_result.get("nws_wet_bulb_penalty", 0.0),
        nws_temp_trend=scan_result.get("nws_temp_trend", ""),
        # Market
        bracket_prices=scan_result.get("bracket_prices", {}),
        # Best opp
        confidence_score=confidence_score,
        trade_score=round(best_trade_score, 4),
        trade_score_tradeable=trade_score_tradeable,
        best_bracket=best_bracket,
        best_kde_prob=round(best_kde_prob, 4),
        best_edge_cents=round(best_edge_cents, 2),
        best_side=best_side,
        strategies_active=strategies_active,
        aifs_ifs_divergence_f=aifs_ifs_divergence,
    )

    # Serialize
    record_dict = asdict(record)

    try:
        # Save individual JSON file
        record_path = CALIBRATION_DIR / f"{today_str}_{city_key}.json"
        _atomic_write(record_path, json.dumps(record_dict, indent=2, default=str))

        # Append to JSONL log
        _append_jsonl(CALIBRATION_LOG, record_dict)

        logger.info(
            "Saved calibration record: %s_%s (conf=%d, ts=%.3f, bracket=%s)",
            today_str, city_key, confidence_score, best_trade_score, best_bracket,
        )
        return record

    except Exception as e:
        logger.error("Failed to save calibration record for %s: %s", city_key, e)
        return None


def enrich_with_settlement(
    date_str: str,
    city_key: str,
    actual_high: float,
) -> Optional[dict]:
    """Enrich a calibration record with post-settlement actual data.

    Parameters
    ----------
    date_str : str
        Date in YYYY-MM-DD format.
    city_key : str
        City code (NYC, CHI, DEN, MIA, LAX).
    actual_high : float
        The actual recorded high temperature (°F) from settlement.

    Returns
    -------
    The enriched record dict, or None if the record was not found.
    """
    record_path = CALIBRATION_DIR / f"{date_str}_{city_key}.json"
    if not record_path.exists():
        logger.warning(
            "No calibration record found for %s_%s — cannot enrich",
            date_str, city_key,
        )
        return None

    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, Exception) as e:
        logger.error("Failed to load calibration record %s: %s", record_path.name, e)
        return None

    # Fill settlement fields
    record["actual_high"] = actual_high
    record["actual_bracket"] = _temp_to_bracket(actual_high)

    # Check if prediction was correct
    # best_bracket is display format "38-39" meaning [38, 40) in scanner
    # convention (exclusive upper bound, per parse_bracket_range adding +1).
    best_bracket = record.get("best_bracket", "")
    if best_bracket and "-" in best_bracket:
        try:
            parts = best_bracket.split("-")
            bracket_low = float(parts[0])
            # Exclusive upper: display "38-39" means temps 38.0 to 39.999...
            bracket_high_exclusive = float(parts[1]) + 1
            record["prediction_correct"] = (
                bracket_low <= actual_high < bracket_high_exclusive
            )
        except (ValueError, IndexError):
            record["prediction_correct"] = None
    else:
        record["prediction_correct"] = None

    # Compute per-model errors (model_mean - actual_high)
    per_model_means = record.get("per_model_means", {})
    model_errors = {}
    for model_name, model_mean in per_model_means.items():
        if model_mean is not None:
            model_errors[model_name] = round(model_mean - actual_high, 2)
    record["model_errors"] = model_errors

    # Save back
    try:
        _atomic_write(record_path, json.dumps(record, indent=2, default=str))
        logger.info(
            "Enriched %s_%s: actual=%.1f°F, bracket=%s, correct=%s, errors=%s",
            date_str, city_key, actual_high, record["actual_bracket"],
            record["prediction_correct"], model_errors,
        )
    except Exception as e:
        logger.error("Failed to save enriched record %s: %s", record_path.name, e)
        return None

    # Also update the JSONL log entry (append enrichment as a new line —
    # the latest entry for a date/city pair is authoritative)
    try:
        _append_jsonl(CALIBRATION_LOG, record)
    except Exception as e:
        logger.warning("Failed to append enrichment to JSONL log: %s", e)

    return record


def load_calibration_records(
    city_key: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> List[dict]:
    """Load calibration records from the JSONL log with optional filters.

    Parameters
    ----------
    city_key : str, optional
        Filter to a single city (e.g. "NYC").
    start_date : str, optional
        Include records on or after this date (YYYY-MM-DD).
    end_date : str, optional
        Include records on or before this date (YYYY-MM-DD).

    Returns
    -------
    List of record dicts, deduplicated by (date, city) keeping the latest
    entry (which has settlement enrichment if available).
    """
    if not CALIBRATION_LOG.exists():
        logger.debug("No calibration log found at %s", CALIBRATION_LOG)
        return []

    records: List[dict] = []
    try:
        with open(CALIBRATION_LOG, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Apply filters
                record_date = r.get("date", "")
                record_city = r.get("city", "")

                if city_key and record_city.upper() != city_key.upper():
                    continue
                if start_date and record_date < start_date:
                    continue
                if end_date and record_date > end_date:
                    continue

                records.append(r)
    except Exception as e:
        logger.error("Failed to read calibration log: %s", e)
        return []

    # Deduplicate by (date, city) — keep last entry (most enriched)
    seen: Dict[tuple, int] = {}
    for idx, r in enumerate(records):
        key = (r.get("date", ""), r.get("city", ""))
        seen[key] = idx

    deduped = [records[idx] for idx in sorted(seen.values())]
    return deduped
