#!/usr/bin/env python3
"""
AUTO TRADER — Fully automated scan-to-execution pipeline.

Orchestrates the complete trading cycle:
  1. Run scan (via auto_scan.run_scan)
  2. Filter for 90+ confidence opportunities
  3. Run all safety checks (trading_guards)
  4. Execute trades (via execute_trade.execute_auto)
  5. Send Discord notifications for every action

Usage:
  python3 auto_trader.py                    # Full auto (scan + execute)
  python3 auto_trader.py --dry-run          # Scan + show what would trade
  python3 auto_trader.py --city NYC         # Single city
  python3 auto_trader.py --scan-only        # Same as auto_scan.py (no execution)

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
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from auto_scan import run_scan
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
from edge_scanner_v2 import shorten_bracket_title
from execute_trade import execute_auto
from kalshi_client import KalshiClient
from notifications import send_discord_alert
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


def _entry_price(opp) -> int:
    """Compute bid+1 entry price, capped at MAX_ENTRY_PRICE."""
    if opp.side == "yes":
        return min(opp.yes_bid + 1, MAX_ENTRY_PRICE_CENTS)
    return min((100 - opp.yes_ask) + 1, MAX_ENTRY_PRICE_CENTS)


def _city_for_ticker(ticker: str) -> str:
    """Extract city code from ticker via series prefix."""
    match = re.match(r"^([A-Z]+)", ticker)
    return SERIES_TO_CITY.get(match.group(1), "UNK") if match else "UNK"


def _find_reentry_candidates(
    positions: list, all_opps: list, now: datetime,
    hours_to_settlement: float = 14.0,
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
            if should_trade(opp, hours_to_settlement):
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
    scan_result = await run_scan(city_filter=city_filter, quiet=False, dry_run=False)
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
                city_codes=list(cities_to_scan.keys()) if city_filter else None,
                nws_forecasts={},
                dry_run=True,
            )
            logger.info("Proxy front scan complete (%d cities)", len(proxy_signals))
        finally:
            await _proxy_broker.stop()
    except Exception as _proxy_err:
        logger.warning("Proxy front scan failed (non-critical, continuing): %s", _proxy_err)

    # ── Step 2: Compute hours to settlement ──
    # Target today's settlement if before settlement hour, else tomorrow's.
    today_settle = datetime.combine(now.date(), datetime.min.time()).replace(
        hour=SETTLEMENT_HOUR_ET, tzinfo=ET,
    )
    if now < today_settle:
        settlement_dt = today_settle
    else:
        tomorrow = (now + timedelta(days=1)).date()
        settlement_dt = datetime.combine(tomorrow, datetime.min.time()).replace(
            hour=SETTLEMENT_HOUR_ET, tzinfo=ET,
        )
    hours_to_settlement = max(0.5, (settlement_dt - now).total_seconds() / 3600)

    # ── Step 2.5: Filter for tradeable (hybrid trade score or legacy gate) ──
    tradeable = []
    trade_scores = {}  # ticker -> TradeScore (for logging)
    near_misses = []   # opps close to threshold (for calibration)

    for opp in all_opps:
        if TRADE_SCORE_ENABLED:
            ts = compute_trade_score(opp, hours_to_settlement)
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

    logger.info("%d TRADEABLE setup(s) found! (hours_to_settlement=%.1fh)", len(tradeable), hours_to_settlement)

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
                        hours_to_settlement=hours_to_settlement,
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
        reentry_candidates = _find_reentry_candidates(positions, all_opps, now, hours_to_settlement)
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
                        hours_to_settlement=hours_to_settlement,
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
            re = " 🔄 RE-ENTRY" if ex.get("reentry") else ""
            ts_txt = ""
            if TRADE_SCORE_ENABLED and o.ticker in trade_scores:
                ts_txt = f" TS:{trade_scores[o.ticker].score:.2f}"
            lines.append(
                f"**{o.side.upper()} {short} @ {ex['price']}c x{ex['qty']}** "
                f"(conf:{o.confidence_score:.0f}{ts_txt}){dr}{re}"
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Auto Trader -- Fully automated trading pipeline")
    parser.add_argument("--city", type=str, default=None, help="Single city code (NYC, CHI, DEN, MIA, LAX)")
    parser.add_argument("--dry-run", action="store_true", help="Scan and evaluate but don't place orders")
    parser.add_argument("--scan-only", action="store_true", help="Scan only, no execution (same as auto_scan.py)")
    args = parser.parse_args()
    asyncio.run(auto_trade(args.city, args.dry_run, args.scan_only))
