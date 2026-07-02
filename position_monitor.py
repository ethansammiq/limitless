#!/usr/bin/env python3
"""
POSITION MONITOR — Automated take-profit and exit management.

Since Kalshi has NO native take-profit or stop-loss orders, this script
polls positions and places sell orders when exit conditions are met.

EXIT RULES:
  1. FREEROLL:    When price doubles (2x entry), sell half to recover cost basis
  2. EFFICIENCY:  When price hits 90¢, sell everything (90¢ now > $1 tomorrow)
  3. THESIS BREAK: When confidence drops below 40, alert to sell everything

Run via cron every 5 minutes when positions are open:
  */5 * * * * cd /Users/miqadmin/Documents/limitless && python3 position_monitor.py >> /tmp/position_monitor.log 2>&1

Or run manually:
  python3 position_monitor.py             # Check all positions
  python3 position_monitor.py --once      # Single check, no loop
  python3 position_monitor.py --status    # Show positions only
"""

import argparse
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from kalshi_client import KalshiClient, parse_fp
from position_store import load_positions, position_transaction
from paper_accounting import settle_position_record, rebuild_balance, balance_drift

# Paper cash may legitimately lead the ledger by up to one unbooked sell's
# proceeds within a cycle; only alert when drift exceeds this (the corruption
# we saw was $2,000+). The per-cycle sync auto-corrects regardless.
BALANCE_DRIFT_ALERT_USD = 1.00
from notifications import send_discord_alert
from preflight import preflight_check
from log_setup import get_logger
from trade_events import log_event, TradeEvent

logger = get_logger(__name__)

ET = ZoneInfo("America/New_York")

# Exit thresholds (single source of truth: config.py)
from config import (
    FREEROLL_MULTIPLIER,
    CAPITAL_EFFICIENCY_THRESHOLD_CENTS as EFFICIENCY_EXIT_CENTS,
    TRAILING_OFFSET_CENTS,
    TRAILING_ZONES,
    MID_PROFIT_THRESHOLD_CENTS,
    MID_PROFIT_SELL_FRACTION,
    SETTLEMENT_HOLD_THRESHOLD_CENTS as SETTLEMENT_HOLD_THRESHOLD,
    SETTLEMENT_HOUR_ET,
    SETTLEMENT_WINDOW_HOURS,
    STOP_LOSS_ROI_PCT,
    STOP_LOSS_FLOOR_CENTS,
    THESIS_BREAK_CONFIDENCE,
    STATIONS,
    PENDING_SELL_EXPIRY_MINUTES,
    RESTING_ORDER_TIMEOUT_MINUTES,
    # ── Exit strategy upgrades ──
    TIME_DECAY_ENABLED,
    TIME_DECAY_MIDPOINT_HOURS,
    TIME_DECAY_STEEPNESS,
    TIME_DECAY_TRAILING_MIN_FACTOR,
    TIME_DECAY_FREEROLL_MIN_FACTOR,
    TIME_DECAY_MID_PROFIT_MIN_FACTOR,
    OBS_TRAILING_ENABLED,
    OBS_TRAILING_WIDEN_FACTOR,
    OBS_TRAILING_TIGHTEN_FACTOR,
    OBS_TRAILING_DIVERGENCE_F,
    ADAPTIVE_FREEROLL_ENABLED,
    ADAPTIVE_FREEROLL_TIERS,
    QUICK_PROFIT_ENABLED,
    QUICK_PROFIT_ROI_PCT,
    QUICK_PROFIT_SELL_FRACTION,
    QUICK_PROFIT_MIN_CONTRACTS,
    MOMENTUM_EXIT_ENABLED,
    MOMENTUM_DROP_ALERT_CENTS,
    MOMENTUM_DROP_TIGHTEN_CENTS,
    THESIS_TRIM_ENABLED,
    THESIS_TRIM_CONFIDENCE_HIGH,
    THESIS_TRIM_SELL_FRACTION,
    SELL_REPRICE_ENABLED,
    SELL_REPRICE_MIN_CYCLES,
    SELL_REPRICE_BID_DRIFT_CENTS,
    SELL_REPRICE_MAX_PER_ORDER,
)

# Build series_ticker -> city_code mapping for bot window checks
_SERIES_TO_CITY = {s.series_ticker: code for code, s in STATIONS.items()}


def _trailing_offset_for_price(price_cents: int) -> int:
    """Return the trailing stop offset based on the current price zone.

    Wider offsets at low prices (house money, let it breathe) and
    tighter offsets near the efficiency exit to protect large gains.
    Falls back to the legacy TRAILING_OFFSET_CENTS if no zone matches.
    """
    for lo, hi, offset in TRAILING_ZONES:
        if lo <= price_cents < hi:
            return offset
    return TRAILING_OFFSET_CENTS  # fallback


# ═══════════════════════════════════════════════════════════════
# EXIT STRATEGY UPGRADE HELPERS — Pure functions, independently testable
# ═══════════════════════════════════════════════════════════════

import math


def _time_decay_factor(hours_to_settlement: float) -> float:
    """Sigmoid-based decay factor: 1.0 far from settlement → 0.0 at settlement.

    f(h) = 1 / (1 + exp(-steepness * (h - midpoint)))
    At midpoint hours: f = 0.5
    At 20+ hours: f ≈ 1.0
    At 0-1 hours: f ≈ 0.0

    Returns value in [0, 1]. Used by other helpers to tighten thresholds.
    """
    return 1.0 / (1.0 + math.exp(-TIME_DECAY_STEEPNESS * (hours_to_settlement - TIME_DECAY_MIDPOINT_HOURS)))


def _scaled_trailing_offset(base_offset: int, hours_to_settlement: float) -> int:
    """Apply time-decay to trailing stop offset. Minimum 2¢.

    As settlement approaches, the offset tightens from `base_offset` toward
    `base_offset * TIME_DECAY_TRAILING_MIN_FACTOR`.
    """
    if not TIME_DECAY_ENABLED:
        return base_offset
    f = _time_decay_factor(hours_to_settlement)
    # Lerp between min_factor and 1.0 based on decay
    effective_factor = TIME_DECAY_TRAILING_MIN_FACTOR + f * (1.0 - TIME_DECAY_TRAILING_MIN_FACTOR)
    return max(2, int(round(base_offset * effective_factor)))


def _scaled_freeroll_multiplier(base_mult: float, hours_to_settlement: float) -> float:
    """Apply time-decay to freeroll multiplier.

    Near settlement, the multiplier drops (triggers freeroll earlier).
    """
    if not TIME_DECAY_ENABLED:
        return base_mult
    f = _time_decay_factor(hours_to_settlement)
    effective_factor = TIME_DECAY_FREEROLL_MIN_FACTOR + f * (1.0 - TIME_DECAY_FREEROLL_MIN_FACTOR)
    return base_mult * effective_factor


def _scaled_mid_profit_threshold(base_cents: int, hours_to_settlement: float) -> int:
    """Apply time-decay to mid-profit threshold. Minimum 50¢.

    Near settlement, the threshold drops (takes mid-profit earlier).
    """
    if not TIME_DECAY_ENABLED:
        return base_cents
    f = _time_decay_factor(hours_to_settlement)
    effective_factor = TIME_DECAY_MID_PROFIT_MIN_FACTOR + f * (1.0 - TIME_DECAY_MID_PROFIT_MIN_FACTOR)
    return max(50, int(round(base_cents * effective_factor)))


def _obs_adjusted_trailing_offset(
    base_offset: int,
    obs_temp: float,
    bracket_low: float,
    bracket_high: float,
    side: str,
) -> int:
    """Adjust trailing offset based on live observation vs bracket.

    YES side:
      - obs inside bracket → widen (thesis confirmed, let it run)
      - obs >DIVERGENCE_F outside → tighten (protect before price catches up)
      - mild divergence → no change

    NO side: inverse logic.
    No obs data (obs_temp <= 0) → return base unchanged.
    """
    if not OBS_TRAILING_ENABLED:
        return base_offset
    if obs_temp <= 0 or bracket_low <= 0 or bracket_high <= 0:
        return base_offset

    inside = bracket_low <= obs_temp < bracket_high

    if side == "yes":
        if inside:
            return max(2, int(round(base_offset * OBS_TRAILING_WIDEN_FACTOR)))
        # Outside — how far from nearest bracket edge?
        dist = max(bracket_low - obs_temp, obs_temp - bracket_high) if not inside else 0
        if dist >= OBS_TRAILING_DIVERGENCE_F:
            return max(2, int(round(base_offset * OBS_TRAILING_TIGHTEN_FACTOR)))
    else:  # NO side
        if inside:
            # Obs inside bracket = bad for NO → tighten
            return max(2, int(round(base_offset * OBS_TRAILING_TIGHTEN_FACTOR)))
        # Outside = good for NO → widen
        dist = max(bracket_low - obs_temp, obs_temp - bracket_high)
        if dist >= OBS_TRAILING_DIVERGENCE_F:
            return max(2, int(round(base_offset * OBS_TRAILING_WIDEN_FACTOR)))

    return base_offset  # Mild divergence → no change


