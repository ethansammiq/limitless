#!/usr/bin/env python3
"""
BACKTEST ANALYZER — Model calibration and performance validation.

Analyzes daily_data.jsonl and ensemble snapshots to measure:
  1. Ensemble accuracy: MAE vs NWS MAE
  2. Per-model ranking: Which model family predicts best?
  3. Bracket hit rate: Did ensemble mean land in the winning bracket?
  4. KDE calibration: Predicted prob vs actual frequency
  5. Simulated P&L: Would scanner recommendations have profited?

Usage:
  python3 backtest_analyzer.py
  python3 backtest_analyzer.py --city NYC
  python3 backtest_analyzer.py --min-days 3
"""

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
BACKTEST_DIR = PROJECT_ROOT / "backtest"
DAILY_DATA_FILE = BACKTEST_DIR / "daily_data.jsonl"
SNAPSHOT_DIR = BACKTEST_DIR / "ensemble_snapshots"


def load_records(city_filter: str = None) -> list:
    """Load all records from daily_data.jsonl, optionally filtered by city."""
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
                if city_filter and r.get("city", "").upper() != city_filter.upper():
                    continue
                records.append(r)
            except json.JSONDecodeError:
                continue
    return records


def load_snapshot(city: str, date: str) -> dict:
    """Load ensemble snapshot for a city/date if it exists."""
    path = SNAPSHOT_DIR / f"{date}_{city}.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, Exception):
            return {}
    return {}


def parse_bracket_from_ticker(ticker: str) -> tuple:
    """
    Extract bracket range from ticker string.
    KXHIGHNY-26FEB10-B36.5 -> (36, 37, 'range')
    KXHIGHNY-26FEB10-T39   -> (39, 999, 'high_tail')
    KXHIGHNY-26FEB10-T32   -> (-999, 32, 'low_tail')

    Heuristic for T-tickers: if the title contains '<' or 'below' it's low_tail.
    Without title context, we infer from context (the winning bracket).
    """
    parts = ticker.split("-")
    if len(parts) < 3:
        return (0, 0, "unknown")

    bracket_code = parts[-1]  # e.g., B36.5, T39, T32

    if bracket_code.startswith("B"):
        # Range bracket: B36.5 means 36-37
        try:
            mid = float(bracket_code[1:])
            low = mid - 0.5
            high = mid + 0.5
            return (low, high, "range")
        except ValueError:
            return (0, 0, "unknown")
    elif bracket_code.startswith("T"):
        # Tail bracket — need to determine direction from context
        try:
            val = float(bracket_code[1:])
            return (val, val, "tail")  # Caller resolves direction
        except ValueError:
            return (0, 0, "unknown")

    return (0, 0, "unknown")


def find_winning_bracket(settlements: list) -> dict:
    """Find the settlement that resolved YES."""
    for s in settlements:
        if s.get("result") == "yes":
            return s
    return {}


def actual_in_bracket(actual: float, ticker: str, title: str = "") -> bool:
    """Check if actual temperature falls within a bracket."""
    low, high, btype = parse_bracket_from_ticker(ticker)
    if btype == "range":
        return low <= actual < high  # Exclusive upper bound, consistent with edge_scanner_v2
    elif btype == "tail":
        # Use title to determine direction
        if title and re.search(r"<|below|under", title, re.I):
            return actual < low
        elif title and re.search(r">|above|over", title, re.I):
            return actual > high
    return False


def value_in_bracket(value: float, ticker: str, title: str = "") -> bool:
    """Check if a forecast value falls within a bracket range."""
    return actual_in_bracket(value, ticker, title)


