#!/usr/bin/env python3
"""
EXECUTE TRADE — One-command trade execution from Discord alerts.

Designed to be copy-pasted from Discord alert into terminal.
Validates everything before placing the order, then confirms.

Usage (from Discord alert):
  python3 execute_trade.py KXHIGHLAX-26FEB11-B62.5 yes 20 10
  #                        ^^^^^^^^^^^^^^^^^^^^^^^^ ^^^ ^^ ^^
  #                        ticker                   side ¢  qty

  python3 execute_trade.py KXHIGHLAX-26FEB11-B62.5 yes 20 10 --confirm
  #  Add --confirm to skip the interactive y/n prompt (still prints summary)

Arguments:
  ticker   — Kalshi market ticker (e.g. KXHIGHNY-26FEB11-B36.5)
  side     — "yes" or "no"
  price    — Limit price in cents (e.g. 20 = 20¢)
  quantity — Number of contracts

Safety:
  - Always uses LIMIT orders (maker, 0% fee)
  - Validates price against MAX_ENTRY_PRICE (50¢)
  - Validates position size against MAX_POSITION_PCT (10% of NLV)
  - Shows full order summary before executing
  - Requires explicit confirmation (unless --confirm flag)
  - Sends Discord confirmation after fill
"""

import argparse
import asyncio
import json
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from log_setup import get_logger
from kalshi_client import KalshiClient
from position_store import register_position
from notifications import send_discord_alert
from utils.state_db import get_db

logger = get_logger(__name__)

ET = ZoneInfo("America/New_York")

# Risk limits (single source of truth: config.py)
from config import MAX_ENTRY_PRICE_CENTS as MAX_ENTRY_PRICE, MAX_POSITION_PCT


async def send_discord_confirmation(ticker: str, side: str, price: int, qty: int, cost: float, status: str):
    """Send trade confirmation to Discord via shared notifications module."""
    color = 0x00FF00 if status == "FILLED" else 0xFFAA00 if status == "RESTING" else 0xFF0000
    now = datetime.now(ET).strftime("%I:%M %p ET")

    emoji = '✅' if status == 'FILLED' else '⏳' if status == 'RESTING' else '❌'
    await send_discord_alert(
        title=f"{emoji} TRADE {status}",
        description=(
            f"**{side.upper()} {ticker}**\n"
            f"Price: {price}¢ | Qty: {qty} | Cost: ${cost:.2f}\n"
            f"Max Payout: ${qty:.2f} | Profit: ${qty - cost:.2f}\n"
            f"Time: {now}"
        ),
        color=color,
        context="execute_trade",
    )


# Kalshi statuses that mean the order is not working — never register these.
REJECTED_STATUSES = frozenset({"REJECTED", "CANCELED", "CANCELLED", "FAILED", "ERROR"})


