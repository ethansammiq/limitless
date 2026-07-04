#!/usr/bin/env python3
"""
AUTO TRADER — Fully automated scan-to-execution pipeline.

Orchestrates the complete trading cycle:
  1. Run scan (via auto_scan.run_scan)
  2. Filter for 90+ confidence opportunities
  3. Run all safety checks (trading_guards)
  4. Execute trades (via execute_trade.execute_auto)
  5. Send Discord notifications for every action

EXECUTION IS OPT-IN since 2026-07: the KDE edge loop measured -EV against
the market (10W/28L lifetime, model Brier 0.159 vs market 0.098), so the
default mode is SCAN ONLY — full scan, alerts, confidence updates, and
heartbeats, but no order placement. Re-enable with --execute or
AUTO_TRADER_EXECUTE=true in .env.

Usage:
  python3 auto_trader.py                    # Scan only (default)
  python3 auto_trader.py --execute          # Scan + place orders
  python3 auto_trader.py --dry-run          # Scan + show what would trade
  python3 auto_trader.py --city NYC         # Single city

Cron setup (replaces auto_scan.py cron entries):
  0 6 * * *   $VENV $PROJ/auto_trader.py >> /tmp/auto_trader.log 2>&1
  0 8 * * *   $VENV $PROJ/auto_trader.py >> /tmp/auto_trader.log 2>&1
  0 10 * * *  $VENV $PROJ/auto_trader.py >> /tmp/auto_trader.log 2>&1
  0 15 * * *  $VENV $PROJ/auto_trader.py >> /tmp/auto_trader.log 2>&1
  0 23 * * *  $VENV $PROJ/auto_trader.py >> /tmp/auto_trader.log 2>&1

Emergency stop:
  touch /Users/miqadmin/Documents/limitless/PAUSE_TRADING
"""

import argparse
import asyncio
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from auto_scan import format_discord_alert, run_scan
from config import (
    STATIONS,
    MIN_CONFIDENCE_TO_TRADE,
    MAX_ENTRY_PRICE_CENTS,
    REENTRY_MIN_CONFIDENCE,
    REENTRY_COOLDOWN_MINUTES,
    REENTRY_MAX_PER_TICKER_PER_DAY,
    SETTLEMENT_HOUR_ET,
    TRADE_SCORE_ENABLED,
    TRADE_SCORE_THRESHOLD,
    WATCHLIST,
    WATCHLIST_MIN_CONFIDENCE,
)
from trade_score import compute_trade_score, should_trade
from edge_scanner_v2 import fetch_kalshi_brackets, shorten_bracket_title
from execute_trade import execute_auto
from notifications import send_discord_alert, send_discord_embeds
from outcome_tracker import log_trade_prediction
from position_store import load_positions, position_transaction
from preflight import preflight_check
from trading_guards import check_kill_switch, run_all_pre_trade_checks
from proxy_arb_engine import run_proxy_scan
from utils.state_db import get_db
from log_setup import get_logger
from trade_events import log_event, TradeEvent

logger = get_logger(__name__)

ET = ZoneInfo("America/New_York")

# Build reverse mapping: series_ticker -> city_code
SERIES_TO_CITY = {s.series_ticker: code for code, s in STATIONS.items()}

# Alert diffing — only re-send the multi-embed scan dump when the opportunity
# set or prices changed materially since the last send (cron fires 6x/day).
ALERT_STATE_FILE = Path(__file__).resolve().parent / "alert_state.json"
ALERT_PRICE_CHANGE_CENTS = float(os.getenv("ALERT_PRICE_CHANGE_CENTS", "3"))

# Ticker date segment, e.g. KXHIGHNY-26JUN13-B34.5 -> 26JUN13
_TICKER_DATE_RE = re.compile(r"-(\d{2}[A-Z]{3}\d{2})-")


def _entry_price(opp) -> int:
    """Compute bid+1 entry price, capped at MAX_ENTRY_PRICE."""
    if opp.side == "yes":
        return min(opp.yes_bid + 1, MAX_ENTRY_PRICE_CENTS)
    return min((100 - opp.yes_ask) + 1, MAX_ENTRY_PRICE_CENTS)


def _city_for_ticker(ticker: str) -> str:
    """Extract city code from ticker via series prefix."""
    match = re.match(r"^([A-Z]+)", ticker)
    return SERIES_TO_CITY.get(match.group(1), "UNK") if match else "UNK"