def _adaptive_freeroll_multiplier(entry_price_cents: int) -> float:
    """Price-level-aware freeroll multiplier.

    Cheap entries get higher multiplier (let them run); expensive entries
    get lower multiplier (lock profit faster).
    Falls back to FREEROLL_MULTIPLIER if no tier matches.
    """
    if not ADAPTIVE_FREEROLL_ENABLED:
        return FREEROLL_MULTIPLIER
    for lo, hi, mult in ADAPTIVE_FREEROLL_TIERS:
        if lo <= entry_price_cents < hi:
            return mult
    return FREEROLL_MULTIPLIER  # fallback


def _check_momentum_drop(
    current_price: int,
    prev_price: int,
    threshold: int = MOMENTUM_DROP_ALERT_CENTS,
) -> tuple:
    """Check for large price drop between monitor cycles.

    Returns (is_drop: bool, drop_amount: int).
    prev_price of 0 means no previous data — returns (False, 0).
    """
    if not MOMENTUM_EXIT_ENABLED or prev_price <= 0:
        return False, 0
    drop = prev_price - current_price
    return (drop >= threshold, drop)


def _thesis_deterioration_action(
    confidence: float | None,
    freerolled: bool,
    thesis_trimmed: bool,
    contracts: int,
) -> str:
    """Graduated thesis deterioration: hold / trim / exit.

    - conf >= THESIS_TRIM_CONFIDENCE_HIGH or freerolled or no data → "hold"
    - THESIS_BREAK_CONFIDENCE <= conf < THESIS_TRIM_CONFIDENCE_HIGH,
      not yet trimmed, contracts > 1 → "trim"
    - conf < THESIS_BREAK_CONFIDENCE → "exit"
    """
    if not THESIS_TRIM_ENABLED:
        # Feature disabled → fall through to existing binary logic
        return "hold"
    if confidence is None or freerolled:
        return "hold"
    if confidence >= THESIS_TRIM_CONFIDENCE_HIGH:
        return "hold"
    if confidence < THESIS_BREAK_CONFIDENCE:
        return "exit"
    # Middle zone: THESIS_BREAK_CONFIDENCE <= conf < THESIS_TRIM_CONFIDENCE_HIGH
    if not thesis_trimmed and contracts > 1:
        return "trim"
    return "hold"  # Already trimmed or only 1 contract


def _best_derived_ask(opposite_levels: list) -> int:
    """Best ask for one side, implied by the opposite side's bids.

    Kalshi orderbooks contain only bids: orderbook["yes"] holds YES-bid
    levels and orderbook["no"] holds NO-bid levels as [price, qty] pairs.
    The best ask for a side is implied by the BEST (highest) bid on the
    opposite side:
        yes_ask = 100 - max(NO-bid prices)
        no_ask  = 100 - max(YES-bid prices)
    Matches core.broker._bid_ask_for_side and edge_scanner_v2. Using the
    lowest opposite bid instead would inflate the ask and place non-urgent
    sells above the true best offer. Returns 0 when the opposite side is
    empty.
    """
    prices = [lvl[0] for lvl in opposite_levels if lvl[1] > 0]
    return 100 - max(prices) if prices else 0


def _extract_sell_prices(orderbook: dict, side: str) -> tuple:
    """Extract bid, ask, and spread for the sell side of a position.

    Returns (bid, ask, spread) in cents.  bid = immediate fill price,
    ask = best offer on the opposite side.  For non-urgent sells we can
    peg to ask-1 to act as a maker (0% fee) and capture more edge.
    """
    bid = 0
    ask = 0

    if side == "yes":
        # Sell YES: our bid is the YES bid, ask is the YES ask
        yes_levels = orderbook.get("yes") or []
        bids = [lvl for lvl in yes_levels if lvl[1] > 0]
        if bids:
            bid = max(b[0] for b in bids)
        ask = _best_derived_ask(orderbook.get("no") or [])
        if ask == 0 and bid > 0:
            ask = bid  # fallback: treat bid as ask when no ask available
    else:
        # Sell NO: our bid is the NO bid, ask is the NO ask
        no_levels = orderbook.get("no") or []
        no_bids = [lvl for lvl in no_levels if lvl[1] > 0]
        if no_bids:
            bid = max(b[0] for b in no_bids)
        else:
            # Fallback: derive NO bid from YES ask (100 - YES ask)
            yes_levels = orderbook.get("yes") or []
            asks = [lvl for lvl in yes_levels if lvl[1] > 0]
            if asks:
                bid = 100 - min(a[0] for a in asks)
        # NO ask derived from best YES bid
        ask = _best_derived_ask(orderbook.get("yes") or [])
        if ask == 0 and bid > 0:
            ask = bid

    spread = max(0, ask - bid) if ask > 0 and bid > 0 else 0
    return bid, ask, spread


def _smart_sell_price(bid: int, ask: int, spread: int, urgent: bool) -> int:
    """Choose optimal sell price based on urgency.

    Non-urgent (freeroll, mid-profit, efficiency): peg to ask-1 when
    spread > 2¢ to act as maker and capture 1-5¢ more per contract.
    Urgent (trailing stop, stop-loss, thesis break): sell at bid for
    guaranteed fill.
    """
    if urgent or bid <= 0:
        return bid
    if spread >= 3 and ask > 0:
        # Peg inside the spread: ask-1 acts as maker (0% fee)
        return max(bid, ask - 1)
    # Tight spread (≤2¢): just hit the bid, not worth the fill risk
    return bid


async def _place_exit_sell(
    client: KalshiClient,
    pos: dict,
    now: datetime,
    *,
    qty: int,
    price: int,
    remaining_qty: int,
    event: TradeEvent,
    note: str,
    action: str,
    actions_taken: list,
    log_payload: dict,
    alert_title: str,
    alert_body: str,
    alert_color: int,
    extra_fields: dict | None = None,
) -> bool:
    """Place a limit exit sell and mark the position pending_sell.

    Single home for the pending_sell bookkeeping contract that
    _check_pending_sells (fill confirmation, repricing, expiry revert)
    depends on. Realized P&L is deliberately NOT booked here — it is
    booked in _check_pending_sells once the fill is confirmed, so an
    expired/cancelled sell never leaves phantom profit in pnl_realized
    (the circuit-breaker and drawdown guards read it).

    `note` may contain a literal `{order_id}` placeholder, substituted
    after the order is placed. `extra_fields` are per-rule flags
    (e.g. freerolled, quick_profit_taken) applied only on success.
    Returns True when the order was placed.
    """
    result = await client.place_order(
        ticker=pos["ticker"],
        side=pos["side"],
        action="sell",
        count=qty,
        price=price,
        order_type="limit",
    )
    if not result:
        return False

    order_id = result.get("order", {}).get("order_id", "")
    pos["status"] = "pending_sell"
    pos["sell_order_id"] = order_id
    pos["sell_placed_at"] = now.isoformat()
    pos["_pending_remaining_qty"] = remaining_qty
    pos["_pre_sell_qty"] = pos["contracts"]
    pos["_sell_price_placed"] = price
    for key, value in (extra_fields or {}).items():
        pos[key] = value
    pos.setdefault("notes", []).append(
        f"{now.isoformat()}: {note.replace('{order_id}', order_id)}"
    )
    actions_taken.append(action)
    log_event(event, "position_monitor", {**log_payload, "order_id": order_id})
    await send_discord_alert(alert_title, alert_body, color=alert_color)
    return True


async def _pull_orders_before_bot_windows(
    client: KalshiClient, positions: list, now: datetime,
) -> list:
    """Cancel resting buy orders if within 15 min of DSM/6-hour release."""
    import re
    from trading_guards import check_bot_window

    actions = []
    resting_orders = await client.get_orders(status="resting")
    if not resting_orders:
        return actions

    for order in resting_orders:
        if order.get("action") != "buy":
            continue
        ticker = order.get("ticker", "")
        order_id = order.get("order_id", "")

        match = re.match(r"^([A-Z]+)", ticker)
        if not match:
            continue
        city_code = _SERIES_TO_CITY.get(match.group(1))
        if not city_code:
            continue

        station = STATIONS[city_code]
        safe, reason = check_bot_window(city_code, station.dsm_times_z, station.six_hour_z)
        if not safe:
            await client.cancel_order(order_id)
            actions.append(f"BOT PROTECT: Cancelled {ticker} order {order_id} -- {reason}")

            for p in positions:
                if p.get("order_id") == order_id and p.get("status") == "resting":
                    p["status"] = "cancelled"
                    p.setdefault("notes", []).append(
                        f"{now.isoformat()}: Order pulled before bot window ({reason})"
                    )

            await send_discord_alert(
                "BOT WINDOW -- ORDER PULLED",
                f"**{ticker}** limit buy cancelled.\n{reason}\n"
                f"Will re-evaluate on next scan cycle.",
                color=0xFF6600,
            )
    return actions


