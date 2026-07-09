#!/usr/bin/env python3
"""DEAD BRACKET SWEEPER — alert when observation-killed brackets still have bids.

The 2026-07-02 live find: Central Park printed 100.0°F at 17:51Z while
KXHIGHNY "98° or below" still carried ~432 contracts of YES bids
(42/38/26/22¢) — ~$108 net riskless to a YES seller. CLI settlement max is
never below a valid spot observation, so once the running max clears a
bracket's ceiling the bracket is dead; any bid on it is free money until
cancelled. Lows are the mirror image (CLI min ≤ any spot ob).

Detection, per station in ladders.json (all ~40 Kalshi weather ladders;
settlement stations derived from Kalshi's own series metadata and validated
against the NWS stations API by scripts/build_ladder_config.py):
  1. NWS obs → station-local calendar-day running max and min.
  2. Lone-spike guard: the extreme must be corroborated by a second ob within
     CORROBORATION_F, else the station is skipped (bad-sensor protection).
  3. Rounding safety: back the extreme off ROUNDING_BACKOFF_F before rounding
     (METAR T-group is 0.1°C; CLI reports integer °F) so a 99.5°F ob never
     claims a certain 100° settle.
  4. Bracket bounds parse from the market SUBTITLE ("98° or below",
     "99° to 100°", "107° or above") — strike-field semantics differ between
     B- and T-tickers, subtitles don't. Unparseable subtitle → skip.
  5. Net proceeds = Σ (bid − taker_fee(bid)) × qty over YES bids ≥ MIN_BID_C.
  6. Discord alert when a run's total net ≥ --min-net, deduped via
     dead_bracket_state.json (re-alert only when a ticker's net grows 25%).

ALERT ONLY — never trades. Heartbeat "dead_bracket_sweeper" on every clean
exit, in or out of findings (liveness ≠ work-done).

Usage:
    python3 dead_bracket_sweeper.py --once            # cron entry point
    python3 dead_bracket_sweeper.py --once --dry-run  # print, no Discord/state

Suggested crontab (NOT auto-installed):
    */15 * * * * $VENV $PROJ/dead_bracket_sweeper.py --once >> /tmp/dead_bracket_sweeper.log 2>&1
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from core.brackets import is_dead, parse_subtitle  # noqa: E402
from core.io import atomic_write_json  # noqa: E402
from core.obs import (  # noqa: E402
    certain_max_settle,
    certain_min_settle,
    corroborated_extreme,
    fetch_day_obs,
)
from core.fees import kalshi_taker_fee_cents  # noqa: E402
from heartbeat import write_heartbeat  # noqa: E402
from ladders import by_station  # noqa: E402
from log_setup import get_logger  # noqa: E402
from market_timeseries import extract_target_date_from_ticker  # noqa: E402

logger = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent
STATE_FILE = PROJECT_ROOT / "dead_bracket_state.json"
JOURNAL_DIR = PROJECT_ROOT / "logs" / "dead_brackets"
MIN_BID_C = 5              # ignore 1-4¢ dust bids
REALERT_GROWTH = 1.25      # re-alert a known ticker only if net grew 25%
STATE_MAX_AGE_H = 48
DEFAULT_MIN_NET_DOLLARS = 10.0

def bid_proceeds_cents(yes_bids: list, min_bid: int = MIN_BID_C) -> tuple[int, int, list]:
    """(net_cents, contracts, levels) selling YES into all bids ≥ min_bid."""
    net = contracts = 0
    levels = []
    for price, qty in yes_bids or []:
        if price < min_bid:
            continue
        net += (price - kalshi_taker_fee_cents(price)) * qty
        contracts += qty
        levels.append([price, qty])
    return net, contracts, sorted(levels, reverse=True)


def journal_findings(findings: list[dict], now_utc: datetime) -> Path | None:
    """Append EVERY finding to the daily journal, regardless of alert floor.

    The alert path is gated (>= --min-net, deduped) so Discord stays quiet,
    but the event-frequency base rate — the number that decides whether this
    edge pays at all — must be measured uncensored."""
    if not findings:
        return None
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    path = JOURNAL_DIR / f"{now_utc.strftime('%Y-%m-%d')}.jsonl"
    with path.open("a") as fh:
        for f in findings:
            fh.write(json.dumps(
                {"ts": now_utc.isoformat(timespec="seconds"), **f},
                separators=(",", ":")) + "\n")
    return path


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        state = json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=STATE_MAX_AGE_H)
    pruned = {}
    for ticker, entry in state.items():
        try:
            if datetime.fromisoformat(entry["ts"]) >= cutoff:
                pruned[ticker] = entry
        except (KeyError, ValueError):
            continue
    return pruned


def should_alert(state: dict, ticker: str, net_cents: int) -> bool:
    prev = state.get(ticker, {}).get("net_cents")
    return prev is None or net_cents >= prev * REALERT_GROWTH


def record_alert(state: dict, ticker: str, net_cents: int) -> None:
    state[ticker] = {
        "net_cents": net_cents,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    atomic_write_json(STATE_FILE, state)


async def sweep() -> list[dict]:
    """One pass over all ladders; returns dead-bracket findings with live bids."""
    import os

    from kalshi_client import KalshiClient

    client = KalshiClient(
        api_key_id=os.getenv("KALSHI_API_KEY_ID", ""),
        private_key_path=os.getenv("KALSHI_PRIVATE_KEY_PATH", ""),
        demo_mode=False,
    )
    findings: list[dict] = []
    await client.start()
    try:
        for icao, group in by_station().items():
            tz = ZoneInfo(group[0].tz)
            local_today = datetime.now(tz).strftime("%Y-%m-%d")
            try:
                temps = fetch_day_obs(icao, tz)
            except Exception as exc:  # noqa: BLE001 — one station must not kill the run
                logger.warning(f"{icao}: obs fetch failed: {exc}")
                continue
            for ladder in group:
                kind, series = ladder.kind, ladder.series
                extreme = corroborated_extreme(temps, kind)
                if extreme is None:
                    logger.info(f"{icao} {kind}: no corroborated extreme yet")
                    continue
                certain = certain_min_settle(extreme) if kind == "high" else certain_max_settle(extreme)
                try:
                    markets = await client.get_markets(series_ticker=series)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"{series}: market fetch failed: {exc}")
                    continue
                for mkt in markets:
                    ticker = mkt.get("ticker", "")
                    if extract_target_date_from_ticker(ticker) != local_today:
                        continue
                    bounds = parse_subtitle(mkt.get("subtitle") or mkt.get("yes_sub_title"))
                    if bounds is None or not is_dead(kind, *bounds, certain):
                        continue
                    # No yes_bid pre-filter: the summary quote lags the book
                    # (observed 2026-07-02 — summary read 0 while the book
                    # still held 432 contracts). Dead brackets are rare, so
                    # always read the book.
                    try:
                        book = await client.get_orderbook(ticker)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(f"{ticker}: book fetch failed: {exc}")
                        continue
                    net, contracts, levels = bid_proceeds_cents((book or {}).get("yes"))
                    if net <= 0:
                        continue
                    findings.append({
                        "ticker": ticker,
                        "subtitle": mkt.get("subtitle") or mkt.get("yes_sub_title"),
                        "kind": kind, "city": ladder.awips, "station": icao,
                        "extreme_f": round(extreme, 1), "certain_settle": certain,
                        "net_cents": net, "contracts": contracts, "levels": levels,
                    })
    finally:
        await client.stop()
    return findings


def format_alert(findings: list[dict]) -> tuple[str, str]:
    total = sum(f["net_cents"] for f in findings)
    title = f"💰 DEAD BRACKET — {len(findings)} bracket(s), ~${total / 100:.2f} net riskless"
    lines = []
    for f in findings:
        word = "runmax" if f["kind"] == "high" else "runmin"
        bound = "≥" if f["kind"] == "high" else "≤"
        levels = ", ".join(f"{p}¢×{q}" for p, q in f["levels"])
        floor_bid = f["levels"][-1][0] if f["levels"] else 0
        lines.append(
            f"**{f['ticker']}** ({f['subtitle']}) — {f['station']} {word} "
            f"{f['extreme_f']}°F → settles {bound}{f['certain_settle']}°\n"
            f"  bids: {levels} → sell YES, net ~${f['net_cents'] / 100:.2f} "
            f"({f['contracts']} contracts)\n"
            f"  `.venv/bin/python scripts/take.py {f['ticker']} sell yes "
            f"{f['contracts']} {floor_bid} --ioc`"
        )
    lines.append("_Alert only — verify the obs trail before trading._")
    return title, "\n".join(lines)


def send_alert(findings: list[dict]) -> None:
    """Discord alert; failure must never block the heartbeat."""
    title, description = format_alert(findings)
    try:
        from notifications import send_discord_alert

        asyncio.run(send_discord_alert(
            title=title, description=description[:4096],
            color=0x2ECC71, context="dead_bracket_sweeper",
        ))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"discord alert failed: {exc}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--once", action="store_true", help="single sweep (cron mode)")
    ap.add_argument("--dry-run", action="store_true", help="print, skip Discord/state")
    ap.add_argument("--min-net", type=float, default=DEFAULT_MIN_NET_DOLLARS,
                    help="alert only when a run's total net proceeds ≥ this ($)")
    args = ap.parse_args()
    if not args.once:
        ap.error("only --once mode is supported; schedule via cron")

    # Single-instance run lock (same rationale as cli_sniper): overlapping
    # cron runs double-alert and clobber each other's whole-dict state saves.
    # Locked-out runs exit WITHOUT heartbeating so a hung run still trips
    # the watchdog.
    import fcntl
    lock_fd = (PROJECT_ROOT / ".dead_bracket_sweeper.lock").open("w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.info("dead-bracket sweeper: previous run still active — skipping")
        return

    findings = asyncio.run(sweep())
    total = sum(f["net_cents"] for f in findings)
    if not args.dry_run:
        journal_findings(findings, datetime.now(timezone.utc))

    if not findings:
        logger.info("dead-bracket sweep: nothing found")
    elif args.dry_run:
        title, description = format_alert(findings)
        print(title)
        print(description)
    elif total >= args.min_net * 100:
        state = load_state()
        fresh = [f for f in findings if should_alert(state, f["ticker"], f["net_cents"])]
        if fresh:
            send_alert(fresh)
            for f in fresh:
                record_alert(state, f["ticker"], f["net_cents"])
            logger.info(f"dead-bracket sweep: alerted {len(fresh)} (${total / 100:.2f})")
        else:
            logger.info("dead-bracket sweep: findings already alerted")
    else:
        logger.info(f"dead-bracket sweep: ${total / 100:.2f} below ${args.min_net:.2f} floor")
    write_heartbeat("dead_bracket_sweeper")


if __name__ == "__main__":
    main()
