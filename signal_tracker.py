#!/usr/bin/env python3
"""
SIGNAL TRACKER — Per-bracket probability calibration pipeline.

Unlike calibration_tracker.py (which saves ONE best opportunity per city/day),
this module logs EVERY opportunity the scanner produces. After settlement, each
signal is scored against reality, enabling probability calibration analysis:

  "Do 40% KDE brackets actually hit ~40% of the time?"
  "Does 90+ confidence really predict correctly 90%+ of the time?"

Data flow:
  1. auto_scan.py calls save_signals() after each city scan
  2. backtest_collector.py calls enrich_signals() after settlement
  3. signal_accuracy_report() answers calibration questions

Storage:
  - backtest/signals.jsonl  — append-only log of every opportunity
  - backtest/signals/YYYY-MM-DD_CITY.json — per-scan JSON (all opps for that day/city)

Usage:
    from signal_tracker import save_signals, enrich_signals, signal_accuracy_report

    # At scan time (auto_scan.py):
    save_signals("NYC", opps, scan_context)

    # After settlement (backtest_collector.py):
    enriched = enrich_signals("2026-02-15", "NYC", actual_high=43.0)

    # For analysis:
    python3 signal_tracker.py --report
    python3 signal_tracker.py --report --city NYC
    python3 signal_tracker.py --calibration-curve
"""

from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from log_setup import get_logger

logger = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent
SIGNALS_DIR = PROJECT_ROOT / "backtest" / "signals"
SIGNALS_LOG = PROJECT_ROOT / "backtest" / "signals.jsonl"


# ─── Data Structure ──────────────────────────────────────────────────────────


@dataclass
class SignalRecord:
    """One opportunity = one signal. Every opportunity from every scan is logged."""

    # ── Identity ──
    date: str                           # YYYY-MM-DD (market date)
    city: str                           # City key (NYC, CHI, DEN, MIA, LAX)
    ticker: str                         # Kalshi ticker (e.g., KXHIGHLAX-26FEB17-T58)
    bracket_title: str                  # Human-readable bracket (e.g., "58-59°")
    scan_time: str                      # ISO 8601 timestamp

    # ── Bracket bounds ──
    bracket_low: float                  # Lower bound (°F)
    bracket_high: float                 # Upper bound exclusive (°F)

    # ── Probabilities ──
    kde_prob: float                     # KDE probability for this bracket
    histogram_prob: float               # Raw histogram probability
    weighted_prob: float                # Final weighted probability used

    # ── Market data ──
    side: str                           # "yes" or "no"
    yes_bid: int                        # Market YES bid (cents)
    yes_ask: int                        # Market YES ask (cents)
    volume: int                         # 24h volume

    # ── Edge metrics ──
    edge_raw: float                     # Raw edge (decimal, e.g., 0.15 = 15¢)
    edge_after_fees: float              # Edge after fees (decimal)
    edge_cents: float                   # Edge in cents for readability

    # ── Scoring ──
    confidence_score: float             # 0-100
    trade_score: float                  # 0.0-1.0 composite
    trade_score_tradeable: bool         # Whether trade score passed threshold
    kelly: float                        # Kelly fraction

    # ── Strategies ──
    strategies: List[str]               # Active strategy flags

    # ── Context ──
    ensemble_mean: float                # Weighted ensemble mean (°F)
    ensemble_std: float                 # Ensemble spread
    nws_forecast_high: float            # NWS point forecast
    nws_physics_high: float             # Physics-adjusted forecast
    lead_time_hours: float              # Hours to settlement at scan time

    # ── Depth data (optional) ──
    depth_grade: str = ""               # A/B/C/D liquidity grade
    bid_depth: int = 0
    ask_depth: int = 0
    imbalance: float = 0.0

    # ── Post-settlement (filled by enrich_signals) ──
    actual_high: Optional[float] = None
    bracket_hit: Optional[bool] = None  # Did actual_high land in this bracket?
    pnl_if_traded: Optional[float] = None  # Hypothetical P&L in cents


