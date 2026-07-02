#!/usr/bin/env python3
"""Stage-2.5 — true taker EV of the settlement-bias-corrected nowcast sweep.

Stage 2 showed the RAW 4pm running-max nowcast hits the winning bracket only
~62%, and correcting for the systematic ~0.8F hourly-METAR-vs-CLI under-read
(+0.5..1.0F) lifts hit rate to ~75%. But Stage 2 priced only the WINNER, so it
could not compute the EV of actually TRADING the corrected nowcast (which is the
wrong bracket ~25% of the time, and those entries are not in the winners cache).

This script closes that gap:
  1. For each settled day x (delta, cutoff) in a grid, compute the corrected
     nowcast bracket ticker = bracket_of(round(running_max(cutoff) + delta)).
  2. Fetch hourly candles for every distinct nowcast ticker NOT already cached
     (winners are in sweep_candles_cache.json). Cache to a separate file.
  3. Strategy: at `cutoff` local, BUY the nowcast bracket at its executable
     yes_ask quoted AT/BEFORE the decision time (no lookahead). Pay taker fee.
     Settle $1.00 if nowcast == winning_ticker, else $0.
  4. Report realized win rate, fill rate, mean/total EV net of fees, and a
     cheap-filter variant (only enter when ask < threshold).

No-lookahead: running max uses obs with hour_local <= cutoff; the price uses the
latest candle whose end_period_ts <= cutoff:00 local. Read-only except the
candle cache it writes. Run: python3 backtest/sweep_stage2_5.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from math import ceil, floor
from pathlib import Path
from statistics import mean, pstdev
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

HERE = ROOT / "backtest"
ASOS = json.loads((HERE / "asos_hourly_cache.json").read_text())
WINNERS_CACHE = HERE / "sweep_candles_cache.json"
NOWCAST_CACHE = HERE / "sweep_nowcast_candles_cache.json"
DAILY = HERE / "daily_data.jsonl"

TZ = {c: ASOS[c]["tz"] for c in ASOS}
TAKER_FEE_RATE = 0.07

DELTAS = [0.0, 0.5, 0.75, 1.0]
CUTOFFS = [15, 16, 17]
CHEAP_THRESHOLDS = [50, 60, 70]


def taker_fee_cents(p: int) -> int:
    p = max(0, min(100, p))
    return ceil(TAKER_FEE_RATE * p * (100 - p) / 100)


def round_half_up(x: float) -> int:
    return floor(x + 0.5)


def bracket_of(t: int, settlements: list[dict]) -> str | None:
    for s in settlements:
        st = s.get("strike_type")
        if st == "greater" and t > s["floor_strike"]:
            return s["ticker"]
        if st == "less" and t < s["cap_strike"]:
            return s["ticker"]
        if st == "between" and s["floor_strike"] <= t <= s["cap_strike"]:
            return s["ticker"]
    return None


def running_max_at(city: str, date: str, hour: int) -> float | None:
    day = ASOS[city]["days"].get(date)
    if not day:
        return None
    return max((o["running_max_f"] for o in day if o["hour_local"] <= hour), default=None)


def nowcast_ticker(day: dict, cutoff: int, delta: float) -> str | None:
    r = running_max_at(day["city"], day["date"], cutoff)
    if r is None:
        return None
    return bracket_of(round_half_up(r + delta), day["settlements"])


def ask_at_decision(candles: list[dict], date: str, city: str, cutoff: int) -> int | None:
    """Executable yes_ask (cents) at the LATEST candle whose close is <= cutoff:00
    local — i.e. the ask available at the decision moment (no lookahead). Returns
    None if the bracket has no quoted ask within 2h before the decision."""
    if not candles:
        return None
    cut_ts = datetime.fromisoformat(date).replace(
        hour=cutoff, tzinfo=ZoneInfo(TZ[city])
    ).astimezone(timezone.utc).timestamp()
    best_ts, best_ask = None, None
    for c in candles:
        ts = c.get("end_period_ts")
        if ts is None or ts > cut_ts:
            continue
        if cut_ts - ts > 2 * 3600:  # quote too stale to be executable
            continue
        a = (c.get("yes_ask") or {}).get("close_dollars")
        if a is None:
            continue
        ac = round(float(a) * 100)
        if not (0 < ac <= 100):
            continue
        if best_ts is None or ts > best_ts:
            best_ts, best_ask = ts, ac
    return best_ask


async def fetch_nowcast_candles(tickers: set[str]) -> dict:
    winners = json.loads(WINNERS_CACHE.read_text()) if WINNERS_CACHE.exists() else {}
    nc = json.loads(NOWCAST_CACHE.read_text()) if NOWCAST_CACHE.exists() else {}
    need = [t for t in tickers if t not in winners and t not in nc]
    if not need:
        print(f"  candle cache complete ({len(tickers)} tickers, 0 to fetch)", file=sys.stderr)
        return {**winners, **nc}

    from kalshi_client import KalshiClient
    # ticker -> the date its candles belong to (settlement day)
    ET = ZoneInfo("America/New_York")
    kc = KalshiClient()
    await kc.start()
    fetched = 0
    try:
        for tk in need:
            date = TICKER_DATE.get(tk)
            if not date:
                nc[tk] = []
                continue
            series = tk.split("-")[0]
            y, m, d = (int(x) for x in date.split("-"))
            start = int(datetime(y, m, d, 0, 0, tzinfo=ET).timestamp())
            end = int(datetime(y, m, d, 23, 59, tzinfo=ET).timestamp()) + 7 * 3600
            path = (f"/series/{series}/markets/{tk}/candlesticks"
                    f"?start_ts={start}&end_ts={end}&period_interval=60")
            try:
                resp = await kc._req_safe("GET", path, auth=True)
                nc[tk] = resp.get("candlesticks", []) if isinstance(resp, dict) else []
            except Exception as exc:  # noqa: BLE001
                print(f"  fetch fail {tk}: {exc}", file=sys.stderr)
                nc[tk] = []
            fetched += 1
            if fetched % 25 == 0:
                print(f"  fetched {fetched}/{len(need)}...", file=sys.stderr)
                NOWCAST_CACHE.write_text(json.dumps(nc))
    finally:
        await kc.stop()
    NOWCAST_CACHE.write_text(json.dumps(nc))
    print(f"  done: {fetched} new nowcast tickers fetched", file=sys.stderr)
    return {**winners, **nc}


TICKER_DATE: dict[str, str] = {}


def evaluate(days: list[dict], candles: dict) -> None:
    print(f"\n{'cut':>3} {'delta':>5} {'fills':>6} {'win%':>5} {'avgAsk':>6} "
          f"{'avgFee':>6} {'meanEV':>7} {'totEV$':>7} {'%win>0':>6}")
    print("-" * 70)
    grid = {}
    for cutoff in CUTOFFS:
        for delta in DELTAS:
            entries = []  # (ev_cents, ask, fee, hit)
            n_signals = n_nofill = 0
            for day in days:
                wt = day.get("winning_ticker")
                if not wt:
                    continue
                tk = nowcast_ticker(day, cutoff, delta)
                if tk is None:
                    continue
                n_signals += 1
                ask = ask_at_decision(candles.get(tk, []), day["date"], day["city"], cutoff)
                if ask is None:
                    n_nofill += 1
                    continue
                fee = taker_fee_cents(ask)
                hit = (tk == wt)
                payoff = 100 if hit else 0
                ev = payoff - ask - fee
                entries.append((ev, ask, fee, hit))
            if not entries:
                continue
            evs = [e[0] for e in entries]
            grid[(cutoff, delta)] = entries
            print(f"{cutoff:>3} {delta:>5} {len(entries):>6} "
                  f"{100*sum(e[3] for e in entries)/len(entries):>5.1f} "
                  f"{mean(e[1] for e in entries):>6.1f} "
                  f"{mean(e[2] for e in entries):>6.1f} "
                  f"{mean(evs):>7.2f} {sum(evs)/100:>7.2f} "
                  f"{100*sum(1 for e in evs if e>0)/len(evs):>6.1f}")
    print(f"  (signals that produced NO executable ask were dropped as no-fill;"
          f" fill rate shown via 'fills' vs ~{len([d for d in days if d.get('winning_ticker')])} days)")

    # cheap-filter variant on the best raw cell
    print(f"\nCHEAP-FILTER (only enter when nowcast ask < threshold):")
    print(f"{'cut':>3} {'delta':>5} {'thr':>4} {'trades':>6} {'win%':>5} "
          f"{'avgAsk':>6} {'meanEV':>7} {'totEV$':>7}")
    print("-" * 60)
    for (cutoff, delta), entries in grid.items():
        if delta not in (0.5, 0.75):  # focus on the corrected cells
            continue
        for thr in CHEAP_THRESHOLDS:
            sub = [e for e in entries if e[1] < thr]
            if not sub:
                continue
            evs = [e[0] for e in sub]
            print(f"{cutoff:>3} {delta:>5} {thr:>4} {len(sub):>6} "
                  f"{100*sum(e[3] for e in sub)/len(sub):>5.1f} "
                  f"{mean(e[1] for e in sub):>6.1f} "
                  f"{mean(evs):>7.2f} {sum(evs)/100:>7.2f}")

    # robustness on the headline cell (cutoff=16, delta=0.75)
    key = (16, 0.75)
    if key in grid:
        evs = [e[0] for e in grid[key]]
        m, s = mean(evs), pstdev(evs)
        print(f"\nHEADLINE cell cut=16 delta=0.75: n={len(evs)} meanEV={m:.2f}¢ "
              f"std={s:.1f}¢ sharpe/trade={m/s:.3f} "
              f"95%CI≈[{m-1.96*s/len(evs)**0.5:.2f}, {m+1.96*s/len(evs)**0.5:.2f}]¢")


async def main():
    days = [json.loads(l) for l in DAILY.read_text().splitlines() if l.strip()]
    # union of all nowcast tickers across the grid + record their settlement date
    need: set[str] = set()
    for day in days:
        if not day.get("winning_ticker"):
            continue
        for cutoff in CUTOFFS:
            for delta in DELTAS:
                tk = nowcast_ticker(day, cutoff, delta)
                if tk:
                    need.add(tk)
                    TICKER_DATE[tk] = day["date"]
    print(f"distinct nowcast tickers across grid: {len(need)}", file=sys.stderr)
    candles = await fetch_nowcast_candles(need)
    evaluate(days, candles)


if __name__ == "__main__":
    asyncio.run(main())
