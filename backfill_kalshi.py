#!/usr/bin/env python3
"""
BACKFILL — Full historical settlement data from Kalshi weather markets.

Fetches ALL settled markets for each city series (back to Aug 2021 for NYC),
derives the actual high temperature from settlement data, and writes clean
records to backtest/daily_data.jsonl.

Three methods for deriving actual_high (in priority order):
  1. expiration_value — exact temperature from Kalshi settlement (best)
  2. Winning bracket midpoint — (floor_strike + cap_strike) / 2
  3. Winning threshold bounds — floor+1 or cap-1 (conservative estimate)

Usage:
  python3 backfill_kalshi.py                     # Backfill ALL history
  python3 backfill_kalshi.py --dry-run            # Preview without writing
  python3 backfill_kalshi.py --since 2025-01-01   # Only recent dates
"""

import argparse
import json
import time
import urllib.request
import urllib.error
from collections import defaultdict
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
BACKTEST_DIR = PROJECT_ROOT / "backtest"
DAILY_DATA_FILE = BACKTEST_DIR / "daily_data.jsonl"
BACKUP_FILE = BACKTEST_DIR / "daily_data.jsonl.pre_backfill"

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
USER_AGENT = "BackfillScript/1.0"
REQUEST_DELAY = 0.6  # Seconds between API calls (respect rate limit)

# City series tickers (mirrors config.py)
CITY_SERIES = {
    "NYC": "KXHIGHNY",
    "CHI": "KXHIGHCHI",
    "DEN": "KXHIGHDEN",
    "MIA": "KXHIGHMIA",
    "LAX": "KXHIGHLAX",
}

# Month abbreviation → number
MONTH_MAP = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def parse_date_from_ticker(date_part: str) -> str:
    """
    Parse Kalshi date part (e.g., '26FEB10') into ISO date (e.g., '2026-02-10').

    Format: YYMMMDD where MMM is 3-letter month abbreviation.
    """
    year = int("20" + date_part[:2])
    month_str = date_part[2:5]
    day = int(date_part[5:])
    month = MONTH_MAP.get(month_str.upper())
    if not month:
        raise ValueError(f"Unknown month in date part: {date_part}")
    return f"{year}-{month:02d}-{day:02d}"


def derive_actual_high(markets: list) -> tuple:
    """
    Derive the actual high temperature from a set of settled markets for one date.

    Returns (actual_high: float, method: str, winning_ticker: str).

    Priority:
      1. expiration_value — exact temperature from Kalshi (available ~96% of dates)
      2. Winning bracket midpoint — (floor + cap) / 2 (precise for bracket markets)
      3. Winning threshold bounds — floor+1 or cap-1 (conservative estimate)
      4. Parse from title — old markets (2021) may lack strike fields
    """
    # ── Method 1: expiration_value (BEST — exact temperature) ──
    for m in markets:
        ev = m.get("expiration_value")
        if ev:
            try:
                actual = float(ev)
                # Find which market was the YES winner for the ticker reference
                yes_ticker = ""
                for w in markets:
                    if w.get("result") == "yes":
                        yes_ticker = w.get("ticker", "")
                        break
                return actual, "expiration_value", yes_ticker or m.get("ticker", "")
            except (ValueError, TypeError):
                pass  # Fall through to other methods

    # ── Method 2+3: From YES winners + strike info ──
    winners = [m for m in markets if m.get("result") == "yes"]

    if not winners:
        return None, "no_winner", ""

    # Prefer bracket winners (most precise after expiration_value)
    bracket_winners = [
        w for w in winners if w.get("strike_type") == "between"
    ]
    if bracket_winners:
        w = bracket_winners[0]
        floor_s = w.get("floor_strike")
        cap_s = w.get("cap_strike")
        if floor_s is not None and cap_s is not None:
            midpoint = (floor_s + cap_s) / 2.0
            return midpoint, "bracket", w["ticker"]

    # Threshold winners
    for w in winners:
        stype = w.get("strike_type", "")
        if stype == "greater":
            floor_s = w.get("floor_strike")
            if floor_s is not None:
                return float(floor_s + 1), "threshold_above", w["ticker"]
        elif stype == "less":
            cap_s = w.get("cap_strike")
            if cap_s is not None:
                return float(cap_s - 1), "threshold_below", w["ticker"]

    # ── Method 4: Parse from title (old 2021 markets without strike fields) ──
    # e.g., "Will the high in New York City be over 86° on Friday?"
    import re
    for w in winners:
        title = w.get("title", "")
        # Match "over X°" or ">X°" patterns
        match = re.search(r'(?:over|>)\s*(\d+)', title)
        if match:
            threshold = int(match.group(1))
            return float(threshold + 1), "title_parse_above", w["ticker"]
        # Match "under X°" or "<X°" patterns
        match = re.search(r'(?:under|<)\s*(\d+)', title)
        if match:
            threshold = int(match.group(1))
            return float(threshold - 1), "title_parse_below", w["ticker"]
        # Match "be X-Y°" bracket pattern
        match = re.search(r'be\s+(\d+)-(\d+)', title)
        if match:
            low, high = int(match.group(1)), int(match.group(2))
            return (low + high) / 2.0, "title_parse_bracket", w["ticker"]

    return None, "unknown_winner_type", winners[0].get("ticker", "")


