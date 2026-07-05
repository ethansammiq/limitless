#!/usr/bin/env python3
"""POLY GATE ANALYZER — does the Polymarket daily-high sweep clear the bar?

The shadow logger has been capturing real L2 books on the 4 US Poly daily-high
events since 2026-07-02. This joins those books against IEM observations and
the resolved winners to answer the pre-registered go/no-go gate:

    >= +10c net-of-ask EV on >= 5 fills AND >= $50 fillable depth per entry

Unlike backtest/poly_sweep_probe.py (which used last-trade price, hiding the
spread), this reads the ASK YOU MUST CROSS and the DEPTH available there — the
exact quantity the June capacity finding said was the binding constraint.

Method, per city+day with both shadow books and a resolved event:
  1. IEM running max at each cutoff (16/17/18 local) -> favorite-so-far bracket.
  2. Nearest shadow snapshot to the cutoff -> that bracket's yes_ask + ask_cum5c.
  3. Fill if ask <= --max-entry (default 70c); EV = (100 if that bracket is the
     resolved winner else 0) - ask - slippage; depth$ = ask_cum5c * ask/100.
  4. Aggregate per cutoff and overall; print the gate verdict + JSON artifact.

Poly-only for now (the gate IS a Polymarket scale decision). Reuses
poly_sweep_probe for obs/events/brackets so settlement quirks count against us.

Usage:
    python3 backtest/poly_gate_analyzer.py                 # all shadow days
    python3 backtest/poly_gate_analyzer.py --max-entry 60 --slippage 2
    python3 backtest/poly_gate_analyzer.py --report discord
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(HERE))

from poly_sweep_probe import (  # noqa: E402
    CITIES,
    _load_cache,
    _save_cache,
    discover_events,
    event_day,
    event_station,
    fetch_day_obs,
    parse_bracket,
)

SHADOW_DIR = PROJECT_ROOT / "logs" / "shadow_books"
VERDICT_FILE = HERE / "poly_gate_verdict.json"
CUTOFFS = (16, 17, 18)                 # local hours
SNAPSHOT_TOL_MIN = 45                  # nearest-cutoff match tolerance
GATE_MIN_EV_C = 10.0
GATE_MIN_FILLS = 5
GATE_MIN_DEPTH_DOLLARS = 50.0


def load_poly_snapshots(shadow_dir: Path = SHADOW_DIR) -> dict:
    """{(city, day): {ts_utc: {token_id: row}}} from poly shadow rows."""
    out: dict = {}
    for path in sorted(shadow_dir.glob("*.jsonl")):
        for line in path.read_text().splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("venue") != "poly" or not r.get("token_id"):
                continue
            city = r["series"].removeprefix("POLY_")
            key = (city, r["target_date"])
            out.setdefault(key, {}).setdefault(r["ts"], {})[str(r["token_id"])] = r
    return out


def bracket_for_runmax(brackets: dict, runmax: float) -> str | None:
    """token_id of the bracket whose inclusive range contains runmax."""
    for token, (lo, hi) in brackets.items():
        if lo <= runmax <= hi:
            return token
    return None


def nearest_snapshot(snapshots: dict, target_utc: datetime, tol_min: int = SNAPSHOT_TOL_MIN) -> dict | None:
    """The token->row map of the capture closest to target_utc, or None if
    the closest is beyond tol_min."""
    best, best_gap = None, None
    for ts, tokens in snapshots.items():
        gap = abs((datetime.fromisoformat(ts) - target_utc).total_seconds())
        if best_gap is None or gap < best_gap:
            best, best_gap = tokens, gap
    if best is None or best_gap > tol_min * 60:
        return None
    return best


def entry_ev_cents(ask: float, won: bool, slippage: float) -> float:
    return (100.0 if won else 0.0) - ask - slippage


def depth_dollars(ask_cum_contracts: float, ask_cents: float) -> float:
    return ask_cum_contracts * ask_cents / 100.0


def gate_verdict(fills: list[dict]) -> dict:
    """Pre-registered gate over the fill list (each: ev_cents, depth_dollars)."""
    n = len(fills)
    mean_ev = statistics.fmean(f["ev_cents"] for f in fills) if fills else 0.0
    med_depth = statistics.median(f["depth_dollars"] for f in fills) if fills else 0.0
    passed = (n >= GATE_MIN_FILLS and mean_ev >= GATE_MIN_EV_C
              and med_depth >= GATE_MIN_DEPTH_DOLLARS)
    return {
        "n_fills": n,
        "mean_ev_cents": round(mean_ev, 2),
        "median_depth_dollars": round(med_depth, 2),
        "win_rate": round(sum(1 for f in fills if f["won"]) / n, 3) if n else 0.0,
        "gate_pass": passed,
        "gate": {"min_ev_cents": GATE_MIN_EV_C, "min_fills": GATE_MIN_FILLS,
                 "min_depth_dollars": GATE_MIN_DEPTH_DOLLARS},
    }


def analyze(max_entry: float, slippage: float) -> dict:
    snapshots = load_poly_snapshots()
    if not snapshots:
        return {"error": "no poly shadow rows found", "fills": [], "by_cutoff": {}}
    days = sorted({day for _, day in snapshots})
    cache = _load_cache()
    events = discover_events(list(CITIES), max_days=40)

    # index resolved events by (city, day) -> (winner_token, station, {token: range})
    resolved: dict = {}
    for ev in events:
        city, day = ev["_city"], event_day(ev)
        station = event_station(ev)
        if not day or not station or day not in days:
            continue
        brackets, winner = {}, None
        for mkt in ev.get("markets") or []:
            rng = parse_bracket(mkt.get("question") or "")
            try:
                token = json.loads(mkt.get("clobTokenIds") or "[]")[0]
                yes_price = float(json.loads(mkt.get("outcomePrices") or "[]")[0])
            except (ValueError, IndexError, json.JSONDecodeError):
                continue
            if not rng:
                continue
            brackets[str(token)] = rng
            if yes_price > 0.99:
                winner = str(token)
        if winner:
            resolved[(city, day)] = (winner, station, brackets)

    fills: list[dict] = []
    pinned: list[dict] = []
    skips: dict[str, int] = {}
    for (city, day), snaps in sorted(snapshots.items()):
        info = resolved.get((city, day))
        if info is None:
            skips["no_resolved_event"] = skips.get("no_resolved_event", 0) + 1
            continue
        winner_token, station, brackets = info
        tz = ZoneInfo(CITIES[city]["tz"])
        try:
            obs = fetch_day_obs(cache, station, day, tz)
        except RuntimeError:
            skips["no_obs"] = skips.get("no_obs", 0) + 1
            continue
        _save_cache(cache)
        for cutoff in CUTOFFS:
            upto = [o["temp_f"] for o in obs if o["minutes"] <= cutoff * 60]
            if not upto:
                continue
            runmax = max(upto)
            fav_token = bracket_for_runmax(brackets, runmax)
            if fav_token is None:
                continue
            target = datetime(*map(int, day.split("-")), cutoff, tzinfo=tz).astimezone(timezone.utc)
            snap = nearest_snapshot(snaps, target)
            if snap is None or fav_token not in snap:
                skips["no_snapshot"] = skips.get("no_snapshot", 0) + 1
                continue
            row = snap[fav_token]
            ask = row.get("yes_ask")
            depth = row.get("ask_cum5c") or 0
            if ask is None or not 0 < ask < 100:
                continue
            won = fav_token == winner_token
            record = {
                "city": city, "day": day, "cutoff": cutoff, "runmax": runmax,
                "ask": ask, "won": won,
                "ev_cents": entry_ev_cents(ask, won, slippage),
                "depth_dollars": depth_dollars(depth, ask),
            }
            # Pinned favorites (ask > max_entry) are NOT skipped — they're the
            # cases the old 5-95 capture band hid. Bucketing them lets the
            # report show "favorite already priced, no cheap entry" explicitly
            # rather than as a blind no-snapshot skip.
            (pinned if ask > max_entry else fills).append(record)

    by_cutoff = {str(c): gate_verdict([f for f in fills if f["cutoff"] == c])
                 for c in CUTOFFS}
    pinned_win = sum(1 for p in pinned if p["won"]) / len(pinned) if pinned else 0.0
    return {"fills": fills, "overall": gate_verdict(fills),
            "by_cutoff": by_cutoff, "skips": skips,
            "pinned": {"n": len(pinned), "win_rate": round(pinned_win, 3),
                       "median_ask": round(statistics.median(p["ask"] for p in pinned), 1)
                       if pinned else 0.0},
            "params": {"max_entry": max_entry, "slippage": slippage},
            "days_covered": days}


def format_report(result: dict) -> str:
    if result.get("error"):
        return f"Poly gate: {result['error']}"
    o = result["overall"]
    lines = [f"**Poly gate — {len(result['fills'])} fills over {len(result['days_covered'])} day(s)**",
             f"days: {', '.join(result['days_covered'])}",
             f"overall: EV **{o['mean_ev_cents']:+.1f}¢**, {o['n_fills']} fills, "
             f"median depth **${o['median_depth_dollars']:.0f}**, win {o['win_rate']:.0%} "
             f"→ {'✅ PASS' if o['gate_pass'] else '❌ fail'}",
             "per cutoff (local):"]
    for c, v in result["by_cutoff"].items():
        lines.append(f"  {c}:00 — EV {v['mean_ev_cents']:+.1f}¢, {v['n_fills']} fills, "
                     f"${v['median_depth_dollars']:.0f} depth, win {v['win_rate']:.0%}")
    pin = result.get("pinned", {})
    if pin.get("n"):
        lines.append(f"pinned favorites (ask>{result['params']['max_entry']:.0f}¢, no cheap "
                     f"entry): {pin['n']} at median {pin['median_ask']:.0f}¢, "
                     f"win {pin['win_rate']:.0%} — edge already priced out")
    if result.get("skips"):
        lines.append("skips: " + ", ".join(f"{k}={v}" for k, v in result["skips"].items()))
    lines.append(f"gate: EV≥{GATE_MIN_EV_C}¢ AND ≥{GATE_MIN_FILLS} fills AND "
                 f"≥${GATE_MIN_DEPTH_DOLLARS} depth. "
                 f"n is small until a week of shadow data accrues.")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--max-entry", type=float, default=70.0, help="max ask to fill (cents)")
    ap.add_argument("--slippage", type=float, default=3.0, help="haircut cents/fill")
    ap.add_argument("--report", choices=("stdout", "discord"), default="stdout")
    args = ap.parse_args()

    result = analyze(args.max_entry, args.slippage)
    report = format_report(result)
    VERDICT_FILE.write_text(json.dumps(
        {k: v for k, v in result.items() if k != "fills"}, indent=1) + "\n")

    if args.report == "discord":
        try:
            import asyncio

            from notifications import send_discord_alert
            asyncio.run(send_discord_alert(
                title="📊 Poly gate analysis", description=report[:4096],
                color=0x9B59B6, context="poly_gate_analyzer"))
        except Exception as exc:  # noqa: BLE001
            print(f"discord send failed: {exc}", file=sys.stderr)
    print(report)


if __name__ == "__main__":
    main()
