#!/usr/bin/env python3
"""WEEKLY DIGEST — live account + dead-bracket base rate + sniper scorecard.

The measurement loop, automated: one Discord embed a week reporting the
live account, the dead-bracket event frequency — the number that decides
whether the riskless edge pays — and the sniper scorecard headline, so
losing patterns get cut on evidence instead of vibes. (The paper
per-strategy section died with the KDE engine, 2026-07-06.)

Sections (all file-based reads — no API calls, cannot fail on auth):
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
FILLS_LOG = PROJECT_ROOT / "logs" / "live_fills.jsonl"
BALANCE_LOG = PROJECT_ROOT / "logs" / "live_balance.jsonl"
DEAD_DIR = PROJECT_ROOT / "logs" / "dead_brackets"
# Written by backtest/sniper_scorecard.py (its own cron); read here so the
# digest stays network-free (no settlement lookups from this job).
SCORECARD_VERDICT = PROJECT_ROOT / "backtest" / "sniper_scorecard_verdict.json"


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

    # (Paper per-strategy section removed 2026-07-06 — the KDE paper engine
    # was retired; the digest reports the settlement-source system only.)
    live = live_summary(_read_jsonl(FILLS_LOG), _read_jsonl(BALANCE_LOG), since)
    lines.append(f"**Live account ({days}d):**")
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

    lines.append("\n**CLI sniper scorecard:**")
    lines.append("• " + sniper_scorecard_line())

    return f"📊 Weather Edge weekly digest — last {days}d", "\n".join(lines)


def sniper_scorecard_line() -> str:
    """One-line headline from the scorecard's verdict artifact (network-free)."""
    try:
        v = json.loads(SCORECARD_VERDICT.read_text())
    except (OSError, json.JSONDecodeError):
        return "no scorecard verdict yet — schedule backtest/sniper_scorecard.py"
    o = v.get("overall") or {}
    if not o.get("n"):
        return f"no settled findings yet ({v.get('pending', 0)} pending)"
    cert = v.get("by_certainty") or {}
    cert_str = ", ".join(f"{k} {c['mean_per_contract_cents']:+.0f}¢×{c['n']}"
                         for k, c in cert.items())
    return (f"{o['n']} settled: hit {o['hit_rate']:.0%}, "
            f"{o['mean_per_contract_cents']:+.1f}¢/contract, "
            f"${o['total_dollars']:+.2f} realized ({cert_str})")


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