def fetch_settled_markets(series_ticker: str) -> list:
    """
    Fetch all settled markets for a series ticker from Kalshi (handles pagination).
    """
    all_markets = []
    cursor = None

    while True:
        url = f"{KALSHI_BASE}/markets?series_ticker={series_ticker}&status=settled&limit=200"
        if cursor:
            url += f"&cursor={cursor}"

        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

        retries = 3
        for attempt in range(retries):
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read())
                break
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < retries - 1:
                    wait = 5 * (attempt + 1)
                    print(f"    Rate limited, waiting {wait}s...")
                    time.sleep(wait)
                else:
                    raise
            except Exception:
                if attempt < retries - 1:
                    time.sleep(2)
                else:
                    raise

        from kalshi_client import normalize_market
        batch = [normalize_market(m) for m in data.get("markets", [])]
        all_markets.extend(batch)
        cursor = data.get("cursor")
        if not cursor or not batch:
            break
        time.sleep(REQUEST_DELAY)

    return all_markets


def group_by_date(markets: list) -> dict:
    """Group markets by their date part (e.g., '26FEB10')."""
    grouped = defaultdict(list)
    for m in markets:
        ticker = m.get("ticker", "")
        parts = ticker.split("-")
        if len(parts) >= 2:
            grouped[parts[1]].append(m)
    return dict(grouped)


