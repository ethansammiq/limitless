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
# Latest full-account snapshot (overwritten each run) — the dashboard reads
# this to show REAL money instead of the paper ledger.
ACCOUNT_FILE = LOGS / "live_account.json"
STATE_FILE = PROJECT_ROOT / "live_watch_state.json"

DEFAULT_STRENGTH_C = 85
REALERT_STEP_C = 3
# A 1-lot 99c flicker is a phantom quote, not an exit (2026-07-04: a bid
# printed 99c for seconds on a book whose last trade was 18c). Only alert
# when there's real size within a few cents of the best bid.
MIN_ALERT_DEPTH = 5
DEPTH_BAND_C = 3


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


def bid_depth_near_best(yes_bids_sorted: list, band: int = DEPTH_BAND_C) -> float:
    """Contracts resting within `band` cents of the best YES bid."""
    if not yes_bids_sorted:
        return 0
    best = yes_bids_sorted[0][0]
    return sum(q for p, q in yes_bids_sorted if p >= best - band)


def should_alert_strength(state: dict, ticker: str, bid: int, threshold: int,
                          depth: float = MIN_ALERT_DEPTH) -> bool:
    if bid < threshold or depth < MIN_ALERT_DEPTH:
        return False
    prev = state.get(ticker, {}).get("bid")
    return prev is None or bid >= prev + REALERT_STEP_C


def account_snapshot(balance, positions: list[dict], fills: list[dict], now: str) -> dict:
    """Full real-account view for the dashboard: balance, realized P&L (incl.
    closed positions), open/closed positions, recent fills."""
    realized_total = 0.0
    open_pos, closed_pos = [], []
    for p in positions or []:
        try:
            qty = float(p.get("position_fp") or 0)
            realized = float(p.get("realized_pnl_dollars") or 0)
            exposure = float(p.get("market_exposure_dollars") or 0)
        except (TypeError, ValueError):
            continue
        realized_total += realized
        row = {"ticker": p.get("ticker"), "qty": qty,
               "realized": round(realized, 2), "exposure": round(exposure, 2)}
        (open_pos if qty != 0 else closed_pos).append(row)
    recent = []
    for f in sorted(fills or [], key=lambda x: x.get("created_time", ""), reverse=True)[:12]:
        try:
            px = round(float(f.get("yes_price_dollars") or 0) * 100)
        except (TypeError, ValueError):
            px = None
        recent.append({"ts": f.get("created_time"), "ticker": f.get("ticker"),
                       "action": f.get("action"), "price_c": px,
                       "count": f.get("count_fp"), "taker": f.get("is_taker")})
    return {
        "updated": now,
        "balance": round(balance, 2) if balance is not None else None,
        "realized_total": round(realized_total, 2),
        "open_positions": open_pos,
        "closed_positions": [c for c in closed_pos if c["realized"] != 0],
        "recent_fills": recent,
    }


def _append(path: Path, obj: dict) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(obj, separators=(",", ":")) + "\n")


def reads_degraded(fills_resp) -> bool:
    """True when the authenticated fills read failed — a real response always
    carries a 'fills' key, while kalshi_client._req_safe degrades errors
    (e.g. 401 from a bad key path) to {}/None. Writing a snapshot from a
    degraded read would show a false $0.00 balance on the dashboard
    (happened live 2026-07-05 after the VPS migration)."""
    return not isinstance(fills_resp, dict) or "fills" not in fills_resp


async def run(threshold: int, dry_run: bool) -> None:
    import os

    from kalshi_client import KalshiClient

    key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
    if not key_path or not Path(key_path).is_file():
        logger.error(f"private key missing at {key_path!r} — refusing a "
                     "credential-less run (would journal a false $0 snapshot)")
        return

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    client = KalshiClient(
        api_key_id=os.getenv("KALSHI_API_KEY_ID", ""),
        private_key_path=key_path,
        demo_mode=False,
    )
    await client.start()
    try:
        # None (not 0.0) on a degraded read — a false $0.00 once reached the
        # public equity curve (2026-07-05). The balance endpoint can fail
        # independently of fills, so it needs its own guard.
        balance = await client.get_balance_checked()
        fills_resp = await client._req_safe("GET", "/portfolio/fills?limit=100", auth=True)
        if reads_degraded(fills_resp) or balance is None:
            logger.error("authenticated reads degraded (auth failure?) — "
                         "skipping journal/snapshot writes to protect live_account.json")
            return
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
                p["bid_depth"] = bid_depth_near_best(yes_bids)
                strength.append((p, bid))

        if dry_run:
            print(f"balance ${balance:.2f} | {len(fresh)} new fill(s) | {len(longs)} open long(s)")
            for p, bid in strength:
                real = bid >= threshold and p["bid_depth"] >= MIN_ALERT_DEPTH
                print(f"  {p['ticker']}: {p['qty']:.0f}x, best bid {bid}c "
                      f"(depth {p['bid_depth']:.0f})"
                      f"{'  <-- SELL-INTO-STRENGTH' if real else ''}")
            return

        for f in fresh:
            _append(FILLS_LOG, {"logged_at": now, **f})
        if fresh:
            logger.info(f"journaled {len(fresh)} new live fill(s)")
        if longs:
            _append(POSITIONS_LOG, {"ts": now, "positions": longs})
        if balance is not None and balance != last_logged_balance():
            _append(BALANCE_LOG, {"ts": now, "balance": balance})

        # Latest full snapshot for the dashboard (atomic overwrite).
        LOGS.mkdir(parents=True, exist_ok=True)
        snap = account_snapshot(balance, positions, (fills_resp or {}).get("fills"), now)
        tmp = ACCOUNT_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(snap, indent=2))
        tmp.replace(ACCOUNT_FILE)

        state = {}
        if STATE_FILE.exists():
            try:
                state = json.loads(STATE_FILE.read_text())
            except (json.JSONDecodeError, OSError):
                state = {}
        pings = [(p, bid) for p, bid in strength
                 if should_alert_strength(state, p["ticker"], bid, threshold, p["bid_depth"])]
        if pings:
            lines = [
                f"**{p['ticker']}** — {p['qty']:.0f} contracts, best bid **{bid}¢** "
                f"({p['bid_depth']:.0f} within {DEPTH_BAND_C}¢)\n"
                f"  Your playbook: sell into strength (90¢ now beats $1 tomorrow).\n"
                f"  `.venv/bin/python scripts/take.py {p['ticker']} sell yes "
                f"{p['qty']:.0f} {max(1, bid - 2)} --ioc`"
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
