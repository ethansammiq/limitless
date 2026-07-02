#!/usr/bin/env python3
"""Stage-2 make-or-break test for the intraday dead-bracket / nowcast sweep.

Question: by late afternoon, does the running MAX temperature (from ASOS, the
same sensor Kalshi settles on) already identify the winning daily-high bracket —
AND was that bracket still buyable cheaply then?

Inputs (all already on disk, no network):
  - backtest/asos_hourly_cache.json   running max per local hour per station
  - backtest/daily_data.jsonl         settled outcomes + bracket strike defs
  - backtest/sweep_candles_cache.json hourly candles for the WINNING bracket

For each settled (city, date) and each cutoff hour H (local):
  1. running_max(H) = max observed temp at/before H:59 local
  2. nowcast bracket = the bracket running_max(H) falls into (strike rules)
  3. hit = (nowcast bracket ticker == winning ticker)
  4. winner ask at H = yes_ask of the winning bracket's candle nearest H:00 local
Reports hit rate (the forecasting test), the winner's price at H (the entry
test), and their joint — the realistic sweep edge. Read-only analysis.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
ASOS = json.loads((HERE / "asos_hourly_cache.json").read_text())
CANDLES = json.loads((HERE / "sweep_candles_cache.json").read_text())

CUTOFFS = [13, 14, 15, 16, 17]  # local hour
CHEAP_CENTS = 50

TZ = {c: ASOS[c]["tz"] for c in ASOS}


def _round_half_up(x: float) -> int:
    """NWS rounds x.50 up, x.49 down — to nearest whole °F."""
    from math import floor
    return floor(x + 0.5)


def _bracket_of(temp_int: int, settlements: list[dict]) -> str | None:
    """Ticker whose strike rule contains an integer temperature."""
    for s in settlements:
        st = s.get("strike_type")
        if st == "greater" and temp_int > s["floor_strike"]:
            return s["ticker"]
        if st == "less" and temp_int < s["cap_strike"]:
            return s["ticker"]
        if st == "between" and s["floor_strike"] <= temp_int <= s["cap_strike"]:
            return s["ticker"]
    return None


def _running_max_at(city: str, date: str, hour: int) -> float | None:
    day = ASOS[city]["days"].get(date)
    if not day:
        return None
    obs = [o for o in day if o["hour_local"] <= hour]
    return max((o["running_max_f"] for o in obs), default=None)


def _winner_ask_cents(ticker: str, date: str, city: str, hour: int) -> int | None:
    """yes_ask (cents) of the winning bracket's candle nearest H:00 local."""
    candles = CANDLES.get(ticker)
    if not candles:
        return None
    target = datetime.fromisoformat(date).replace(
        hour=hour, tzinfo=ZoneInfo(TZ[city])
    ).astimezone(timezone.utc).timestamp()
    best, best_gap = None, 70 * 60  # within ±70 min of cutoff
    for c in candles:
        ts = c.get("end_period_ts")
        if ts is None:
            continue
        gap = abs(ts - target)
        if gap <= best_gap:
            best, best_gap = c, gap
    if best is None:
        return None
    ya = (best.get("yes_ask") or {}).get("close_dollars")
    px = ya or (best.get("price") or {}).get("close_dollars")
    if px is None:
        return None
    cents = round(float(px) * 100)
    return cents if cents > 0 else None


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def run() -> None:
    days = [json.loads(l) for l in (HERE / "daily_data.jsonl").read_text().splitlines() if l.strip()]

    # sanity: does actual_high map to the winning ticker via our strike rules?
    sane = mism = 0
    for d in days:
        wt = d.get("winning_ticker")
        if not wt:
            continue
        b = _bracket_of(_round_half_up(d["actual_high"]), d["settlements"])
        if b == wt:
            sane += 1
        else:
            mism += 1
    print(f"strike-rule sanity: actual_high→bracket matches winning_ticker on "
          f"{sane}/{sane+mism} days ({mism} mismatch)\n")

    print(f"{'H':>3} {'n':>4} {'hit%':>5} {'medMax-Act':>10} "
          f"{'medAsk¢':>8} {'<50¢%':>6} {'hit&cheap%':>10} {'EV¢/contract':>12}")
    print("-" * 66)

    for H in CUTOFFS:
        n = hits = cheap = hit_and_cheap = 0
        gaps: list[float] = []
        winner_asks: list[int] = []
        ev_terms: list[float] = []
        for d in days:
            city, date, wt = d["city"], d["date"], d.get("winning_ticker")
            if not wt:
                continue
            rmax = _running_max_at(city, date, H)
            if rmax is None:
                continue
            n += 1
            gaps.append(round(d["actual_high"] - rmax, 1))
            nowcast = _bracket_of(_round_half_up(rmax), d["settlements"])
            hit = nowcast == wt
            hits += hit

            ask = _winner_ask_cents(wt, date, city, H)
            if ask is not None:
                winner_asks.append(ask)
                if ask < CHEAP_CENTS:
                    cheap += 1
                    if hit:
                        hit_and_cheap += 1
                # realistic strategy: buy the NOWCAST bracket at H.
                # right days: pay winner ask, settle $1. wrong days: lose entry.
                # (nowcast-bracket price on wrong days isn't cached → use winner
                #  ask as a same-ballpark proxy for entry cost; flagged below.)
                ev_terms.append((100 - ask) if hit else (-ask))
        hit_pct = 100 * hits / n if n else 0
        cheap_pct = 100 * cheap / len(winner_asks) if winner_asks else 0
        hc_pct = 100 * hit_and_cheap / n if n else 0
        ev = _median(ev_terms)
        print(f"{H:>3} {n:>4} {hit_pct:>5.1f} {str(_median(gaps)):>10} "
              f"{str(_median(winner_asks)):>8} {cheap_pct:>6.1f} {hc_pct:>10.1f} "
              f"{('%.1f' % (sum(ev_terms)/len(ev_terms))) if ev_terms else 'n/a':>12}")

    print("\nLegend: hit% = running-max bracket == winning bracket (the forecast test).")
    print("medMax-Act = median (final high − running max at H); ~0 means peak already in.")
    print("medAsk¢ = median price of the WINNING bracket at H (the entry test).")
    print("EV¢ = mean of: (100−ask) on hit days, (−ask) on miss days, using winner")
    print("      ask as entry-cost proxy on miss days (nowcast-bracket price not cached).")


if __name__ == "__main__":
    run()
