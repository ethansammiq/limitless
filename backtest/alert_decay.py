#!/usr/bin/env python3
"""ALERT DECAY — how fast does the edge die after the sniper fires?

Joins sniper journal findings (detection ts + detected ask/bid) to Kalshi
1-MINUTE candlesticks and measures the entry price at +1/+2/+5/+10/+20 min
after detection. This is the pre-registered evidence for (or against) the
event-driven daemon and any faster executor: if the edge is mostly intact
at +10 min, a human on Discord captures it and the daemon buys nothing; if
it's gone by +2 min, latency is the binding constraint.

buy_winner findings track yes_ask (cost to enter rises as the market
reprices toward the winner). sell_dead findings track yes_bid (proceeds
decay as bids get pulled).

Read-only measurement over logs/cli_sniper/*.jsonl — reuses the scorecard's
loader, so bug-era rows are already excluded. Candles cached to
backtest/alert_decay_cache.json (gitignored). Ad-hoc; run ON THE VPS where
the journal lives:

    .venv/bin/python3 backtest/alert_decay.py --days 14
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from backtest.sniper_scorecard import load_findings  # noqa: E402
from log_setup import get_logger  # noqa: E402

logger = get_logger(__name__)

CACHE = HERE / "alert_decay_cache.json"
METAR_JOURNAL_DIR = PROJECT_ROOT / "logs" / "metar_sniper"
OFFSETS_MIN = (1, 2, 5, 10, 20)
WINDOW_BEFORE_S = 120
WINDOW_AFTER_S = 25 * 60
FILLABLE_HORIZON_MIN = 25
DEFAULT_CAP_C = 20  # the standing 20¢ max-entry rule


def _cents(candle_side: dict | None, field: str = "close_dollars") -> float | None:
    """Fixed-point dollar string → cents."""
    try:
        return round(float((candle_side or {})[field]) * 100, 1)
    except (KeyError, TypeError, ValueError):
        return None


def price_at_offsets(candles: list[dict], alert_ts: int, side_key: str,
                     offsets_min: tuple[int, ...] = OFFSETS_MIN) -> dict[int, float | None]:
    """For each offset, the last known 1-min close at or before alert+offset.

    side_key: 'yes_ask' (buy_winner entry cost) or 'yes_bid' (sell_dead
    proceeds). Candles use end_period_ts; a candle 'covers' up to that ts.
    """
    series = sorted(
        ((c.get("end_period_ts", 0), _cents(c.get(side_key))) for c in candles or []),
        key=lambda x: x[0],
    )
    out: dict[int, float | None] = {}
    for off in offsets_min:
        cutoff = alert_ts + off * 60
        val = None
        for ts, cents in series:
            if ts > cutoff:
                break
            if cents is not None:
                val = cents
        out[off] = val
    return out


def fillable_minutes(candles: list[dict], alert_ts: int, cap_c: float,
                     horizon_min: int = FILLABLE_HORIZON_MIN) -> dict:
    """How long a reacting human still fills at ≤ cap after the alert.

    Minute-by-minute carried ask (same last-known-at-cutoff semantics as
    price_at_offsets, so gaps and flicker are handled): counts the minutes
    at or under the cap and the LAST such minute — the reaction budget. The
    2026-07-12 MSP T91 winner flickered 18-26¢ for ~13 min post-alert before
    leaving for good; a simple first-crossing metric would have read that
    window as 2 minutes.
    """
    series = sorted(
        ((c.get("end_period_ts", 0), _cents(c.get("yes_ask"))) for c in candles or []),
        key=lambda x: x[0],
    )
    count, last = 0, None
    val, idx = None, 0
    for m in range(1, horizon_min + 1):
        cutoff = alert_ts + m * 60
        while idx < len(series) and series[idx][0] <= cutoff:
            if series[idx][1] is not None:
                val = series[idx][1]
            idx += 1
        if val is not None and val <= cap_c:
            count += 1
            last = m
    return {"minutes_fillable": count, "last_fillable_min": last}


def decay_rows(findings: list[dict], candles_by_ticker: dict[str, list[dict]],
               cap_c: float = DEFAULT_CAP_C) -> list[dict]:
    rows = []
    for f in findings:
        ticker, kind = f.get("ticker"), f.get("kind")
        detected = f.get("ask") if kind == "buy_winner" else None
        try:
            alert_ts = int(datetime.fromisoformat(f["ts"]).timestamp())
        except (KeyError, ValueError):
            continue
        side_key = "yes_ask" if kind == "buy_winner" else "yes_bid"
        candles = candles_by_ticker.get(ticker, [])
        at = price_at_offsets(candles, alert_ts, side_key)
        row = {"ticker": ticker, "kind": kind,
               "final": bool(f.get("is_final")),
               "detected_cents": detected, "at_offsets": at}
        # Reaction budget only where the entry rule applies: an alert
        # detected over the cap was never takeable, so its window is
        # definitionally empty and would poison the median with zeros.
        if kind == "buy_winner" and detected is not None and detected <= cap_c:
            row["fillable"] = fillable_minutes(candles, alert_ts, cap_c)
        rows.append(row)
    return rows


def summarize(rows: list[dict], cap_c: float = DEFAULT_CAP_C) -> str:
    lines = [f"**Alert decay — {len(rows)} finding(s)** "
             f"(entry price at +N min vs detection)"]
    for label, grp in (("final", [r for r in rows if r["final"]]),
                       ("floor", [r for r in rows if not r["final"]])):
        buys = [r for r in grp if r["kind"] == "buy_winner"
                and r["detected_cents"] is not None]
        if not buys:
            continue
        parts = []
        for off in OFFSETS_MIN:
            deltas = [r["at_offsets"][off] - r["detected_cents"]
                      for r in buys if r["at_offsets"].get(off) is not None]
            parts.append(f"+{off}m: {statistics.median(deltas):+.0f}¢ (n={len(deltas)})"
                         if deltas else f"+{off}m: —")
        lines.append(f"  {label} buys ({len(buys)}): " + "  ".join(parts))
        capped = [r["fillable"] for r in buys if "fillable" in r]
        if capped:
            lasts = [c["last_fillable_min"] or 0 for c in capped]
            counts = [c["minutes_fillable"] for c in capped]
            lines.append(
                f"  {label} reaction budget ≤{cap_c:.0f}¢ (n={len(capped)}): "
                f"last fillable minute med +{statistics.median(lasts):.0f}m "
                f"(min +{min(lasts)}m), fillable {statistics.median(counts):.0f}"
                f"/{FILLABLE_HORIZON_MIN} min")
    lines.append("_Positive Δ = the ask rose after the alert = edge that "
                 "detection latency would have cost. Reaction budget = how "
                 "long a human could still fill at or under the entry cap._")
    return "\n".join(lines)


def load_metar_findings(journal_dir: Path = METAR_JOURNAL_DIR,
                        since: datetime | None = None) -> list[dict]:
    """Flatten metar_sniper journal rows to the shape decay_rows expects.

    The metar journal has no floor/final split — every 6-hr group is a
    floor-class print (is_final=False), so its buys land in the 'floor'
    row of the summary. Suppressed low-ladder buys are excluded: they
    were never alerted, so their decay says nothing about the race.
    """
    out: list[dict] = []
    if not journal_dir.exists():
        return out
    for path in sorted(journal_dir.glob("*.jsonl")):
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = row.get("ts", "")
            if since is not None:
                try:
                    if datetime.fromisoformat(ts) < since:
                        continue
                except ValueError:
                    pass
            for f in row.get("findings") or []:
                if f.get("suppressed"):
                    continue
                out.append({"ts": ts, "is_final": False, **f, "final": False})
    return out


async def fetch_candles(findings: list[dict]) -> dict[str, list[dict]]:
    import os

    from kalshi_client import KalshiClient

    cache: dict[str, list[dict]] = {}
    if CACHE.exists():
        try:
            cache = json.loads(CACHE.read_text())
        except json.JSONDecodeError:
            cache = {}

    todo = []
    for f in findings:
        ticker = f.get("ticker")
        key = f"{ticker}@{f.get('ts')}"
        if ticker and key not in cache:
            todo.append((key, f))
    if not todo:
        return {k.split("@")[0]: v for k, v in cache.items()}

    kc = KalshiClient(api_key_id=os.getenv("KALSHI_API_KEY_ID", ""),
                      private_key_path=os.getenv("KALSHI_PRIVATE_KEY_PATH", ""),
                      demo_mode=False)
    await kc.start()
    try:
        for key, f in todo:
            ticker = f["ticker"]
            series = ticker.split("-")[0]
            alert_ts = int(datetime.fromisoformat(f["ts"]).timestamp())
            path = (f"/series/{series}/markets/{ticker}/candlesticks"
                    f"?start_ts={alert_ts - WINDOW_BEFORE_S}"
                    f"&end_ts={alert_ts + WINDOW_AFTER_S}&period_interval=1")
            resp = await kc._req_safe("GET", path, auth=True)
            cache[key] = resp.get("candlesticks", []) if isinstance(resp, dict) else []
    finally:
        await kc.stop()
    CACHE.write_text(json.dumps(cache))
    return {k.split("@")[0]: v for k, v in cache.items()}


async def run(days: int, journal: str = "cli",
              cap_c: float = DEFAULT_CAP_C) -> None:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    findings = (load_metar_findings(since=since) if journal == "metar"
                else load_findings(since=since))
    if not findings:
        print(f"no {journal} findings in the last {days} day(s)")
        return
    candles = await fetch_candles(findings)
    rows = decay_rows(findings, candles, cap_c=cap_c)
    print(summarize(rows, cap_c=cap_c))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--journal", choices=("cli", "metar"), default="cli",
                    help="which sniper journal to measure (default: cli)")
    ap.add_argument("--cap", type=float, default=DEFAULT_CAP_C,
                    help="entry cap in cents for the reaction-budget metric "
                         f"(default: {DEFAULT_CAP_C})")
    args = ap.parse_args()
    asyncio.run(run(args.days, args.journal, args.cap))


if __name__ == "__main__":
    main()
