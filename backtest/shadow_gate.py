#!/usr/bin/env python3
"""SHADOW GATE — evaluate the Poly executor decision gate on captured books.

The gate set on 2026-07-02 when the shadow logger shipped: build a live
executor only when the shadow data shows, at the entry cutoff,
  1. net-of-real-ask EV >= +10c/contract,
  2. on >= 5 distinct entries,
  3. with >= $50 fillable ask-side depth per entry.

Everything upstream of this script measured the signal against last-trade
prices (candles, prices-history), which hide the spread. This joins the
REAL crossable asks + resting depth that shadow_logger captured live
(logs/shadow_books/*.jsonl) against:
  - the IEM METAR archive: the running max the sweep would have known at
    each snapshot's own timestamp (no lookahead), and
  - Poly's own resolved winners (gamma outcomePrices), so Wunderground
    settlement quirks count against us, exactly as in poly_sweep_probe.

An entry at cutoff H = the latest live-book snapshot at/before H:00 local
whose bracket contains the rounded running max at that snapshot's time.
Snapshots staler than --max-stale-min are discarded rather than trusted —
a book from a sleeping-Mac gap is not a fill.

Read-only; no trading path. Reuses poly_sweep_probe's obs/event plumbing
and its cache file (obs are shared; resolved events get a new cache key).

Usage:
    python3 backtest/shadow_gate.py                    # cutoffs 16,17,18
    python3 backtest/shadow_gate.py --cutoffs 17 -v    # per-entry detail
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from statistics import mean, median
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

import poly_sweep_probe as probe  # noqa: E402 — sibling module, shared cache
from shadow_logger import _TZ, POLY_CITIES  # noqa: E402

BOOKS_DIR = ROOT / "logs" / "shadow_books"

GATE_MIN_EV_C = 10.0
GATE_MIN_ENTRIES = 5
GATE_MIN_DEPTH_D = 50.0
DEPTH_BAND_C = 5.0


def round_half_up(x: float) -> int:
    """WU displays integer °F; 89.5 must go UP a bracket, not banker's-round."""
    return int(x + 0.5) if x >= 0 else -int(-x + 0.5)


def contains(bracket: tuple[float, float], temp_f: float) -> bool:
    lo, hi = bracket
    return lo <= round_half_up(temp_f) <= hi


def depth_dollars(ask_levels: list, band_c: float = DEPTH_BAND_C) -> float:
    """Dollars of resting asks within band_c of the best ask."""
    lvls = sorted(
        ((float(p), float(s)) for p, s in ask_levels or []), key=lambda x: x[0])
    if not lvls:
        return 0.0
    best = lvls[0][0]
    return round(sum(p / 100 * s for p, s in lvls if p <= best + band_c), 2)


def ev_cents(ask_c: float, won: bool) -> float:
    """Net-of-real-ask EV per contract; Poly taker fees are ~zero."""
    return round((100 - ask_c) if won else -ask_c, 1)


def runmax_at(obs: list[dict], minutes: int) -> float | None:
    upto = [o["temp_f"] for o in obs if o["minutes"] <= minutes]
    return max(upto) if upto else None


def pick_entry(snapshots: list[dict], cutoff_min: int, max_stale_min: int) -> dict | None:
    """Latest on-signal live-book snapshot at/before the cutoff, if fresh enough.

    Each snapshot: {min, bracket, ask, ask_levels, on_signal, live}.
    """
    best = None
    for snap in snapshots:
        if snap["min"] > cutoff_min or not snap["live"] or not snap["on_signal"]:
            continue
        if snap["ask"] is None or cutoff_min - snap["min"] > max_stale_min:
            continue
        if best is None or snap["min"] > best["min"]:
            best = snap
    return best