async def _place_and_register(
    client: KalshiClient, ticker: str, side: str, price: int, quantity: int,
    total_cost: float, interactive: bool,
) -> dict:
    """Shared order pipeline for execute() and execute_auto().

    register_order → place_order → verify order_id/status → confirm_order →
    register_position (with orphaned-order alert) → Discord confirmation.

    interactive=True prints progress to the terminal (execute);
    interactive=False logs instead (execute_auto).

    Returns:
        {"success": bool, "order_id": str, "status": str, "cost": float,
         "error": str} plus "client_order_id" on success.
    """
    # Generate client_order_id BEFORE the API call. Kalshi deduplicates
    # on this key, so a network timeout + retry won't create a second order.
    db = get_db()
    order_uuid = str(uuid.uuid4())
    db.register_order(
        client_order_id=order_uuid, ticker=ticker, side=side,
        count=quantity, price=price,
        # PaperBroker exposes mode="paper"; live brokers/raw clients are live.
        is_paper=getattr(client, "mode", "live") == "paper",
    )
    if interactive:
        print("\n  Placing order...")
    result = await client.place_order(
        ticker=ticker,
        side=side,
        action="buy",
        count=quantity,
        price=price,
        order_type="limit",
        client_order_id=order_uuid,
    )

    if not result:
        db.reject_order(order_uuid)
        if interactive:
            print("  ✗ Order placement failed. Check logs.")
        await send_discord_confirmation(ticker, side, price, quantity, total_cost, "FAILED")
        return {"success": False, "order_id": "", "status": "FAILED", "cost": total_cost,
                "error": "Order placement returned empty result"}

    order = result.get("order", result)
    status = order.get("status", "unknown").upper()
    order_id = order.get("order_id", "")

    # ── Verify order was actually accepted before registering ──
    if not order_id or order_id == "N/A":
        db.reject_order(order_uuid)
        db.write_audit("ORDER_MISSING_ID", ticker=ticker, payload={
            "client_order_id": order_uuid, "side": side,
            "quantity": quantity, "price": price, "status": status,
            "raw": str(result)[:300],
        })
        if interactive:
            print("  ✗ Order response missing order_id — NOT registering position")
            print(f"    Raw response: {json.dumps(result, default=str)[:300]}")
        else:
            logger.error("Order response missing order_id — NOT registering position")
        await send_discord_alert(
            title="⚠ ORDER MISSING ID — Position NOT registered",
            description=(
                f"Kalshi returned a response without order_id.\n"
                f"**{side.upper()} {ticker}** — {quantity}x @ {price}c\n"
                f"Status: {status}\n"
                f"**Check Kalshi dashboard for orphaned orders.**"
            ),
            color=0xFF0000,
            context="missing_order_id",
        )
        return {"success": False, "order_id": "", "status": status, "cost": total_cost,
                "error": "Order response missing order_id"}

    if status in REJECTED_STATUSES:
        db.reject_order(order_uuid)
        db.write_audit("ORDER_REJECTED", ticker=ticker, payload={
            "client_order_id": order_uuid, "kalshi_order_id": order_id,
            "status": status, "side": side, "quantity": quantity, "price": price,
        })
        if interactive:
            print(f"  ✗ Order REJECTED by Kalshi (status: {status})")
            print(f"     Order ID: {order_id}")
            # Interactive flow notifies Discord directly; auto_trader handles
            # rejection notifications from the returned dict.
            await send_discord_confirmation(ticker, side, price, quantity, total_cost, status)
        else:
            logger.warning("Order REJECTED by Kalshi (status: %s, order: %s)", status, order_id)
        return {"success": False, "order_id": order_id, "status": status, "cost": total_cost,
                "error": f"Order rejected: {status}"}

    db.confirm_order(order_uuid, kalshi_order_id=order_id,
                     status="resting" if status in ("RESTING", "PENDING") else "open")
    db.write_audit("ORDER_PLACED", ticker=ticker, payload={
        "client_order_id": order_uuid, "kalshi_order_id": order_id,
        "side": side, "quantity": quantity, "price": price, "status": status,
    })

    if interactive:
        if status in ("RESTING", "PENDING"):
            print("  ⏳ Order RESTING (limit order in book)")
            print(f"     Order ID: {order_id}")
            print(f"     Waiting for fill at {price}¢...")
        elif status == "EXECUTED":
            print("  ✅ Order FILLED!")
            print(f"     Order ID: {order_id}")
        else:
            print(f"  Order status: {status}")
            print(f"     Order ID: {order_id}")

    # Register position for the position monitor to track.
    # Critical: if this fails, we have an orphaned order on Kalshi
    # that position_monitor can't see. Log loudly + Discord alert.
    try:
        register_position(ticker, side, price, quantity, order_id, status,
                          client_order_id=order_uuid)
    except Exception as reg_err:
        db.write_audit("ORPHANED_ORDER", ticker=ticker, payload={
            "client_order_id": order_uuid, "kalshi_order_id": order_id,
            "side": side, "quantity": quantity, "price": price,
            "error": str(reg_err),
        })
        if interactive:
            print(f"  ⚠ CRITICAL: Order placed but register_position failed: {reg_err}")
            print(f"    Orphaned order: {order_id} — {quantity}x {side} {ticker} @ {price}c")
        else:
            logger.error(
                "ORPHANED ORDER: %s placed but register_position failed: %s",
                order_id, reg_err,
            )
        await send_discord_alert(
            title="🚨 ORPHANED ORDER — register_position FAILED",
            description=(
                f"Order {order_id} placed successfully but position tracking failed.\n"
                f"**{side.upper()} {ticker}** — {quantity}x @ {price}c\n"
                f"Error: {reg_err}\n"
                f"**Manual action required:** Add to positions.json or cancel order."
            ),
            color=0xFF0000,
            context="orphaned_order",
        )

    await send_discord_confirmation(ticker, side, price, quantity, total_cost, status)
    return {"success": True, "order_id": order_id, "status": status, "cost": total_cost,
            "client_order_id": order_uuid, "error": ""}


