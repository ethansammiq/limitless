"""Policy trader CLI — runs the scanner and (in non-dry-run mode) places orders.

Usage:
  # Paper-mode dry run — scan + log but no orders:
  PAPER_TRADING_MODE=true python3 -m markets.policy.trader --dry-run

  # Paper-mode live scan + paper order placement:
  PAPER_TRADING_MODE=true python3 -m markets.policy.trader

  # Real live trading (careful — places real orders):
  python3 -m markets.policy.trader

Emergency stop:
  touch /Users/miqadmin/Documents/limitless/PAUSE_TRADING
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

from log_setup import get_logger
from config import (
    PAPER_TRADING_MODE,
    PAPER_FILL_MODE,
    POLICY_MAX_ENTRY_PRICE_CENTS,
)
from core.broker_factory import get_broker
from core.opportunity import DocOpportunity
from markets.policy.scanner import scan
from notifications import send_discord_alert
from position_store import load_positions, register_position
from trade_events import log_event, TradeEvent
from trading_guards import check_kill_switch

logger = get_logger(__name__)
ET = ZoneInfo("America/New_York")


async def run(dry_run: bool = False, scan_only: bool = False) -> int:
    """Run one scan-and-trade cycle. Returns the number of orders placed."""
    mode_label = "PAPER" if PAPER_TRADING_MODE else "LIVE"
    if dry_run or scan_only:
        mode_label += " [DRY RUN]"
    logger.info("Policy Trader — %s | fill_mode=%s", mode_label, PAPER_FILL_MODE)

    ok, reason = check_kill_switch()
    if not ok:
        logger.error("HALTED: %s", reason)
        log_event(TradeEvent.KILL_SWITCH_ACTIVE, "policy_trader", {"reason": reason})
        return 0

    broker = await get_broker()
    placed: list[tuple[DocOpportunity, dict]] = []

    try:
        opportunities, stats = await scan(broker=broker)

        if not opportunities:
            logger.info(
                "No tradeable policy opportunities this scan. "
                "Stats: policy_series=%d prefilter=%d fresh_doc=%d llm_ok=%d near_miss=%d",
                stats.policy_series, stats.passed_prefilter, stats.with_fresh_doc,
                stats.llm_calls - stats.llm_failures, stats.near_misses,
            )
            return 0

        logger.info("%d TRADEABLE policy setup(s) found", len(opportunities))
        for opp in opportunities:
            _log_opportunity(opp)

        if scan_only or dry_run:
            logger.info("scan-only / dry-run — not placing orders.")
            return 0

        # ── Execute ──
        positions = load_positions()
        existing_tickers = {
            p.get("ticker") for p in positions
            if p.get("status") in ("open", "resting", "pending_sell")
        }

        for opp in opportunities:
            if opp.ticker in existing_tickers:
                logger.info("skip %s — existing open/resting position", opp.ticker)
                continue

            entry_price = _entry_price(opp)
            contracts = opp.suggested_contracts
            if contracts <= 0:
                continue

            logger.info(
                "Placing %s %s %dx @ %dc (broker=%s)",
                opp.side.upper(), opp.ticker, contracts, entry_price, broker.mode,
            )
            result = await broker.place_order(
                ticker=opp.ticker, side=opp.side, action="buy",
                count=contracts, price=entry_price, order_type="limit",
            )
            order = result.get("order", {}) if isinstance(result, dict) else {}
            status = str(order.get("status", "UNKNOWN")).upper()
            order_id = order.get("order_id", "")

            if status in ("EXECUTED", "RESTING") and order_id:
                try:
                    register_position(
                        ticker=opp.ticker, side=opp.side, price=entry_price,
                        quantity=contracts, order_id=order_id, status=status,
                    )
                    placed.append((opp, order))
                    log_event(TradeEvent.TRADE_EXECUTED, "policy_trader", {
                        "ticker": opp.ticker, "side": opp.side,
                        "price": entry_price, "qty": contracts,
                        "cost": round(entry_price * contracts / 100, 2),
                        "llm_prob": round(opp.llm_prob, 3),
                        "divergence_pp": round(opp.divergence_pp, 1),
                        "confidence_tier": opp.llm_confidence_tier,
                        "source_adapter": opp.source_adapter,
                        "order_id": order_id,
                        "broker_mode": broker.mode,
                    })
                except Exception as e:
                    logger.error("register_position failed for %s: %s", order_id, e)
            else:
                reason = order.get("reject_reason") or status
                logger.warning("order not placed for %s: %s", opp.ticker, reason)
                log_event(TradeEvent.TRADE_FAILED, "policy_trader", {
                    "ticker": opp.ticker, "error": reason, "broker_mode": broker.mode,
                })

        if placed:
            await _send_summary(placed, broker.mode)
        logger.info("Done. %d order(s) placed.", len(placed))
        return len(placed)

    finally:
        await broker.stop()


def _entry_price(opp: DocOpportunity) -> int:
    """bid+1 for YES, or (100-ask)+1 for NO, capped at the policy entry cap."""
    if opp.side == "yes":
        return min(opp.yes_bid + 1, POLICY_MAX_ENTRY_PRICE_CENTS)
    return min((100 - opp.yes_ask) + 1, POLICY_MAX_ENTRY_PRICE_CENTS)


def _log_opportunity(opp: DocOpportunity) -> None:
    logger.info(
        "  %s %s @ %dc x%d | LLM %.1f%% vs mkt %.1f%% (div %.1fpp, %s) | %s",
        opp.side.upper(), opp.ticker, _entry_price(opp),
        opp.suggested_contracts,
        opp.llm_prob * 100, opp.market_implied_prob * 100,
        opp.divergence_pp, opp.llm_confidence_tier,
        opp.bracket_title[:60],
    )


async def _send_summary(
    placed: list[tuple[DocOpportunity, dict]], broker_mode: str,
) -> None:
    lines = []
    for opp, order in placed:
        price = order.get("price", _entry_price(opp))
        lines.append(
            f"**{opp.side.upper()} {opp.ticker} @ {price}c x{opp.suggested_contracts}** "
            f"(LLM {opp.llm_prob:.1%} vs mkt {opp.market_implied_prob:.1%}, "
            f"div {opp.divergence_pp:.1f}pp, {opp.llm_confidence_tier})"
        )
    await send_discord_alert(
        title=f"POLICY TRADER [{broker_mode.upper()}]: {len(placed)} trade(s)",
        description="\n".join(lines),
        color=0x3498DB if broker_mode == "paper" else 0x00FF00,
        context="policy_trader",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Policy market trader (Kalshi)")
    parser.add_argument("--dry-run", action="store_true", help="Scan + log but don't place orders")
    parser.add_argument("--scan-only", action="store_true", help="Same as --dry-run")
    args = parser.parse_args()
    asyncio.run(run(dry_run=args.dry_run, scan_only=args.scan_only))