def gate_verdict(entries: list[dict]) -> dict:
    """Apply the three 2026-07-02 criteria to one cutoff's entries."""
    n = len(entries)
    evs = [e["ev_c"] for e in entries]
    depths = [e["depth_d"] for e in entries]
    mean_ev = round(mean(evs), 1) if evs else None
    med_depth = round(median(depths), 2) if depths else None
    checks = {
        "ev": mean_ev is not None and mean_ev >= GATE_MIN_EV_C,
        "n": n >= GATE_MIN_ENTRIES,
        "depth": med_depth is not None and med_depth >= GATE_MIN_DEPTH_D,
    }
    return {"n": n, "mean_ev_c": mean_ev, "median_depth_d": med_depth,
            "checks": checks, "passed": all(checks.values())}


def resolve_event(cache: dict, city: str, day: str) -> dict | None:
    """{station, winner: [lo, hi]} for a settled Poly city-day, cached.

    Winner comes from gamma's own outcomePrices so WU quirks are honored.
    """
    key = f"{city}@{day}"
    events_cache = cache.setdefault("events", {})
    if key in events_cache:
        return events_cache[key]
    title_prefix = POLY_CITIES[city]["title"]
    found = None
    for offset in (0, 100):
        batch = probe._get_json(
            f"{probe.GAMMA_URL}/events?closed=true&tag_slug=weather"
            f"&end_date_min={day}T00:00:00Z&end_date_max={day}T23:59:59Z"
            f"&limit=100&offset={offset}")
        events = batch if isinstance(batch, list) else batch.get("events", [])
        if not events:
            break
        for ev in events:
            if ev.get("title", "").startswith(title_prefix) and probe.event_day(ev) == day:
                found = ev
                break
        if found:
            break
    result = None
    if found:
        station = probe.event_station(found)
        winner = None
        for mkt in found.get("markets") or []:
            try:
                prices = json.loads(mkt.get("outcomePrices") or "[]")
            except json.JSONDecodeError:
                continue
            if prices and float(prices[0]) == 1.0:
                winner = probe.parse_bracket(mkt.get("question") or "")
                break
        if station and winner:
            result = {"station": station, "winner": list(winner)}
    # Cache only resolved events: an unresolved lookup is a statement about
    # NOW (markets settle the next morning), and caching it would freeze
    # yesterday's "not yet" into every future re-run.
    if result:
        events_cache[key] = result
    return result