async def execute(ticker: str, side: str, price: int, quantity: int, confirm: bool = False):
    """Validate and execute a trade."""
    now = datetime.now(ET)
    print(f"\n{'='*56}")
    print(f"  TRADE EXECUTION — {now.strftime('%I:%M %p ET, %a %b %d')}")
    print(f"{'='*56}")

    # ── Validate inputs ──
    side = side.lower()
    if side not in ("yes", "no"):
        print(f"  ✗ Invalid side: {side}. Must be 'yes' or 'no'.")
        return False

    if price < 1 or price > 99:
        print(f"  ✗ Invalid price: {price}¢. Must be 1-99.")
        return False

    if side == "yes" and price > MAX_ENTRY_PRICE:
        print(f"  ✗ Price {price}¢ exceeds MAX_ENTRY_PRICE ({MAX_ENTRY_PRICE}¢).")
        print(f"    Risk/reward too poor above {MAX_ENTRY_PRICE}¢ on YES side.")
        return False

    if quantity < 1:
        print(f"  ✗ Invalid quantity: {quantity}. Must be ≥ 1.")
        return False

    # ── Connect (paper or live based on PAPER_TRADING_MODE) ──
    try:
        from core.broker_factory import get_broker
        client = await get_broker()
    except RuntimeError as e:
        print(f"  ✗ {e}")
        return False

    try:
        # ── Fetch balance ──
        balance = await client.get_balance()
        print(f"\n  Account Balance: ${balance:.2f}")

        # ── Validate position size ──
        cost_per_contract = price / 100
        total_cost = cost_per_contract * quantity
        max_allowed = balance * MAX_POSITION_PCT

        if total_cost > max_allowed:
            print(f"  ✗ Total cost ${total_cost:.2f} exceeds {MAX_POSITION_PCT*100:.0f}% of NLV (${max_allowed:.2f})")
            print(f"    Max contracts at {price}¢: {int(max_allowed / cost_per_contract)}")
            return False

        if total_cost > balance:
            print(f"  ✗ Insufficient balance. Need ${total_cost:.2f}, have ${balance:.2f}")
            return False

        # ── Fetch current market data ──
        print(f"\n  Fetching orderbook for {ticker}...")
        orderbook = await client.get_orderbook(ticker)

        ob_yes_bid = 0
        ob_yes_ask = 0
        yes_levels = orderbook.get("yes") or []
        yes_bids = [l for l in yes_levels if l[1] > 0]
        if yes_bids:
            ob_yes_bid = max(b[0] for b in yes_bids)
        no_levels = orderbook.get("no") or []
        no_bids = [l for l in no_levels if l[1] > 0]
        if no_bids:
            ob_yes_ask = 100 - max(b[0] for b in no_bids)

        # ── Order summary ──
        max_payout = quantity
        profit_if_win = max_payout - total_cost
        roi = (profit_if_win / total_cost * 100) if total_cost > 0 else 0

        print("\n  ORDER SUMMARY")
        print(f"  {'─'*42}")
        print(f"  Ticker:       {ticker}")
        print(f"  Side:         {side.upper()}")
        print(f"  Price:        {price}¢ (LIMIT, maker 0% fee)")
        print(f"  Quantity:     {quantity} contracts")
        print(f"  Cost:         ${total_cost:.2f}")
        print(f"  Max Payout:   ${max_payout:.2f}")
        print(f"  Profit:       ${profit_if_win:.2f} ({roi:.0f}% ROI)")
        print(f"  {'─'*42}")
        print(f"  Book:         Bid={ob_yes_bid}¢  Ask={ob_yes_ask}¢")
        print(f"  Balance after: ${balance - total_cost:.2f}")
        print(f"  {'─'*42}")

        # Check if our price crosses the spread
        if side == "yes" and ob_yes_ask > 0 and price >= ob_yes_ask:
            print(f"\n  ⚠ Your bid ({price}¢) is AT/ABOVE the ask ({ob_yes_ask}¢).")
            print(f"    You'll pay taker fees. Consider bidding {max(1, ob_yes_ask - 1)}¢.")

        # Check if this would average into an existing position
        try:
            from position_store import load_positions
            existing = [p for p in load_positions()
                        if p.get("ticker") == ticker and p.get("side") == side
                        and p.get("status") == "open"]
            if existing:
                ep = existing[0]
                old_qty = ep.get("contracts", 0)
                old_price = ep.get("avg_price", 0)
                new_total = old_qty + quantity
                new_avg = round((old_price * old_qty + price * quantity) / new_total, 1)
                direction = "DOWN" if price < old_price else "UP"
                print(f"\n  ⚠ AVERAGING {direction} — existing position detected!")
                print(f"    Current: {old_qty}x @ {old_price:.0f}¢")
                print(f"    After:   {new_total}x @ {new_avg:.0f}¢ (adding {quantity}x @ {price}¢)")
        except Exception as e:
            logger.warning("Advisory position check failed: %s", e)

        # ── Confirm ──
        if not confirm:
            response = input("\n  Execute this trade? (y/n): ").strip().lower()
            if response != "y":
                print("  Cancelled.")
                return False

        # ── Execute ──
        outcome = await _place_and_register(
            client, ticker, side, price, quantity, total_cost, interactive=True,
        )
        return outcome["success"]

    finally:
        await client.stop()


