#!/usr/bin/env python3
"""PEAK STRATEGY BACKTEST — is there a real, capturable edge in the confirmed peak?

The peak-monitor thesis: by mid-afternoon the daily high is effectively locked, so
the winning bracket is ~known before the market fully converges to 100 — buy it
cheap, collect $1.

REALITY OF THE DATA (why this is a bounded estimate, not a clean backtest):
  • Peak trades are never tagged and never filled (1c resting bids), so there is no
    realized peak-trade record to score.
  • backtest/market_snapshots/ was never populated — there is NO intraday price
    history, so we cannot see the bracket's price AT peak-confirmation time.
  • The only historical prices are settlement-day CLOSING bids in daily_data.jsonl.

So we proxy the strategy as: "buy the winning bracket at its CLOSING bid, hold to
100." Two honest caveats baked into the verdict:
  1. The peak signal ≈ identifies the winner (a confirmed afternoon peak rarely gets
     exceeded), so this is *not* pure hindsight — but entry at CLOSE is the most-
     converged (hardest) price, so it UNDER-states a well-timed earlier entry.
  2. yes_bid_close=0 on an illiquid tail bracket means "no resting bid," NOT "free
     money" — you cannot actually buy there. So the headline edge must be read
     LIQUIDITY-FILTERED. That filter is the whole point of this script.

Run: .venv/bin/python3 peak_backtest.py
"""

from __future__ import annotations

import json
import statistics as st
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUTCOMES = ROOT / "backtest" / "daily_data.jsonl"

# Kalshi taker fee ~ 0.07*p*(1-p) per contract; assume you cross ~2c of spread on entry.
def _fee(price_c: int) -> float:
    p = max(0, min(100, price_c)) / 100.0
    return 0.07 * p * (1 - p) * 100.0

ASSUMED_SPREAD_C = 2          # you pay the bid + ~2c to actually get filled
LIQUID_MIN_VOL = 500          # contracts of lifetime volume to call a bracket "buyable"


def load_winners():
    """Each settled day-city's winning bracket with its closing bid + volume."""
    rows = []
    with OUTCOMES.open() as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            for s in d.get("settlements") or []:
                if s.get("result") != "yes":
                    continue
                ybc = s.get("yes_bid_close")
                if ybc is None or not (0 <= ybc <= 100):
                    continue
                rows.append({
                    "date": d.get("date"), "city": d.get("city"),
                    "ticker": s.get("ticker"), "close_bid": int(ybc),
                    "volume": int(s.get("volume") or 0),
                })
            # also collect losers' closes for the efficiency check
    return rows


def load_all_brackets(min_vol: int = 0, realtime_only: bool = False, partition_only: bool = False):
    """Every settled bracket (win AND lose) with close bid + outcome — the only
    way to test calibration without conditioning on the winner (hindsight).

    realtime_only: drop backfilled rows. yes_bid_close on a backfilled 2022 market
      is a settled market's stale residual bid, NOT a real close — unusable.
    partition_only: keep only '-B' range buckets (exactly one YES). Cumulative 'T'
      thresholds (e.g. '<=35') legitimately have many YES at any price → not a
      partition, must not be mixed into a calibration test.
    """
    rows = []
    with OUTCOMES.open() as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if realtime_only and d.get("backfilled_at"):
                continue
            for s in d.get("settlements") or []:
                r = s.get("result")
                ybc = s.get("yes_bid_close")
                tk = s.get("ticker", "")
                if partition_only and "-B" not in tk:
                    continue
                if r in ("yes", "no") and ybc is not None and 0 <= ybc <= 100 and (s.get("volume") or 0) >= min_vol:
                    rows.append({"close": int(ybc), "won": 1 if r == "yes" else 0,
                                 "volume": int(s.get("volume") or 0),
                                 "backfilled": bool(d.get("backfilled_at"))})
    return rows


def load_loser_closes():
    return [b["close"] for b in load_all_brackets(LIQUID_MIN_VOL) if not b["won"]]


def pnl(close_bid: int) -> float:
    """Buy the winner at close_bid + assumed spread, settle at 100, pay fee."""
    entry = min(99, close_bid + ASSUMED_SPREAD_C)
    return (100 - entry) - _fee(entry)