def load_existing_records() -> dict:
    """
    Load existing daily_data.jsonl records, keyed by (date, city).

    Preserves ensemble snapshots and per_model_means from existing records.
    """
    existing = {}
    if not DAILY_DATA_FILE.exists():
        return existing

    with open(DAILY_DATA_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                key = (r.get("date"), r.get("city"))
                existing[key] = r
            except json.JSONDecodeError:
                continue
    return existing


def main():
    parser = argparse.ArgumentParser(description="Backfill settlement data from Kalshi")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument("--since", type=str, default="2021-01-01",
                        help="Only include dates from this date (YYYY-MM-DD). Default: 2021-01-01 (all history)")
    args = parser.parse_args()

    since_date = args.since

    BACKTEST_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now().isoformat()

    print(f"\n{'='*55}")
    print("  KALSHI SETTLEMENT BACKFILL")
    print(f"{'='*55}")

    # Load existing records to preserve ensemble data
    existing = load_existing_records()
    print(f"  Existing records: {len(existing)}")

    all_records = {}  # (date, city) → record
    stats = {"total": 0, "expiration_value": 0, "bracket": 0, "threshold_above": 0,
             "threshold_below": 0, "title_parse_above": 0, "title_parse_below": 0,
             "title_parse_bracket": 0, "failed": 0}

    for city, series in CITY_SERIES.items():
        print(f"\n  {city} ({series})...")
        time.sleep(REQUEST_DELAY)

        try:
            markets = fetch_settled_markets(series)
        except Exception as e:
            print(f"    FAILED to fetch: {e}")
            stats["failed"] += 1
            continue

        by_date = group_by_date(markets)
        print(f"    Fetched {len(markets)} markets across {len(by_date)} dates")

        for date_part, date_markets in sorted(by_date.items()):
            try:
                iso_date = parse_date_from_ticker(date_part)
            except ValueError as e:
                continue  # Silently skip unparseable dates

            # Filter by --since date
            if iso_date < since_date:
                continue

            actual_high, method, winning_ticker = derive_actual_high(date_markets)

            if actual_high is None:
                # Only log failures for dates in our target range
                print(f"    {iso_date}: no winner found (result field empty?)")
                stats["failed"] += 1
                continue

            stats["total"] += 1
            stats[method] = stats.get(method, 0) + 1

            # Build settlement list (include all useful fields)
            settlements = []
            for m in date_markets:
                entry = {
                    "ticker": m.get("ticker", ""),
                    "title": m.get("title", ""),
                    "result": m.get("result", ""),
                    "yes_bid_close": m.get("yes_bid", 0),
                    "volume": m.get("volume", 0),
                }
                # Include strike info when available
                if m.get("floor_strike") is not None:
                    entry["floor_strike"] = m["floor_strike"]
                if m.get("cap_strike") is not None:
                    entry["cap_strike"] = m["cap_strike"]
                if m.get("strike_type"):
                    entry["strike_type"] = m["strike_type"]
                if m.get("expiration_value"):
                    entry["expiration_value"] = m["expiration_value"]
                settlements.append(entry)

            # Build the record
            key = (iso_date, city)
            old_record = existing.get(key, {})

            record = {
                "date": iso_date,
                "city": city,
                "actual_high": actual_high,
                "actual_high_source": method,
                "winning_ticker": winning_ticker,
                "settlements": settlements,
                "collected_at": old_record.get("collected_at", now),
                "backfilled_at": now,
            }

            # Preserve ensemble data from original collection if it exists
            for ens_key in ("ensemble_mean", "ensemble_std", "ensemble_count",
                            "per_model_means", "nws_forecast"):
                if ens_key in old_record:
                    record[ens_key] = old_record[ens_key]

            # Flag if the old actual_high was wrong
            old_high = old_record.get("actual_high")
            if old_high is not None and abs(old_high - actual_high) > 2.0:
                record["nws_actual_high"] = old_high
                record["nws_deviation"] = round(old_high - actual_high, 1)

            all_records[key] = record

        # Brief status per city
        city_count = sum(1 for k in all_records if k[1] == city)
        print(f"    → {city_count} date records derived")

    # Sort records by date then city
    sorted_records = sorted(all_records.values(), key=lambda r: (r["date"], r["city"]))

    # Summary
    n_dates = len(set(r["date"] for r in sorted_records))
    print(f"\n{'─'*55}")
    print("  SUMMARY")
    print(f"{'─'*55}")
    print(f"  Total records:     {len(sorted_records)} ({n_dates} unique dates)")
    print(f"  By expiration_val: {stats['expiration_value']} (exact temperature — best)")
    print(f"  By bracket:        {stats['bracket']} (midpoint of floor/cap)")
    print(f"  By threshold >X:   {stats['threshold_above']} (floor+1 estimate)")
    print(f"  By threshold <X:   {stats['threshold_below']} (cap-1 estimate)")
    title_total = stats['title_parse_above'] + stats['title_parse_below'] + stats['title_parse_bracket']
    if title_total:
        print(f"  By title parse:    {title_total} (old format markets)")
    print(f"  Failed:            {stats['failed']}")

    # Show date ranges per city
    city_dates = defaultdict(list)
    for r in sorted_records:
        city_dates[r["city"]].append(r["date"])
    print("\n  Date ranges:")
    for city in sorted(city_dates.keys()):
        dates = sorted(city_dates[city])
        print(f"    {city:4s}: {dates[0]} → {dates[-1]} ({len(dates)} days)")

    # Show NWS deviation examples
    deviations = [r for r in sorted_records if "nws_deviation" in r]
    if deviations:
        print("\n  NWS vs Kalshi deviations (top 10):")
        for r in sorted(deviations, key=lambda x: abs(x["nws_deviation"]), reverse=True)[:10]:
            print(f"    {r['date']} {r['city']:4s}: NWS={r['nws_actual_high']:.1f}°F  "
                  f"Kalshi={r['actual_high']:.1f}°F  "
                  f"Δ={r['nws_deviation']:+.1f}°F")

    if args.dry_run:
        print("\n  DRY RUN — no changes written")
    else:
        # Backup existing file
        if DAILY_DATA_FILE.exists():
            import shutil
            shutil.copy2(DAILY_DATA_FILE, BACKUP_FILE)
            print(f"\n  Backed up existing data → {BACKUP_FILE.name}")

        # Write all records
        with open(DAILY_DATA_FILE, "w") as f:
            for r in sorted_records:
                f.write(json.dumps(r, default=str) + "\n")
        print(f"  Wrote {len(sorted_records)} records → {DAILY_DATA_FILE.name}")

        # Invalidate calibration cache (force recalibration with clean data)
        cache_file = PROJECT_ROOT / "calibration_cache.json"
        if cache_file.exists():
            cache_file.unlink()
            print("  Cleared calibration cache (will recalibrate on next scan)")

    print(f"{'='*55}\n")


if __name__ == "__main__":
    main()