def _parse_market_close(market: dict) -> datetime | None:
    """Extract the close/settlement timestamp from a Kalshi market payload.

    Prefers close_time (trading deadline); falls back to the expected or
    actual expiration. Returns an aware datetime, or None if unparseable.
    """
    raw = (
        market.get("close_time")
        or market.get("expected_expiration_time")
        or market.get("expiration_time")
    )
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)  # Kalshi timestamps are UTC
    return parsed


def _settlement_from_ticker(ticker: str) -> datetime | None:
    """Fallback: settlement derived from the ticker's embedded market date.

    A daily-high market for date D is determined by end of day D and pays out
    the next morning, so settlement is D+1 at SETTLEMENT_HOUR_ET.
    """
    match = _TICKER_DATE_RE.search(ticker)
    if not match:
        return None
    try:
        market_date = datetime.strptime(match.group(1), "%y%b%d").date()
    except ValueError:
        return None
    return datetime.combine(
        market_date + timedelta(days=1), datetime.min.time()
    ).replace(hour=SETTLEMENT_HOUR_ET, tzinfo=ET)


def _next_settlement_heuristic(now: datetime) -> datetime:
    """Last resort when neither payload nor ticker yields a date: next 7 AM ET."""
    today_settle = datetime.combine(now.date(), datetime.min.time()).replace(
        hour=SETTLEMENT_HOUR_ET, tzinfo=ET,
    )
    if now < today_settle:
        return today_settle
    return datetime.combine(
        now.date() + timedelta(days=1), datetime.min.time()
    ).replace(hour=SETTLEMENT_HOUR_ET, tzinfo=ET)


async def _fetch_market_close_times(opps: list) -> dict[str, datetime]:
    """Fetch ticker -> close timestamp for every series present in opps."""
    city_codes = {_city_for_ticker(opp.ticker) for opp in opps}
    city_codes.discard("UNK")
    close_times: dict[str, datetime] = {}
    if not city_codes:
        return close_times
    async with aiohttp.ClientSession() as session:
        for city_code in sorted(city_codes):
            try:
                markets = await fetch_kalshi_brackets(session, city_code)
            except Exception as e:
                logger.warning("Close-time fetch failed for %s: %s", city_code, e)
                continue
            for market in markets:
                ticker = market.get("ticker", "")
                parsed = _parse_market_close(market)
                if ticker and parsed is not None:
                    close_times[ticker] = parsed
    return close_times


def _hours_to_settlement_by_ticker(
    opps: list,
    close_times: dict[str, datetime],
    now: datetime,
) -> dict[str, float]:
    """Per-ticker hours to settlement from the actual market close timestamps.

    The old next-7AM heuristic made the 6 AM run treat tomorrow-settling
    markets (~25-49h out) as ~1h away, inflating the trade-score urgency
    signal and flipping live trade decisions.
    """
    hours: dict[str, float] = {}
    for opp in opps:
        settle_dt = close_times.get(opp.ticker) or _settlement_from_ticker(opp.ticker)
        if settle_dt is None:
            settle_dt = _next_settlement_heuristic(now)
        hours[opp.ticker] = max(0.5, (settle_dt - now).total_seconds() / 3600)
    return hours


def _opportunity_fingerprint(opps: list, tradeable_tickers: set[str]) -> dict[str, dict]:
    """Compact per-ticker snapshot used to detect material changes between scans."""
    fingerprint: dict[str, dict] = {}
    for opp in opps:
        price = opp.yes_bid if opp.side == "yes" else (100 - opp.yes_ask)
        fingerprint[opp.ticker] = {
            "side": opp.side,
            "price": int(price),
            "edge_cents": round(opp.edge_after_fees * 100, 1),
            "tradeable": opp.ticker in tradeable_tickers,
        }
    return fingerprint


def _scan_changed_materially(
    prev: dict | None,
    curr: dict,
    price_threshold_cents: float | None = None,
) -> bool:
    """True when the opportunity set, sides, tradeable flags, or prices moved.

    Price/edge moves below the threshold (ALERT_PRICE_CHANGE_CENTS) are
    treated as noise and do not trigger a re-alert.
    """
    if prev is None:
        return True
    threshold = (
        price_threshold_cents if price_threshold_cents is not None
        else ALERT_PRICE_CHANGE_CENTS
    )
    if set(prev) != set(curr):
        return True
    for ticker, c in curr.items():
        p = prev[ticker]
        if p.get("side") != c["side"] or bool(p.get("tradeable")) != c["tradeable"]:
            return True
        if abs(float(p.get("price", 0)) - c["price"]) >= threshold:
            return True
        if abs(float(p.get("edge_cents", 0.0)) - c["edge_cents"]) >= threshold:
            return True
    return False


