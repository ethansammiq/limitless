#!/usr/bin/env python3
"""
BIAS COLLECTOR — Previous Runs API bias correction builder.

Uses Open-Meteo's Previous Runs API to fetch what each ensemble model
predicted 24h ago for today's high, then compares to actual settlement
temperature. Builds a rolling per-model, per-city bias correction file.

Why this matters:
  - Existing backtest_collector.py relies on local ensemble snapshots
  - If a snapshot is missing (scanner didn't run, crash, etc.), no bias data
  - This script retroactively fetches historical model predictions from the API
  - Fills gaps and provides independent validation of bias corrections

Data flow:
  1. Fetch yesterday's model run predictions for today via Previous Runs API
  2. Compare to actual settlement temperature from daily_data.jsonl
  3. Append to bias_data.jsonl: {date, city, model, predicted, actual, error}
  4. Compute rolling 14-day mean bias per (model, city)
  5. Write model_bias_corrections.json for the scanner to load

Cron setup (run after backtest_collector, ~8:30 AM ET):
  30 8 * * * cd /Users/miqadmin/Documents/limitless && python3 bias_collector.py >> /tmp/bias_collector.log 2>&1

Manual:
  python3 bias_collector.py                    # Collect today's bias data
  python3 bias_collector.py --date 2026-02-15  # Specific date
  python3 bias_collector.py --report           # Print bias correction table
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import aiohttp
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from log_setup import get_logger

logger = get_logger(__name__)

ET = ZoneInfo("America/New_York")
PROJECT_ROOT = Path(__file__).resolve().parent
BACKTEST_DIR = PROJECT_ROOT / "backtest"
DAILY_DATA_FILE = BACKTEST_DIR / "daily_data.jsonl"
BIAS_DATA_FILE = BACKTEST_DIR / "bias_data.jsonl"
CORRECTIONS_FILE = PROJECT_ROOT / "model_bias_corrections.json"

# Previous Runs API endpoint
PREV_RUNS_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"

# Models to track bias for (must match edge_scanner_v2.py ENSEMBLE_MODELS)
BIAS_MODELS = [
    "ecmwf_ifs025",
    "ecmwf_aifs025",
    "gfs_seamless",
    "icon_seamless",
    "gem_global",
    "bom_access_global_ensemble",
    "ukmo_global_ensemble_20km",
]

# Rolling window for bias correction (days)
ROLLING_WINDOW = 14

# Minimum records needed before applying a correction
MIN_RECORDS = 5

# Import cities from config
from config import STATIONS as _STATIONS

CITIES = {
    code: {"lat": s.lat, "lon": s.lon, "name": s.city_name}
    for code, s in _STATIONS.items()
}


async def fetch_previous_run_max(
    session: aiohttp.ClientSession,
    lat: float,
    lon: float,
    model: str,
    target_date: str,
) -> Optional[float]:
    """
    Fetch what a model predicted for target_date's max temp using
    the Previous Runs API (historical model runs).

    The API rejects past_days alongside start_date/end_date as mutually
    exclusive (HTTP 400) — sending all three is why this silently returned
    None for every model/city, so bias_data.jsonl never got a record. Instead
    we size past_days to reach target_date and match it in the returned series.
    Returns the predicted daily max in Fahrenheit, or None.
    """
    try:
        tgt = datetime.strptime(target_date, "%Y-%m-%d").date()
    except ValueError:
        return None
    days_back = (datetime.now(ET).date() - tgt).days
    if days_back < 0:
        return None  # future date — no prior run to evaluate
    # Window must include target_date; cap at the API's past_days ceiling.
    past_days = min(max(days_back + 1, 2), 92)

    params = {
        "latitude": lat,
        "longitude": lon,
        "models": model,
        "daily": "temperature_2m_max",
        "temperature_unit": "fahrenheit",
        "past_days": past_days,
        "forecast_days": 1,
    }

    try:
        async with session.get(
            PREV_RUNS_URL, params=params,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                logger.debug("Previous Runs API %d for %s/%s", resp.status, model, target_date)
                return None
            data = await resp.json()

        daily = data.get("daily", {})
        dates = daily.get("time", [])
        temps = daily.get("temperature_2m_max", [])

        # Find temperature for our target date
        for d, t in zip(dates, temps):
            if d == target_date and t is not None:
                return round(t, 1)

        return None

    except Exception as e:
        logger.debug("Previous Runs fetch failed %s/%s: %s", model, target_date, e)
        return None


async def collect_bias_data(target_date_str: Optional[str] = None):
    """
    Collect bias data for each model and city.

    For each (model, city):
      1. Fetch what the model predicted via Previous Runs API
      2. Load actual settlement temp from daily_data.jsonl
      3. Compute error and append to bias_data.jsonl
    """
    now = datetime.now(ET)

    if target_date_str:
        target_date = datetime.strptime(target_date_str, "%Y-%m-%d").date()
    else:
        # Default: yesterday (we want to evaluate yesterday's forecast vs reality)
        target_date = (now - timedelta(days=1)).date()

    target_str = target_date.isoformat()

    print(f"\n  BIAS COLLECTOR — {now.strftime('%I:%M %p ET')}")
    print(f"  Target date: {target_str}")
    print(f"  {'─' * 50}")

    # Load actual highs from daily_data.jsonl
    actuals = _load_actual_highs(target_str)
    if not actuals:
        print(f"  ✗ No actual settlement data for {target_str}")
        print("    Run backtest_collector.py first, then re-run this script.")
        # Liveness ≠ work-done: this is a benign "upstream data not ready yet"
        # exit, not a crash. Beat the heartbeat anyway so the watchdog doesn't
        # read a healthy no-op run as a dead service. (This exact early-exit,
        # which sits ABOVE the success-path beat below, is why the heartbeat
        # froze for 8 days while the process kept exiting 0.)
        try:
            from heartbeat import write_heartbeat
            write_heartbeat("bias_collector")
        except Exception:
            pass
        return

    print(f"  Actual highs: {', '.join(f'{c}={t:.1f}°F' for c, t in actuals.items())}")
    print()

    BACKTEST_DIR.mkdir(parents=True, exist_ok=True)
    records = []

    async with aiohttp.ClientSession() as session:
        # Build all tasks: (model, city) combinations
        tasks = []
        task_keys = []
        for city_key, city_info in CITIES.items():
            if city_key not in actuals:
                continue
            for model in BIAS_MODELS:
                tasks.append(fetch_previous_run_max(
                    session, city_info["lat"], city_info["lon"],
                    model, target_str,
                ))
                task_keys.append((model, city_key))

        # Run all fetches in parallel
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for (model, city_key), result in zip(task_keys, results):
            if isinstance(result, Exception):
                logger.debug("Fetch error %s/%s: %s", model, city_key, result)
                continue
            if result is None:
                continue

            actual = actuals[city_key]
            error = round(result - actual, 2)

            records.append({
                "date": target_str,
                "city": city_key,
                "model": model,
                "predicted": result,
                "actual": actual,
                "error": error,
                "collected_at": now.isoformat(),
            })

    # Print summary
    if records:
        print(f"  {'Model':<30s} {'City':<5s} {'Pred':>6s} {'Actual':>7s} {'Error':>7s}")
        print(f"  {'─' * 30} {'─' * 5} {'─' * 6} {'─' * 7} {'─' * 7}")
        for r in sorted(records, key=lambda x: (x["model"], x["city"])):
            print(f"  {r['model']:<30s} {r['city']:<5s} {r['predicted']:>5.1f}°F {r['actual']:>6.1f}°F {r['error']:>+6.2f}°F")

        # Append to JSONL
        with open(BIAS_DATA_FILE, "a") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

        print(f"\n  Saved {len(records)} bias records to {BIAS_DATA_FILE.name}")
    else:
        print("  ✗ No predictions fetched from Previous Runs API")

    # Recompute corrections
    _recompute_corrections()

    # Heartbeat
    try:
        from heartbeat import write_heartbeat
        write_heartbeat("bias_collector")
    except Exception:
        pass

    print(f"  {'─' * 50}")


def _load_actual_highs(target_date: str) -> dict[str, float]:
    """Load actual settlement temperatures from daily_data.jsonl."""
    if not DAILY_DATA_FILE.exists():
        return {}

    actuals = {}
    with open(DAILY_DATA_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if r.get("date") == target_date and r.get("actual_high") is not None:
                actuals[r["city"]] = r["actual_high"]

    return actuals


def _load_bias_records() -> list[dict]:
    """Load all bias records from bias_data.jsonl."""
    if not BIAS_DATA_FILE.exists():
        return []

    records = []
    with open(BIAS_DATA_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                continue

    return records


def _recompute_corrections():
    """
    Compute rolling 14-day mean bias per (model, city) and write corrections file.

    Format: {("ecmwf_aifs025", "LAX"): -0.8} means AIFS runs 0.8°F hot → subtract.
    Written as JSON: {"ecmwf_aifs025|LAX": -0.8, ...}
    """
    records = _load_bias_records()
    if not records:
        print("  No bias records — corrections file not updated")
        return

    # Group by (model, city), sorted by date
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in records:
        key = (r["model"], r["city"])
        grouped[key].append(r)

    corrections = {}
    for (model, city), recs in grouped.items():
        # Sort by date, take last ROLLING_WINDOW records
        recs.sort(key=lambda x: x["date"])
        recent = recs[-ROLLING_WINDOW:]

        if len(recent) < MIN_RECORDS:
            continue

        mean_error = sum(r["error"] for r in recent) / len(recent)
        # Correction is negative of error: if model runs +2°F hot, correct by -2°F
        correction = round(-mean_error, 2)

        if abs(correction) > 0.1:  # Only store meaningful corrections
            corrections[f"{model}|{city}"] = correction

    # Write corrections file
    CORRECTIONS_FILE.write_text(json.dumps(corrections, indent=2))
    print(f"  Updated {CORRECTIONS_FILE.name}: {len(corrections)} corrections")

    if corrections:
        # Print summary
        print(f"\n  ROLLING BIAS CORRECTIONS ({ROLLING_WINDOW}-day)")
        print(f"  {'Model':<30s} {'City':<5s} {'Correction':>11s}")
        print(f"  {'─' * 30} {'─' * 5} {'─' * 11}")
        for key, corr in sorted(corrections.items()):
            model, city = key.split("|")
            direction = "cool" if corr < 0 else "warm"
            print(f"  {model:<30s} {city:<5s} {corr:>+10.2f}°F ({direction})")


def print_report():
    """Print a summary report of all bias data."""
    records = _load_bias_records()
    if not records:
        print("\n  No bias data collected yet.")
        print("  Run: python3 bias_collector.py")
        return

    dates = sorted(set(r["date"] for r in records))
    cities = sorted(set(r["city"] for r in records))
    models = sorted(set(r["model"] for r in records))

    print(f"\n  {'=' * 70}")
    print("  BIAS COLLECTOR REPORT")
    print(f"  {'=' * 70}")
    print(f"  Records: {len(records)} ({len(dates)} days, {len(cities)} cities, {len(models)} models)")
    print(f"  Date range: {dates[0]} → {dates[-1]}")
    print()

    # Per-model aggregate bias
    print(f"  {'Model':<30s} {'N':>4s} {'MAE':>6s} {'Bias':>7s} {'RMSE':>6s}")
    print(f"  {'─' * 30} {'─' * 4} {'─' * 6} {'─' * 7} {'─' * 6}")

    for model in models:
        model_recs = [r for r in records if r["model"] == model]
        errors = [r["error"] for r in model_recs]
        n = len(errors)
        mae = sum(abs(e) for e in errors) / n
        bias = sum(errors) / n
        rmse = (sum(e**2 for e in errors) / n) ** 0.5
        print(f"  {model:<30s} {n:>4d} {mae:>5.2f}° {bias:>+6.2f}° {rmse:>5.2f}°")

    # Load and display corrections
    if CORRECTIONS_FILE.exists():
        corrections = json.loads(CORRECTIONS_FILE.read_text())
        if corrections:
            print(f"\n  ACTIVE CORRECTIONS ({CORRECTIONS_FILE.name})")
            print(f"  {'Model':<30s} {'City':<5s} {'Correction':>11s}")
            print(f"  {'─' * 30} {'─' * 5} {'─' * 11}")
            for key, corr in sorted(corrections.items()):
                model, city = key.split("|")
                print(f"  {model:<30s} {city:<5s} {corr:>+10.2f}°F")

    print(f"  {'=' * 70}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Bias Collector — Previous Runs API bias correction builder"
    )
    parser.add_argument(
        "--date", type=str, default=None,
        help="Target date (YYYY-MM-DD). Default: yesterday",
    )
    parser.add_argument(
        "--report", action="store_true",
        help="Print bias report without collecting new data",
    )
    args = parser.parse_args()

    if args.report:
        print_report()
    else:
        asyncio.run(collect_bias_data(args.date))