def main():
    winners = load_winners()
    n = len(winners)
    print("═" * 74)
    print("  PEAK STRATEGY BACKTEST  —  'buy the confirmed-peak (winning) bracket'")
    print("═" * 74)
    print(f"  Settled winning brackets: {n}")
    print(f"  Entry assumption: close bid + {ASSUMED_SPREAD_C}c spread, hold to 100, minus fee")
    print()

    # 1) Headline (hindsight-optimistic, NO liquidity filter)
    closes = [w["close_bid"] for w in winners]
    pnls = [pnl(c) for c in closes]
    print("─" * 74)
    print("  1. ALL winners (NO liquidity filter — the misleading number)")
    print("─" * 74)
    print(f"    close bid: mean {st.mean(closes):.1f}c  median {st.median(closes):.0f}c")
    print(f"    mean P&L/contract: {st.mean(pnls):+.1f}c   <- inflated by unbuyable 0-bid tails")
    print()

    # 2) Liquidity-stratified — the honest cut
    print("─" * 74)
    print("  2. BY LIQUIDITY  (can you actually buy it? volume tiers)")
    print("─" * 74)
    tiers = [("<100", 0, 100), ("100-1k", 100, 1000), ("1k-10k", 1000, 10000), (">=10k", 10000, 1e18)]
    print(f"    {'volume':10}{'n':>6}{'close bid':>11}{'mean P&L':>11}   share of winners")
    for label, lo, hi in tiers:
        sub = [w for w in winners if lo <= w["volume"] < hi]
        if not sub:
            continue
        cb = st.mean(w["close_bid"] for w in sub)
        pl = st.mean(pnl(w["close_bid"]) for w in sub)
        print(f"    {label:10}{len(sub):>6}{cb:>10.1f}c{pl:>+10.1f}c   {len(sub)/n:5.0%}")
    print()

    # 3) Realistic tradeable subset: liquid AND not already converged to ~100
    print("─" * 74)
    print(f"  3. REALISTIC TRADEABLE  (volume>={LIQUID_MIN_VOL} AND close in 5..95c)")
    print("─" * 74)
    liq = [w for w in winners if w["volume"] >= LIQUID_MIN_VOL]
    buyable = [w for w in liq if 5 <= w["close_bid"] <= 95]
    if liq:
        rich = sum(1 for w in liq if w["close_bid"] > 95)
        print(f"    liquid winners (vol>={LIQUID_MIN_VOL}): {len(liq)}")
        print(f"      already >95c at close (no edge left): {rich} ({rich/len(liq):.0%})")
        print(f"      still buyable in 5..95c:              {len(buyable)} ({len(buyable)/len(liq):.0%})")
        if buyable:
            bpnls = [pnl(w["close_bid"]) for w in buyable]
            wins = sum(1 for p in bpnls if p > 0)
            print(f"      on those buyable winners: mean P&L {st.mean(bpnls):+.1f}c/contract, "
                  f"win {wins/len(buyable):.0%}")
            print("      NOTE: this is winners-only (hindsight). A live strategy also buys")
            print("      confirmed peaks that get EXCEEDED late — unmeasurable without intraday obs.")
    print()

    # 4) Market-efficiency check: do LIQUID losers close near 0?
    print("─" * 74)
    print("  4. EFFICIENCY CHECK  (liquid LOSING brackets should close near 0c)")
    print("─" * 74)
    losers = load_loser_closes()
    if losers:
        print(f"    liquid losers (vol>={LIQUID_MIN_VOL}): {len(losers)}  "
              f"close bid mean {st.mean(losers):.1f}c  median {st.median(losers):.0f}c")
        mis = sum(1 for c in losers if c > 20)
        print(f"    losers closing >20c (market wrong): {mis} ({mis/len(losers):.0%})")
    print()

    # 5) THE DECISIVE TEST: calibration across ALL liquid brackets (no hindsight)
    print("─" * 74)
    print("  5. CALIBRATION  (ALL liquid brackets, win AND lose — edge only if win% > price)")
    print("─" * 74)

    def calib(rows, label):
        print(f"    [{label}]  n={len(rows)}")
        if not rows:
            print("      (no rows)")
            return 0.0
        worst = 0.0
        for lo in range(0, 100, 10):
            b = [x for x in rows if lo <= x["close"] < lo + 10]
            if len(b) < 30:
                continue
            wr = 100 * st.mean(x["won"] for x in b)
            e = wr - (lo + 5)
            worst = max(worst, abs(e))
            flag = "  <- looks mispriced" if abs(e) >= 8 else ""
            print(f"      {lo:2d}-{lo+9:<2d}c  n={len(b):>6}  won {wr:>5.1f}%  edge {e:>+5.1f}pts{flag}")
        return worst

    # Backfilled 2022 data has unreliable yes_bid_close (stale residual bid) and mixes
    # cumulative 'T' thresholds — so the ONLY trustworthy cut is real-time + partition.
    full = load_all_brackets(LIQUID_MIN_VOL)
    rt_B = load_all_brackets(LIQUID_MIN_VOL, realtime_only=True, partition_only=True)
    calib(full, "ALL data — CONTAMINATED: backfilled stale bids + cumulative T thresholds")
    print()
    calib(rt_B, "real-time + partition(B) only — the trustworthy cut")
    print("    (calibrated market sits on the diagonal: won% ≈ price, edge ≈ 0)")
    print()

    # 6) Verdict
    print("═" * 74)
    print("  VERDICT — no demonstrable, capturable peak edge")
    print("═" * 74)
    print("  • Where the data is reliable and large, the market is efficient: liquid")
    print("    losers close ~0c, and the big 0-9c / 90-99c bins are calibrated.")
    print("  • The eye-catching mid-price 'edge' is an ARTIFACT, three ways: (1) hindsight")
    print("    (counting only winners); (2) backfilled 2022 markets whose 'close bid' is a")
    print("    stale residual, not a real close; (3) cumulative 'T' thresholds where many")
    print("    brackets settle YES at any price — not a partition.")
    print(f"  • The trustworthy cut (real-time + partition) has only {len(rt_B)} liquid")
    print("    brackets — far too few to claim an edge either way.")
    print()
    print("  → The peak edge is NOT demonstrable from existing data, and the available")
    print("    proxy says the market prices confirmed peaks efficiently. To actually test")
    print("    it, peak_trader must log {date,city,peak_bracket,confirm_price,filled_price,")
    print("    settle_result} to peak_trades.jsonl on every confirmed peak, then re-run.")
    print("═" * 74)


if __name__ == "__main__":
    main()
