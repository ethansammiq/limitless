#!/usr/bin/env python3
"""CALIBRATION REPORT — does the prediction scheme actually beat the market?

The bot logs every scan signal to backtest/signals.jsonl (kde_prob,
confidence_score, market bid/ask, bracket bounds) but never joins the realized
settlement back in — so calibration was logged and thrown away. This closes the
loop: it joins signals to outcomes (backtest/daily_data.jsonl: actual_high),
recomputes whether each bracket actually hit, and scores the bot's probabilities
against the market's price-as-a-probability.

It answers three questions with numbers:
  1. Is the model's probability more accurate than the market's? (Brier/log-loss)
  2. Does "90 confidence" actually mean ~90% hit rate? (confidence reliability)
  3. Is the predicted "edge" real? (realized P&L by edge bucket)

It also fits an isotonic recalibration map (kde_prob -> empirical P(hit)) with an
out-of-sample check, and with --save writes it to calibration_map.json.

Run:
    .venv/bin/python3 calibration_report.py
    .venv/bin/python3 calibration_report.py --min-volume 50 --save
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
SIGNALS_FILE = ROOT / "backtest" / "signals.jsonl"
OUTCOMES_FILE = ROOT / "backtest" / "daily_data.jsonl"
MAP_FILE = ROOT / "calibration_map.json"

_DATE_RE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})-")
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}

# Kalshi fee per contract (maker is 0; taker ~ 0.07*price*(1-price), rounded).
# We price entries conservatively at the ask (taker) so "edge" must survive fees.
def _kalshi_fee_cents(price_cents: int) -> float:
    p = max(0, min(100, price_cents)) / 100.0
    return round(0.07 * p * (1 - p) * 100, 2)  # cents per contract


def _settle_date(ticker: str):
    m = _DATE_RE.search(ticker or "")
    if not m:
        return None
    yy, mon, dd = m.groups()
    if mon not in _MONTHS:
        return None
    return f"20{yy}-{_MONTHS[mon]:02d}-{int(dd):02d}"


def load_outcomes() -> dict:
    out = {}
    with OUTCOMES_FILE.open() as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("actual_high") is not None:
                out[(d["date"], d["city"])] = float(d["actual_high"])
    return out


def load_joined(min_volume: int = 0) -> list:
    """Join logged signals to realized outcomes; recompute bracket_hit."""
    outcomes = load_outcomes()
    rows = []
    with SIGNALS_FILE.open() as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            sd = _settle_date(d.get("ticker"))
            ah = outcomes.get((sd, d.get("city")))
            if ah is None:
                continue
            lo, hi = d.get("bracket_low"), d.get("bracket_high")
            if lo is None or hi is None:
                continue
            kde = d.get("kde_prob")
            if kde is None:
                continue
            bid = d.get("yes_bid") or 0
            ask = d.get("yes_ask") or 0
            vol = d.get("volume") or 0
            if vol < min_volume:
                continue
            hit = 1 if (lo <= ah <= hi) else 0           # YES event: high lands in bracket
            mid = (bid + ask) / 200.0 if (bid and ask) else (ask / 100.0 if ask else (bid / 100.0 if bid else None))
            rows.append({
                "date": d.get("date"), "city": d.get("city"), "ticker": d.get("ticker"),
                "settle": sd, "kde": float(kde),
                "conf": d.get("confidence_score"), "trade_score": d.get("trade_score"),
                "tradeable": bool(d.get("trade_score_tradeable")),
                "side": d.get("side"), "yes_bid": bid, "yes_ask": ask, "volume": vol,
                "market_prob": mid, "hit": hit,
                "edge_after_fees": d.get("edge_after_fees"),
                "lead": d.get("lead_time_hours"), "strategies": d.get("strategies") or [],
            })
    rows.sort(key=lambda r: r["settle"] or "")           # chronological for OOS split
    return rows


# ─── metrics ──────────────────────────────────────────────────────────────────
def brier(p, y):
    p, y = np.asarray(p, float), np.asarray(y, float)
    return float(np.mean((p - y) ** 2))


def logloss(p, y):
    p, y = np.clip(np.asarray(p, float), 1e-6, 1 - 1e-6), np.asarray(y, float)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def ece(p, y, bins=10):
    p, y = np.asarray(p, float), np.asarray(y, float)
    edges = np.linspace(0, 1, bins + 1)
    e, n = 0.0, len(y)
    for i in range(bins):
        m = (p >= edges[i]) & (p < edges[i + 1] if i < bins - 1 else p <= edges[i + 1])
        if m.sum() == 0:
            continue
        e += (m.sum() / n) * abs(p[m].mean() - y[m].mean())
    return float(e)


def _pav_blocks(x, y):
    """Pool-adjacent-violators isotonic regression. Sorts by x, then returns
    (xs_sorted, fitted) where fitted is a monotonic non-decreasing step fit of y."""
    order = np.argsort(x, kind="mergesort")
    xs, ys = np.asarray(x, float)[order], np.asarray(y, float)[order]
    level_val, level_w, level_count = [], [], []
    for v in ys:
        level_val.append(float(v)); level_w.append(1.0); level_count.append(1)
        while len(level_val) > 1 and level_val[-2] > level_val[-1]:
            v2 = level_val.pop(); w2 = level_w.pop(); c2 = level_count.pop()
            v1 = level_val.pop(); w1 = level_w.pop(); c1 = level_count.pop()
            nv = (v1 * w1 + v2 * w2) / (w1 + w2)
            level_val.append(nv); level_w.append(w1 + w2); level_count.append(c1 + c2)
    out = []
    for v, c in zip(level_val, level_count):
        out.extend([v] * c)
    return xs, np.asarray(out, float)


def isotonic_map(xs, yhat, p):
    """Apply a fitted isotonic step function (xs, yhat) to new probabilities p."""
    return np.interp(p, xs, yhat, left=yhat[0], right=yhat[-1])


def reliability(p, y, bins=10):
    p, y = np.asarray(p, float), np.asarray(y, float)
    edges = np.linspace(0, 1, bins + 1)
    out = []
    for i in range(bins):
        m = (p >= edges[i]) & (p < edges[i + 1] if i < bins - 1 else p <= edges[i + 1])
        if m.sum() == 0:
            continue
        out.append((edges[i], edges[i + 1], int(m.sum()), float(p[m].mean()), float(y[m].mean())))
    return out


def _bar(frac, width=20):
    f = max(0.0, min(1.0, frac))
    return "█" * round(f * width) + "·" * (width - round(f * width))


# ─── report ───────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Calibration report for Weather Edge")
    ap.add_argument("--min-volume", type=int, default=0, help="only score signals with >= this volume")
    ap.add_argument("--save", action="store_true", help="write isotonic recalibration to calibration_map.json")
    args = ap.parse_args()

    rows = load_joined(args.min_volume)
    n = len(rows)
    print("═" * 74)
    print("  WEATHER EDGE — CALIBRATION REPORT")
    print("═" * 74)
    if n == 0:
        print("  No joinable signals (need signals.jsonl + daily_data.jsonl outcomes).")
        return
    base = np.mean([r["hit"] for r in rows])
    span = f'{rows[0]["settle"]} → {rows[-1]["settle"]}'
    print(f"  Joined signals: {n}   settle span: {span}   base hit rate: {base:.1%}")
    if args.min_volume:
        print(f"  (filtered to volume >= {args.min_volume})")
    print()

    # 1) MODEL vs MARKET — head-to-head on signals with a real two-sided market
    hh = [r for r in rows if r["market_prob"] is not None]
    print("─" * 74)
    print(f"  1. MODEL vs MARKET   (head-to-head on {len(hh)} signals with a two-sided book)")
    print("─" * 74)
    if hh:
        ym = [r["hit"] for r in hh]
        pk = [r["kde"] for r in hh]
        pm = [r["market_prob"] for r in hh]
        bk, bm = brier(pk, ym), brier(pm, ym)
        lk, lm = logloss(pk, ym), logloss(pm, ym)
        bbase = brier([np.mean(ym)] * len(ym), ym)
        print(f"    {'':14}{'Brier↓':>10}{'LogLoss↓':>11}{'ECE↓':>9}   skill vs base-rate")
        print(f"    {'MODEL kde':14}{bk:>10.4f}{lk:>11.4f}{ece(pk,ym):>9.3f}   {1-bk/bbase:+.1%}")
        print(f"    {'MARKET price':14}{bm:>10.4f}{lm:>11.4f}{ece(pm,ym):>9.3f}   {1-bm/bbase:+.1%}")
        print(f"    {'(base rate)':14}{bbase:>10.4f}")
        verdict = "MODEL beats market ✓" if bk < bm else "market beats model ✗ (model adds no edge)"
        print(f"\n    → {verdict}   (Brier: lower is better)")
    else:
        print("    No signals with a two-sided market to compare.")
    print()

    # 2) CONFIDENCE RELIABILITY — does 90 confidence mean ~90%?
    print("─" * 74)
    print("  2. CONFIDENCE RELIABILITY   (does 'N confidence' = N% hit rate?)")
    print("─" * 74)
    conf_rows = [r for r in rows if r["conf"] is not None]
    print(f"    {'conf bucket':14}{'n':>6}{'hit rate':>10}   {'mkt prob':>9}  reliability (want hit≈bucket)")
    for k in range(0, 100, 10):
        b = [r for r in conf_rows if k <= r["conf"] < k + 10]
        if not b:
            continue
        hr = np.mean([r["hit"] for r in b])
        mkts = [r["market_prob"] for r in b if r["market_prob"] is not None]
        mp = f"{np.mean(mkts):>8.1%}" if mkts else f"{'—':>8}"
        print(f"    {k:3d}-{k+9:<10d}{len(b):>6}{hr:>9.1%}   {mp}  {_bar(hr)}")
    print("    NOTE: confidence_score is a composite, not a probability — read it as a rank, not a %.")
    print()

    # 3) MODEL RELIABILITY (kde_prob deciles)
    print("─" * 74)
    print("  3. MODEL PROBABILITY RELIABILITY   (kde_prob decile vs actual hit rate)")
    print("─" * 74)
    yk = [r["hit"] for r in rows]
    pk_all = [r["kde"] for r in rows]
    print(f"    {'kde bucket':14}{'n':>6}{'pred':>8}{'actual':>9}   over/under-confident")
    for lo, hiq, cnt, pmean, ymean in reliability(pk_all, yk, bins=10):
        flag = "overconfident" if pmean > ymean + 0.04 else ("underconfident" if pmean < ymean - 0.04 else "calibrated")
        print(f"    {lo:.1f}-{hiq:.1f}{'':6}{cnt:>6}{pmean:>8.1%}{ymean:>9.1%}   {flag}")
    print()

    # 4) REALIZED EDGE — is the predicted edge real money?
    print("─" * 74)
    print("  4. REALIZED EDGE   (if you traded the signal's side at the ask, after fees)")
    print("─" * 74)

    def pnl_cents(r):
        if r["side"] == "yes":
            entry = r["yes_ask"]
            if not entry:
                return None
            payoff = 100 if r["hit"] else 0
        else:  # no side
            entry = 100 - r["yes_bid"] if r["yes_bid"] else None
            if not entry:
                return None
            payoff = 100 if not r["hit"] else 0
        return (payoff - entry) - _kalshi_fee_cents(entry)

    traded = [(r, pnl_cents(r)) for r in rows]
    traded = [(r, p) for r, p in traded if p is not None]
    if traded:
        for label, sub in (("ALL signals", traded),
                           ("tradeable gate", [(r, p) for r, p in traded if r["tradeable"]])):
            if not sub:
                print(f"    {label:18} n=0")
                continue
            pnls = [p for _, p in sub]
            wins = np.mean([1 if p > 0 else 0 for _, p in sub])
            print(f"    {label:18} n={len(sub):>4}  mean P&L {np.mean(pnls):+6.1f}c  "
                  f"total {np.sum(pnls)/100:+7.1f}$  win {wins:.0%}")
    print()

    # 5) LEAD-TIME — is the intraday/peak angle better than next-day forecasting?
    print("─" * 74)
    print("  5. LEAD-TIME BREAKDOWN   (forecast horizon vs model calibration)")
    print("─" * 74)
    buckets = [("intraday  <6h", 0, 6), ("same-day  6-18h", 6, 18), ("next-day  18-36h", 18, 36), ("long     >36h", 36, 1e9)]
    print(f"    {'horizon':18}{'n':>6}{'Brier↓':>9}{'mkt Brier':>11}   model beats mkt?")
    for label, lo, hi in buckets:
        b = [r for r in rows if r["lead"] is not None and lo <= r["lead"] < hi and r["market_prob"] is not None]
        if len(b) < 10:
            continue
        ym = [r["hit"] for r in b]
        bk = brier([r["kde"] for r in b], ym)
        bm = brier([r["market_prob"] for r in b], ym)
        print(f"    {label:18}{len(b):>6}{bk:>9.4f}{bm:>11.4f}   {'YES ✓' if bk<bm else 'no ✗'}")
    print()

    # 6) ISOTONIC RECALIBRATION with out-of-sample check
    print("─" * 74)
    print("  6. RECALIBRATION   (isotonic kde_prob -> empirical P(hit), 70/30 time split)")
    print("─" * 74)
    cut = int(len(rows) * 0.7)
    tr, te = rows[:cut], rows[cut:]
    xs, yhat = _pav_blocks(np.array([r["kde"] for r in tr]), np.array([r["hit"] for r in tr], float))
    if len(te) >= 20:
        pte = np.array([r["kde"] for r in te]); yte = np.array([r["hit"] for r in te], float)
        raw_b = brier(pte, yte)
        cal_b = brier(isotonic_map(xs, yhat, pte), yte)
        print(f"    OOS Brier  raw kde {raw_b:.4f}  ->  recalibrated {cal_b:.4f}   "
              f"({'improves' if cal_b < raw_b else 'no improvement'})")
    # Fit final map on ALL data for saving
    xs_all, yhat_all = _pav_blocks(np.array([r["kde"] for r in rows]), np.array([r["hit"] for r in rows], float))
    if args.save:
        # Compress to ~50 breakpoints
        idx = np.linspace(0, len(xs_all) - 1, min(50, len(xs_all))).astype(int)
        MAP_FILE.write_text(json.dumps({
            "kind": "isotonic", "fitted_on": n, "settle_span": span,
            "x": [round(float(xs_all[i]), 4) for i in idx],
            "y": [round(float(yhat_all[i]), 4) for i in idx],
        }, separators=(",", ":")))
        print(f"    saved recalibration map -> {MAP_FILE.name}")
    print()
    print("═" * 74)


if __name__ == "__main__":
    main()
