#!/usr/bin/env python3
"""WEEKLY DIGEST — per-strategy P&L + live account + dead-bracket base rate.

The measurement loop, automated: strategy attribution has been recorded on
paper positions since 2026-07 but nothing reported it, live trades were
journaled nowhere, and the dead-bracket event frequency — the number that
decides whether the riskless edge pays — was unmeasured. One Discord embed
a week answers all three, so losing patterns get cut on evidence instead
of vibes.

Sections (all file-based reads — no API calls, cannot fail on auth):
  paper      positions_paper.json, entry_time in window, grouped by
             `strategy` (legacy rows without one -> "unattributed")
  live       logs/live_fills.jsonl + logs/live_balance.jsonl (written by
             live_watch.py — schedule that or this section reads empty)
  sweeper    logs/dead_brackets/*.jsonl finding counts + total net

Usage:
    python3 weekly_digest.py --dry-run          # print, no Discord
    python3 weekly_digest.py --days 30

Suggested crontab (NOT auto-installed):
    0 18 * * 0 $VENV $PROJ/weekly_digest.py >> /tmp/weekly_digest.log 2>&1
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from heartbeat import write_heartbeat  # noqa: E402
from log_setup import get_logger  # noqa: E402

logger = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent
POSITIONS_FILE = PROJECT_ROOT / "positions_paper.json"
FILLS_LOG = PROJECT_ROOT / "logs" / "live_fills.jsonl"
BALANCE_LOG = PROJECT_ROOT / "logs" / "live_balance.jsonl"
DEAD_DIR = PROJECT_ROOT / "logs" / "dead_brackets"


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def paper_by_strategy(positions: list[dict], since: datetime) -> dict[str, dict]:
    """Aggregate windowed paper positions per strategy.

    entry_time is the window key — positions here open and resolve within
    a day or two, and no close timestamp is recorded."""
    out: dict[str, dict] = {}
    for p in positions or []:
        ts = _parse_ts(p.get("entry_time"))
        if ts is None or ts < since:
            continue
        strat = p.get("strategy") or "unattributed"
        agg = out.setdefault(strat, {"n": 0, "settled": 0, "wins": 0, "pnl": 0.0, "open": 0})
        agg["n"] += 1
        pnl = p.get("pnl_realized")
        status = p.get("status")
        if status == "open":
            agg["open"] += 1
        if status in ("settled", "closed") and pnl is not None:
            agg["settled"] += 1
            agg["pnl"] += float(pnl)
            if float(pnl) > 0:
                agg["wins"] += 1
    return out


def live_summary(fills: list[dict], balances: list[dict], since: datetime) -> dict:
    window = [f for f in fills if (_parse_ts(f.get("created_time")) or since) >= since]
    fees = sum(float(f.get("fee_cost") or 0) for f in window)
    notional = 0.0
    for f in window:
        try:
            notional += float(f.get("count_fp") or 0) * float(f.get("yes_price_dollars") or 0)
        except (TypeError, ValueError):
            continue
    pts = [(b.get("ts"), b.get("balance")) for b in balances if b.get("balance") is not None]
    delta = None
    if len(pts) >= 2:
        in_window = [p for p in pts if (_parse_ts(p[0]) or since) >= since]
        if len(in_window) >= 2:
            delta = round(in_window[-1][1] - in_window[0][1], 2)
    return {"fills": len(window), "fees": round(fees, 2),
            "notional": round(notional, 2), "balance_delta": delta,
            "balance": pts[-1][1] if pts else None}


def sweeper_summary(since: datetime) -> dict:
    findings = []
    if DEAD_DIR.exists():
        for path in sorted(DEAD_DIR.glob("*.jsonl")):
            findings += [f for f in _read_jsonl(path)
                         if (_parse_ts(f.get("ts")) or since) >= since]
    tickers = {f.get("ticker") for f in findings}
    return {"findings": len(findings), "distinct_brackets": len(tickers),
            "total_net_dollars": round(sum(f.get("net_cents", 0) for f in findings) / 100, 2)}


def build_digest(days: int) -> tuple[str, str]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    lines = []

    try:
        positions = json.loads(POSITIONS_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        positions = []
    strategies = paper_by_strategy(positions, since)
    lines.append(f"**Paper, per strategy ({days}d):**")
    if strategies:
        for strat, a in sorted(strategies.items(), key=lambda kv: -kv[1]["pnl"]):
            wr = f"{a['wins']}/{a['settled']}" if a["settled"] else "0 settled"
            lines.append(f"• {strat}: {a['n']} opened, {wr} won, "
                         f"P&L ${a['pnl']:.2f}" + (f", {a['open']} open" if a["open"] else ""))
    else:
        lines.append("• no positions opened in window")

    live = live_summary(_read_jsonl(FILLS_LOG), _read_jsonl(BALANCE_LOG), since)
    lines.append(f"\n**Live account ({days}d):**")
    if live["fills"] or live["balance"] is not None:
        delta = f"{live['balance_delta']:+.2f}" if live["balance_delta"] is not None else "n/a"
        bal = f"${live['balance']:.2f}" if live["balance"] is not None else "unknown"
        lines.append(f"• {live['fills']} fill(s), ${live['notional']:.2f} notional, "
                     f"${live['fees']:.2f} fees · balance {bal} (Δ {delta})")
    else:
        lines.append("• no live journal yet — schedule live_watch.py")

    sw = sweeper_summary(since)
    lines.append(f"\n**Dead-bracket base rate ({days}d):**")
    lines.append(f"• {sw['findings']} finding(s) on {sw['distinct_brackets']} bracket(s), "
                 f"${sw['total_net_dollars']:.2f} total capturable")

    return f"📊 Weather Edge weekly digest — last {days}d", "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--dry-run", action="store_true", help="print, no Discord")
    args = ap.parse_args()

    title, body = build_digest(args.days)
    if args.dry_run:
        print(title)
        print(body)
    else:
        try:
            from notifications import send_discord_alert

            asyncio.run(send_discord_alert(
                title=title, description=body[:4096],
                color=0x3498DB, context="weekly_digest",
            ))
            logger.info("weekly digest sent")
        except Exception as exc:  # noqa: BLE001 — digest must never crash cron
            logger.warning(f"digest send failed: {exc}")
    write_heartbeat("weekly_digest")


if __name__ == "__main__":
    main()