# ─── Helper Functions ────────────────────────────────────────────────────────


def _atomic_write(path: Path, content: str) -> None:
    """Write content to path atomically (write to tmp, then rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=path.parent, prefix=".tmp_", suffix=path.suffix
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _append_jsonl(path: Path, record: dict) -> None:
    """Append a single JSON record to a JSONL file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, default=str, separators=(",", ":")) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)


def _load_all_signals() -> List[dict]:
    """Load all signal records, preferring enriched per-scan JSON files.

    Per-scan JSON files in backtest/signals/ are the source of truth
    (they get enriched with settlement data). Falls back to JSONL for
    any records not found in JSON files.
    """
    records = []

    # Primary: load from per-scan JSON files (enriched)
    json_keys: set = set()  # Track (date, city) pairs loaded from JSON
    if SIGNALS_DIR.exists():
        for json_path in sorted(SIGNALS_DIR.glob("*.json")):
            try:
                sigs = json.loads(json_path.read_text(encoding="utf-8"))
                if isinstance(sigs, list):
                    for s in sigs:
                        records.append(s)
                        json_keys.add((s.get("date", ""), s.get("city", "")))
            except (json.JSONDecodeError, Exception):
                continue

    # Fallback: JSONL for any records not covered by JSON files
    if SIGNALS_LOG.exists():
        with open(SIGNALS_LOG, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    key = (r.get("date", ""), r.get("city", ""))
                    if key not in json_keys:
                        records.append(r)
                except json.JSONDecodeError:
                    continue

    return records


# ─── Core API ────────────────────────────────────────────────────────────────


def save_signals(
    city_key: str,
    opps: list,
    scan_context: dict,
) -> int:
    """Save all opportunities from a city scan as signal records.

    Parameters
    ----------
    city_key : str
        City code (NYC, CHI, DEN, MIA, LAX).
    opps : list
        List of Opportunity objects from analyze_opportunities_v2.
    scan_context : dict
        Context from the scan containing:
        - ensemble_mean, ensemble_std
        - nws_forecast_high, nws_physics_high
        - lead_time_hours
        - depth_map (optional): dict[ticker, OrderBookDepth]

    Returns
    -------
    int — number of signals saved.
    """
    if not opps:
        return 0

    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")
    scan_time_str = now.isoformat() + "Z"

    depth_map = scan_context.get("depth_map", {})
    ensemble_mean = scan_context.get("ensemble_mean", 0.0)
    ensemble_std = scan_context.get("ensemble_std", 0.0)
    nws_forecast_high = scan_context.get("nws_forecast_high", 0.0)
    nws_physics_high = scan_context.get("nws_physics_high", 0.0)
    lead_time_hours = scan_context.get("lead_time_hours", 0.0)

    records = []
    for opp in opps:
        # Extract trade score components
        ts_components = getattr(opp, "trade_score_components", {})

        # Depth data
        depth = depth_map.get(getattr(opp, "ticker", "")) if depth_map else None

        rec = SignalRecord(
            date=today_str,
            city=city_key,
            ticker=getattr(opp, "ticker", ""),
            bracket_title=getattr(opp, "bracket_title", ""),
            scan_time=scan_time_str,
            bracket_low=getattr(opp, "low", 0.0),
            bracket_high=getattr(opp, "high", 0.0),
            kde_prob=round(getattr(opp, "kde_prob", 0.0), 6),
            histogram_prob=round(getattr(opp, "histogram_prob", 0.0), 6),
            weighted_prob=round(getattr(opp, "weighted_prob", 0.0), 6),
            side=getattr(opp, "side", "yes"),
            yes_bid=getattr(opp, "yes_bid", 0),
            yes_ask=getattr(opp, "yes_ask", 0),
            volume=getattr(opp, "volume", 0),
            edge_raw=round(getattr(opp, "edge_raw", 0.0), 6),
            edge_after_fees=round(getattr(opp, "edge_after_fees", 0.0), 6),
            edge_cents=round(getattr(opp, "edge_after_fees", 0.0) * 100, 2),
            confidence_score=getattr(opp, "confidence_score", 0.0),
            trade_score=round(getattr(opp, "trade_score", 0.0), 4),
            trade_score_tradeable=ts_components.get("tradeable", False),
            kelly=round(getattr(opp, "kelly", 0.0), 4),
            strategies=getattr(opp, "strategies", []),
            ensemble_mean=round(ensemble_mean, 2),
            ensemble_std=round(ensemble_std, 2),
            nws_forecast_high=round(nws_forecast_high, 1),
            nws_physics_high=round(nws_physics_high, 1),
            lead_time_hours=round(lead_time_hours, 2),
            depth_grade=getattr(depth, "grade", "") if depth else "",
            bid_depth=getattr(depth, "bid_depth", 0) if depth else 0,
            ask_depth=getattr(depth, "ask_depth", 0) if depth else 0,
            imbalance=round(getattr(depth, "imbalance", 0.0), 3) if depth else 0.0,
        )
        records.append(rec)

    # Serialize
    record_dicts = [asdict(r) for r in records]

    try:
        # Append each to JSONL
        for rd in record_dicts:
            _append_jsonl(SIGNALS_LOG, rd)

        # Save per-scan JSON (all opps for this date/city)
        SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
        scan_path = SIGNALS_DIR / f"{today_str}_{city_key}.json"
        _atomic_write(scan_path, json.dumps(record_dicts, indent=2, default=str))

        logger.info(
            "Saved %d signals for %s_%s (conf range: %d-%d, ts range: %.3f-%.3f)",
            len(records), today_str, city_key,
            min(r.confidence_score for r in records),
            max(r.confidence_score for r in records),
            min(r.trade_score for r in records),
            max(r.trade_score for r in records),
        )
        return len(records)

    except Exception as e:
        logger.error("Failed to save signals for %s: %s", city_key, e)
        return 0


def enrich_signals(
    date_str: str,
    city_key: str,
    actual_high: float,
) -> int:
    """Enrich signal records with post-settlement actual data.

    For each signal on (date, city), determine:
      - bracket_hit: did actual_high land in [bracket_low, bracket_high)?
      - pnl_if_traded: hypothetical P&L if we'd taken the trade at bid+1

    Parameters
    ----------
    date_str : str
        Date in YYYY-MM-DD format.
    city_key : str
        City code.
    actual_high : float
        The actual recorded high temperature from settlement.

    Returns
    -------
    int — number of signals enriched.
    """
    scan_path = SIGNALS_DIR / f"{date_str}_{city_key}.json"
    if not scan_path.exists():
        logger.debug("No signal file for %s_%s — nothing to enrich", date_str, city_key)
        return 0

    try:
        signals = json.loads(scan_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, Exception) as e:
        logger.error("Failed to load signals %s: %s", scan_path.name, e)
        return 0

    enriched_count = 0
    for sig in signals:
        if sig.get("actual_high") is not None:
            continue  # Already enriched

        bracket_low = sig.get("bracket_low", 0)
        bracket_high = sig.get("bracket_high", 0)
        side = sig.get("side", "yes")

        # Did the actual high land in this bracket?
        bracket_hit = bracket_low <= actual_high < bracket_high

        # Hypothetical P&L if traded at bid+1 (maker entry)
        yes_bid = sig.get("yes_bid", 0)
        yes_ask = sig.get("yes_ask", 0)

        if side == "yes":
            entry_price = min(yes_bid + 1, 50)  # bid+1, max 50¢
            if bracket_hit:
                pnl = 100 - entry_price  # Win: payout $1 - entry
            else:
                pnl = -entry_price  # Lose: lost entry cost
        else:
            no_cost = 100 - yes_ask + 1  # NO entry = 100 - ask + 1
            entry_price = min(no_cost, 50)
            if not bracket_hit:  # NO wins when bracket doesn't hit
                pnl = 100 - entry_price
            else:
                pnl = -entry_price

        sig["actual_high"] = actual_high
        sig["bracket_hit"] = bracket_hit
        sig["pnl_if_traded"] = pnl
        enriched_count += 1

    if enriched_count > 0:
        try:
            _atomic_write(scan_path, json.dumps(signals, indent=2, default=str))
            logger.info(
                "Enriched %d signals for %s_%s (actual=%.1f°F)",
                enriched_count, date_str, city_key, actual_high,
            )
        except Exception as e:
            logger.error("Failed to save enriched signals: %s", e)
            return 0

    return enriched_count


# ─── Analysis & Reporting ────────────────────────────────────────────────────


def _bin_probability(prob: float, bin_size: float = 0.10) -> str:
    """Bin a probability into a display range like '30-40%'."""
    lower = int(prob / bin_size) * bin_size
    upper = lower + bin_size
    return f"{lower*100:.0f}-{upper*100:.0f}%"


def signal_accuracy_report(
    city_key: Optional[str] = None,
    min_date: Optional[str] = None,
) -> dict:
    """Generate a calibration accuracy report from enriched signals.

    Answers:
      1. KDE calibration: do X% KDE brackets hit X% of the time?
      2. Confidence calibration: does 90+ confidence predict correctly?
      3. Trade score calibration: does high trade_score predict correctly?
      4. Hypothetical P&L: what would total P&L be if we traded everything?

    Returns a dict with all computed metrics (also prints a report).
    """
    all_signals = _load_all_signals()

    # Filter to enriched signals only (have actual_high)
    signals = [
        s for s in all_signals
        if s.get("actual_high") is not None
        and (city_key is None or s.get("city", "").upper() == city_key.upper())
        and (min_date is None or s.get("date", "") >= min_date)
    ]

    # Deduplicate: keep latest entry per (date, city, ticker)
    seen: Dict[tuple, int] = {}
    for idx, s in enumerate(signals):
        key = (s.get("date"), s.get("city"), s.get("ticker"))
        seen[key] = idx
    signals = [signals[idx] for idx in sorted(seen.values())]

    total = len(signals)
    total_unenriched = len([s for s in all_signals if s.get("actual_high") is None])

    if total == 0:
        print("\n  No enriched signal data available yet.")
        print("  Signals are enriched after settlement via backtest_collector.py")
        print(f"  Total unenriched signals: {total_unenriched}")
        return {"total": 0, "total_unenriched": total_unenriched}

    dates = sorted(set(s["date"] for s in signals))
    cities = sorted(set(s["city"] for s in signals))

    print(f"\n  {'=' * 72}")
    print(f"  SIGNAL TRACKER — CALIBRATION REPORT")
    print(f"  {'=' * 72}")
    print(f"  Enriched signals: {total} ({len(dates)} days, {len(cities)} cities)")
    print(f"  Unenriched:       {total_unenriched}")
    print(f"  Date range:       {dates[0]} → {dates[-1]}")
    if city_key:
        print(f"  City filter:      {city_key.upper()}")
    print()

    # ── 1. KDE PROBABILITY CALIBRATION ──
    # Bin signals by KDE probability, compute actual hit rate per bin
    kde_bins: Dict[str, list] = defaultdict(list)
    for s in signals:
        kde_p = s.get("kde_prob", 0)
        bin_label = _bin_probability(kde_p, 0.10)
        kde_bins[bin_label].append(s)

    print(f"  ┌─ KDE PROBABILITY CALIBRATION ──────────────────────────────┐")
    print(f"  │ {'KDE Bin':<12s} {'Signals':>8s} {'Hits':>6s} {'Hit Rate':>9s} {'Expected':>9s} {'Gap':>8s} │")
    print(f"  │ {'─'*12} {'─'*8} {'─'*6} {'─'*9} {'─'*9} {'─'*8} │")

    calibration_data = {}
    for bin_label in sorted(kde_bins.keys()):
        sigs = kde_bins[bin_label]
        n = len(sigs)
        hits = sum(1 for s in sigs if s.get("bracket_hit"))
        hit_rate = hits / n if n > 0 else 0
        # Expected = midpoint of bin
        mid = sum(s.get("kde_prob", 0) for s in sigs) / n if n > 0 else 0
        gap = hit_rate - mid
        calibration_data[bin_label] = {
            "n": n, "hits": hits, "hit_rate": hit_rate,
            "expected": mid, "gap": gap,
        }
        gap_str = f"{gap:+.1%}"
        print(f"  │ {bin_label:<12s} {n:>8d} {hits:>6d} {hit_rate:>8.1%} {mid:>8.1%} {gap_str:>8s} │")

    print(f"  └{'─' * 62}┘")
    print()

    # ── 2. CONFIDENCE SCORE CALIBRATION ──
    conf_bins = {
        "90-100 (ELITE)": [s for s in signals if s.get("confidence_score", 0) >= 90],
        "80-89 (HIGH)":   [s for s in signals if 80 <= s.get("confidence_score", 0) < 90],
        "70-79 (MED)":    [s for s in signals if 70 <= s.get("confidence_score", 0) < 80],
        "60-69 (LOW)":    [s for s in signals if 60 <= s.get("confidence_score", 0) < 70],
        "<60 (PASS)":     [s for s in signals if s.get("confidence_score", 0) < 60],
    }

    print(f"  ┌─ CONFIDENCE SCORE CALIBRATION ────────────────────────────┐")
    print(f"  │ {'Conf Band':<18s} {'Signals':>8s} {'Hits':>6s} {'Hit Rate':>9s} {'Avg Edge':>9s} │")
    print(f"  │ {'─'*18} {'─'*8} {'─'*6} {'─'*9} {'─'*9} │")

    confidence_data = {}
    for label, sigs in conf_bins.items():
        n = len(sigs)
        if n == 0:
            continue
        hits = sum(1 for s in sigs if s.get("bracket_hit"))
        hit_rate = hits / n
        avg_edge = sum(s.get("edge_cents", 0) for s in sigs) / n
        confidence_data[label] = {
            "n": n, "hits": hits, "hit_rate": hit_rate, "avg_edge": avg_edge,
        }
        print(f"  │ {label:<18s} {n:>8d} {hits:>6d} {hit_rate:>8.1%} {avg_edge:>+8.1f}¢ │")

    print(f"  └{'─' * 56}┘")
    print()

    # ── 3. TRADE SCORE CALIBRATION ──
    ts_bins = {
        "≥0.50 (TRADE)":  [s for s in signals if s.get("trade_score", 0) >= 0.50],
        "0.40-0.49":      [s for s in signals if 0.40 <= s.get("trade_score", 0) < 0.50],
        "0.30-0.39":      [s for s in signals if 0.30 <= s.get("trade_score", 0) < 0.40],
        "<0.30":          [s for s in signals if s.get("trade_score", 0) < 0.30],
    }

    print(f"  ┌─ TRADE SCORE CALIBRATION ─────────────────────────────────┐")
    print(f"  │ {'Score Band':<18s} {'Signals':>8s} {'Hits':>6s} {'Hit Rate':>9s} {'Avg P&L':>9s} │")
    print(f"  │ {'─'*18} {'─'*8} {'─'*6} {'─'*9} {'─'*9} │")

    trade_score_data = {}
    for label, sigs in ts_bins.items():
        n = len(sigs)
        if n == 0:
            continue
        hits = sum(1 for s in sigs if s.get("bracket_hit"))
        hit_rate = hits / n
        avg_pnl = sum(s.get("pnl_if_traded", 0) for s in sigs) / n
        trade_score_data[label] = {
            "n": n, "hits": hits, "hit_rate": hit_rate, "avg_pnl": avg_pnl,
        }
        print(f"  │ {label:<18s} {n:>8d} {hits:>6d} {hit_rate:>8.1%} {avg_pnl:>+8.1f}¢ │")

    print(f"  └{'─' * 56}┘")
    print()

    # ── 4. HYPOTHETICAL P&L ──
    all_pnl = [s.get("pnl_if_traded", 0) for s in signals]
    tradeable_signals = [s for s in signals if s.get("trade_score_tradeable")]
    tradeable_pnl = [s.get("pnl_if_traded", 0) for s in tradeable_signals]

    total_pnl_all = sum(all_pnl)
    total_pnl_tradeable = sum(tradeable_pnl)
    win_rate_all = sum(1 for p in all_pnl if p > 0) / len(all_pnl) if all_pnl else 0
    win_rate_tradeable = sum(1 for p in tradeable_pnl if p > 0) / len(tradeable_pnl) if tradeable_pnl else 0

    print(f"  ┌─ HYPOTHETICAL P&L (per contract, cents) ──────────────────┐")
    print(f"  │ All signals:       {len(all_pnl):>5d} trades  {total_pnl_all:>+8.0f}¢  "
          f"(win rate: {win_rate_all:.1%}){' ' * 3}│")
    print(f"  │ Tradeable only:    {len(tradeable_pnl):>5d} trades  {total_pnl_tradeable:>+8.0f}¢  "
          f"(win rate: {win_rate_tradeable:.1%}){' ' * 3}│")
    if tradeable_pnl:
        avg_win = sum(p for p in tradeable_pnl if p > 0) / max(1, sum(1 for p in tradeable_pnl if p > 0))
        avg_loss = sum(p for p in tradeable_pnl if p <= 0) / max(1, sum(1 for p in tradeable_pnl if p <= 0))
        print(f"  │ Avg win:  {avg_win:>+6.1f}¢   Avg loss: {avg_loss:>+6.1f}¢"
              f"   R:R = {abs(avg_win/avg_loss) if avg_loss != 0 else 0:.1f}:1{' ' * 9}│")
    print(f"  └{'─' * 56}┘")
    print()

    # ── 5. PER-CITY BREAKDOWN ──
    city_data = defaultdict(lambda: {"n": 0, "hits": 0, "pnl": 0})
    for s in signals:
        city = s.get("city", "?")
        city_data[city]["n"] += 1
        if s.get("bracket_hit"):
            city_data[city]["hits"] += 1
        city_data[city]["pnl"] += s.get("pnl_if_traded", 0)

    print(f"  ┌─ PER-CITY BREAKDOWN ──────────────────────────────────────┐")
    print(f"  │ {'City':<6s} {'Signals':>8s} {'Hits':>6s} {'Hit Rate':>9s} {'Total P&L':>10s} │")
    print(f"  │ {'─'*6} {'─'*8} {'─'*6} {'─'*9} {'─'*10} │")
    for city in sorted(city_data.keys()):
        d = city_data[city]
        hr = d["hits"] / d["n"] if d["n"] > 0 else 0
        print(f"  │ {city:<6s} {d['n']:>8d} {d['hits']:>6d} {hr:>8.1%} {d['pnl']:>+9.0f}¢ │")
    print(f"  └{'─' * 47}┘")

    print(f"\n  {'=' * 72}")

    return {
        "total": total,
        "total_unenriched": total_unenriched,
        "dates": dates,
        "calibration_kde": calibration_data,
        "calibration_confidence": confidence_data,
        "calibration_trade_score": trade_score_data,
        "pnl_all": total_pnl_all,
        "pnl_tradeable": total_pnl_tradeable,
        "city_data": dict(city_data),
    }


def calibration_curve_data() -> List[dict]:
    """Generate calibration curve data points for plotting.

    Returns list of dicts with:
      - predicted_prob: midpoint of KDE bin
      - actual_prob: observed hit rate
      - n_samples: number of signals in this bin
      - stderr: standard error of the hit rate

    A perfectly calibrated system would have predicted_prob == actual_prob
    for all bins (the 45-degree line).
    """
    all_signals = _load_all_signals()
    signals = [s for s in all_signals if s.get("actual_high") is not None]

    # Deduplicate
    seen: Dict[tuple, int] = {}
    for idx, s in enumerate(signals):
        key = (s.get("date"), s.get("city"), s.get("ticker"))
        seen[key] = idx
    signals = [signals[idx] for idx in sorted(seen.values())]

    if not signals:
        return []

    # 5% bins for finer resolution
    bins: Dict[int, list] = defaultdict(list)
    for s in signals:
        kde_p = s.get("kde_prob", 0)
        bin_idx = min(int(kde_p / 0.05), 19)  # 0-19 for 0-100%
        bins[bin_idx].append(s)

    curve = []
    for bin_idx in sorted(bins.keys()):
        sigs = bins[bin_idx]
        n = len(sigs)
        if n < 3:  # Need minimum samples for meaningful data point
            continue
        predicted = sum(s.get("kde_prob", 0) for s in sigs) / n
        actual = sum(1 for s in sigs if s.get("bracket_hit")) / n
        stderr = math.sqrt(actual * (1 - actual) / n) if n > 0 else 0
        curve.append({
            "predicted_prob": round(predicted, 4),
            "actual_prob": round(actual, 4),
            "n_samples": n,
            "stderr": round(stderr, 4),
        })

    return curve


# ─── CLI ─────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Signal Tracker — Per-bracket probability calibration pipeline"
    )
    parser.add_argument(
        "--report", action="store_true",
        help="Print full calibration accuracy report",
    )
    parser.add_argument(
        "--city", type=str, default=None,
        help="Filter report to a single city (NYC, CHI, DEN, MIA, LAX)",
    )
    parser.add_argument(
        "--since", type=str, default=None,
        help="Only include signals from this date onward (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--calibration-curve", action="store_true",
        help="Print calibration curve data points (for plotting)",
    )
    parser.add_argument(
        "--stats", action="store_true",
        help="Print quick signal count statistics",
    )
    args = parser.parse_args()

    if args.report:
        signal_accuracy_report(city_key=args.city, min_date=args.since)
    elif args.calibration_curve:
        curve = calibration_curve_data()
        if curve:
            print(f"\n  CALIBRATION CURVE DATA (predicted vs actual)")
            print(f"  {'Predicted':>10s} {'Actual':>8s} {'N':>6s} {'StdErr':>8s}")
            print(f"  {'─'*10} {'─'*8} {'─'*6} {'─'*8}")
            for pt in curve:
                print(f"  {pt['predicted_prob']:>9.1%} {pt['actual_prob']:>7.1%} "
                      f"{pt['n_samples']:>6d} {pt['stderr']:>7.3f}")
            print()
            # Perfect calibration check
            total_gap = sum(abs(pt["actual_prob"] - pt["predicted_prob"]) * pt["n_samples"] for pt in curve)
            total_n = sum(pt["n_samples"] for pt in curve)
            avg_gap = total_gap / total_n if total_n > 0 else 0
            print(f"  Weighted avg calibration gap: {avg_gap:.3f}")
            print(f"  (0.000 = perfect, <0.05 = well-calibrated, >0.10 = needs adjustment)")
        else:
            print("\n  No enriched signal data available for calibration curve.")
    elif args.stats:
        all_signals = _load_all_signals()
        enriched = [s for s in all_signals if s.get("actual_high") is not None]
        dates = sorted(set(s.get("date", "") for s in all_signals))
        print(f"\n  Signal Tracker Stats:")
        print(f"  Total signals:     {len(all_signals)}")
        print(f"  Enriched:          {len(enriched)}")
        print(f"  Unenriched:        {len(all_signals) - len(enriched)}")
        if dates:
            print(f"  Date range:        {dates[0]} → {dates[-1]}")
        cities = sorted(set(s.get("city", "") for s in all_signals))
        print(f"  Cities:            {', '.join(cities) if cities else 'none'}")
    else:
        parser.print_help()