def analyze(city_filter: str = None, min_days: int = 3):
    """Run the full backtest analysis."""
    records = load_records(city_filter)

    if not records:
        print("No backtest data found. Run backtest_collector.py first.")
        return

    # Deduplicate by (date, city)
    seen = set()
    unique_records = []
    for r in records:
        key = (r.get("date"), r.get("city"))
        if key not in seen:
            seen.add(key)
            unique_records.append(r)
    records = unique_records

    dates = sorted(set(r.get("date", "") for r in records))
    cities = sorted(set(r.get("city", "") for r in records))

    if len(dates) < min_days:
        print(f"Only {len(dates)} days of data (need {min_days}). Collect more first.")
        return

    print(f"\n{'='*60}")
    print("  BACKTEST ANALYSIS REPORT")
    print(f"{'='*60}")
    print(f"  Period: {dates[0]} to {dates[-1]} ({len(dates)} days, {len(cities)} cities)")
    print(f"  Records: {len(records)}")

    # Load snapshots where available
    snapshot_count = 0
    for r in records:
        snap = load_snapshot(r["city"], r["date"])
        if snap:
            r["_snapshot"] = snap
            snapshot_count += 1
    print(f"  With snapshots: {snapshot_count}")

    # ── 1. Ensemble & NWS Accuracy ──
    ensemble_errors = []
    nws_errors = []
    ensemble_hits = 0
    nws_hits = 0
    total_with_forecasts = 0

    for r in records:
        actual = r.get("actual_high")
        if actual is None:
            continue

        snap = r.get("_snapshot", {})
        ens_mean = snap.get("mean") or r.get("ensemble_mean")
        nws_high = snap.get("nws_forecast_high") or r.get("nws_forecast")

        winner = find_winning_bracket(r.get("settlements", []))
        if not winner:
            continue

        if ens_mean is not None:
            ensemble_errors.append(abs(ens_mean - actual))
            if value_in_bracket(ens_mean, winner["ticker"], winner.get("title", "")):
                ensemble_hits += 1
            total_with_forecasts += 1

        if nws_high is not None:
            nws_errors.append(abs(nws_high - actual))
            if value_in_bracket(nws_high, winner["ticker"], winner.get("title", "")):
                nws_hits += 1

    print("\n  FORECAST ACCURACY")
    if ensemble_errors:
        ens_mae = sum(ensemble_errors) / len(ensemble_errors)
        print(f"  Ensemble MAE:    {ens_mae:.1f}°F  (n={len(ensemble_errors)})")
    if nws_errors:
        nws_mae = sum(nws_errors) / len(nws_errors)
        print(f"  NWS MAE:         {nws_mae:.1f}°F  (n={len(nws_errors)})")
    if ensemble_errors and nws_errors:
        if ens_mae < nws_mae:
            print(f"  Ensemble beats NWS by {nws_mae - ens_mae:.1f}°F")
        else:
            print(f"  NWS beats ensemble by {ens_mae - nws_mae:.1f}°F")

    if total_with_forecasts > 0:
        print("\n  BRACKET HIT RATE")
        print(f"  Ensemble mean in winning bracket: {ensemble_hits}/{total_with_forecasts} ({ensemble_hits/total_with_forecasts*100:.0f}%)")
        if nws_errors:
            print(f"  NWS forecast in winning bracket:  {nws_hits}/{len(nws_errors)} ({nws_hits/len(nws_errors)*100:.0f}%)")

    # ── 2. Per-Model Ranking ──
    model_errors = defaultdict(list)
    for r in records:
        actual = r.get("actual_high")
        if actual is None:
            continue
        snap = r.get("_snapshot", {})
        per_model = snap.get("per_model_means") or r.get("per_model_means", {})
        for model, mean in per_model.items():
            if mean is not None:
                model_errors[model].append(abs(mean - actual))

    if model_errors:
        print("\n  MODEL RANKING (by MAE, lower = better)")
        ranked = sorted(model_errors.items(), key=lambda x: sum(x[1]) / len(x[1]))
        for i, (model, errors) in enumerate(ranked, 1):
            mae = sum(errors) / len(errors)
            label = "★" if "aifs" in model else " "
            print(f"  {label} {i}. {model:<20s} MAE: {mae:.1f}°F  (n={len(errors)})")

    # ── 3. KDE Calibration ──
    # Bucket KDE probabilities and check actual hit rates
    kde_bins = defaultdict(lambda: {"predicted": [], "actual": []})

    for r in records:
        snap = r.get("_snapshot", {})
        opps = snap.get("opportunities", [])
        settlements = {s["ticker"]: s["result"] for s in r.get("settlements", [])}

        for opp in opps:
            ticker = opp.get("ticker", "")
            kde = opp.get("kde_prob", 0)
            side = opp.get("side", "yes")

            settled = settlements.get(ticker)
            if settled is None:
                continue

            # Did this opportunity's prediction come true?
            if side == "yes":
                hit = 1 if settled == "yes" else 0
            else:
                hit = 1 if settled == "no" else 0

            # Bucket by predicted probability
            bucket = min(int(kde * 100 // 20) * 20, 80)  # 0, 20, 40, 60, 80
            kde_bins[bucket]["predicted"].append(kde)
            kde_bins[bucket]["actual"].append(hit)

    if kde_bins:
        print("\n  KDE CALIBRATION (predicted prob vs actual hit rate)")
        print(f"  {'Bin':<10s} {'Predicted':>10s} {'Actual':>10s} {'Count':>8s}  {'Status'}")
        print(f"  {'─'*50}")
        for bucket in sorted(kde_bins.keys()):
            data = kde_bins[bucket]
            avg_pred = sum(data["predicted"]) / len(data["predicted"])
            avg_actual = sum(data["actual"]) / len(data["actual"])
            count = len(data["predicted"])
            if avg_actual > avg_pred + 0.1:
                status = "UNDER-CONFIDENT"
            elif avg_actual < avg_pred - 0.1:
                status = "OVER-CONFIDENT"
            else:
                status = "CALIBRATED"
            print(f"  {bucket:>2d}-{bucket+20:<5d}   {avg_pred:>8.0%}    {avg_actual:>8.0%}    {count:>5d}   {status}")

    # ── 4. Simulated P&L ──
    total_trades = 0
    winners = 0
    total_edge_realized = 0

    for r in records:
        snap = r.get("_snapshot", {})
        opps = snap.get("opportunities", [])
        settlements = {s["ticker"]: s["result"] for s in r.get("settlements", [])}
        bracket_prices = snap.get("bracket_prices", {})

        for opp in opps:
            ticker = opp.get("ticker", "")
            conf = opp.get("confidence_score", 0)
            side = opp.get("side", "yes")
            kde = opp.get("kde_prob", 0)

            # Only count trades that would have passed our filters
            if conf < 90 or kde < 0.20:
                continue

            prices = bracket_prices.get(ticker, {})
            entry = prices.get("yes_bid", 0) + 1 if side == "yes" else (100 - prices.get("yes_ask", 100)) + 1
            if entry > 50 or entry <= 0:
                continue

            settled = settlements.get(ticker)
            if settled is None:
                continue

            total_trades += 1
            if side == "yes" and settled == "yes":
                winners += 1
                total_edge_realized += (100 - entry)
            elif side == "no" and settled == "no":
                winners += 1
                total_edge_realized += (100 - entry)
            else:
                total_edge_realized -= entry

    if total_trades > 0:
        print("\n  SIMULATED P&L (conf >= 90 trades only)")
        print(f"  Trades: {total_trades}")
        print(f"  Winners: {winners} ({winners/total_trades*100:.0f}%)")
        print(f"  Net edge: {total_edge_realized:+.0f}c")
        print(f"  Avg per trade: {total_edge_realized/total_trades:+.1f}c")
    else:
        print("\n  SIMULATED P&L")
        print("  No 90+ confidence trades in historical data yet.")
        print("  (This is expected — need more data collection days)")

    print(f"\n{'='*60}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest Analyzer — Model calibration report")
    parser.add_argument("--city", type=str, default=None, help="Filter by city code (NYC, CHI, DEN, MIA, LAX)")
    parser.add_argument("--min-days", type=int, default=3, help="Minimum days required for analysis")
    args = parser.parse_args()
    analyze(city_filter=args.city, min_days=args.min_days)
