#!/usr/bin/env python3
"""
PEAK TRADER — Strategy G: Peak → Trade Pipeline

When peak_monitor confirms the daily high, this module auto-executes a
trade on the settlement bracket.  The confirmed peak means the outcome
is ~95%+ certain, so any bracket priced below 85¢ is a near-guaranteed
profit opportunity.

Flow:
  peak_monitor.poll_once()
    → peak confirmed → fetch bracket prices
    → peak_trader.evaluate_peak_trade(state, bracket_info, tz)
      → safety checks (kill switch, balance, position limits)
      → execute_auto() if edge passes
      → Discord alert

Designed to be called from peak_monitor.py — NOT standalone.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from log_setup import get_logger
from config import (
    PEAK_TRADE_ENABLED,
    PEAK_TRADE_MIN_EDGE_CENTS,
    PEAK_TRADE_MAX_PRICE_CENTS,
    PEAK_TRADE_MIN_HOURS_TO_SETTLE,
    PEAK_TRADE_TRUE_PROB_CENTS,
    PEAK_TRADE_MAX_CONTRACTS,
    MAX_POSITION_PCT,
    SETTLEMENT_HOUR_ET,
    STATIONS,
)

logger = get_logger(__name__)

ET = ZoneInfo("America/New_York")


def _hours_until_settlement() -> float:
    """Compute hours until next settlement (7 AM ET)."""
    now = datetime.now(ET)
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
    return max(0.0, (settlement_dt - now).total_seconds() / 3600)


def compute_peak_trade(
    bracket_info: dict,
    balance: float,
) -> dict:
    """Evaluate whether a peak trade should be executed.

    Parameters
    ----------
    bracket_info : dict
        From peak_monitor.find_bracket_price(). Must contain:
        yes_bid, yes_ask, ticker, title, volume.
    balance : float
        Current account balance in dollars.

    Returns
    -------
    dict with keys:
        execute: bool — whether to trade
        ticker: str
        side: str ("yes")
        entry_price: int — bid + 1 (maker, 0% fee)
        contracts: int
        cost: float
        edge_cents: int
        hours_to_settlement: float
        reason: str — why we're trading or not
    """
    result = {
        "execute": False,
        "ticker": bracket_info.get("ticker", ""),
        "side": "yes",
        "entry_price": 0,
        "contracts": 0,
        "cost": 0.0,
        "edge_cents": 0,
        "hours_to_settlement": 0.0,
        "reason": "",
    }

    if not PEAK_TRADE_ENABLED:
        result["reason"] = "PEAK_TRADE_ENABLED is False"
        return result

    bid = bracket_info.get("yes_bid", 0)
    ask = bracket_info.get("yes_ask", 0)
    ticker = bracket_info.get("ticker", "")
    volume = bracket_info.get("volume", 0)

    if not ticker:
        result["reason"] = "No ticker in bracket_info"
        return result

    # Edge calculation: true prob (~95%) minus market bid
    edge_cents = PEAK_TRADE_TRUE_PROB_CENTS - bid
    result["edge_cents"] = edge_cents

    hours_to_settle = _hours_until_settlement()
    result["hours_to_settlement"] = hours_to_settle

    # ── Gate 1: Minimum edge ──
    if edge_cents < PEAK_TRADE_MIN_EDGE_CENTS:
        result["reason"] = f"Edge {edge_cents}¢ < min {PEAK_TRADE_MIN_EDGE_CENTS}¢"
        return result

    # ── Gate 2: Maximum price (don't buy above 85¢) ──
    if bid >= PEAK_TRADE_MAX_PRICE_CENTS:
        result["reason"] = f"Bid {bid}¢ >= max {PEAK_TRADE_MAX_PRICE_CENTS}¢ — too expensive"
        return result

    # ── Gate 3: Minimum time to settlement ──
    if hours_to_settle < PEAK_TRADE_MIN_HOURS_TO_SETTLE:
        result["reason"] = f"Only {hours_to_settle:.1f}h to settlement (min {PEAK_TRADE_MIN_HOURS_TO_SETTLE}h)"
        return result

    # ── Gate 4: Balance check ──
    if balance <= 0:
        result["reason"] = "Zero balance"
        return result

    # ── Sizing: bid+1 entry (maker, 0% fee), capped at budget ──
    entry_price = min(bid + 1, PEAK_TRADE_MAX_PRICE_CENTS)
    max_cost = balance * MAX_POSITION_PCT
    contracts = min(
        PEAK_TRADE_MAX_CONTRACTS,
        int(max_cost / (entry_price / 100)) if entry_price > 0 else 0,
    )

    if contracts <= 0:
        result["reason"] = f"0 contracts at {entry_price}¢ (budget ${max_cost:.2f})"
        return result

    cost = (entry_price / 100) * contracts

    result.update({
        "execute": True,
        "entry_price": entry_price,
        "contracts": contracts,
        "cost": cost,
        "reason": (
            f"Peak trade: {ticker} YES @ {entry_price}¢ x{contracts} "
            f"(edge={edge_cents}¢, {hours_to_settle:.1f}h to settle)"
        ),
    })
    return result


async def execute_peak_trade(
    city_key: str,
    peak_temp: float,
    bracket_info: dict,
    dry_run: bool = False,
) -> dict:
    """Full peak trade pipeline: evaluate → safety checks → execute → alert.

    Returns dict with 'success', 'order_id', 'reason', 'transient', 'trade'.
    'transient' marks failures worth retrying on a later poll (API blips),
    as opposed to deterministic gates (edge/price/position limits).
    """
    from trading_guards import check_kill_switch
    from notifications import send_discord_alert
    from position_store import load_positions
    from core.broker_factory import get_broker

    result = {
        "success": False,
        "order_id": "",
        "reason": "",
        "trade": None,
        "transient": False,
    }

    # ── Kill switch ──
    ok, reason = check_kill_switch()
    if not ok:
        result["reason"] = f"Kill switch: {reason}"
        logger.info("Peak trade blocked by kill switch: %s", reason)
        return result

    # ── Broker (routes to PaperBroker when PAPER_TRADING_MODE=true) ──
    try:
        broker = await get_broker()
    except RuntimeError as e:
        # Misconfiguration (e.g. missing live credentials) — retry won't help.
        result["reason"] = f"Broker init failed: {e}"
        logger.error("Peak trade broker init failed: %s", e)
        return result
    except Exception as e:
        result["reason"] = f"Broker init failed: {e}"
        result["transient"] = True
        logger.error("Peak trade broker init failed: %s", e)
        return result

    try:
        # ── Fetch balance ──
        try:
            balance = await broker.get_balance()
        except Exception as e:
            result["reason"] = f"Balance fetch failed: {e}"
            result["transient"] = True
            logger.error("Peak trade balance fetch failed: %s", e)
            return result

        # ── Evaluate trade ──
        trade = compute_peak_trade(bracket_info, balance)
        result["trade"] = trade

        if not trade["execute"]:
            result["reason"] = trade["reason"]
            logger.info("Peak trade not executing: %s", trade["reason"])
            return result

        # ── Check for existing position on same ticker ──
        try:
            positions = load_positions()
            existing = [
                p for p in positions
                if p.get("ticker") == trade["ticker"]
                and p.get("status") in ("open", "resting")
            ]
            if existing:
                result["reason"] = f"Already have position on {trade['ticker']}"
                logger.info("Peak trade skipped — existing position on %s", trade["ticker"])
                return result
        except Exception as e:
            logger.warning("Failed to check existing positions for %s: %s — proceeding without duplicate check", trade["ticker"], e)

        station = STATIONS.get(city_key)
        station_name = station.city_name if station else city_key

        if dry_run:
            logger.info(
                "[DRY RUN] Would execute peak trade: %s YES %s @ %d¢ x%d",
                city_key, trade["ticker"], trade["entry_price"], trade["contracts"],
            )
            result["success"] = True
            result["reason"] = f"[DRY RUN] {trade['reason']}"

            await send_discord_alert(
                title=f"🔒⚡ PEAK TRADE [DRY RUN] — {station_name}",
                description=(
                    f"**Peak: {peak_temp:.1f}°F confirmed**\n"
                    f"**YES {trade['ticker']} @ {trade['entry_price']}¢ x{trade['contracts']}**\n"
                    f"Edge: +{trade['edge_cents']}¢ | Cost: ${trade['cost']:.2f}\n"
                    f"Settlement in {trade['hours_to_settlement']:.1f}h\n"
                    f"Balance: ${balance:.2f}"
                ),
                color=0x3498DB,
                context="peak_trader",
            )
            return result

        # ── Execute ──
        try:
            from execute_trade import execute_auto

            exec_result = await execute_auto(
                ticker=trade["ticker"],
                side="yes",
                price=trade["entry_price"],
                quantity=trade["contracts"],
                client=broker,
                close_client=False,
                strategy="peak_trader",
            )

            if exec_result["success"]:
                result["success"] = True
                result["order_id"] = exec_result["order_id"]
                result["reason"] = f"EXECUTED: {exec_result['status']} — {trade['reason']}"

                logger.info(
                    "🔒⚡ PEAK TRADE EXECUTED: %s %s @ %d¢ x%d (order: %s)",
                    city_key, trade["ticker"], trade["entry_price"],
                    trade["contracts"], exec_result["order_id"],
                )

                await send_discord_alert(
                    title=f"🔒⚡ PEAK TRADE EXECUTED — {station_name}",
                    description=(
                        f"**Peak: {peak_temp:.1f}°F confirmed → AUTO TRADE**\n"
                        f"**YES {trade['ticker']} @ {trade['entry_price']}¢ x{trade['contracts']}**\n"
                        f"Edge: +{trade['edge_cents']}¢ | Cost: ${trade['cost']:.2f}\n"
                        f"Settlement in {trade['hours_to_settlement']:.1f}h\n"
                        f"Order: `{exec_result['order_id']}`\n"
                        f"Status: {exec_result['status']}"
                    ),
                    color=0x00FF00,
                    context="peak_trader",
                )
            else:
                result["reason"] = f"Execution failed: {exec_result['error']}"
                # Could be an API blip at the order endpoint — let the caller
                # retry on the next poll rather than forfeit the day's trade.
                result["transient"] = True
                logger.error("Peak trade execution failed: %s", exec_result["error"])

                await send_discord_alert(
                    title=f"🔒❌ PEAK TRADE FAILED — {station_name}",
                    description=(
                        f"**Peak: {peak_temp:.1f}°F confirmed but trade FAILED**\n"
                        f"Attempted: YES {trade['ticker']} @ {trade['entry_price']}¢ x{trade['contracts']}\n"
                        f"Error: {exec_result['error']}"
                    ),
                    color=0xFF0000,
                    context="peak_trader",
                )

        except Exception as e:
            result["reason"] = f"Exception: {e}"
            result["transient"] = True
            logger.error("Peak trade exception: %s", e, exc_info=True)

        return result
    finally:
        await broker.stop()