async def _cancel_stale_resting_orders(
    positions: list, client: KalshiClient, now: datetime,
) -> list[str]:
    """Cancel resting buy orders that have been unfilled past the timeout.

    A limit buy order that hasn't filled in RESTING_ORDER_TIMEOUT_MINUTES
    means the market moved away from our price. Cancel it to:
      1. Free up the daily exposure budget for fresh opportunities
      2. Avoid a stale fill if price briefly dips back to our level
         after the thesis has shifted
    """
    actions = []
    resting = [p for p in positions if p.get("status") == "resting"]
    if not resting:
        return actions

    for pos in resting:
        entry_time_str = pos.get("entry_time", "")
        if not entry_time_str:
            continue
        try:
            entry_time = datetime.fromisoformat(entry_time_str)
        except (ValueError, TypeError):
            continue

        elapsed_min = (now - entry_time).total_seconds() / 60
        if elapsed_min < RESTING_ORDER_TIMEOUT_MINUTES:
            continue

        ticker = pos["ticker"]
        order_id = pos.get("order_id", "")
        logger.info("STALE ORDER: %s resting for %.0f min (limit: %d) — cancelling",
                     ticker, elapsed_min, RESTING_ORDER_TIMEOUT_MINUTES)

        if order_id:
            try:
                await client.cancel_order(order_id)
            except Exception as e:
                logger.warning("Failed to cancel stale order %s: %s", order_id, e)

        pos["status"] = "cancelled"
        pos.setdefault("notes", []).append(
            f"{now.isoformat()}: Stale order cancelled after {elapsed_min:.0f}min "
            f"(timeout: {RESTING_ORDER_TIMEOUT_MINUTES}min)"
        )
        actions.append(f"STALE CANCEL: {ticker} (resting {elapsed_min:.0f}min)")
        log_event(TradeEvent.STALE_ORDER_CANCELLED, "position_monitor", {
            "ticker": ticker, "order_id": order_id,
            "elapsed_min": round(elapsed_min, 1),
            "timeout_min": RESTING_ORDER_TIMEOUT_MINUTES,
        })
        await send_discord_alert(
            "STALE ORDER CANCELLED",
            f"**{ticker}** limit buy order cancelled after **{elapsed_min:.0f} min** unfilled.\n"
            f"Timeout: {RESTING_ORDER_TIMEOUT_MINUTES} min.\n"
            f"Exposure budget freed for next scan.",
            color=0xFF9900,
        )

    return actions


async def _check_pending_sells(positions: list, client: KalshiClient, now: datetime) -> list[str]:
    """Check positions with pending_sell status — confirm fill or cancel stale orders."""
    actions = []
    for pos in positions:
        if pos.get("status") != "pending_sell":
            continue

        ticker = pos["ticker"]
        sell_order_id = pos.get("sell_order_id", "")
        sell_placed_at = pos.get("sell_placed_at", "")

        # Check if the sell order has filled by querying Kalshi positions
        api_positions = await client.get_positions()
        api_qty = 0
        for ap in api_positions:
            if ap.get("ticker") == ticker:
                # Kalshi now reports contracts as the fixed-point string
                # position_fp; fall back to legacy `position` for resilience.
                api_qty = abs(parse_fp(ap.get("position_fp", ap.get("position"))))
                break

        expected_remaining = pos.get("_pending_remaining_qty", 0)
        intended_sell_qty = max(0, pos.get("_pre_sell_qty", pos["contracts"]) - expected_remaining)

        sell_filled = api_qty <= expected_remaining
        if not sell_filled and sell_order_id and intended_sell_qty > 0:
            # Secondary check via fills: PaperBroker positions mirror the
            # local store, so the quantity test above can never observe a
            # paper fill. Fills are authoritative on both brokers.
            try:
                fills = await client.get_fills(ticker=ticker)
            except Exception as e:
                logger.warning("  %s: get_fills failed: %s", ticker, e)
                fills = []
            filled_for_order = sum(
                parse_fp(f.get("count_fp", f.get("count")))
                for f in fills if f.get("order_id") == sell_order_id
            )
            sell_filled = filled_for_order >= intended_sell_qty

        if sell_filled:
            # Book realized P&L now that the fill is confirmed — NOT at
            # placement — so an expired/cancelled sell never leaves phantom
            # profit in pnl_realized (circuit-breaker/drawdown guards read it).
            sell_price_placed = pos.get("_sell_price_placed", 0)
            if intended_sell_qty > 0 and sell_price_placed > 0:
                realized = (sell_price_placed - pos.get("avg_price", 0)) / 100 * intended_sell_qty
                pos["pnl_realized"] = pos.get("pnl_realized", 0) + realized
            if expected_remaining == 0:
                pos["status"] = "closed"
                pos["notes"].append(f"{now.isoformat()}: Sell order filled — position closed")
                actions.append(f"CONFIRMED FILL: {ticker} sell order filled, position closed")
            else:
                pos["status"] = "open"
                pos["contracts"] = expected_remaining
                pos.pop("sell_order_id", None)
                pos.pop("sell_placed_at", None)
                pos["notes"].append(f"{now.isoformat()}: Partial sell filled — {expected_remaining} contracts remain")
                actions.append(f"PARTIAL FILL: {ticker} — {expected_remaining} contracts remain open")
            # Clear sell-cycle bookkeeping so the next sell starts fresh
            for key in ("_pending_remaining_qty", "_pre_sell_qty", "_sell_price_placed",
                        "_sell_cycle_count", "_sell_reprice_count"):
                pos.pop(key, None)
        else:
            # Sell hasn't filled — check if stale
            if sell_placed_at:
                placed = datetime.fromisoformat(sell_placed_at)
                elapsed_min = (now - placed).total_seconds() / 60

                # ── Upgrade 6: Smart Sell Repricing ──
                if SELL_REPRICE_ENABLED:
                    sell_cycles = pos.get("_sell_cycle_count", 0) + 1
                    pos["_sell_cycle_count"] = sell_cycles
                    reprice_count = pos.get("_sell_reprice_count", 0)
                    original_sell_price = pos.get("_sell_price_placed", 0)

                    if (sell_cycles >= SELL_REPRICE_MIN_CYCLES
                            and reprice_count < SELL_REPRICE_MAX_PER_ORDER
                            and original_sell_price > 0):
                        # Check current bid to see if market moved
                        try:
                            ob = await client.get_orderbook(ticker)
                            current_bid, _, _ = _extract_sell_prices(ob, pos["side"])
                            bid_drift = abs(current_bid - original_sell_price)
                            if bid_drift >= SELL_REPRICE_BID_DRIFT_CENTS and current_bid > 0:
                                # Cancel old order and place new one
                                if sell_order_id:
                                    await client.cancel_order(sell_order_id)
                                new_price = current_bid
                                # Sell qty = original position - what should remain after fill
                                pre_sell = pos.get("_pre_sell_qty", pos["contracts"])
                                remaining_qty = pos.get("_pending_remaining_qty", 0)
                                sell_qty = pre_sell - remaining_qty
                                if sell_qty <= 0:
                                    # Fallback: data inconsistent, sell what we originally intended
                                    logger.warning("  %s: sell_qty=%d (pre=%d, remain=%d), using contracts=%d",
                                                   ticker, sell_qty, pre_sell, remaining_qty, pos["contracts"])
                                    sell_qty = pos["contracts"]
                                result = await client.place_order(
                                    ticker=ticker, side=pos["side"], action="sell",
                                    count=sell_qty, price=new_price, order_type="limit",
                                )
                                if result:
                                    new_order_id = result.get("order", {}).get("order_id", "")
                                    pos["sell_order_id"] = new_order_id
                                    pos["sell_placed_at"] = now.isoformat()
                                    pos["_sell_price_placed"] = new_price
                                    pos["_sell_reprice_count"] = reprice_count + 1
                                    pos["_sell_cycle_count"] = 0
                                    pos["notes"].append(
                                        f"{now.isoformat()}: REPRICED sell {original_sell_price}c -> {new_price}c (drift={bid_drift}c)"
                                    )
                                    actions.append(f"REPRICED: {ticker} sell {original_sell_price}c -> {new_price}c")
                                    from trade_events import log_event, TradeEvent
                                    log_event(TradeEvent.SELL_REPRICED, "position_monitor", {
                                        "ticker": ticker, "old_price": original_sell_price,
                                        "new_price": new_price, "bid_drift": bid_drift,
                                        "reprice_count": reprice_count + 1,
                                        "order_id": new_order_id,
                                    })
                                    continue  # Skip stale check, we just repriced
                        except Exception as e:
                            logger.warning("  %s: Repricing failed: %s", ticker, e)

                if elapsed_min > PENDING_SELL_EXPIRY_MINUTES:
                    # Cancel stale order and revert to open. No P&L to back
                    # out: pnl_realized is only booked on confirmed fills.
                    if sell_order_id:
                        await client.cancel_order(sell_order_id)
                    pos["status"] = "open"
                    pos["contracts"] = pos.get("_pre_sell_qty", pos["contracts"])
                    pos.pop("sell_order_id", None)
                    pos.pop("sell_placed_at", None)
                    pos.pop("_pending_remaining_qty", None)
                    pos.pop("_pre_sell_qty", None)
                    pos.pop("_sell_price_placed", None)
                    pos.pop("_sell_cycle_count", None)
                    pos.pop("_sell_reprice_count", None)
                    pos["notes"].append(
                        f"{now.isoformat()}: Sell order unfilled after {elapsed_min:.0f}min — cancelled, reverted to open"
                    )
                    actions.append(f"STALE ORDER: {ticker} sell cancelled after {elapsed_min:.0f}min — reverted to open")
                    await send_discord_alert(
                        "⏳ SELL ORDER EXPIRED",
                        f"**{ticker}**: Limit sell unfilled after {elapsed_min:.0f} minutes.\n"
                        f"Order cancelled — position reverted to OPEN.\n"
                        f"Will re-evaluate on next monitor cycle.",
                        color=0xFFAA00,
                    )
                else:
                    logger.debug("  %s: pending_sell — order placed %.0fmin ago, waiting...", ticker, elapsed_min)
    return actions


