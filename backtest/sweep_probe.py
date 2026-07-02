#!/usr/bin/env python3
"""
SWEEP PROBE — does the eventually-winning bracket stay cheap before settlement?

Reads settled outcomes (backtest/daily_data.jsonl), pulls hourly candlesticks
for each winning bracket over its settlement day, and measures the traded price
of the winner by ET hour-of-day. Two questions:

  1. OPPORTUNITY CEILING: across the day, how cheap was the eventual winner?
     (Necessary condition for any sweep edge — if winners are always ~95c by
      noon, the market is efficient and there is nothing to harvest.)

  2. OBS-WATCHER EDGE (the near-riskless one): how cheap was the winner LATE
     (3-6pm ET) — after the daily peak is largely formed and observable on the
     ASOS sensor, but before settlement? A cheap winner here is capturable with
     observation alone, no forecasting skill required.

Read-only. Caches raw candles to backtest/sweep_candles_cache.json.
Run:  python3 backtest/sweep_probe.py
"""
import asyncio
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kalshi_client import KalshiClient  # noqa: E402

ET = ZoneInfo("America/New_York")
DATA = ROOT / "backtest" / "daily_data.jsonl"
CACHE = ROOT / "backtest" / "sweep_candles_cache.json"
FEE_CENTS = 1  # taker cross cost ballpark at the cheap end of the book


def _d(v):
    """Parse a fixed-point dollar-string to cents (int), or None."""
    if v is None:
        return None
    try:
        return round(float(v) * 100)
    except (ValueError, TypeError):
        return None


def winner_price_cents(candle):
    """Best estimate of the winner's TRADED price this hour, in cents.

    Prefer mean traded price, then close, then previous (carry-forward when no
    trades that hour). Returns None if the bracket never traded up to here.
    """
    p = candle.get("price", {})
    for k in ("mean_dollars", "close_dollars", "previous_dollars"):
        c = _d(p.get(k))
        if c is not None:
            return c
    return None


def winner_ask_cents(candle):
    """Executable ask (what you'd PAY to buy the winner), in cents."""
    a = candle.get("yes_ask", {})
    return _d(a.get("close_dollars"))


async def fetch_all():
    rows = [json.loads(l) for l in DATA.read_text().splitlines() if l.strip()]
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    kc = KalshiClient()
    await kc.start()
    fetched = 0
    try:
        for i, r in enumerate(rows):
            tk = r.get("winning_ticker")
            date = r.get("date")
            if not tk or not date:
                continue
            if tk in cache:
                continue
            series = tk.split("-")[0]
            y, m, d = (int(x) for x in date.split("-"))
            start = int(datetime(y, m, d, 0, 0, tzinfo=ET).timestamp())
            end = int(datetime(y, m, d, 23, 59, tzinfo=ET).timestamp()) + 7 * 3600
            path = (f"/series/{series}/markets/{tk}/candlesticks"
                    f"?start_ts={start}&end_ts={end}&period_interval=60")
            resp = await kc._req_safe("GET", path, auth=True)
            cache[tk] = resp.get("candlesticks", []) if isinstance(resp, dict) else []
            fetched += 1
            if fetched % 25 == 0:
                print(f"  fetched {fetched}...", file=sys.stderr)
                CACHE.write_text(json.dumps(cache))
    finally:
        await kc.stop()
    CACHE.write_text(json.dumps(cache))
    print(f"  done: {fetched} new, {len(cache)} cached total", file=sys.stderr)
    return rows, cache


def analyze(rows, cache):
    # bucket winner traded-price by ET hour-of-day
    by_hour_price = defaultdict(list)
    by_hour_ask = defaultdict(list)
    # per-day: cheapest the winner ever got, and its late (15-18 ET) price
    cheapest, late_price = [], []
    n_days = 0

    for r in rows:
        tk = r.get("winning_ticker")
        candles = cache.get(tk)
        if not candles:
            continue
        n_days += 1
        day_min = None
        late_vals = []
        for c in candles:
            ts = c.get("end_period_ts")
            if ts is None:
                continue
            hr = datetime.fromtimestamp(ts, ET).hour
            pc = winner_price_cents(c)
            ac = winner_ask_cents(c)
            if pc is not None and 0 < pc < 100:
                by_hour_price[hr].append(pc)
                day_min = pc if day_min is None else min(day_min, pc)
                if 15 <= hr <= 18:
                    late_vals.append(pc)
            if ac is not None and 0 < ac <= 100:
                by_hour_ask[hr].append(ac)
        if day_min is not None:
            cheapest.append(day_min)
        if late_vals:
            late_price.append(min(late_vals))

    def stats(xs):
        if not xs:
            return (0, 0, 0, 0)
        xs = sorted(xs)
        n = len(xs)
        med = xs[n // 2]
        return (n, sum(xs) / n, med, xs[0])

    print(f"\nSettled days with candle data: {n_days}/{len(rows)}\n")
    print("WINNER's TRADED PRICE by ET hour-of-day (peak typically 13-17 ET):")
    print(f"{'ET hr':>5} {'n':>4} {'mean¢':>6} {'med¢':>5} {'%<50¢':>6} {'%<30¢':>6}")
    print("-" * 40)
    for hr in range(0, 24):
        v = by_hour_price.get(hr, [])
        if not v:
            continue
        n = len(v)
        mean = sum(v) / n
        med = sorted(v)[n // 2]
        p50 = 100 * sum(1 for x in v if x < 50) / n
        p30 = 100 * sum(1 for x in v if x < 30) / n
        print(f"{hr:>5} {n:>4} {mean:>6.1f} {med:>5} {p50:>5.0f}% {p30:>5.0f}%")

    print("\n=== HEADLINE METRICS ===")
    n, mean, med, mn = stats(cheapest)
    print(f"Cheapest the winner EVER got (per day): mean {mean:.0f}¢, median {med}¢ "
          f"(n={n})  <- opportunity ceiling")
    n, mean, med, mn = stats(late_price)
    if n:
        cheap50 = 100 * sum(1 for x in late_price if x < 50) / n
        cheap70 = 100 * sum(1 for x in late_price if x < 70) / n
        ev = sum((100 - x - FEE_CENTS) for x in late_price) / n
        print(f"Winner's cheapest LATE price (15-18 ET, peak ~formed): "
              f"mean {mean:.0f}¢, median {med}¢ (n={n})")
        print(f"  Days winner buyable <50¢ late: {cheap50:.0f}% | <70¢ late: {cheap70:.0f}%")
        print(f"  Naive EV if you bought winner at its cheapest late price: "
              f"+{ev:.1f}¢/contract (gross of nowcast skill)  <- OBS-WATCHER edge")
    print("\nNOTE: this is the opportunity CEILING (hindsight winner). It proves "
          "whether cheap winners EXIST to chase. It does NOT prove a nowcast can "
          "identify them in real time — that needs intraday ASOS obs (stage 2).")


async def main():
    rows, cache = await fetch_all()
    analyze(rows, cache)


if __name__ == "__main__":
    asyncio.run(main())
