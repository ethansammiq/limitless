#!/usr/bin/env python3
"""Stage-1 probe: port the intraday temperature sweep to Polymarket.

Polymarket runs daily "Highest temperature in <city>" bracket markets that
settle on a Wunderground AIRPORT-station page (METAR-derived observations),
not the NWS CLI report Kalshi uses. That collapses the hard problem (predict
the CLI-vs-METAR offset) into the solved one: observe the METAR running max
and know when the day's peak is in. These books do $50k-130k/day per city —
2-3 orders of magnitude more than the Kalshi ladders whose capacity killed
the Stage-2.5 strategy.

For each resolved event this probe joins, with NO lookahead:
  entry   = bracket containing the IEM METAR running max at the cutoff hour,
            priced at the last trade at/before the cutoff (prices-history)
  settle  = Polymarket's actual resolved winner (so Wunderground quirks and
            late specials count AGAINST us, as they would live)
  EV      = win ? 100 - entry - slip : -(entry + slip)   [cents/contract]

Caveats (all conservative):
  - prices-history is last-trade, not the crossable ask -> --slippage-cents
    haircut (default 3c). Live book depth needs the CLOB API; next gate.
  - IEM report_type=3 is routine ~hourly METAR without specials, so the
    running max here is a floor; Wunderground counts specials.

Cron does NOT pick up files under backtest/, so this is safe to add/run.

Usage:
    python3 backtest/poly_sweep_probe.py                    # all cities, 30 days
    python3 backtest/poly_sweep_probe.py --cities NYC --days 10 --verbose
    python3 backtest/poly_sweep_probe.py --cutoffs 15,16,17 --slippage-cents 5
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"
IEM_ASOS_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"

HERE = Path(__file__).resolve().parent
CACHE_PATH = HERE / "poly_probe_cache.json"

# Title fragment -> local tz. The settlement STATION is parsed per-event from
# the Wunderground URL in the market description (NYC settles KLGA there, not
# the KNYC Central Park station Kalshi uses).
CITIES: dict[str, dict[str, str]] = {
    "NYC": {"title": "Highest temperature in NYC on", "tz": "America/New_York"},
    "CHI": {"title": "Highest temperature in Chicago on", "tz": "America/Chicago"},
    "DAL": {"title": "Highest temperature in Dallas on", "tz": "America/Chicago"},
    "SFO": {"title": "Highest temperature in San Francisco on", "tz": "America/Los_Angeles"},
}

MONTHS = {m: i + 1 for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June",
     "July", "August", "September", "October", "November", "December"])}

_BETWEEN = re.compile(r"between (-?\d+)-(-?\d+)°F", re.I)
_ABOVE = re.compile(r"(-?\d+)°F or (?:higher|above)", re.I)
_BELOW = re.compile(r"(-?\d+)°F or (?:lower|below)", re.I)
_WU_STATION = re.compile(r"wunderground\.com/history/daily/[^\s\"]*/([A-Z][A-Z0-9]{2,4})")
_TITLE_DATE = re.compile(r"on (January|February|March|April|May|June|July|August|September|October|November|December) (\d{1,2})\?")


def _fetch(url: str, retries: int = 3) -> str:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "WeatherEdgePolyProbe/1.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001 — network, retry
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"fetch failed after {retries} attempts: {url[:120]} ({last})")


def _get_json(url: str):
    return json.loads(_fetch(url))


def _load_cache() -> dict:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"prices": {}, "obs": {}}


def _save_cache(cache: dict) -> None:
    tmp = CACHE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cache))
    tmp.replace(CACHE_PATH)


def parse_bracket(question: str) -> tuple[float, float] | None:
    """Bracket [lo, hi] inclusive in whole degrees F from a market question."""
    m = _BETWEEN.search(question)
    if m:
        return float(m.group(1)), float(m.group(2))
    m = _ABOVE.search(question)
    if m:
        return float(m.group(1)), float("inf")
    m = _BELOW.search(question)
    if m:
        return float("-inf"), float(m.group(1))
    return None


def discover_events(wanted_cities: list[str], max_days: int) -> list[dict]:
    """Resolved Poly temp events, newest first, up to max_days per city."""
    per_city: dict[str, list[dict]] = {c: [] for c in wanted_cities}
    seen: set[str] = set()
    for offset in range(0, 2000, 100):
        batch = _get_json(
            f"{GAMMA_URL}/events?closed=true&tag_slug=weather&order=endDate"
            f"&ascending=false&limit=100&offset={offset}")
        events = batch if isinstance(batch, list) else batch.get("events", [])
        if not events:
            break
        for ev in events:
            eid = str(ev.get("id"))
            title = ev.get("title", "")
            if eid in seen:
                continue
            seen.add(eid)
            for city in wanted_cities:
                if CITIES[city]["title"] in title and len(per_city[city]) < max_days:
                    ev["_city"] = city
                    per_city[city].append(ev)
        if all(len(v) >= max_days for v in per_city.values()):
            break
        time.sleep(0.15)
    return [ev for evs in per_city.values() for ev in evs]


def event_day(ev: dict) -> str | None:
    """Local settlement date YYYY-MM-DD from the event title + endDate year."""
    m = _TITLE_DATE.search(ev.get("title", ""))
    end = ev.get("endDate") or ""
    if not m or len(end) < 4:
        return None
    return f"{end[:4]}-{MONTHS[m.group(1)]:02d}-{int(m.group(2)):02d}"


def event_station(ev: dict) -> str | None:
    """IEM station id from the Wunderground URL in any market description."""
    for mkt in ev.get("markets") or []:
        m = _WU_STATION.search(mkt.get("description") or "")
        if m:
            code = m.group(1)
            return code[1:] if len(code) == 4 and code.startswith("K") else code
    return None


def fetch_day_obs(cache: dict, station: str, day: str, tz: ZoneInfo) -> list[dict]:
    """(local_minutes, temp_f) METAR obs for one local climate day, cached."""
    key = f"{station}@{day}"
    if key in cache["obs"]:
        return cache["obs"][key]
    day_start = datetime.fromisoformat(day).replace(tzinfo=tz)
    sts = (day_start - timedelta(hours=2)).astimezone(timezone.utc)
    ets = (day_start + timedelta(hours=26)).astimezone(timezone.utc)
    url = (f"{IEM_ASOS_URL}?station={station}"
           f"&sts={sts.strftime('%Y-%m-%dT%H:%M:%SZ')}"
           f"&ets={ets.strftime('%Y-%m-%dT%H:%M:%SZ')}"
           f"&vars[]=tmpf&direct=no&report_type=3")
    rows: list[dict] = []
    reader = csv.DictReader(
        line for line in io.StringIO(_fetch(url)) if not line.startswith("#"))
    for row in reader:
        valid = (row.get("valid(UTC)") or row.get("valid") or "").strip()
        tmpf = (row.get("tmpf") or "M").strip()
        if not valid or tmpf in ("M", "", "None"):
            continue
        try:
            ts = datetime.strptime(valid, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            temp = float(tmpf)
        except (ValueError, TypeError):
            continue
        loc = ts.astimezone(tz)
        if loc.strftime("%Y-%m-%d") == day and -60 < temp < 150:
            rows.append({"minutes": loc.hour * 60 + loc.minute, "temp_f": temp})
    rows.sort(key=lambda r: r["minutes"])
    cache["obs"][key] = rows
    return rows


def price_at_cutoff(cache: dict, token_id: str, day: str, tz: ZoneInfo,
                    cutoff_hour: int) -> float | None:
    """Last traded price at/before cutoff (local), cents. None if no prints."""
    key = f"{token_id}@{day}@{cutoff_hour}"
    if key in cache["prices"]:
        return cache["prices"][key]
    cut = datetime.fromisoformat(day).replace(hour=cutoff_hour, tzinfo=tz)
    start = int((cut - timedelta(hours=3)).timestamp())
    hist = _get_json(
        f"{CLOB_URL}/prices-history?market={token_id}"
        f"&startTs={start}&endTs={int(cut.timestamp())}&fidelity=10")
    points = hist.get("history") or []
    px = round(points[-1]["p"] * 100, 1) if points else None
    cache["prices"][key] = px
    return px


def run_probe(cities: list[str], max_days: int, cutoffs: list[int],
              slippage: float, verbose: bool) -> None:
    cache = _load_cache()
    events = discover_events(cities, max_days)
    counts = ", ".join(
        f"{c}:{sum(1 for e in events if e['_city'] == c)}" for c in cities)
    print(f"resolved events joined: {len(events)} ({counts})")

    # rows: (city, day, cutoff, entry_px, win)
    rows: list[tuple[str, str, int, float, bool]] = []
    skipped: dict[str, int] = {"no_day": 0, "no_station": 0, "no_winner": 0,
                               "no_obs": 0, "no_bracket": 0, "no_price": 0}
    for ev in events:
        city = ev["_city"]
        tz = ZoneInfo(CITIES[city]["tz"])
        day = event_day(ev)
        station = event_station(ev)
        if not day:
            skipped["no_day"] += 1
            continue
        if not station:
            skipped["no_station"] += 1
            continue

        brackets: list[tuple[tuple[float, float], str, bool]] = []
        winner_range: tuple[float, float] | None = None
        for mkt in ev.get("markets") or []:
            rng = parse_bracket(mkt.get("question") or "")
            if not rng:
                continue
            try:
                yes_price = float(json.loads(mkt.get("outcomePrices") or "[]")[0])
                token = json.loads(mkt.get("clobTokenIds") or "[]")[0]
            except (ValueError, IndexError, json.JSONDecodeError):
                continue
            won = yes_price > 0.99
            brackets.append((rng, token, won))
            if won:
                winner_range = rng
        if winner_range is None or not brackets:
            skipped["no_winner"] += 1
            continue

        try:
            obs = fetch_day_obs(cache, station, day, tz)
        except RuntimeError as exc:
            print(f"  obs fetch failed {station}@{day}: {exc}", file=sys.stderr)
            skipped["no_obs"] += 1
            continue
        if not obs:
            skipped["no_obs"] += 1
            continue

        for cutoff in cutoffs:
            upto = [o["temp_f"] for o in obs if o["minutes"] <= cutoff * 60]
            if not upto:
                continue
            runmax = max(upto)
            entry = next(((rng, token) for rng, token, _ in brackets
                          if rng[0] <= runmax <= rng[1]), None)
            if entry is None:
                skipped["no_bracket"] += 1
                continue
            try:
                px = price_at_cutoff(cache, entry[1], day, tz, cutoff)
            except RuntimeError:
                px = None
            if px is None or not 0 < px < 100:
                skipped["no_price"] += 1
                continue
            win = entry[0] == winner_range
            rows.append((city, day, cutoff, px, win))
            if verbose:
                print(f"  {city} {day} @{cutoff:02d}:00 runmax={runmax:.0f}F "
                      f"entry={px:.0f}c {'WIN' if win else 'LOSS'}")
        _save_cache(cache)
        time.sleep(0.1)

    print(f"\nskips: {', '.join(f'{k}={v}' for k, v in skipped.items() if v)}")
    print(f"slippage haircut: {slippage:.0f}c/side (last-trade px, not ask)\n")
    header = f"{'city':5s} {'cutoff':>6s} {'n':>4s} {'hit%':>6s} {'med_px':>7s} {'EV_net(c)':>10s}"
    print(header)
    for city in cities + ["ALL"]:
        for cutoff in cutoffs:
            sel = [r for r in rows if r[2] == cutoff and (city == "ALL" or r[0] == city)]
            if not sel:
                continue
            wins = [r for r in sel if r[4]]
            evs = [(100 - px - slippage) if win else -(px + slippage)
                   for _, _, _, px, win in sel]
            print(f"{city:5s} {cutoff:>5d}h {len(sel):>4d} "
                  f"{len(wins) / len(sel):>6.1%} {median(r[3] for r in sel):>6.0f}c "
                  f"{sum(evs) / len(evs):>+9.1f}")
    print("\nNext gate before building: live CLOB book depth at the cutoff hours"
          "\n(prices-history hides the spread), and Polymarket US account eligibility.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cities", default=",".join(CITIES),
                    help=f"comma list from {','.join(CITIES)}")
    ap.add_argument("--days", type=int, default=30, help="resolved days per city")
    ap.add_argument("--cutoffs", default="14,15,16,17",
                    help="local entry hours, comma list")
    ap.add_argument("--slippage-cents", type=float, default=3.0)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    cities = [c.strip().upper() for c in args.cities.split(",") if c.strip()]
    unknown = [c for c in cities if c not in CITIES]
    if unknown:
        ap.error(f"unknown cities: {unknown}")
    cutoffs = [int(h) for h in args.cutoffs.split(",")]
    run_probe(cities, args.days, cutoffs, args.slippage_cents, args.verbose)


if __name__ == "__main__":
    main()
