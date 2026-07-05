"""PaperBroker tests — fill modes, state isolation, balance, positions."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


# Redirect paper state files to a temp dir for every test
_tmpdir = tempfile.mkdtemp()
_test_balance = Path(_tmpdir) / "paper_balance.json"
_test_orders = Path(_tmpdir) / "paper_orders.json"
_test_positions = Path(_tmpdir) / "positions_paper.json"


@pytest.fixture(autouse=True)
def _paper_env(monkeypatch):
    import config
    monkeypatch.setattr(config, "PAPER_BALANCE_FILE", _test_balance)
    monkeypatch.setattr(config, "PAPER_ORDERS_FILE", _test_orders)
    monkeypatch.setattr(config, "PAPER_POSITIONS_FILE", _test_positions)
    monkeypatch.setattr(config, "PAPER_TRADING_MODE", True)
    monkeypatch.setattr(config, "PAPER_INITIAL_BALANCE", 1000.0)
    # Also reload the re-exported constants in core.broker, which imports them
    import core.broker as cb
    monkeypatch.setattr(cb, "PAPER_BALANCE_FILE", _test_balance)
    monkeypatch.setattr(cb, "PAPER_ORDERS_FILE", _test_orders)
    monkeypatch.setattr(cb, "PAPER_INITIAL_BALANCE", 1000.0)
    for p in (_test_balance, _test_orders, _test_positions):
        if p.exists():
            p.unlink()
    yield


def _mock_quote_client(books: dict):
    """Build a mock KalshiClient whose get_orderbook returns pre-canned books.

    books: {ticker: {"yes": [[price, qty], ...], "no": [[price, qty], ...]}}
    """
    mock = AsyncMock()
    mock.start = AsyncMock(return_value=None)
    mock.stop = AsyncMock(return_value=None)

    async def _orderbook(ticker, depth=10):
        return books.get(ticker, {"yes": [], "no": []})

    mock.get_orderbook = _orderbook
    mock.get_markets = AsyncMock(return_value=[])
    return mock


class TestFillModes:
    """Instant vs resting fill semantics."""

    def test_instant_crosses_and_fills(self):
        from core.broker import PaperBroker
        # Book: yes-bid 40¢ (qty 100), no-bid 55¢ (qty 100) → yes-ask = 100-55 = 45¢
        books = {"X": {"yes": [[40, 100]], "no": [[55, 100]]}}
        broker = PaperBroker(
            initial_balance=1000.0, fill_mode="instant",
            quote_client=_mock_quote_client(books),
        )

        async def go():
            await broker.start()
            # Buy YES at 46¢ — crosses the 45¢ ask, should fill
            resp = await broker.place_order("X", "yes", "buy", 10, 46)
            order = resp["order"]
            assert order["status"] == "EXECUTED"
            # Balance deducted: 10 * 46c = $4.60
            assert await broker.get_balance() == pytest.approx(1000.0 - 4.60)
            await broker.stop()

        asyncio.run(go())

    def test_instant_fill_persists_to_ledger(self):
        """Regression: immediately-crossing fills must land in paper_orders.json.

        They used to be returned to the caller but never appended to _orders,
        so get_fills() missed them and fills_watch had to reconstruct fills
        from positions_paper.json (the DEN-T88 case).
        """
        from core.broker import PaperBroker
        books = {"X": {"yes": [[40, 100]], "no": [[55, 100]]}}
        broker = PaperBroker(
            initial_balance=1000.0, fill_mode="instant",
            quote_client=_mock_quote_client(books),
        )

        async def go():
            await broker.start()
            resp = await broker.place_order("X", "yes", "buy", 10, 46)
            order_id = resp["order"]["order_id"]
            fills = await broker.get_fills(ticker="X")
            assert [f["order_id"] for f in fills] == [order_id]
            saved = json.loads(_test_orders.read_text())
            assert saved[0]["order_id"] == order_id
            assert saved[0]["status"] == "EXECUTED"
            await broker.stop()

        asyncio.run(go())

    def test_instant_rejects_when_not_crossing(self):
        from core.broker import PaperBroker
        books = {"X": {"yes": [[40, 100]], "no": [[55, 100]]}}
        broker = PaperBroker(
            initial_balance=1000.0, fill_mode="instant",
            quote_client=_mock_quote_client(books),
        )

        async def go():
            await broker.start()
            # Buy YES at 44¢ — below 45¢ ask, should reject
            resp = await broker.place_order("X", "yes", "buy", 10, 44)
            assert resp["order"]["status"] == "REJECTED"
            assert await broker.get_balance() == 1000.0  # unchanged
            await broker.stop()

        asyncio.run(go())

    def test_resting_queues_then_fills_on_sweep(self):
        """Order rests below ask; fills when a fresher book shows the ask dropped."""
        from core.broker import PaperBroker
        # Initial book: yes-ask=45 (no-bid=55)
        books = {"X": {"yes": [[40, 100]], "no": [[55, 100]]}}
        quote_client = _mock_quote_client(books)
        broker = PaperBroker(
            initial_balance=1000.0, fill_mode="resting", quote_client=quote_client,
        )

        async def go():
            await broker.start()
            # Buy YES at 44¢ — below ask, rests
            resp = await broker.place_order("X", "yes", "buy", 10, 44)
            assert resp["order"]["status"] == "RESTING"
            # Book tightens: no-bid drops to 54 → yes-ask becomes 46. Still above our 44.
            books["X"] = {"yes": [[40, 100]], "no": [[54, 100]]}
            await broker.get_orderbook("X")  # triggers sweep
            open_orders = await broker.get_orders(ticker="X", status="resting")
            assert len(open_orders) == 1  # still resting
            # Book drops further: no-bid 55 → ask 45; still not touched
            # Now ask crosses: no-bid=58 → ask=42, order fills
            books["X"] = {"yes": [[43, 100]], "no": [[58, 100]]}
            await broker.get_orderbook("X")
            filled = await broker.get_orders(ticker="X", status="executed")
            assert len(filled) == 1
            assert await broker.get_balance() == pytest.approx(1000.0 - 4.40)
            await broker.stop()

        asyncio.run(go())


class TestStateIsolation:
    """Paper state must not touch live state files."""

    def test_balance_persists_across_restart(self):
        from core.broker import PaperBroker
        books = {"X": {"yes": [[40, 100]], "no": [[55, 100]]}}

        async def go():
            # Session 1: place order, close
            b1 = PaperBroker(
                initial_balance=1000.0, fill_mode="instant",
                quote_client=_mock_quote_client(books),
            )
            await b1.start()
            await b1.place_order("X", "yes", "buy", 10, 46)
            await b1.stop()

            # Session 2: fresh broker should see the deducted balance
            b2 = PaperBroker(
                initial_balance=1000.0,  # ignored if file exists
                fill_mode="instant",
                quote_client=_mock_quote_client(books),
            )
            await b2.start()
            bal = await b2.get_balance()
            assert bal == pytest.approx(1000.0 - 4.60)
            await b2.stop()

        asyncio.run(go())

    def test_paper_state_never_writes_live_positions(self):
        """PaperBroker never writes to positions.json (live)."""
        from core.broker import PaperBroker
        import config
        live_file = config.LIVE_POSITIONS_FILE
        live_file_exists_before = live_file.exists()
        live_content_before = live_file.read_text() if live_file_exists_before else None

        async def go():
            books = {"X": {"yes": [[40, 100]], "no": [[55, 100]]}}
            broker = PaperBroker(
                initial_balance=1000.0, fill_mode="instant",
                quote_client=_mock_quote_client(books),
            )
            await broker.start()
            await broker.place_order("X", "yes", "buy", 10, 46)
            await broker.stop()

        asyncio.run(go())

        # Live file must be unchanged
        if live_file_exists_before:
            assert live_file.read_text() == live_content_before
        else:
            assert not live_file.exists()


class TestCancelAndGetOrders:
    """Order management."""

    def test_cancel_resting_order(self):
        from core.broker import PaperBroker
        books = {"X": {"yes": [[40, 100]], "no": [[55, 100]]}}
        broker = PaperBroker(
            initial_balance=1000.0, fill_mode="resting",
            quote_client=_mock_quote_client(books),
        )

        async def go():
            await broker.start()
            resp = await broker.place_order("X", "yes", "buy", 10, 44)
            order_id = resp["order"]["order_id"]
            assert resp["order"]["status"] == "RESTING"

            cancel = await broker.cancel_order(order_id)
            assert cancel["order"]["status"] == "CANCELED"

            orders = await broker.get_orders(status="canceled")
            assert len(orders) == 1
            await broker.stop()

        asyncio.run(go())

    def test_insufficient_balance_rejects(self):
        from core.broker import PaperBroker
        books = {"X": {"yes": [[40, 100]], "no": [[55, 100]]}}
        broker = PaperBroker(
            initial_balance=1.0, fill_mode="instant",  # $1 balance
            quote_client=_mock_quote_client(books),
        )

        async def go():
            await broker.start()
            # Try to buy 100 contracts at 46¢ = $46 — exceeds $1 balance
            resp = await broker.place_order("X", "yes", "buy", 100, 46)
            assert resp["order"]["status"] == "REJECTED"
            assert "insufficient" in resp["order"].get("reject_reason", "").lower()
            assert await broker.get_balance() == 1.0  # unchanged
            await broker.stop()

        asyncio.run(go())


class TestPlaceOrderSignatureParity:
    """PaperBroker.place_order must accept the exact kwargs production passes.

    Regression: execute_trade.py / proxy_arb_engine.py pass client_order_id;
    PaperBroker used to raise TypeError, swallowed upstream as a failed trade.
    """

    # Exact kwargs from execute_trade.py:206 / :361 and proxy_arb_engine.py:1104
    PRODUCTION_KWARGS = dict(
        ticker="X", side="yes", action="buy", count=10, price=46,
        order_type="limit", client_order_id="11111111-2222-3333-4444-555555555555",
    )

    def test_accepts_production_kwargs_and_records_client_order_id(self):
        from core.broker import PaperBroker
        books = {"X": {"yes": [[40, 100]], "no": [[55, 100]]}}
        broker = PaperBroker(
            initial_balance=1000.0, fill_mode="instant",
            quote_client=_mock_quote_client(books),
        )

        async def go():
            await broker.start()
            resp = await broker.place_order(**self.PRODUCTION_KWARGS)
            order = resp["order"]
            assert order["status"] == "EXECUTED"
            assert order["client_order_id"] == self.PRODUCTION_KWARGS["client_order_id"]
            await broker.stop()

        asyncio.run(go())

    def test_resting_order_persists_client_order_id(self):
        """Persisted, so StateDB reconciliation can match it after restart."""
        from core.broker import PaperBroker
        books = {"X": {"yes": [[40, 100]], "no": [[55, 100]]}}
        broker = PaperBroker(
            initial_balance=1000.0, fill_mode="resting",
            quote_client=_mock_quote_client(books),
        )

        async def go():
            await broker.start()
            # bid+1 entry as production places it: 41c rests below the 45c ask
            kwargs = {**self.PRODUCTION_KWARGS, "price": 41}
            resp = await broker.place_order(**kwargs)
            assert resp["order"]["status"] == "RESTING"
            saved = json.loads(_test_orders.read_text())
            assert saved[0]["client_order_id"] == self.PRODUCTION_KWARGS["client_order_id"]
            await broker.stop()

        asyncio.run(go())

    def test_generates_client_order_id_when_omitted(self):
        """Parity with KalshiClient, which always sends an idempotency key."""
        from core.broker import PaperBroker
        books = {"X": {"yes": [[40, 100]], "no": [[55, 100]]}}
        broker = PaperBroker(
            initial_balance=1000.0, fill_mode="resting",
            quote_client=_mock_quote_client(books),
        )

        async def go():
            await broker.start()
            # position_monitor.py-style call: no client_order_id
            resp = await broker.place_order(
                ticker="X", side="yes", action="sell",
                count=5, price=60, order_type="limit",
            )
            assert resp["order"]["client_order_id"]
            await broker.stop()

        asyncio.run(go())

    def test_signature_matches_kalshi_client(self):
        """Guard against future drift: both brokers must bind production kwargs."""
        import inspect
        from core.broker import PaperBroker
        from kalshi_client import KalshiClient

        for cls in (PaperBroker, KalshiClient):
            sig = inspect.signature(cls.place_order)
            sig.bind(None, **self.PRODUCTION_KWARGS)  # raises TypeError on mismatch


class TestBidAskExtraction:
    """Verify _bid_ask_for_side math matches Kalshi conventions."""

    def test_bid_ask_for_yes(self):
        from core.broker import _bid_ask_for_side
        book = {"yes": [[40, 100], [38, 200]], "no": [[55, 100]]}
        bid, ask = _bid_ask_for_side(book, "yes")
        assert bid == 40           # best yes-bid
        assert ask == 100 - 55     # 45

    def test_bid_ask_for_no(self):
        from core.broker import _bid_ask_for_side
        book = {"yes": [[40, 100]], "no": [[55, 100], [50, 200]]}
        bid, ask = _bid_ask_for_side(book, "no")
        assert bid == 55           # best no-bid
        assert ask == 100 - 40     # 60

    def test_empty_book(self):
        from core.broker import _bid_ask_for_side
        bid, ask = _bid_ask_for_side({"yes": [], "no": []}, "yes")
        assert bid == 0
        assert ask == 100  # fallback when no counterparty

    def test_would_cross(self):
        from core.broker import _would_cross
        assert _would_cross("buy", 45, bid=40, ask=45) is True   # >= ask
        assert _would_cross("buy", 44, bid=40, ask=45) is False
        assert _would_cross("sell", 40, bid=40, ask=45) is True  # <= bid
        assert _would_cross("sell", 41, bid=40, ask=45) is False


class TestStrategyAttribution:
    """strategy= tags paper order records; live delegate strips it."""

    def test_paper_order_records_strategy(self):
        from core.broker import PaperBroker
        books = {"X": {"yes": [[40, 100]], "no": [[55, 100]]}}
        broker = PaperBroker(
            initial_balance=1000.0, fill_mode="resting",
            quote_client=_mock_quote_client(books),
        )

        async def go():
            await broker.start()
            # 41c rests below the 45c ask — resting orders are persisted
            resp = await broker.place_order(
                "X", "yes", "buy", 10, 41, strategy="auto_trader",
            )
            assert resp["order"]["strategy"] == "auto_trader"
            saved = json.loads(_test_orders.read_text())
            assert saved[0]["strategy"] == "auto_trader"
            await broker.stop()

        asyncio.run(go())

    def test_paper_order_defaults_to_untagged(self):
        from core.broker import PaperBroker
        books = {"X": {"yes": [[40, 100]], "no": [[55, 100]]}}
        broker = PaperBroker(
            initial_balance=1000.0, fill_mode="resting",
            quote_client=_mock_quote_client(books),
        )

        async def go():
            await broker.start()
            resp = await broker.place_order("X", "yes", "buy", 10, 41)
            assert resp["order"]["strategy"] == "untagged"
            await broker.stop()

        asyncio.run(go())

    def test_kalshi_broker_strips_strategy_before_api_client(self):
        """The live API rejects unknown params — strategy must never reach it."""
        from core.broker import KalshiBroker

        broker = KalshiBroker(api_key_id="k", private_key_path="p")

        # Strict fake mirroring KalshiClient.place_order's exact signature:
        # a leaked strategy kwarg would raise TypeError here.
        class StrictClient:
            async def place_order(self, ticker, side, action, count, price,
                                  order_type="limit", client_order_id=None):
                return {"order": {"order_id": "live_1", "status": "resting"}}

        broker._client = StrictClient()

        resp = asyncio.run(broker.place_order(
            ticker="X", side="yes", action="buy", count=10, price=46,
            order_type="limit", client_order_id="cid-1", strategy="peak_trader",
        ))
        assert resp["order"]["order_id"] == "live_1"