async def _check_balance_invariant(positions: list, loaded_balance: float) -> None:
    """Paper-only: alert when persisted cash has drifted from the position
    ledger (the corruption/race class). The per-cycle balance sync corrects it;
    this makes the drift LOUD instead of silently healed, so a recurring writer
    bug surfaces instead of hiding."""
    drift, ledger = balance_drift(loaded_balance, positions)
    if abs(drift) <= BALANCE_DRIFT_ALERT_USD:
        if drift:
            logger.debug("[PAPER] balance invariant ok (drift $%.2f)", drift)
        return
    logger.warning(
        "[PAPER] BALANCE DRIFT $%+.2f — persisted $%.2f vs ledger $%.2f (auto-correcting this cycle)",
        drift, loaded_balance, ledger,
    )
    try:
        await send_discord_alert(
            title="⚠ Paper balance drift detected",
            description=(
                f"Persisted balance **${loaded_balance:.2f}** vs ledger **${ledger:.2f}** "
                f"(drift **${drift:+.2f}**).\nAuto-corrected this cycle. Recurring drift means "
                f"an out-of-band balance writer or a concurrent-write race — investigate."
            ),
            color=0xFF6600,
            context="balance_invariant",
        )
    except Exception:  # noqa: BLE001 — alerting must never break the monitor
        pass


async def _settle_paper_positions(positions: list, client, now: datetime) -> list[str]:
    """Paper-only: settle positions whose Kalshi market has resolved.

    For each open/pending position, query the market's settlement result and, if
    resolved, mark it settled and book settlement P&L on held contracts (winner
    pays $1/contract). The cash balance is recomputed separately from the full
    ledger after persistence, so this only updates the position records. Live
    mode never calls this (gated by client.mode == "paper").
    """
    actions: list[str] = []
    for pos in positions:
        if pos.get("status") not in ("open", "pending_sell"):
            continue
        ticker = pos["ticker"]
        status, result = await client.get_market_result(ticker)
        if status not in ("settled", "finalized") or result not in ("yes", "no"):
            continue
        won = result == str(pos.get("side", "yes")).lower()
        spnl = settle_position_record(pos, won, now)
        msg = f"SETTLED {'WON' if won else 'LOST'}: {ticker} ({pos.get('contracts')}x) pnl ${spnl:+.2f}"
        actions.append(msg)
        logger.info("[PAPER] %s", msg)
        try:
            log_event(TradeEvent.EXIT_SETTLED, "position_monitor", {
                "ticker": ticker, "result": result, "won": won, "settle_pnl": spnl,
            })
        except Exception:  # noqa: BLE001 — telemetry must never break settlement
            pass
    return actions


