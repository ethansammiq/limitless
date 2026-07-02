#!/usr/bin/env python3
"""Stage-2 ASOS backfill: hourly observations for the 5 settlement stations.

Pulls hourly METAR temperatures (tmpf) from the IEM ASOS archive for the date
range covered by backtest/daily_data.jsonl, groups each observation into its
LOCAL climate day (the day Kalshi/NWS CLI settles on), and computes the running
max at each observation. Caches to backtest/asos_hourly_cache.json.

This is the necessary input for the Stage-2 make-or-break test: does the
late-afternoon (~4pm local) running max already identify the winning Kalshi
bracket, and was that bracket still cheap then? Backfill only — no trading.
Cron does NOT pick up files under backtest/, so this is safe to add/run.

Usage:
    python3 backtest/asos_backfill.py            # full backfill, all 5 stations
    python3 backtest/asos_backfill.py --probe    # one station, short window (smoke test)
"""
from __future__ import annotations

import csv
import io
import json
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

IEM_ASOS_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"

HERE = Path(__file__).resolve().parent
DAILY_DATA = HERE / "daily_data.jsonl"
OUT_CACHE = HERE / "asos_hourly_cache.json"

# city_code -> (iem_id, local tz). Settlement stations per CLAUDE.md station authority.
# NYC=Central Park (KNYC), CHI=Midway (KMDW), DEN=DIA, MIA=MIA airport, LAX=LAX.
STATIONS: dict[str, dict[str, str]] = {
    "NYC": {"iem_id": "NYC", "tz": "America/New_York"},
    "CHI": {"iem_id": "MDW", "tz": "America/Chicago"},
    "LAX": {"iem_id": "LAX", "tz": "America/Los_Angeles"},
    "DEN": {"iem_id": "DEN", "tz": "America/Denver"},
    "MIA": {"iem_id": "MIA", "tz": "America/New_York"},
}


def _date_range() -> tuple[str, str]:
    """Min/max settlement date present in daily_data.jsonl (YYYY-MM-DD)."""
    dates: set[str] = set()
    with DAILY_DATA.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            dates.add(json.loads(line)["date"])
    return min(dates), max(dates)


def _iem_url(station: str, start_utc: datetime, end_utc: datetime) -> str:
    """IEM ASOS request URL (asos.py, report_type=3 — routine ~hourly METAR).

    Literal vars[] brackets are required (IEM rejects the %5B%5D form).
    """
    sts = start_utc.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ets = end_utc.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f"{IEM_ASOS_URL}?station={station}&sts={sts}&ets={ets}"
        f"&vars[]=tmpf&direct=no&report_type=3"
    )


def _fetch(url: str, retries: int = 3) -> str:
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "WeatherEdgeBackfill/1.0"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001 — network, retry
            last_exc = exc
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"IEM fetch failed after {retries} attempts: {last_exc}")


def _parse_rows(text: str) -> list[tuple[datetime, float]]:
    """Extract every (utc_ts, tmpf) observation from an IEM ASOS CSV."""
    out: list[tuple[datetime, float]] = []
    reader = csv.DictReader(line for line in io.StringIO(text) if not line.startswith("#"))
    for row in reader:
        valid_str = (row.get("valid(UTC)") or row.get("valid") or "").strip()
        tmpf_str = (row.get("tmpf") or "M").strip()
        if not valid_str or tmpf_str in ("M", "", "None"):
            continue
        try:
            ts = datetime.strptime(valid_str, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            t = float(tmpf_str)
        except (ValueError, TypeError):
            continue
        if -60 < t < 150:
            out.append((ts, t))
    out.sort(key=lambda r: r[0])
    return out


def _group_by_local_day(
    rows: list[tuple[datetime, float]], tz: ZoneInfo
) -> dict[str, list[dict]]:
    """Group obs into local climate days with per-observation running max."""
    by_day: dict[str, list[dict]] = {}
    for ts_utc, temp in rows:
        local = ts_utc.astimezone(tz)
        day = local.strftime("%Y-%m-%d")
        by_day.setdefault(day, []).append(
            {
                "ts_utc": int(ts_utc.timestamp()),
                "hour_local": local.hour,
                "minute_local": local.minute,
                "temp_f": round(temp, 1),
            }
        )
    for day, obs in by_day.items():
        obs.sort(key=lambda o: o["ts_utc"])
        running = float("-inf")
        for o in obs:
            running = max(running, o["temp_f"])
            o["running_max_f"] = round(running, 1)
    return by_day


def backfill(probe: bool = False) -> dict:
    start_date, end_date = _date_range()
    # Pad ±1 day so local-day windows at the edges are fully covered, then in UTC.
    win_start = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc) - timedelta(days=1)
    win_end = datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc) + timedelta(days=2)

    if probe:
        # One station, 3-day window — smoke test the fetch/parse path.
        win_start = win_end - timedelta(days=3)
        stations = {"CHI": STATIONS["CHI"]}
    else:
        stations = STATIONS

    print(f"IEM ASOS backfill | window {win_start.date()} → {win_end.date()} UTC")
    cache: dict[str, dict] = {}
    for city, meta in stations.items():
        url = _iem_url(meta["iem_id"], win_start, win_end)
        text = _fetch(url)
        rows = _parse_rows(text)
        by_day = _group_by_local_day(rows, ZoneInfo(meta["tz"]))
        cache[city] = {
            "iem_id": meta["iem_id"],
            "tz": meta["tz"],
            "n_obs": len(rows),
            "days": by_day,
        }
        peaks = {d: max(o["temp_f"] for o in obs) for d, obs in by_day.items()}
        print(
            f"  {city} ({meta['iem_id']}): {len(rows):>4} obs across {len(by_day):>2} local days"
            + (f" | sample peak {next(iter(peaks))}={peaks[next(iter(peaks))]}°F" if peaks else "")
        )
        time.sleep(1)  # be polite to IEM

    if not probe:
        OUT_CACHE.write_text(json.dumps(cache, indent=0))
        print(f"\nWrote {OUT_CACHE} ({OUT_CACHE.stat().st_size:,} bytes)")
    else:
        print("\n[probe] not written to cache")
    return cache


if __name__ == "__main__":
    backfill(probe="--probe" in sys.argv)
