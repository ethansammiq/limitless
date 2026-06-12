"""Broker abstractions — drop-in replacement for direct KalshiClient use.

BrokerInterface mirrors KalshiClient's public async method surface so the two
are swappable at every call site. KalshiBroker is a thin delegate around
KalshiClient. PaperBroker fetches real Kalshi quotes but simulates fills
locally, writing state to positions_paper.json (via position_store) and to
paper_balance.json / paper_orders.json (managed here).

Paper fill modes (via config.PAPER_FILL_MODE env):
  resting  Order rests; fills only when a real Kalshi quote touches the limit.
           Most realistic for the bid+1 strategy. Default.
  instant  Fills immediately if the limit crosses the current book; rejects
           otherwise. Simpler; good for fast unit tests.

Resting orders are swept opportunistically on every get_orderbook(),
get_positions(), and get_orders() call, so callers naturally trigger fills
just by running their normal loop.
"""

from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from log_setup import get_logger
from config import (
    PAPER_INITIAL_BALANCE,
    PAPER_FILL_MODE,
    PAPER_BALANCE_FILE,
    PAPER_ORDERS_FILE,
)
from kalshi_client import KalshiClient

logger = get_logger(__name__)
ET = ZoneInfo("America/New_York")

__all__ = ["BrokerInterface", "KalshiBroker", "PaperBroker"]


class BrokerInterface(ABC):
    """Public method surface shared by KalshiBroker and PaperBroker.

    Every method mirrors the equivalent on KalshiClient so the two are
    interchangeable from the caller's perspective.
    """

    mode: str = "abstract"  # "live" or "paper"

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def get_markets(
        self, series_ticker: str = None, status: str = "open", limit: int = 100,
    ) -> list: ...

    @abstractmethod
    async def get_orderbook(self, ticker: str, depth: int = 10) -> dict: ...

    @abstractmethod
    async def get_balance(self) -> float: ...

    @abstractmethod
    async def get_positions(self) -> list: ...

    @abstractmethod
    async def get_fills(self, ticker: str = None, limit: int = 200) -> list: ...

    @abstractmethod
    async def place_order(
        self, ticker: str, side: str, action: str, count: int, price: int,
        order_type: str = "limit",
    ) -> dict: ...

    @abstractmethod
    async def get_orders(self, ticker: str = None, status: str = "resting") -> list: ...

    @abstractmethod
    async def cancel_order(self, order_id: str) -> dict: ...


class KalshiBroker(BrokerInterface):
    """Thin delegate around KalshiClient. No behavior change vs direct use."""

    mode = "live"

    def __init__(self, api_key_id: str, private_key_path: str, demo_mode: bool = False):
        self._client = KalshiClient(
            api_key_id=api_key_id,
            private_key_path=private_key_path,
            demo_mode=demo_mode,
        )

    async def start(self) -> None:
        await self._client.start()

    async def stop(self) -> None:
        await self._client.stop()

    async def get_markets(self, *args, **kwargs):
        return await self._client.get_markets(*args, **kwargs)

    async def get_orderbook(self, *args, **kwargs):
        return await self._client.get_orderbook(*args, **kwargs)

    async def get_balance(self) -> float:
        return await self._client.get_balance()

    async def get_positions(self) -> list:
        return await self._client.get_positions()

    async def get_fills(self, *args, **kwargs):
        return await self._client.get_fills(*args, **kwargs)

    async def place_order(self, *args, **kwargs):
        return await self._client.place_order(*args, **kwargs)

    async def get_orders(self, *args, **kwargs):
        return await self._client.get_orders(*args, **kwargs)

    async def cancel_order(self, *args, **kwargs):
        return await self._client.cancel_order(*args, **kwargs)