async def check_and_manage_positions():
    """Check all open positions against exit rules."""
    # Preflight: validate credentials before touching real positions
    ok, issues = preflight_check(fatal=False)
    if not ok:
        logger.error("Preflight failed: %s", issues)
        log_event(TradeEvent.PREFLIGHT_FAILED, "position_monitor", {"issues": issues})
        return

    positions = load_positions()
    open_positions = [p for p in positions if p["status"] == "open"]
    pending_sells = [p for p in positions if p.get("status") == "pending_sell"]
    resting_positions = [p for p in positions if p.get("status") == "resting"]

    if not open_positions and not pending_sells and not resting_positions:
        logger.info("No open positions to monitor.")
        return

    from core.broker_factory import get_broker
    try:
        client = await get_broker()
    except RuntimeError as e:
        logger.error("Broker init failed: %s", e)
        return

    try:
        # Get current positions from Kalshi (actual fills)
        api_positions = await client.get_positions()
        api_pos_map = {}
        for ap in api_positions:
            ticker = ap.get("ticker", "")
            # Net position (positive = yes, negative = no), from fixed-point field.
            qty = parse_fp(ap.get("position_fp", ap.get("position")))
            if qty != 0:
                api_pos_map[ticker] = ap

        balance = await client.get_balance()
        now = datetime.now(ET)

        # Paper-only: settle positions whose Kalshi market has resolved, so the
        # books self-reconcile each cycle (live mode credits/closes natively).
        settle_actions: list[str] = []
        if getattr(client, "mode", "") == "paper":
            await _check_balance_invariant(positions, balance)  # detect drift before we heal it
            settle_actions = await _settle_paper_positions(positions, client, now)

        # Promote resting -> open when order fills on Kalshi
        resting_positions = [p for p in positions if p.get("status") == "resting"]
        for pos in resting_positions:
            ticker = pos["ticker"]
            order_id = pos.get("order_id", "")
            if order_id:
                resting_orders = await client.get_orders(ticker=ticker, status="resting")
                still_resting = any(o.get("order_id") == order_id for o in resting_orders)
                if not still_resting:
                    if ticker in api_pos_map:
                        api_qty = abs(parse_fp(api_pos_map[ticker].get("position_fp", api_pos_map[ticker].get("position"))))
                        pos["status"] = "open"
                        pos["contracts"] = api_qty
                        pos["original_contracts"] = api_qty
                        pos.setdefault("notes", []).append(
                            f"{now.isoformat()}: Promoted resting -> open ({api_qty} contracts filled)"
                        )
                        logger.info("RESTING -> OPEN: %s (%d contracts filled)", ticker, api_qty)
                        log_event(TradeEvent.POSITION_PROMOTED, "position_monitor", {
                            "ticker": ticker, "qty": api_qty,
                        })
                    else:
                        pos["status"] = "cancelled"
                        pos.setdefault("notes", []).append(
                            f"{now.isoformat()}: Order cancelled or expired (no fill)"
                        )
                        logger.info("RESTING -> CANCELLED: %s", ticker)
                        log_event(TradeEvent.POSITION_CANCELLED, "position_monitor", {
                            "ticker": ticker,
                        })
        # Refresh open positions after resting promotion
        open_positions = [p for p in positions if p["status"] == "open"]

        logger.info("POSITION MONITOR — %s", now.strftime("%I:%M %p ET"))
        logger.info("Balance: $%.2f | Open: %d | Pending sells: %d", balance, len(open_positions), len(pending_sells))

        actions_taken = list(settle_actions)

        # ── THESIS GUARD: cancel resting orders if confidence collapsed ──
        # auto_trader writes last_confidence on resting positions each scan.
        # If confidence has dropped below the thesis-break threshold, cancel
        # the unfilled order BEFORE it fills at a stale price.
        still_resting = [p for p in positions if p.get("status") == "resting"]
        for pos in still_resting:
            last_conf = pos.get("last_confidence")
            if last_conf is not None and last_conf < THESIS_BREAK_CONFIDENCE:
                order_id = pos.get("order_id", "")
                ticker = pos["ticker"]
                logger.info("THESIS GUARD: %s confidence=%s < %d — cancelling resting order %s",
                            ticker, last_conf, THESIS_BREAK_CONFIDENCE, order_id)
                if order_id:
                    try:
                        await client.cancel_order(order_id)
                    except Exception as cancel_err:
                        logger.warning("Failed to cancel order %s: %s", order_id, cancel_err)
                pos["status"] = "cancelled"
                pos.setdefault("notes", []).append(
                    f"{now.isoformat()}: THESIS GUARD — cancelled resting order (confidence={last_conf})"
                )
                actions_taken.append(f"THESIS GUARD: Cancelled {ticker} (confidence={last_conf})")
                log_event(TradeEvent.POSITION_CANCELLED, "position_monitor", {
                    "ticker": ticker, "reason": "thesis_guard",
                    "confidence": last_conf, "threshold": THESIS_BREAK_CONFIDENCE,
                })
                await send_discord_alert(
                    "🛡️ THESIS GUARD — ORDER CANCELLED",
                    f"**{ticker}** resting buy order cancelled.\n"
                    f"Confidence dropped to **{last_conf}/100** (threshold: {THESIS_BREAK_CONFIDENCE})\n"
                    f"Models shifted against the trade — order pulled before fill.",
                    color=0xFF6600,
                )

        # First: pull resting buy orders before DSM/6-hour bot windows
        bot_actions = await _pull_orders_before_bot_windows(client, positions, now)
        actions_taken.extend(bot_actions)

        # Cancel resting buy orders that have been unfilled past the timeout
        stale_actions = await _cancel_stale_resting_orders(positions, client, now)
        actions_taken.extend(stale_actions)

        # Check pending sell orders for fill confirmation
        if pending_sells:
            pending_actions = await _check_pending_sells(positions, client, now)
            actions_taken.extend(pending_actions)
            # Refresh open positions after pending sell resolution
            open_positions = [p for p in positions if p["status"] == "open"]

        # ── Settlement proximity (only depends on clock, not per-position) ──
        # Hours until next 7 AM ET settlement. Always non-negative [0, 24).
        # At 5 AM: (7-5) % 24 = 2h.  At 10 AM: (7-10) % 24 = 21h (next day).
        # At 7 AM: (7-7) % 24 = 0h (settling now).
        hours_to_settlement = (SETTLEMENT_HOUR_ET - now.hour) % 24
        near_settlement = hours_to_settlement <= SETTLEMENT_WINDOW_HOURS

        for pos in open_positions:
            ticker = pos["ticker"]
            side = pos["side"]
            entry_price = pos["avg_price"]
            contracts = pos["contracts"]

            # Get current market price (bid + ask for smart pegging)
            orderbook = await client.get_orderbook(ticker)
            ob_bid, ob_ask, ob_spread = _extract_sell_prices(orderbook, side)
            # Default sell_price = bid (conservative). Individual exit rules
            # may override with _smart_sell_price() for non-urgent sells.
            sell_price = ob_bid

            # Calculate P&L — formula is the same for YES and NO
            # since both entry_price and sell_price are denominated in the
            # same contract type (YES cents or NO cents respectively).
            if sell_price > 0 and entry_price > 0:
                pnl_per_contract = (sell_price - entry_price) / 100
                total_pnl = pnl_per_contract * contracts
                roi = (sell_price - entry_price) / entry_price * 100
            else:
                pnl_per_contract = 0
                total_pnl = 0
                roi = 0

            pnl_color = "+" if total_pnl >= 0 else ""
            logger.info("%s %dx %s | Entry: %dc Now: %dc P&L: %s$%.2f (%+.0f%%)",
                        side.upper(), contracts, ticker, entry_price, sell_price, pnl_color, total_pnl, roi)

            # Compute bid volume + thin book check (used by trailing stop + stop-loss)
            bid_volume = 0
            book_key = "yes" if side == "yes" else "no"
            for lvl in (orderbook.get(book_key) or []):
                if lvl[0] == sell_price and lvl[1] > 0:
                    bid_volume = lvl[1]
                    break
            thin_book = (bid_volume < 3 and sell_price <= 5)

            # Check if position actually exists on Kalshi
            api_pos = api_pos_map.get(ticker)
            if not api_pos:
                logger.warning("%s: Not found in Kalshi positions — may have settled", ticker)
                # Auto-close if market likely settled
                pos["notes"].append(f"{now.isoformat()}: Not found in API — likely settled")
                pos["status"] = "settled"
                actions_taken.append(f"SETTLED: {ticker} no longer in API positions")
                log_event(TradeEvent.EXIT_SETTLED, "position_monitor", {
                    "ticker": ticker, "side": side, "entry": entry_price, "qty": contracts,
                })
                await send_discord_alert(
                    "📋 POSITION SETTLED",
                    f"**{side.upper()} {contracts}x {ticker}**\n"
                    f"Entry: {entry_price}c | No longer in API — market settled.\n"
                    f"Check Kalshi portfolio for final settlement.",
                    color=0x3498DB,
                )
                continue

            # ═══════════════════════════════════════════════════
            # EXIT RULE 1: EFFICIENCY EXIT (90¢)
            # OVERRIDE: If near settlement AND price > 80¢ AND
            #           observations support the bracket, HOLD for $1
            # Uses quantitative distance-to-bracket instead of
            # binary trend flags for more precise decisions.
            # ═══════════════════════════════════════════════════
            if sell_price >= EFFICIENCY_EXIT_CENTS:
                # Quantitative settlement hold: compute distance from current
                # observed temp to the bracket boundaries. If observations
                # are significantly outside the bracket, sell instead of holding.
                should_hold = False
                hold_reason = ""
                if near_settlement and sell_price >= SETTLEMENT_HOLD_THRESHOLD:
                    obs_temp = pos.get("current_obs_temp", 0.0)
                    bracket_lo = pos.get("bracket_low", 0.0)
                    bracket_hi = pos.get("bracket_high", 0.0)

                    if obs_temp > 0 and bracket_lo > 0 and bracket_hi > 0:
                        # Distance-to-bracket: how far is the current observation
                        # from the bracket we need it to land in?
                        if side == "yes":
                            # We need temp IN the bracket [lo, hi)
                            if obs_temp < bracket_lo:
                                dist = bracket_lo - obs_temp  # below bracket
                                should_hold = dist <= 2.0  # hold if within 2°F of bracket
                                hold_reason = f"obs {obs_temp:.1f}°F is {dist:.1f}°F below bracket [{bracket_lo:.0f}-{bracket_hi:.0f})"
                            elif obs_temp >= bracket_hi:
                                dist = obs_temp - bracket_hi + 1  # above bracket
                                should_hold = dist <= 2.0
                                hold_reason = f"obs {obs_temp:.1f}°F is {dist:.1f}°F above bracket [{bracket_lo:.0f}-{bracket_hi:.0f})"
                            else:
                                should_hold = True  # currently IN the bracket
                                hold_reason = f"obs {obs_temp:.1f}°F is INSIDE bracket [{bracket_lo:.0f}-{bracket_hi:.0f})"
                        else:
                            # NO side: we need temp OUTSIDE the bracket
                            if bracket_lo <= obs_temp < bracket_hi:
                                # Currently inside — bad for NO
                                dist = min(obs_temp - bracket_lo, bracket_hi - obs_temp)
                                should_hold = dist <= 1.0  # hold only if barely inside (could drift out)
                                hold_reason = f"obs {obs_temp:.1f}°F is inside bracket [{bracket_lo:.0f}-{bracket_hi:.0f}) by {dist:.1f}°F"
                            else:
                                should_hold = True  # currently outside — good for NO
                                hold_reason = f"obs {obs_temp:.1f}°F is OUTSIDE bracket [{bracket_lo:.0f}-{bracket_hi:.0f})"
                    else:
                        # No observation data — fall back to trend flag
                        obs_trend = pos.get("trend", "")
                        trend_against = (
                            (side == "yes" and obs_trend == "running_cold")
                            or (side == "no" and obs_trend == "running_hot")
                        )
                        should_hold = not trend_against
                        hold_reason = f"no obs data, trend={obs_trend or 'unknown'}"

                if should_hold:
                    logger.info("  %s: SETTLEMENT HOLD — %dc >= %dc, %dh to settlement (%s)",
                                ticker, sell_price, EFFICIENCY_EXIT_CENTS, hours_to_settlement, hold_reason)
                    logger.info("  %s: Expected +$%.2f more if correct", ticker, (100 - sell_price) / 100 * contracts)
                    pos["notes"].append(f"{now.isoformat()}: Held at {sell_price}c — {hold_reason}")
                    log_event(TradeEvent.SETTLEMENT_HOLD, "position_monitor", {
                        "ticker": ticker, "price": sell_price, "hours_to_settlement": hours_to_settlement,
                        "reason": hold_reason,
                    })
                elif near_settlement and sell_price >= SETTLEMENT_HOLD_THRESHOLD:
                    logger.info("  %s: SELLING despite settlement — obs contradict bracket (%s)", ticker, hold_reason)
                    # Fall through to sell below

                if not should_hold:
                    # Non-urgent sell — use smart peg for better fill
                    eff_price = _smart_sell_price(ob_bid, ob_ask, ob_spread, urgent=False)
                    logger.info("  %s: EFFICIENCY EXIT — %dc >= %dc, selling %dx @ %dc (bid=%d ask=%d spread=%d)",
                                ticker, sell_price, EFFICIENCY_EXIT_CENTS, contracts, eff_price, ob_bid, ob_ask, ob_spread)

                    await _place_exit_sell(
                        client, pos, now,
                        qty=contracts, price=eff_price, remaining_qty=0,
                        event=TradeEvent.EXIT_EFFICIENCY,
                        note=f"EFFICIENCY EXIT sell placed at {eff_price}c (order: {{order_id}})",
                        action=f"EFFICIENCY EXIT: Sell {contracts}x {ticker} @ {eff_price}c placed (pending fill)",
                        actions_taken=actions_taken,
                        log_payload={
                            "ticker": ticker, "side": side, "price": eff_price,
                            "qty": contracts, "entry": entry_price, "pnl": round(total_pnl, 2),
                            "roi_pct": round(roi, 1),
                        },
                        alert_title="💰 EFFICIENCY EXIT — SELL PLACED",
                        alert_body=(
                            f"**Sell {contracts}x {ticker} @ {eff_price}c** (limit, ask-peg)\n"
                            f"Entry: {entry_price}c | Expected P&L: ${total_pnl:.2f} ({roi:+.0f}% ROI)\n"
                            f"Status: PENDING FILL — will confirm on next cycle."
                        ),
                        alert_color=0x00FF00,
                    )
                continue

            # ═══════════════════════════════════════════════════
            # EXIT RULE 2: FREEROLL (sell half at 2x entry)
            # ═══════════════════════════════════════════════════
            # Upgrade 3: Adaptive freeroll multiplier (price-level-aware)
            base_freeroll_mult = _adaptive_freeroll_multiplier(entry_price)
            # Upgrade 1: Time-decay tightens freeroll near settlement
            effective_freeroll_mult = _scaled_freeroll_multiplier(base_freeroll_mult, hours_to_settlement)
            freeroll_price = entry_price * effective_freeroll_mult
            if not pos.get("freerolled") and sell_price >= freeroll_price and contracts > 1:
                sell_qty = contracts // 2
                fr_price = _smart_sell_price(ob_bid, ob_ask, ob_spread, urgent=False)
                logger.info("  %s: FREEROLL — %dc >= %.0fc (2x entry), selling %d of %d @ %dc",
                            ticker, sell_price, freeroll_price, sell_qty, contracts, fr_price)

                expected_pnl = (fr_price - entry_price) / 100 * sell_qty
                remaining = contracts - sell_qty
                trailing_offset = _trailing_offset_for_price(sell_price)
                await _place_exit_sell(
                    client, pos, now,
                    qty=sell_qty, price=fr_price, remaining_qty=remaining,
                    event=TradeEvent.EXIT_FREEROLL,
                    note=f"FREEROLL sell {sell_qty}x @ {fr_price}c placed (order: {{order_id}})",
                    action=f"FREEROLL: Sell {sell_qty}x {ticker} @ {fr_price}c placed (pending fill)",
                    actions_taken=actions_taken,
                    log_payload={
                        "ticker": ticker, "side": side, "price": fr_price,
                        "sell_qty": sell_qty, "remaining": remaining, "entry": entry_price,
                        "pnl": round(expected_pnl, 2),
                    },
                    alert_title="🎰 FREEROLL — SELL PLACED",
                    alert_body=(
                        f"**Sell {sell_qty} of {contracts} {ticker} @ {fr_price}c** (ask-peg)\n"
                        f"Entry: {entry_price}c | Expected: +${expected_pnl:.2f}\n"
                        f"Status: PENDING FILL — will confirm on next cycle."
                    ),
                    alert_color=0x00FF00,
                    extra_fields={
                        "freerolled": True,
                        "peak_price": sell_price,
                        "trailing_floor": max(entry_price, sell_price - trailing_offset),
                    },
                )
                continue

            # ═══════════════════════════════════════════════════
            # EXIT RULE 2.25: QUICK PROFIT (pre-freeroll partial take)
            # When price is up significantly but hasn't reached freeroll
            # target, sell 30% to bank gains before a reversal.
            # Added 2026-02-15: LAX peaked at 37c (entry 26c, +42% ROI)
            # but no profit was taken because freeroll target was 47c.
            # ═══════════════════════════════════════════════════
            if (QUICK_PROFIT_ENABLED
                    and not pos.get("freerolled")
                    and not pos.get("quick_profit_taken")
                    and sell_price > 0 and entry_price > 0
                    and roi >= QUICK_PROFIT_ROI_PCT
                    and contracts >= QUICK_PROFIT_MIN_CONTRACTS):
                sell_qty = max(1, int(contracts * QUICK_PROFIT_SELL_FRACTION))
                remaining_after = contracts - sell_qty
                qp_price = _smart_sell_price(ob_bid, ob_ask, ob_spread, urgent=False)
                logger.info("  %s: QUICK PROFIT — ROI %.0f%% >= %d%%, selling %d of %d @ %dc",
                            ticker, roi, QUICK_PROFIT_ROI_PCT, sell_qty, contracts, qp_price)

                expected_pnl = (qp_price - entry_price) / 100 * sell_qty
                await _place_exit_sell(
                    client, pos, now,
                    qty=sell_qty, price=qp_price, remaining_qty=remaining_after,
                    event=TradeEvent.EXIT_QUICK_PROFIT,
                    note=f"QUICK PROFIT sell {sell_qty}x @ {qp_price}c (ROI={roi:.0f}%, order: {{order_id}})",
                    action=f"QUICK PROFIT: Sell {sell_qty}x {ticker} @ {qp_price}c (ROI={roi:.0f}%)",
                    actions_taken=actions_taken,
                    log_payload={
                        "ticker": ticker, "side": side, "price": qp_price,
                        "sell_qty": sell_qty, "remaining": remaining_after,
                        "entry": entry_price, "roi_pct": round(roi, 1),
                        "pnl": round(expected_pnl, 2),
                    },
                    alert_title="💵 QUICK PROFIT — PARTIAL EXIT",
                    alert_body=(
                        f"**Sell {sell_qty} of {contracts} {ticker} @ {qp_price}c** (ask-peg)\n"
                        f"Entry: {entry_price}c | ROI: {roi:+.0f}%\n"
                        f"{remaining_after} contracts still riding toward freeroll.\n"
                        f"Expected: +${expected_pnl:.2f}"
                    ),
                    alert_color=0x2ECC71,
                    extra_fields={"quick_profit_taken": True},
                )
                continue

            # ═══════════════════════════════════════════════════
            # EXIT RULE 2.5: MID-RANGE PROFIT TAKE (after freeroll)
            # At 65¢ on $0 cost-basis contracts, sell half of remaining
            # to lock in profit while keeping a runner for 90¢+
            # ═══════════════════════════════════════════════════
            if (pos.get("freerolled")
                    and not pos.get("mid_profit_taken")
                    and sell_price >= _scaled_mid_profit_threshold(MID_PROFIT_THRESHOLD_CENTS, hours_to_settlement)
                    and contracts > 1):
                sell_qty = max(1, int(contracts * MID_PROFIT_SELL_FRACTION))
                remaining_after = contracts - sell_qty
                mp_price = _smart_sell_price(ob_bid, ob_ask, ob_spread, urgent=False)
                logger.info("  %s: MID-PROFIT — %dc >= %dc, selling %d of %d @ %dc (keeping %d runner)",
                            ticker, sell_price, MID_PROFIT_THRESHOLD_CENTS, sell_qty, contracts, mp_price, remaining_after)

                expected_pnl = (mp_price - entry_price) / 100 * sell_qty
                await _place_exit_sell(
                    client, pos, now,
                    qty=sell_qty, price=mp_price, remaining_qty=remaining_after,
                    event=TradeEvent.EXIT_MID_PROFIT,
                    note=f"MID-PROFIT sell {sell_qty}x @ {mp_price}c (order: {{order_id}})",
                    action=f"MID-PROFIT: Sell {sell_qty}x {ticker} @ {mp_price}c placed (pending fill)",
                    actions_taken=actions_taken,
                    log_payload={
                        "ticker": ticker, "side": side, "price": mp_price,
                        "sell_qty": sell_qty, "remaining": remaining_after, "entry": entry_price,
                        "pnl": round(expected_pnl, 2),
                    },
                    alert_title="📊 MID-RANGE PROFIT — SELL PLACED",
                    alert_body=(
                        f"**Sell {sell_qty} of {contracts} {ticker} @ {mp_price}c** (Tier 2, ask-peg)\n"
                        f"Entry: {entry_price}c | {remaining_after} contracts riding to 90¢+\n"
                        f"Expected: +${expected_pnl:.2f}\n"
                        f"Status: PENDING FILL — will confirm on next cycle."
                    ),
                    alert_color=0x2ECC71,
                    extra_fields={"mid_profit_taken": True},
                )
                continue

            # ═══════════════════════════════════════════════════
            # EXIT RULE 3: TRAILING PROFIT LOCK (after freeroll)
            # Ratchets up as price rises, sells if price drops from peak
            # Scaled offset: wider at low prices, tighter near 90¢
            # ═══════════════════════════════════════════════════
            if pos.get("freerolled") and contracts > 0:
                peak = pos.get("peak_price", sell_price)
                floor = pos.get("trailing_floor", entry_price)

                # Update peak and floor if price made new high
                # Use scaled offset: wider when cheap, tighter near 90¢
                base_offset = _trailing_offset_for_price(sell_price)
                current_offset = _scaled_trailing_offset(base_offset, hours_to_settlement)
                # Upgrade 2: Obs-aware adjustment
                current_offset = _obs_adjusted_trailing_offset(
                    current_offset,
                    pos.get("current_obs_temp", 0.0),
                    pos.get("bracket_low", 0.0),
                    pos.get("bracket_high", 0.0),
                    side,
                )
                if sell_price > peak:
                    pos["peak_price"] = sell_price
                    new_floor = max(entry_price, sell_price - current_offset)
                    if new_floor > floor:
                        pos["trailing_floor"] = new_floor
                        floor = new_floor
                        logger.info("  %s: New peak %dc — trailing floor raised to %dc (offset=%dc)",
                                    ticker, sell_price, floor, current_offset)

                # Check if trailing stop triggered (bid_volume/thin_book computed above)
                if sell_price <= floor and sell_price > 0 and not thin_book:
                    logger.info("  %s: TRAILING STOP — %dc <= floor %dc (peak %dc), selling %dx",
                                ticker, sell_price, floor, peak, contracts)

                    expected_pnl = (sell_price - entry_price) / 100 * contracts
                    await _place_exit_sell(
                        client, pos, now,
                        qty=contracts, price=sell_price, remaining_qty=0,
                        event=TradeEvent.EXIT_TRAILING_STOP,
                        note=f"TRAILING STOP sell placed at {sell_price}c (order: {{order_id}})",
                        action=f"TRAILING STOP: Sell {contracts}x {ticker} @ {sell_price}c placed (pending fill)",
                        actions_taken=actions_taken,
                        log_payload={
                            "ticker": ticker, "side": side, "price": sell_price,
                            "qty": contracts, "entry": entry_price, "peak": peak, "floor": floor,
                            "pnl": round(expected_pnl, 2),
                        },
                        alert_title="📉 TRAILING STOP — SELL PLACED",
                        alert_body=(
                            f"**Sell {contracts}x {ticker} @ {sell_price}c** (limit order)\n"
                            f"Entry: {entry_price}c | Peak: {peak}c | Floor: {floor}c\n"
                            f"Expected: +${expected_pnl:.2f}\n"
                            f"Status: PENDING FILL — will confirm on next cycle."
                        ),
                        alert_color=0xFF6600,
                    )
                    continue
                elif thin_book and sell_price <= floor:
                    logger.info("  %s: Trailing stop SKIPPED — thin book (bid_vol=%d, price=%dc)", ticker, bid_volume, sell_price)
                else:
                    logger.debug("  %s: Trailing: peak=%dc floor=%dc current=%dc — holding", ticker, peak, floor, sell_price)

            # ═══════════════════════════════════════════════════
            # EXIT RULE 3.5: MOMENTUM / VELOCITY EXIT
            # Large price drops between cycles trigger alert + floor
            # ═══════════════════════════════════════════════════
            if not pos.get("freerolled") and MOMENTUM_EXIT_ENABLED:
                prev_cycle_price = pos.get("_prev_cycle_price", 0)
                is_drop, drop_amount = _check_momentum_drop(sell_price, prev_cycle_price)

                # Always update prev cycle price
                pos["_prev_cycle_price"] = sell_price

                if is_drop:
                    momentum_floor = sell_price + MOMENTUM_DROP_TIGHTEN_CENTS
                    pos["_momentum_floor"] = momentum_floor
                    logger.info("  %s: MOMENTUM ALERT — dropped %dc in one cycle (%dc → %dc), floor set to %dc",
                                ticker, drop_amount, prev_cycle_price, sell_price, momentum_floor)
                    log_event(TradeEvent.EXIT_MOMENTUM_ALERT, "position_monitor", {
                        "ticker": ticker, "prev_price": prev_cycle_price,
                        "current_price": sell_price, "drop": drop_amount,
                        "momentum_floor": momentum_floor,
                    })
                    await send_discord_alert(
                        "⚡ MOMENTUM ALERT",
                        f"**{side.upper()} {contracts}x {ticker}**\n"
                        f"Price dropped **{drop_amount}¢** in one cycle ({prev_cycle_price}¢ → {sell_price}¢)\n"
                        f"Temporary floor set at **{momentum_floor}¢** — will sell if breached next cycle.",
                        color=0xFF6600,
                    )

                # Check if momentum floor breached (set in a PREVIOUS cycle, not this one)
                mfloor = pos.get("_momentum_floor", 0)
                if not is_drop and mfloor > 0 and sell_price < mfloor and sell_price > STOP_LOSS_FLOOR_CENTS and not thin_book:
                    logger.info("  %s: MOMENTUM FLOOR BREACHED — %dc < floor %dc, selling %dx",
                                ticker, sell_price, mfloor, contracts)
                    expected_pnl = (sell_price - entry_price) / 100 * contracts
                    await _place_exit_sell(
                        client, pos, now,
                        qty=contracts, price=sell_price, remaining_qty=0,
                        event=TradeEvent.EXIT_MOMENTUM_ALERT,
                        note=f"MOMENTUM EXIT — floor {mfloor}c breached at {sell_price}c",
                        action=f"MOMENTUM EXIT: Sell {contracts}x {ticker} @ {sell_price}c",
                        actions_taken=actions_taken,
                        log_payload={
                            "ticker": ticker, "side": side, "price": sell_price,
                            "qty": contracts, "entry": entry_price,
                            "momentum_floor": mfloor, "pnl": round(expected_pnl, 2),
                        },
                        alert_title="⚡ MOMENTUM EXIT — SELL PLACED",
                        alert_body=(
                            f"**Sell {contracts}x {ticker} @ {sell_price}c** (limit order)\n"
                            f"Entry: {entry_price}c | Momentum floor: {mfloor}c\n"
                            f"Expected P&L: ${expected_pnl:.2f}\n"
                            f"Status: PENDING FILL — will confirm on next cycle."
                        ),
                        alert_color=0xFF0000,
                    )
                    continue
            elif not pos.get("freerolled"):
                # Still update prev cycle price even when momentum disabled
                pos["_prev_cycle_price"] = sell_price

            # ═══════════════════════════════════════════════════
            # EXIT RULE 4: LOSS WARNING (alert only, no auto-sell)
            # ═══════════════════════════════════════════════════
            if sell_price < entry_price and roi < -30:
                logger.warning("  %s: Position down %.0f%% — monitor closely", ticker, roi)
                last_alert = pos.get("_last_loss_alert", "")
                roi_bucket = str(int(roi / 10) * 10)  # Alert per 10% bucket
                if roi_bucket != last_alert:
                    pos["_last_loss_alert"] = roi_bucket
                    await send_discord_alert(
                        "⚠️ POSITION WARNING",
                        f"**{side.upper()} {contracts}x {ticker}**\n"
                        f"Entry: {entry_price}c | Now: {sell_price}c | P&L: ${total_pnl:.2f} ({roi:+.0f}%)\n"
                        f"Consider re-scanning to check if thesis still holds.\n"
                        f"`python3 edge_scanner_v2.py` to re-evaluate",
                        color=0xFF0000,
                    )
            # ═══════════════════════════════════════════════════
            # EXIT RULE 5a: GRADUATED THESIS DETERIORATION
            # Three zones: hold (conf>=70) / trim (40-70) / exit (<40)
            # Falls back to binary thesis break when THESIS_TRIM_ENABLED=False
            # ═══════════════════════════════════════════════════
            last_conf = pos.get("last_confidence")
            thesis_action = _thesis_deterioration_action(
                last_conf, pos.get("freerolled", False),
                pos.get("thesis_trimmed", False), contracts,
            )

            if thesis_action == "trim" and sell_price > STOP_LOSS_FLOOR_CENTS and not thin_book:
                trim_qty = max(1, int(contracts * THESIS_TRIM_SELL_FRACTION))
                remaining_after = contracts - trim_qty
                logger.info("  %s: THESIS TRIM — confidence %s (40-%d zone), selling %d of %d @ %dc",
                            ticker, last_conf, THESIS_TRIM_CONFIDENCE_HIGH, trim_qty, contracts, sell_price)

                expected_pnl = (sell_price - entry_price) / 100 * trim_qty
                await _place_exit_sell(
                    client, pos, now,
                    qty=trim_qty, price=sell_price, remaining_qty=remaining_after,
                    event=TradeEvent.EXIT_THESIS_TRIM,
                    note=f"THESIS TRIM — sold {trim_qty}x @ {sell_price}c (conf={last_conf})",
                    action=f"THESIS TRIM: Sell {trim_qty}x {ticker} @ {sell_price}c (conf={last_conf})",
                    actions_taken=actions_taken,
                    log_payload={
                        "ticker": ticker, "side": side, "price": sell_price,
                        "trim_qty": trim_qty, "remaining": remaining_after,
                        "entry": entry_price, "confidence": last_conf,
                        "pnl": round(expected_pnl, 2),
                    },
                    alert_title="✂️ THESIS TRIM — PARTIAL EXIT",
                    alert_body=(
                        f"**Sell {trim_qty} of {contracts} {ticker} @ {sell_price}c**\n"
                        f"Entry: {entry_price}c | Confidence: {last_conf}/100\n"
                        f"Zone: {THESIS_BREAK_CONFIDENCE}-{THESIS_TRIM_CONFIDENCE_HIGH} (trim, not full exit)\n"
                        f"{remaining_after} contracts remain. Expected: +${expected_pnl:.2f}"
                    ),
                    alert_color=0xFFA500,
                    extra_fields={"thesis_trimmed": True},
                )
                continue

            elif thesis_action == "exit" and sell_price > STOP_LOSS_FLOOR_CENTS and not thin_book:
                # Full thesis break — same as original Rule 5a
                logger.info("  %s: THESIS BREAK — confidence %s/100 < %d threshold, selling %dx @ %dc",
                            ticker, last_conf, THESIS_BREAK_CONFIDENCE, contracts, sell_price)

                expected_pnl = (sell_price - entry_price) / 100 * contracts
                await _place_exit_sell(
                    client, pos, now,
                    qty=contracts, price=sell_price, remaining_qty=0,
                    event=TradeEvent.EXIT_THESIS_BREAK,
                    note=f"THESIS BREAK sell at {sell_price}c (conf={last_conf})",
                    action=f"THESIS BREAK: Sell {contracts}x {ticker} @ {sell_price}c (conf={last_conf})",
                    actions_taken=actions_taken,
                    log_payload={
                        "ticker": ticker, "side": side, "price": sell_price,
                        "qty": contracts, "entry": entry_price, "confidence": last_conf,
                        "threshold": THESIS_BREAK_CONFIDENCE, "pnl": round(expected_pnl, 2),
                        "roi_pct": round(roi, 1),
                    },
                    alert_title="🧠 THESIS BREAK — EXIT",
                    alert_body=(
                        f"**Sell {contracts}x {ticker} @ {sell_price}c** (limit order)\n"
                        f"Entry: {entry_price}c | ROI: {roi:+.0f}%\n"
                        f"Expected P&L: ${expected_pnl:.2f}\n"
                        f"Reason: Confidence dropped to {last_conf}/100 (threshold: {THESIS_BREAK_CONFIDENCE})"
                    ),
                    alert_color=0xFF0000,
                )
                continue

            # ═══════════════════════════════════════════════════
            # EXIT RULE 5b: HARD ROI BACKSTOP (secondary)
            # Catches edge cases where re-scan can't update confidence
            # Only for non-freerolled positions (trailing stop covers those)
            # ═══════════════════════════════════════════════════
            if (not pos.get("freerolled")
                    and sell_price > 0
                    and roi <= STOP_LOSS_ROI_PCT
                    and sell_price > STOP_LOSS_FLOOR_CENTS
                    and not thin_book):
                logger.info("  %s: ROI BACKSTOP — ROI %.0f%% breached %d%% threshold, selling %dx @ %dc",
                            ticker, roi, STOP_LOSS_ROI_PCT, contracts, sell_price)

                expected_pnl = (sell_price - entry_price) / 100 * contracts
                await _place_exit_sell(
                    client, pos, now,
                    qty=contracts, price=sell_price, remaining_qty=0,
                    event=TradeEvent.EXIT_ROI_BACKSTOP,
                    note=f"ROI BACKSTOP sell at {sell_price}c ({roi:.0f}% ROI)",
                    action=f"ROI BACKSTOP: Sell {contracts}x {ticker} @ {sell_price}c",
                    actions_taken=actions_taken,
                    log_payload={
                        "ticker": ticker, "side": side, "price": sell_price,
                        "qty": contracts, "entry": entry_price,
                        "roi_pct": round(roi, 1), "pnl": round(expected_pnl, 2),
                    },
                    alert_title="🛑 ROI BACKSTOP — EXIT",
                    alert_body=(
                        f"**Sell {contracts}x {ticker} @ {sell_price}c** (limit order)\n"
                        f"Entry: {entry_price}c | ROI: {roi:+.0f}%\n"
                        f"Expected loss: ${expected_pnl:.2f}\n"
                        f"Reason: ROI breached {STOP_LOSS_ROI_PCT}% threshold (thesis not re-scanned)"
                    ),
                    alert_color=0xFF0000,
                )
                continue

            else:
                if not pos.get("freerolled"):
                    logger.debug("  %s: Holding — no exit trigger (freeroll at %.0fc)", ticker, freeroll_price)

        # Save updated positions using transaction to prevent race with execute_trade.
        # We re-read under lock and merge our modifications by ticker to avoid
        # overwriting positions that execute_trade may have added concurrently.
        modified_by_ticker = {p["ticker"]: p for p in positions}
        with position_transaction() as current_positions:
            for i, p in enumerate(current_positions):
                if p["ticker"] in modified_by_ticker:
                    current_positions[i] = modified_by_ticker[p["ticker"]]
                    del modified_by_ticker[p["ticker"]]
            # Any remaining are positions we had that weren't in the fresh read
            # (shouldn't happen, but defensive)
            for leftover in modified_by_ticker.values():
                current_positions.append(leftover)
            # Recompute paper cash from the authoritative merged ledger while we
            # still hold the lock; self-healing against concurrent-write drift.
            _, _, paper_balance, _ = rebuild_balance(current_positions)

        # Paper-only: pin the balance to the recomputed value (stop() persists it).
        if getattr(client, "mode", "") == "paper":
            await client.sync_balance(paper_balance)

        # Record successful completion for watchdog
        from heartbeat import write_heartbeat
        write_heartbeat("position_monitor")

        if actions_taken:
            logger.info("ACTIONS TAKEN: %d", len(actions_taken))
            for a in actions_taken:
                logger.info("  %s", a)
        else:
            logger.info("No exit triggers met. Holding all positions.")

    finally:
        await client.stop()