async def execute_auto(
    ticker: str, side: str, price: int, quantity: int,
    client: KalshiClient = None, close_client: bool = False,
) -> dict:
    """Non-interactive trade execution for auto_trader.py.

    Caller is responsible for safety checks (trading_guards).
    Accepts optional KalshiClient to avoid reconnection overhead.

    Returns:
        {"success": bool, "order_id": str, "status": str, "cost": float, "error": str}
    """
    side = side.lower()
    if side not in ("yes", "no"):
        return {"success": False, "order_id": "", "status": "", "cost": 0, "error": f"Invalid side: {side}"}
    if price < 1 or price > 99:
        return {"success": False, "order_id": "", "status": "", "cost": 0, "error": f"Invalid price: {price}"}
    if side == "yes" and price > MAX_ENTRY_PRICE:
        return {"success": False, "order_id": "", "status": "", "cost": 0, "error": f"Price {price}c > MAX {MAX_ENTRY_PRICE}c"}
    if quantity < 1:
        return {"success": False, "order_id": "", "status": "", "cost": 0, "error": f"Invalid quantity: {quantity}"}

    own_client = False
    if client is None:
        from core.broker_factory import get_broker
        try:
            client = await get_broker()
        except RuntimeError as e:
            return {"success": False, "order_id": "", "status": "", "cost": 0, "error": str(e)}
        own_client = True

    try:
        balance = await client.get_balance()
        total_cost = (price / 100) * quantity
        max_allowed = balance * MAX_POSITION_PCT

        if total_cost > max_allowed:
            return {"success": False, "order_id": "", "status": "", "cost": total_cost,
                    "error": f"Cost ${total_cost:.2f} > {MAX_POSITION_PCT*100:.0f}% NLV (${max_allowed:.2f})"}
        if total_cost > balance:
            return {"success": False, "order_id": "", "status": "", "cost": total_cost,
                    "error": f"Insufficient balance: ${balance:.2f}"}

        return await _place_and_register(
            client, ticker, side, price, quantity, total_cost, interactive=False,
        )

    except Exception as e:
        return {"success": False, "order_id": "", "status": "ERROR", "cost": 0, "error": str(e)}

    finally:
        if own_client or close_client:
            await client.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Execute Trade — One-command Kalshi order placement",
        epilog="Example: python3 execute_trade.py KXHIGHLAX-26FEB11-B62.5 yes 20 10",
    )
    parser.add_argument("ticker", help="Kalshi market ticker")
    parser.add_argument("side", choices=["yes", "no"], help="Trade side")
    parser.add_argument("price", type=int, help="Limit price in cents (1-99)")
    parser.add_argument("quantity", type=int, help="Number of contracts")
    parser.add_argument("--confirm", action="store_true", help="Skip interactive confirmation")
    args = parser.parse_args()

    asyncio.run(execute(args.ticker, args.side, args.price, args.quantity, args.confirm))
