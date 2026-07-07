#!/usr/bin/env python3
"""WALL FINGERPRINT — who defends settlement theses, when, and are they right?

Runs the competitor-dossier study (2026-07-07) over the shadow-book archive:
for every Kalshi bracket-day, track defense-class walls (core.walls) across
snapshots, then join to settlement.

Questions answered:
  1. Wall win rate — is a defense wall informed money? (n=1 said yes: the
     2026-07-06 MIA 93-94 wall was right.)
  2. Arrival timing — station-local hour a wall first appears. Walls that
     arrive before the day's peak (~15:00 local) are conviction-before-data;
     stations where such walls recur AND win are faster-flow suspects.
  3. Censoring — a wall present at the window's first snapshot may be older
     than capture; those timings are lower bounds, reported separately.

Alert-only research; never trades.

Usage:
    python3 backtest/wall_fingerprint.py            # whole archive
    python3 backtest/wall_fingerprint.py --days 7
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.walls import detect_wall  # noqa: E402

SHADOW_DIR = PROJECT_ROOT / "logs" / "shadow_books"
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
PEAK_LOCAL_H = 15  # walls first seen before this local hour = pre-peak conviction


def _series_tz() -> dict[str, str]:
    from ladders import load_ladders

    return {lad.series: lad.tz for lad in load_ladders()}


def load_wall_days(days: int | None) -> dict[tuple, dict]:
    """(target_date, ticker, side) -> wall trajectory summary."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat() if days else ""
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for path in sorted(SHADOW_DIR.glob("*.jsonl")):
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("venue") != "kalshi" or not row.get("live"):
                continue
            if (row.get("target_date") or "") < cutoff:
                continue
            groups[(row.get("target_date"), row.get("ticker"))].append(row)

    out: dict[tuple, dict] = {}
    for (tdate, ticker), rows in groups.items():
        rows.sort(key=lambda r: r.get("ts", ""))
        window_open = rows[0].get("ts")
        for side in ("yes", "no"):
            first = last = None
            peak_total = 0.0
            for row in rows:
                w = detect_wall(row.get(f"{side}_levels"))
                if w and w["kind"] == "defense":
                    first = first or row.get("ts")
                    last = w
                    peak_total = max(peak_total, w["total"])
            if first is None:
                continue
            out[(tdate, ticker, side)] = {
                "series": rows[0].get("series"), "first_seen": first,
                "censored": first == window_open, "peak_total": peak_total,
                "band": last["band"], "snapshots": len(rows),
            }
    return out


def fetch_results(tickers: list[str]) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for i, t in enumerate(tickers):
        try:
            req = urllib.request.Request(f"{KALSHI_BASE}/markets/{t}",
                                         headers={"User-Agent": "WeatherEdge/1.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                mk = json.loads(r.read()).get("market") or {}
            res = mk.get("result")
            out[t] = res if res in ("yes", "no") else None
        except Exception:  # noqa: BLE001 — one lookup must not kill the study
            out[t] = None
        if i % 10 == 9:
            time.sleep(0.5)
        time.sleep(0.12)
    return out


def local_hour(ts: str, tz: str | None) -> int | None:
    try:
        return datetime.fromisoformat(ts).astimezone(ZoneInfo(tz)).hour if tz else None
    except Exception:  # noqa: BLE001
        return None


def build_report(walls: dict, results: dict, tz_by_series: dict) -> str:
    lines = [f"**Wall fingerprint — {len(walls)} defense-wall sides across the archive**"]
    settled = {k: v for k, v in walls.items()
               if results.get(k[1]) in ("yes", "no")}

    # 1. Win rate: a YES-side wall wins when result==yes; NO-side when no.
    for censored in (False, True):
        subset = {k: v for k, v in settled.items() if v["censored"] == censored}
        if not subset:
            continue
        wins = sum(1 for (_, t, side), v in subset.items() if results[t] == side)
        tag = "censored (already there at window open)" if censored else "seen arriving mid-window"
        lines.append(f"  win rate, {tag}: {wins}/{len(subset)} = {wins/len(subset):.0%}")

    # 2. Arrival timing (uncensored only — true arrivals).
    arrivals = [(k, v) for k, v in settled.items() if not v["censored"]]
    hours = Counter()
    early_winners = []
    for (tdate, ticker, side), v in arrivals:
        h = local_hour(v["first_seen"], tz_by_series.get(v["series"]))
        if h is None:
            continue
        hours[h] += 1
        if h < PEAK_LOCAL_H and results[ticker] == side:
            early_winners.append((v["series"], tdate, ticker, side, h, v["peak_total"]))
    if hours:
        lines.append("  arrival local-hour histogram (uncensored): "
                     + ", ".join(f"{h:02d}h×{n}" for h, n in sorted(hours.items())))
    if early_winners:
        lines.append(f"  ⚠ pre-peak conviction walls that WON ({len(early_winners)}) — faster-flow suspects:")
        by_series = Counter(s for s, *_ in early_winners)
        for s, n in by_series.most_common():
            lines.append(f"    {s}: {n}")
        for s, tdate, ticker, side, h, tot in sorted(early_winners, key=lambda x: -x[5])[:8]:
            lines.append(f"      {ticker} {side.upper()} arrived {h:02d}h local, {tot:,.0f} contracts")

    # 3. Biggest walls.
    top = sorted(walls.items(), key=lambda kv: -kv[1]["peak_total"])[:8]
    lines.append("  largest walls:")
    for (tdate, ticker, side), v in top:
        res = results.get(ticker)
        verdict = "WON" if res == side else ("LOST" if res in ("yes", "no") else "pending")
        cen = " (censored)" if v["censored"] else ""
        lines.append(f"    {ticker} {side.upper()} {v['peak_total']:>9,.0f} @ "
                     f"{v['band'][0]:.0f}-{v['band'][1]:.0f}c → {verdict}{cen}")
    pending = sum(1 for k in walls if results.get(k[1]) is None)
    lines.append(f"  ({pending} sides on unsettled/unfetched markets excluded from rates)")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=None)
    args = ap.parse_args()
    walls = load_wall_days(args.days)
    tickers = sorted({t for (_, t, _) in walls})
    print(f"scanning settlements for {len(tickers)} tickers…", file=sys.stderr)
    results = fetch_results(tickers)
    print(build_report(walls, results, _series_tz()))


if __name__ == "__main__":
    main()