async def show_status():
    """Display all positions with current status."""
    positions = load_positions()

    logger.info("POSITIONS — %s", datetime.now(ET).strftime("%I:%M %p ET, %a %b %d"))

    open_pos = [p for p in positions if p["status"] == "open"]
    pending_pos = [p for p in positions if p.get("status") == "pending_sell"]
    closed_pos = [p for p in positions if p["status"] == "closed"]

    if not positions:
        logger.info("No tracked positions.")
        return

    if pending_pos:
        logger.info("PENDING SELL (%d):", len(pending_pos))
        for p in pending_pos:
            placed = p.get("sell_placed_at", "unknown")
            logger.info("  %s %dx %s @ %dc | Sell placed: %s | Order: %s",
                        p["side"].upper(), p["contracts"], p["ticker"], p["avg_price"],
                        placed, p.get("sell_order_id", "N/A"))

    if open_pos:
        logger.info("OPEN (%d):", len(open_pos))
        for p in open_pos:
            fr = " [FREEROLLED]" if p.get("freerolled") else ""
            logger.info("  %s %dx %s @ %dc%s | Opened: %s",
                        p["side"].upper(), p["contracts"], p["ticker"], p["avg_price"],
                        fr, p["entry_time"])
            if p.get("pnl_realized", 0) > 0:
                logger.info("    Realized P&L: $%.2f", p["pnl_realized"])

    if closed_pos:
        logger.info("CLOSED (%d):", len(closed_pos))
        total_pnl = 0
        for p in closed_pos:
            pnl = p.get("pnl_realized", 0)
            total_pnl += pnl
            logger.info("  %s %dx %s -> $%+.2f",
                        p["side"].upper(), p.get("original_contracts", p["contracts"]),
                        p["ticker"], pnl)
        logger.info("Total Realized P&L: $%+.2f", total_pnl)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Position Monitor — Auto take-profit and exit management")
    parser.add_argument("--status", action="store_true", help="Show all positions")
    parser.add_argument("--once", action="store_true", help="Single check (for cron)")
    args = parser.parse_args()

    if args.status:
        asyncio.run(show_status())
    else:
        asyncio.run(check_and_manage_positions())