def _load_alert_state() -> dict | None:
    """Last-sent opportunity fingerprint, or None if absent/corrupt."""
    try:
        with open(ALERT_STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    fingerprint = data.get("fingerprint")
    return fingerprint if isinstance(fingerprint, dict) else None


def _save_alert_state(fingerprint: dict) -> None:
    try:
        payload = {
            "sent_at": datetime.now(ET).isoformat(),
            "fingerprint": fingerprint,
        }
        with open(ALERT_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    except OSError as e:
        logger.warning("Could not persist alert state: %s", e)


async def _send_scan_embeds_if_changed(
    *,
    all_opps: list,
    tradeable: list,
    city_summaries: list,
    balance: float,
    scan_time: str,
    hours_by_ticker: dict[str, float],
    scan_only: bool,
) -> None:
    """Send the scan-summary embeds only when the scan changed materially.

    auto_trader suppresses run_scan's own Discord send (dry_run=True) and
    gates the embeds here on a fingerprint diff, so the 6x/day cron only
    alerts when the opportunity set or prices actually moved.
    """
    fingerprint = _opportunity_fingerprint(all_opps, {o.ticker for o in tradeable})
    if not _scan_changed_materially(_load_alert_state(), fingerprint):
        logger.info("Scan unchanged since last alert — skipping Discord embeds")
        return

    # Display-only: format_discord_alert takes one representative h2s value.
    rep_h2s = min(hours_by_ticker.values()) if hours_by_ticker else 14.0
    embeds = format_discord_alert(
        all_opps, city_summaries, balance, scan_time, hours_to_settlement=rep_h2s,
    )
    if not scan_only:
        # The bot executes tradeable setups itself seconds later — a manual
        # execute_trade.py prompt would invite duplicate fills.
        embeds = [e for e in embeds if "execute_trade.py" not in e.get("description", "")]
    if embeds:
        await send_discord_embeds(embeds, context="auto_trader")
    _save_alert_state(fingerprint)


def _find_reentry_candidates(
    positions: list, all_opps: list, now: datetime,
    hours_by_ticker: dict[str, float] | None = None,
) -> list[tuple]:
    """Find positions that were recently trailing-stop exited but still have high scanner confidence.

    Returns list of (Opportunity, exited_position_dict) tuples.

    Re-entry criteria:
    - Position was closed by trailing stop (not thesis break or ROI backstop)
    - Exited within the last REENTRY_COOLDOWN_MINUTES (enough time to re-assess)
    - Scanner still shows tradeable (trade_score or confidence >= REENTRY_MIN_CONFIDENCE)
    - No more than REENTRY_MAX_PER_TICKER_PER_DAY re-entries for this ticker today
    """
    if not all_opps:
        return []

    # Build ticker -> opp lookup (only tradeable opps)
    opp_by_ticker = {}
    for opp in all_opps:
        if TRADE_SCORE_ENABLED:
            h2s = (hours_by_ticker or {}).get(opp.ticker, 14.0)
            if should_trade(opp, h2s):
                opp_by_ticker[opp.ticker] = opp
        else:
            if opp.confidence_score >= REENTRY_MIN_CONFIDENCE:
                opp_by_ticker[opp.ticker] = opp

    if not opp_by_ticker:
        return []

    today_str = now.strftime("%Y-%m-%d")
    candidates = []

    for pos in positions:
        # Only consider recently closed/pending_sell positions from trailing stop
        if pos.get("status") not in ("closed", "pending_sell", "settled"):
            continue

        ticker = pos.get("ticker", "")
        if ticker not in opp_by_ticker:
            continue

        # Check exit reason: only re-enter after trailing stop, not thesis break.
        # Search ALL notes (not just last N) to avoid missing exit reasons
        # that got pushed out by subsequent notes like confidence updates.
        notes = pos.get("notes", [])
        is_trailing_exit = any("TRAILING STOP" in str(n) for n in notes)
        is_thesis_exit = any("THESIS BREAK" in str(n) for n in notes)
        is_roi_exit = any("ROI BACKSTOP" in str(n) for n in notes)

        if not is_trailing_exit or is_thesis_exit or is_roi_exit:
            continue

        # Check cooldown: must have exited at least REENTRY_COOLDOWN_MINUTES ago
        sell_placed = pos.get("sell_placed_at", "")
        if sell_placed:
            try:
                exit_time = datetime.fromisoformat(sell_placed)
                if exit_time.tzinfo is None:
                    exit_time = exit_time.replace(tzinfo=ET)
                minutes_since_exit = (now - exit_time).total_seconds() / 60
                if minutes_since_exit < REENTRY_COOLDOWN_MINUTES:
                    continue  # Too soon
            except (ValueError, TypeError):
                continue  # Can't parse exit time, skip

        # Check daily re-entry cap for this ticker
        reentry_count_today = sum(
            1 for n in notes
            if "RE-ENTRY" in str(n) and today_str in str(n)
        )
        if reentry_count_today >= REENTRY_MAX_PER_TICKER_PER_DAY:
            continue

        candidates.append((opp_by_ticker[ticker], pos))

    return candidates


async def auto_trade(
    city_filter: str = None,
    dry_run: bool = False,
    scan_only: bool = False,
):
    """Main auto-trading pipeline."""
    now = datetime.now(ET)
    mode = "DRY RUN" if dry_run else "SCAN ONLY" if scan_only else "LIVE"
    logger.info("AUTO TRADER — %s | Mode: %s", now.strftime("%I:%M %p ET, %a %b %d"), mode)

    # ── Step -1: Preflight credential validation ──
    if not dry_run and not scan_only:
        preflight_check(fatal=True)  # Exits with code 1 if critical issue

    # ── Step 0: Kill switch ──
    ok, reason = check_kill_switch()
    if not ok:
        logger.error("HALTED: %s", reason)
        log_event(TradeEvent.KILL_SWITCH_ACTIVE, "auto_trader", {"reason": reason})
        await send_discord_alert(
            "AUTO TRADER HALTED",
            reason,
            color=0xFF0000,
            context="auto_trader",
        )
        return

    # ── Step 1: Run scan (reuse auto_scan.run_scan) ──
    # dry_run=True suppresses run_scan's own Discord send; auto_trader
    # re-sends the embeds diff-gated in Step 2.8 to avoid 6x/day repeats.
    scan_result = await run_scan(city_filter=city_filter, quiet=False, dry_run=True)
    all_opps = scan_result.get("opps", [])
    balance = scan_result.get("balance", 0.0)
    city_summaries = scan_result.get("city_summaries", [])

    # ── Step 1.5: Run concurrent proxy scan for upwind front data ──
    # The proxy engine fetches 1-min ASOS + wind vectors for every city.
    # dry_run=True here prevents the proxy engine from placing its own orders;
    # auto_trader controls all order placement in Step 3.
    proxy_signals: dict = {}
    try:
        from core.broker_factory import get_broker as _get_broker
        _proxy_broker = await _get_broker()
        try:
            proxy_signals = await run_proxy_scan(
                kalshi_client=_proxy_broker,
                db=get_db(),
                city_codes=[city_filter.upper()] if city_filter else None,
                nws_forecasts={},
                dry_run=True,
            )
            logger.info("Proxy front scan complete (%d cities)", len(proxy_signals))
        finally:
            await _proxy_broker.stop()
    except Exception as _proxy_err:
        logger.warning("Proxy front scan failed (non-critical, continuing): %s", _proxy_err)

    # ── Step 2: Compute hours to settlement per market ──
    # Use the actual close/settlement timestamp from each Kalshi market
    # payload (falling back to the ticker's market date) instead of the old
    # next-7AM heuristic, which treated tomorrow-settling markets as ~1h out
    # at the 6 AM run and inflated the trade-score urgency signal.
    close_times: dict[str, datetime] = {}
    if all_opps:
        try:
            close_times = await _fetch_market_close_times(all_opps)
        except Exception as e:
            logger.warning("Market close-time fetch failed (using ticker dates): %s", e)
    hours_by_ticker = _hours_to_settlement_by_ticker(all_opps, close_times, now)

    # ── Step 2.5: Filter for tradeable (hybrid trade score or legacy gate) ──
    tradeable = []
    trade_scores = {}  # ticker -> TradeScore (for logging)
    near_misses = []   # opps close to threshold (for calibration)

    for opp in all_opps:
        if TRADE_SCORE_ENABLED:
            ts = compute_trade_score(opp, hours_by_ticker[opp.ticker])
            trade_scores[opp.ticker] = ts
            if ts.tradeable:
                tradeable.append(opp)
            elif ts.score >= TRADE_SCORE_THRESHOLD * 0.9:
                near_misses.append((opp, ts))
        else:
            if opp.confidence_score >= MIN_CONFIDENCE_TO_TRADE:
                tradeable.append(opp)

    # Log near-misses for calibration data
    if near_misses:
        logger.info("Near-miss(es) (%d):", len(near_misses))
        for opp, ts in near_misses:
            short = shorten_bracket_title(opp.bracket_title)
            fails = ", ".join(ts.floor_failures) if ts.floor_failures else "none"
            logger.info("  %s: TS=%.3f (threshold=%.2f) conf=%.0f edge=%.0f¢ floors=[%s]",
                        short, ts.score, TRADE_SCORE_THRESHOLD, opp.confidence_score,
                        opp.edge_after_fees * 100, fails)
            log_event(TradeEvent.NEAR_MISS, "auto_trader", {
                "ticker": opp.ticker, "bracket": short, "trade_score": round(ts.score, 3),
                "threshold": TRADE_SCORE_THRESHOLD, "confidence": round(opp.confidence_score, 1),
                "edge_cents": round(opp.edge_after_fees * 100, 1), "floor_failures": fails,
            })

    # ── Step 2.7: Update open positions with latest confidence + trend ──
    _update_position_confidence(all_opps, city_summaries)

    # ── Step 2.8: Scan-summary embeds, diff-gated against the last send ──
    await _send_scan_embeds_if_changed(
        all_opps=all_opps,
        tradeable=tradeable,
        city_summaries=city_summaries,
        balance=balance,
        scan_time=scan_result.get("scan_time", ""),
        hours_by_ticker=hours_by_ticker,
        scan_only=scan_only,
    )

    if not tradeable:
        gate_desc = f"TS>={TRADE_SCORE_THRESHOLD}" if TRADE_SCORE_ENABLED else "90+ confidence"
        miss_note = f" ({len(near_misses)} near-miss)" if near_misses else ""
        logger.info("No %s setups%s. Patience is the edge.", gate_desc, miss_note)

        # Log watchlist cities even when nothing is tradeable
        for cs in city_summaries:
            if cs["key"] not in WATCHLIST:
                continue
            city_opps = cs.get("opps", [])
            if not city_opps:
                continue
            best = max(city_opps, key=lambda o: o.confidence_score)
            if best.confidence_score >= WATCHLIST_MIN_CONFIDENCE:
                short = shorten_bracket_title(best.bracket_title)
                price = best.yes_bid if best.side == "yes" else (100 - best.yes_ask)
                logger.info(
                    "  👁️ WATCHLIST %s: conf=%d, %s %s @ %d¢, KDE=%.1f%%, σ=%.1f°, trend=%s",
                    cs["key"], best.confidence_score, best.side.upper(), short, price,
                    best.kde_prob * 100, cs.get("std", 0), cs.get("temp_trend", "?"),
                )

        _write_heartbeat()
        return

    h_vals = sorted(hours_by_ticker[o.ticker] for o in tradeable)
    logger.info("%d TRADEABLE setup(s) found! (hours_to_settlement=%.1f-%.1fh)",
                len(tradeable), h_vals[0], h_vals[-1])

    if scan_only:
        logger.info("--scan-only mode, not executing.")
        _write_heartbeat()
        return

    # ── Step 3: Execute each tradeable opportunity ──
    positions = load_positions()
    executed = []
    skipped = []

    # Best score first (trade score when enabled, else confidence)
    if TRADE_SCORE_ENABLED:
        tradeable.sort(key=lambda o: trade_scores.get(o.ticker, None) and trade_scores[o.ticker].score or 0, reverse=True)
    else:
        tradeable.sort(key=lambda o: o.confidence_score, reverse=True)

    # Broker (live or paper based on PAPER_TRADING_MODE)
    from core.broker_factory import get_broker
    try:
        client = await get_broker()
    except RuntimeError as e:
        logger.error("Broker init failed: %s", e)
        await send_discord_alert(
            "AUTO TRADER ERROR",
            str(e),
            color=0xFF0000,
            context="auto_trader",
        )
        _write_heartbeat()
        return

    try:
        for opp in tradeable:
            entry_price = _entry_price(opp)
            contracts = opp.suggested_contracts
            cost = (entry_price / 100) * contracts
            short = shorten_bracket_title(opp.bracket_title)
            city_code = _city_for_ticker(opp.ticker)
            station = STATIONS.get(city_code)

            ts_info = ""
            if TRADE_SCORE_ENABLED and opp.ticker in trade_scores:
                ts = trade_scores[opp.ticker]
                ts_info = f" TS={ts.score:.3f}"
                for reason in ts.reasons:
                    logger.debug("  %s", reason)
            logger.info("Evaluating: %s %s @ %dc x%d (conf:%.0f%s)",
                        opp.side.upper(), short, entry_price, contracts, opp.confidence_score, ts_info)

            # ── Safety checks (including upwind shield if proxy data available) ──
            dsm_times = station.dsm_times_z if station else []
            six_hour = station.six_hour_z if station else []
            _proxy_sig = proxy_signals.get(city_code)
            _proxy_vectors = _proxy_sig.propagation_vectors if _proxy_sig else None
            all_ok, check_results = run_all_pre_trade_checks(
                positions=positions,
                balance=balance,
                city_key=city_code,
                new_cost=cost,
                dsm_times_z=dsm_times,
                six_hour_z=six_hour,
                series_to_city=SERIES_TO_CITY,
                dry_run=dry_run,
                proxy_vectors=_proxy_vectors,
                bracket_bounds=(opp.low, opp.high),
                trade_side=opp.side,
            )

            for cr in check_results:
                logger.debug("  %s", cr)

            if not all_ok:
                fail_reasons = "; ".join(r for r in check_results if r.startswith("FAIL"))
                skipped.append({"opp": opp, "reason": fail_reasons})
                logger.info("  SKIPPED — safety check(s) failed")
                log_event(TradeEvent.TRADE_SKIPPED, "auto_trader", {
                    "ticker": opp.ticker, "reason": fail_reasons, "confidence": round(opp.confidence_score, 1),
                })
                continue

            if contracts <= 0:
                skipped.append({"opp": opp, "reason": "0 contracts (budget exhausted)"})
                logger.info("  SKIPPED — 0 contracts suggested")
                log_event(TradeEvent.TRADE_SKIPPED, "auto_trader", {
                    "ticker": opp.ticker, "reason": "budget_exhausted",
                })
                continue

            # ── Duplicate order guard ──
            # Don't place a new order if we already have a resting or open position for this ticker
            existing = [p for p in positions
                        if p.get("ticker") == opp.ticker and p.get("status") in ("resting", "open")]
            if existing:
                skipped.append({"opp": opp, "reason": f"Already have {existing[0]['status']} position"})
                logger.info("  SKIPPED — already %s for %s", existing[0]["status"], opp.ticker)
                log_event(TradeEvent.TRADE_SKIPPED, "auto_trader", {
                    "ticker": opp.ticker, "reason": f"duplicate_{existing[0]['status']}",
                })
                continue

            # ── Execute ──
            if dry_run:
                logger.info("  [DRY RUN] Would place: %s %s @ %dc x%d", opp.side.upper(), opp.ticker, entry_price, contracts)
                executed.append({"opp": opp, "price": entry_price, "qty": contracts, "dry_run": True})
            else:
                result = await execute_auto(
                    ticker=opp.ticker,
                    side=opp.side,
                    price=entry_price,
                    quantity=contracts,
                    client=client,
                    close_client=False,
                    strategy="auto_trader",
                )
                if result["success"]:
                    logger.info("  EXECUTED: %s (order: %s)", result["status"], result["order_id"])
                    executed.append({"opp": opp, "price": entry_price, "qty": contracts, "result": result})
                    log_event(TradeEvent.TRADE_EXECUTED, "auto_trader", {
                        "ticker": opp.ticker, "side": opp.side, "price": entry_price,
                        "qty": contracts, "cost": round(cost, 2),
                        "confidence": round(opp.confidence_score, 1),
                        "trade_score": round(trade_scores[opp.ticker].score, 3) if opp.ticker in trade_scores else None,
                        "order_id": result["order_id"],
                    })
                    # Log prediction for calibration tracking
                    log_trade_prediction(
                        opp,
                        trade_score=trade_scores.get(opp.ticker),
                        hours_to_settlement=hours_by_ticker.get(opp.ticker, 14.0),
                        entry_price=entry_price,
                        action="entry",
                    )
                    # Refresh for next iteration's checks
                    positions = load_positions()
                    balance = await client.get_balance()
                else:
                    skipped.append({"opp": opp, "reason": result["error"]})
                    logger.error("  FAILED: %s", result["error"])
                    log_event(TradeEvent.TRADE_FAILED, "auto_trader", {
                        "ticker": opp.ticker, "error": result["error"],
                    })

        # ── Step 3.5: Re-entry after trailing stop exits ──
        # If a position was exited by trailing stop recently and the scanner
        # still shows high confidence for that ticker, re-enter.
        reentry_candidates = _find_reentry_candidates(positions, all_opps, now, hours_by_ticker)
        if reentry_candidates:
            logger.info("%d RE-ENTRY candidate(s) found", len(reentry_candidates))
        for opp, exited_pos in reentry_candidates:
            entry_price = _entry_price(opp)
            contracts = opp.suggested_contracts
            cost = (entry_price / 100) * contracts
            short = shorten_bracket_title(opp.bracket_title)
            city_code = _city_for_ticker(opp.ticker)
            station = STATIONS.get(city_code)

            logger.info("RE-ENTRY: %s %s @ %dc x%d (conf:%.0f, exited %s)",
                        opp.side.upper(), short, entry_price, contracts,
                        opp.confidence_score, exited_pos.get("_exit_reason", "trailing_stop"))

            # Safety checks (same as fresh trades, including upwind shield)
            dsm_times = station.dsm_times_z if station else []
            six_hour = station.six_hour_z if station else []
            # Refresh positions for accurate guard checks
            positions = load_positions()
            _proxy_sig = proxy_signals.get(city_code)
            _proxy_vectors = _proxy_sig.propagation_vectors if _proxy_sig else None
            all_ok, check_results = run_all_pre_trade_checks(
                positions=positions,
                balance=balance,
                city_key=city_code,
                new_cost=cost,
                dsm_times_z=dsm_times,
                six_hour_z=six_hour,
                series_to_city=SERIES_TO_CITY,
                dry_run=dry_run,
                proxy_vectors=_proxy_vectors,
                bracket_bounds=(opp.low, opp.high),
                trade_side=opp.side,
            )
            for cr in check_results:
                logger.debug("    %s", cr)

            if not all_ok or contracts <= 0:
                reason = "; ".join(r for r in check_results if r.startswith("FAIL")) or "0 contracts"
                skipped.append({"opp": opp, "reason": f"RE-ENTRY blocked: {reason}"})
                logger.info("  RE-ENTRY SKIPPED — %s", reason)
                log_event(TradeEvent.TRADE_SKIPPED, "auto_trader", {
                    "ticker": opp.ticker, "reason": f"reentry_blocked: {reason}",
                    "reentry": True,
                })
                continue

            if dry_run:
                logger.info("  [DRY RUN] Would re-enter: %s %s @ %dc x%d",
                            opp.side.upper(), opp.ticker, entry_price, contracts)
                executed.append({"opp": opp, "price": entry_price, "qty": contracts, "dry_run": True, "reentry": True})
            else:
                result = await execute_auto(
                    ticker=opp.ticker, side=opp.side, price=entry_price,
                    quantity=contracts, client=client, close_client=False,
                    strategy="auto_trader",
                )
                if result["success"]:
                    logger.info("  RE-ENTERED: %s (order: %s)", result["status"], result["order_id"])
                    executed.append({"opp": opp, "price": entry_price, "qty": contracts, "result": result, "reentry": True})
                    log_event(TradeEvent.TRADE_REENTRY, "auto_trader", {
                        "ticker": opp.ticker, "side": opp.side, "price": entry_price,
                        "qty": contracts, "cost": round(cost, 2),
                        "confidence": round(opp.confidence_score, 1),
                        "order_id": result["order_id"],
                        "exited_reason": exited_pos.get("_exit_reason", "trailing_stop"),
                    })
                    log_trade_prediction(
                        opp,
                        trade_score=trade_scores.get(opp.ticker),
                        hours_to_settlement=hours_by_ticker.get(opp.ticker, 14.0),
                        entry_price=entry_price,
                        action="reentry",
                    )
                    positions = load_positions()
                    balance = await client.get_balance()
                else:
                    skipped.append({"opp": opp, "reason": f"RE-ENTRY failed: {result['error']}"})
                    logger.error("  RE-ENTRY FAILED: %s", result["error"])
                    log_event(TradeEvent.TRADE_FAILED, "auto_trader", {
                        "ticker": opp.ticker, "error": result["error"], "reentry": True,
                    })

    finally:
        await client.stop()

    # ── Step 4: Summary Discord alert ──
    if executed:
        lines = []
        for ex in executed:
            o = ex["opp"]
            short = shorten_bracket_title(o.bracket_title)
            dr = " [DRY RUN]" if ex.get("dry_run") else ""
            reentry_tag = " 🔄 RE-ENTRY" if ex.get("reentry") else ""
            ts_txt = ""
            if TRADE_SCORE_ENABLED and o.ticker in trade_scores:
                ts_txt = f" TS:{trade_scores[o.ticker].score:.2f}"
            lines.append(
                f"**{o.side.upper()} {short} @ {ex['price']}c x{ex['qty']}** "
                f"(conf:{o.confidence_score:.0f}{ts_txt}){dr}{reentry_tag}"
            )
        await send_discord_alert(
            f"AUTO TRADER: {len(executed)} trade(s) {'simulated' if dry_run else 'placed'}",
            "\n".join(lines) + f"\n\nBalance: ${balance:.2f}",
            color=0x00FF00 if not dry_run else 0x3498DB,
            context="auto_trader",
        )

    if skipped and not dry_run:
        skip_lines = []
        for sk in skipped[:5]:
            o = sk["opp"]
            short = shorten_bracket_title(o.bracket_title)
            skip_lines.append(f"**{short}** -- {sk['reason']}")
        await send_discord_alert(
            f"AUTO TRADER: {len(skipped)} skipped",
            "\n".join(skip_lines),
            color=0xFFAA00,
            context="auto_trader",
        )

    _write_heartbeat()

    total_exec = len(executed)
    total_skip = len(skipped)
    logger.info("SUMMARY: %d executed, %d skipped, %d tradeable", total_exec, total_skip, len(tradeable))


def _update_position_confidence(all_opps: list, city_summaries: list = None):
    """Write latest confidence score, trend, bracket bounds, and current obs into open positions.

    The position_monitor reads these fields to decide:
    - thesis-break exits (last_confidence < 40)
    - settlement-hold overrides (distance_to_bracket)
    - observation-aware sell/hold decisions (current_obs_temp vs bracket_low/bracket_high)
    """
    if not all_opps:
        return

    # Build ticker -> opp data lookup from scan results
    opp_lookup = {}
    for opp in all_opps:
        opp_lookup[opp.ticker] = {
            "confidence": opp.confidence_score,
            "bracket_low": opp.low,
            "bracket_high": opp.high,
        }

    # Build city_key -> current_temp lookup from city summaries
    city_temp_lookup = {}
    if city_summaries:
        for cs in city_summaries:
            city_temp_lookup[cs["key"]] = {
                "current_temp": cs.get("current_temp", 0.0),
                "temp_trend": cs.get("temp_trend", ""),
            }

    try:
        with position_transaction() as positions:
            updated = 0
            for p in positions:
                if p.get("status") not in ("open", "pending_sell", "resting"):
                    continue
                ticker = p.get("ticker", "")
                if ticker in opp_lookup:
                    data = opp_lookup[ticker]
                    p["last_confidence"] = data["confidence"]
                    p["bracket_low"] = data["bracket_low"]
                    p["bracket_high"] = data["bracket_high"]

                    # Look up city-level observation data
                    city_code = _city_for_ticker(ticker)
                    if city_code in city_temp_lookup:
                        city_data = city_temp_lookup[city_code]
                        p["current_obs_temp"] = city_data["current_temp"]
                        p["trend"] = city_data["temp_trend"]
                    updated += 1
            if updated:
                logger.info("Updated confidence/trend/bracket on %d open position(s)", updated)
                log_event(TradeEvent.CONFIDENCE_UPDATED, "auto_trader", {"positions_updated": updated})
    except Exception as e:
        # Non-fatal: if this fails, ROI backstop still protects
        logger.warning("Could not update position confidence: %s", e)


def _write_heartbeat():
    """Record heartbeat for watchdog monitoring."""
    try:
        from heartbeat import write_heartbeat
        write_heartbeat("auto_trader")
    except Exception as e:
        logger.warning("Failed to write heartbeat: %s", e)


def resolve_scan_only(execute_flag: bool, scan_only_flag: bool, env_value: str | None) -> bool:
    """Scan-only unless execution is explicitly requested via flag or env.

    Default flipped 2026-07: the KDE edge loop is measured -EV, so order
    placement is opt-in while scanning, alerts, and heartbeats keep running.
    An explicit --scan-only always wins over the env override.
    """
    if scan_only_flag:
        return True
    execute = execute_flag or (env_value or "").strip().lower() in ("1", "true", "yes")
    return not execute


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Auto Trader -- scan pipeline (execution opt-in)")
    parser.add_argument("--city", type=str, default=None, help="Single city code (NYC, CHI, DEN, MIA, LAX)")
    parser.add_argument("--dry-run", action="store_true", help="Scan and evaluate but don't place orders")
    parser.add_argument("--execute", action="store_true",
                        help="Place orders (default scan-only; AUTO_TRADER_EXECUTE=true also enables)")
    parser.add_argument("--scan-only", action="store_true", help="Force scan-only (overrides env)")
    args = parser.parse_args()
    asyncio.run(auto_trade(
        args.city, args.dry_run,
        resolve_scan_only(args.execute, args.scan_only, os.getenv("AUTO_TRADER_EXECUTE")),
    ))