def load_snapshot_days() -> dict[tuple[str, str], list[dict]]:
    """(city, day) -> raw poly shadow rows, oldest file first."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for path in sorted(BOOKS_DIR.glob("*.jsonl")):
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("venue") != "poly":
                continue
            city = (row.get("series") or "")[5:]
            if city in POLY_CITIES and row.get("target_date"):
                groups.setdefault((city, row["target_date"]), []).append(row)
    return groups


def to_snapshot(row: dict, tz: ZoneInfo) -> dict | None:
    """Shadow row -> join-ready snapshot (local minutes, bracket, book)."""
    bracket = probe.parse_bracket(row.get("ticker") or "")
    ts_raw = row.get("ts")
    if bracket is None or not ts_raw:
        return None
    loc = datetime.fromisoformat(ts_raw).astimezone(tz)
    if loc.strftime("%Y-%m-%d") != row["target_date"]:
        return None  # captured outside the target's local day (late UTC runs)
    return {
        "min": loc.hour * 60 + loc.minute,
        "bracket": bracket,
        "ask": row.get("yes_ask"),
        "ask_levels": row.get("ask_levels"),
        # pre-2026-07-05 rows had no live flag: book presence implies live
        "live": bool(row.get("ask_levels")) and row.get("live", True),
        "on_signal": False,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cutoffs", default="16,17,18",
                    help="local entry hours, comma-separated")
    ap.add_argument("--gate-cutoff", type=int, default=17,
                    help="cutoff hour the PASS/FAIL verdict is applied to")
    ap.add_argument("--max-stale-min", type=int, default=45,
                    help="discard entries whose book snapshot is staler than this")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    cutoffs = [int(c) for c in args.cutoffs.split(",")]

    cache = probe._load_cache()
    groups = load_snapshot_days()
    skipped = {"unresolved_event": 0, "no_obs": 0, "no_snapshots": 0}
    entries: dict[int, list[dict]] = {c: [] for c in cutoffs}

    for (city, day), rows in sorted(groups.items()):
        tz = ZoneInfo(_TZ[POLY_CITIES[city]["tz"]])
        try:
            event = resolve_event(cache, city, day)
        except RuntimeError as exc:
            print(f"  {city} {day}: gamma fetch failed: {exc}", file=sys.stderr)
            event = None
        if not event:
            skipped["unresolved_event"] += 1
            continue
        try:
            obs = probe.fetch_day_obs(cache, event["station"], day, tz)
        except RuntimeError as exc:
            print(f"  {city} {day}: obs fetch failed: {exc}", file=sys.stderr)
            obs = []
        if not obs:
            skipped["no_obs"] += 1
            continue

        snapshots = [s for s in (to_snapshot(r, tz) for r in rows) if s]
        for snap in snapshots:
            rm = runmax_at(obs, snap["min"])
            snap["on_signal"] = rm is not None and contains(snap["bracket"], rm)
        if not snapshots:
            skipped["no_snapshots"] += 1
            continue

        winner = tuple(event["winner"])
        for cutoff in cutoffs:
            entry = pick_entry(snapshots, cutoff * 60, args.max_stale_min)
            if entry is None:
                continue
            won = entry["bracket"] == winner
            rec = {
                "city": city, "day": day, "cutoff": cutoff,
                "ask_c": entry["ask"], "ev_c": ev_cents(entry["ask"], won),
                "depth_d": depth_dollars(entry["ask_levels"]),
                "stale_min": cutoff * 60 - entry["min"], "won": won,
            }
            entries[cutoff].append(rec)
            if args.verbose:
                print(f"  {city} {day} @{cutoff:02d}:00 ask={rec['ask_c']:.0f}c "
                      f"depth=${rec['depth_d']:.0f} stale={rec['stale_min']}m "
                      f"{'WIN' if won else 'LOSS'} ev={rec['ev_c']:+.0f}c")

    probe._save_cache(cache)

    print(f"\ncity-days with poly books: {len(groups)}   "
          f"skipped: {', '.join(f'{k}={v}' for k, v in skipped.items() if v)}")
    print(f"{'cutoff':>6s} {'n':>3s} {'hit%':>6s} {'med_ask':>8s} "
          f"{'mean_EV(c)':>11s} {'med_depth$':>11s}")
    for cutoff in cutoffs:
        ent = entries[cutoff]
        if not ent:
            print(f"{cutoff:>6d}   0      -        -           -           -")
            continue
        hit = 100 * sum(e["won"] for e in ent) / len(ent)
        print(f"{cutoff:>6d} {len(ent):>3d} {hit:>5.0f}% "
              f"{median(e['ask_c'] for e in ent):>7.0f}c "
              f"{mean(e['ev_c'] for e in ent):>+10.1f} "
              f"{median(e['depth_d'] for e in ent):>10.0f}")

    verdict = gate_verdict(entries.get(args.gate_cutoff, []))
    checks = verdict["checks"]
    print(f"\nGATE @ {args.gate_cutoff}:00 local — "
          f"EV {verdict['mean_ev_c']}c (need >= +{GATE_MIN_EV_C:.0f}) "
          f"[{'PASS' if checks['ev'] else 'fail'}], "
          f"n {verdict['n']} (need >= {GATE_MIN_ENTRIES}) "
          f"[{'PASS' if checks['n'] else 'fail'}], "
          f"depth ${verdict['median_depth_d']} (need >= ${GATE_MIN_DEPTH_D:.0f}) "
          f"[{'PASS' if checks['depth'] else 'fail'}]")
    print("VERDICT: " + ("PASS — executor build is justified"
                         if verdict["passed"]
                         else "NOT YET — keep accruing shadow data"))


if __name__ == "__main__":
    main()
