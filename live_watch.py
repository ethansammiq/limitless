#!/usr/bin/env python3
"""LIVE WATCH — read-only journal + sell-into-strength alerts for the LIVE account.

The bot's guards, exits, and ledger all live on the paper path; real-money
trades enter manually and were invisible to everything (2026-07-04: the first
live positions existed only in Kalshi's database). This job closes that gap
WITHOUT trading:

  fills     new fills (deduped by fill_id) -> logs/live_fills.jsonl
  positions snapshot of nonzero positions  -> logs/live_positions.jsonl
  balance   appended on change             -> logs/live_balance.jsonl (equity curve)
  alert     long YES position with best bid >= --threshold (default 85c)
            -> Discord "sell into strength" ping, deduped via
            live_watch_state.json (re-pings only if the bid climbs 3c+)

READ-ONLY by design: it tells you when to sell; it never sells. (Live order
placement is mid-migration to Kalshi's V2 schema anyway.)

Usage:
    python3 live_watch.py --once              # cron entry point
    python3 live_watch.py --once --dry-run    # print, no writes/Discord

Suggested crontab (NOT auto-installed):
    */10 * * * * $VENV $PROJ/live_watch.py --once >> /tmp/live_watch.log 2>&1
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from heartbeat import write_heartbeat  # noqa: E402
from log_setup import get_logger  # noqa: E402

logger = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent
LOGS = PROJECT_ROOT / "logs"
FILLS_LOG = LOGS / "live_fills.jsonl"
POSITIONS_LOG = LOGS / "live_positions.jsonl"
BALANCE_LOG = LOGS / "live_balance.jsonl"
STATE_FILE = PROJECT_ROOT / "live_watch_state.json"

DEFAULT_STRENGTH_C = 85
REALERT_STEP_C = 3


def known_fill_ids() -> set[str]:
    if not FILLS_LOG.exists():
        return set()
    ids = set()
    for line in FILLS_LOG.read_text().splitlines():
        try:
            fid = json.loads(line).get("fill_id")
        except json.JSONDecodeError:
            continue
        if fid:
            ids.add(fid)
    return ids


def new_fills(fills: list[dict], known: set[str]) -> list[dict]:
    return [f for f in fills or [] if f.get("fill_id") and f["fill_id"] not in known]


def last_logged_balance() -> float | None:
    if not BALANCE_LOG.exists():
        return None
    lines = BALANCE_LOG.read_text().splitlines()
    for line in reversed(lines):
        try:
            return json.loads(line).get("balance")
        except json.JSONDecodeError:
            continue
    return None


def open_long_positions(positions: list[dict]) -> list[dict]:
    out = []
    for p in positions or []:
        try:
            qty = float(p.get("position_fp") or 0)
        except (TypeError, ValueError):
            continue
        if qty > 0:
            out.append({**p, "qty": qty})
    return out


def should_alert_strength(state: dict, ticker: str, bid: int, threshold: int) -> bool:
    if bid < threshold:
        return False
    prev = state.get(ticker, {}).get("bid")
    return prev is None or bid >= prev + REALERT_STEP_C


def _append(path: Path, obj: dict) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(obj, separators=(",", ":")) + "\n")


async def run(threshold: int, dry_run: bool) -> None:
    import os

    from kalshi_client import KalshiClient

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    client = KalshiClient(
        api_key_id=os.getenv("KALSHI_API_KEY_ID", ""),
        private_key_path=os.getenv("KALSHI_PRIVATE_KEY_PATH", ""),
        demo_mode=False,
    )
    await client.start()
    try:
        balance = await client.get_balance()
        fills_resp = await client._req_safe("GET", "/portfolio/fills?limit=100", auth=True)
        fresh = new_fills((fills_resp or {}).get("fills"), known_fill_ids())
        positions = await client.get_positions()
        longs = open_long_positions(positions)

        strength: list[tuple[dict, int]] = []
        for p in longs:
            try:
                book = await client.get_orderbook(p["ticker"])
            except Exception as exc:  # noqa: BLE001 — one book must not kill the run
                logger.warning(f"{p['ticker']}: book fetch failed: {exc}")
                continue
            yes_bids = sorted((book or {}).get("yes") or [], key=lambda lv: -lv[0])
            bid = yes_bids[0][0] if yes_bids else None
            if bid is not None:
                p["best_bid"] = bid
                strength.append((p, bid))

        if dry_run:
            print(f"balance ${balance:.2f} | {len(fresh)} new fill(s) | {len(longs)} open long(s)")
            for p, bid in strength:
                print(f"  {p['ticker']}: {p['qty']:.0f}x, best bid {bid}c"
                      f"{'  <-- SELL-INTO-STRENGTH' if bid >= threshold else ''}")
            return

        for f in fresh:
            _append(FILLS_LOG, {"logged_at": now, **f})
        if fresh:
            logger.info(f"journaled {len(fresh)} new live fill(s)")
        if longs:
            _append(POSITIONS_LOG, {"ts": now, "positions": longs})
        if balance is not None and balance != last_logged_balance():
            _append(BALANCE_LOG, {"ts": now, "balance": balance})

        state = {}
        if STATE_FILE.exists():
            try:
                state = json.loads(STATE_FILE.read_text())
            except (json.JSONDecodeError, OSError):
                state = {}
        pings = [(p, bid) for p, bid in strength if should_alert_strength(state, p["ticker"], bid, threshold)]
        if pings:
            lines = [
                f"**{p['ticker']}** — {p['qty']:.0f} contracts, best bid **{bid}¢**\n"
                f"  Your playbook: sell into strength (90¢ now beats $1 tomorrow)."
                for p, bid in pings
            ]
            try:
                from notifications import send_discord_alert

                await send_discord_alert(
                    title=f"📈 LIVE position(s) at {threshold}¢+ — consider selling",
                    description="\n".join(lines)[:4096],
                    color=0xF1C40F,
                    context="live_watch",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"discord alert failed: {exc}")
            for p, bid in pings:
                state[p["ticker"]] = {"bid": bid, "ts": now}
            STATE_FILE.write_text(json.dumps(state, indent=2))
            logger.info(f"strength alert sent for {len(pings)} position(s)")
    finally:
        await client.stop()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--once", action="store_true", help="single pass (cron mode)")
    ap.add_argument("--dry-run", action="store_true", help="print, no writes/Discord")
    ap.add_argument("--threshold", type=int, default=DEFAULT_STRENGTH_C,
                    help="best-bid cents that triggers the sell-into-strength ping")
    args = ap.parse_args()
    if not args.once:
        ap.error("only --once mode is supported; schedule via cron")
    asyncio.run(run(args.threshold, args.dry_run))
    write_heartbeat("live_watch")


if __name__ == "__main__":
    main()