class PaperBroker(BrokerInterface):
    """Simulated execution using real Kalshi quotes.

    Quote operations (get_markets, get_orderbook, get_fills shape) delegate to
    a read-only KalshiClient. Execution operations (place_order, cancel_order,
    balance, positions) are fully simulated and persisted locally.
    """

    mode = "paper"

    def __init__(
        self,
        initial_balance: float = PAPER_INITIAL_BALANCE,
        fill_mode: str = PAPER_FILL_MODE,
        quote_client: Optional[KalshiClient] = None,
    ):
        self._fill_mode = fill_mode if fill_mode in ("resting", "instant") else "resting"
        self._initial_balance = initial_balance
        self._balance: float = initial_balance
        self._orders: list[dict] = []  # every order we ever placed (resting + filled + canceled)
        self._next_order_id = 0
        # Quote client runs without credentials — public endpoints only
        self._quote_client = quote_client or KalshiClient(demo_mode=False)

    # ── lifecycle ──

    async def start(self) -> None:
        await self._quote_client.start()
        self._load_state()
        logger.info(
            "PaperBroker started | fill_mode=%s | balance=$%.2f",
            self._fill_mode, self._balance,
        )

    async def stop(self) -> None:
        self._save_state()
        await self._quote_client.stop()

    # ── state persistence ──

    def _load_state(self) -> None:
        if PAPER_BALANCE_FILE.exists():
            try:
                data = json.loads(PAPER_BALANCE_FILE.read_text())
                self._balance = float(data.get("balance", self._initial_balance))
                self._initial_balance = float(data.get("initial_balance", self._initial_balance))
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning(
                    "paper_balance.json unreadable (%s) — resetting to $%.2f",
                    e, self._initial_balance,
                )
                self._balance = self._initial_balance
        if PAPER_ORDERS_FILE.exists():
            try:
                self._orders = json.loads(PAPER_ORDERS_FILE.read_text())
                paper_ids = [
                    int(o["order_id"].split("_")[-1])
                    for o in self._orders
                    if str(o.get("order_id", "")).startswith("paper_")
                ]
                self._next_order_id = max(paper_ids, default=-1) + 1
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning("paper_orders.json unreadable (%s) — starting fresh", e)
                self._orders = []

    def _save_state(self) -> None:
        try:
            PAPER_BALANCE_FILE.write_text(
                json.dumps(
                    {
                        "balance": round(self._balance, 2),
                        "initial_balance": self._initial_balance,
                        "last_updated": datetime.now(ET).isoformat(),
                    },
                    indent=2,
                )
            )
            PAPER_ORDERS_FILE.write_text(json.dumps(self._orders, indent=2, default=str))
        except Exception as e:
            logger.error("Failed to persist paper state: %s", e)

    # ── passthrough reads (real quotes) ──

    async def get_markets(self, *args, **kwargs):
        return await self._quote_client.get_markets(*args, **kwargs)

    async def get_orderbook(self, ticker: str, depth: int = 10) -> dict:
        book = await self._quote_client.get_orderbook(ticker, depth=depth)
        await self._sweep_fills_for_ticker(ticker, book)
        return book

    async def get_fills(self, ticker: str = None, limit: int = 200) -> list:
        filled = [o for o in self._orders if o.get("status") == "EXECUTED"]
        if ticker:
            filled = [o for o in filled if o.get("ticker") == ticker]
        return filled[-limit:]

    # ── simulated execution ──

    async def get_balance(self) -> float:
        return round(self._balance, 2)

    async def get_positions(self) -> list:
        # Sweep fills before reading positions so callers see fresh state
        await self._sweep_all_fills()
        # Positions are tracked by position_store (positions_paper.json in paper mode).
        # We reshape to match Kalshi's /portfolio/positions response loosely.
        from position_store import load_positions  # lazy import: avoid cycle
        positions = load_positions()
        out = []
        for p in positions:
            qty = p.get("contracts", 0)
            signed = qty if p.get("side") == "yes" else -qty
            out.append({
                "ticker": p.get("ticker"),
                "position": signed,
                "market_exposure": int(round(p.get("avg_price", 0) * qty)),
                "realized_pnl": int(round(p.get("pnl_realized", 0) * 100)),
                "resting_orders_count": sum(
                    1 for o in self._orders
                    if o.get("ticker") == p.get("ticker") and o.get("status") == "RESTING"
                ),
                "fees_paid": 0,
                "last_updated_ts": p.get("entry_time", ""),
            })
        return out

    async def place_order(
        self, ticker: str, side: str, action: str, count: int, price: int,
        order_type: str = "limit",
    ) -> dict:
        order_id = f"paper_{self._next_order_id}"
        self._next_order_id += 1
        now = datetime.now(ET).isoformat()

        book = await self._quote_client.get_orderbook(ticker)
        book_bid, book_ask = _bid_ask_for_side(book, side)

        order = {
            "order_id": order_id,
            "ticker": ticker,
            "side": side,
            "action": action,
            "count": count,
            "price": price,
            "type": order_type,
            "status": "UNKNOWN",
            "created_at": now,
            "book_bid_at_placement": book_bid,
            "book_ask_at_placement": book_ask,
        }

        crosses = _would_cross(action, price, book_bid, book_ask)

        if crosses:
            if not self._simulate_fill(order):
                order["status"] = "REJECTED"
                order["reject_reason"] = "insufficient_balance"
                self._orders.append(order)
        elif self._fill_mode == "instant":
            order["status"] = "REJECTED"
            order["reject_reason"] = (
                f"price_{price}c_does_not_cross_book_(bid={book_bid},ask={book_ask})"
            )
            self._orders.append(order)
        else:  # resting
            order["status"] = "RESTING"
            self._orders.append(order)
            logger.info(
                "[PAPER] RESTING %s %s %dx %s @ %dc (book %d/%d)",
                action.upper(), side.upper(), count, ticker, price, book_bid, book_ask,
            )

        self._save_state()
        return {"order": order}

    async def get_orders(self, ticker: str = None, status: str = "resting") -> list:
        await self._sweep_all_fills()
        status_upper = status.upper() if status else ""
        result = []
        for o in self._orders:
            if status_upper and o.get("status") != status_upper:
                continue
            if ticker and o.get("ticker") != ticker:
                continue
            result.append(o)
        return result

    async def cancel_order(self, order_id: str) -> dict:
        for o in self._orders:
            if o.get("order_id") == order_id and o.get("status") == "RESTING":
                o["status"] = "CANCELED"
                o["canceled_at"] = datetime.now(ET).isoformat()
                self._save_state()
                logger.info("[PAPER] CANCELED %s", order_id)
                return {"order": o}
        return {}

    # ── paper-only helpers ──

    async def _sweep_fills_for_ticker(self, ticker: str, book: dict) -> None:
        """Check resting orders for one ticker against the fresh book."""
        changed = False
        for o in self._orders:
            if o.get("status") != "RESTING" or o.get("ticker") != ticker:
                continue
            bid, ask = _bid_ask_for_side(book, o["side"])
            if _would_cross(o["action"], o["price"], bid, ask):
                if self._simulate_fill(o):
                    changed = True
        if changed:
            self._save_state()

    async def _sweep_all_fills(self) -> None:
        """Pull fresh books for all tickers that have a resting order and sweep."""
        tickers = {o["ticker"] for o in self._orders if o.get("status") == "RESTING"}
        for ticker in tickers:
            try:
                book = await self._quote_client.get_orderbook(ticker)
                await self._sweep_fills_for_ticker(ticker, book)
            except Exception as e:
                logger.debug("Sweep failed for %s: %s", ticker, e)

    def _simulate_fill(self, order: dict) -> bool:
        """Mark order filled, adjust balance. Returns False if insufficient funds."""
        count = order["count"]
        price = order["price"]
        cost_dollars = count * price / 100.0

        if order["action"] == "buy":
            if self._balance < cost_dollars:
                logger.warning(
                    "[PAPER] FILL REJECTED — insufficient balance ($%.2f < $%.2f) for %s",
                    self._balance, cost_dollars, order["order_id"],
                )
                return False
            self._balance -= cost_dollars
        elif order["action"] == "sell":
            self._balance += cost_dollars

        order["status"] = "EXECUTED"
        order["filled_at"] = datetime.now(ET).isoformat()
        order["filled_price"] = price
        order["filled_count"] = count
        logger.info(
            "[PAPER] FILLED %s %s %dx %s @ %dc (cost $%.2f, balance $%.2f)",
            order["action"].upper(), order["side"].upper(), count,
            order["ticker"], price, cost_dollars, self._balance,
        )
        return True


# ── module-level helpers ──

def _bid_ask_for_side(book: dict, side: str) -> tuple[int, int]:
    """Return (bid, ask) for this side's perspective, in cents.

    Kalshi orderbook yields YES-bid levels and NO-bid levels. Ask-for-YES is
    derived as (100 - best NO-bid).
    """
    yes_levels = book.get("yes") or []
    no_levels = book.get("no") or []
    yes_bids = [lv[0] for lv in yes_levels if len(lv) >= 2 and lv[1] > 0]
    no_bids = [lv[0] for lv in no_levels if len(lv) >= 2 and lv[1] > 0]

    best_yes_bid = max(yes_bids) if yes_bids else 0
    best_no_bid = max(no_bids) if no_bids else 0

    if side == "yes":
        bid = best_yes_bid
        ask = (100 - best_no_bid) if best_no_bid > 0 else 100
    else:
        bid = best_no_bid
        ask = (100 - best_yes_bid) if best_yes_bid > 0 else 100
    return bid, ask


def _would_cross(action: str, price: int, bid: int, ask: int) -> bool:
    """Does a limit order cross the current book immediately?"""
    if action == "buy":
        return ask > 0 and price >= ask
    if action == "sell":
        return bid > 0 and price <= bid
    return False
