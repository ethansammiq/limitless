#!/usr/bin/env python3
"""Tests for fill monitoring — stale resting order cancellation and duplicate guard."""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

ET = ZoneInfo("America/New_York")


# ═══════════════════════════════════════════════════════════════
# STALE RESTING ORDER CANCELLATION
# ═══════════════════════════════════════════════════════════════

class TestCancelStaleRestingOrders:
    """Test _cancel_stale_resting_orders from position_monitor."""

    @pytest.fixture
    def mock_client(self):
        client = AsyncMock()
        client.cancel_order = AsyncMock(return_value={})
        return client

    def _make_resting_pos(self, ticker="KXHIGHNY-26FEB16-B36.5", minutes_ago=0):
        entry = (datetime.now(ET) - timedelta(minutes=minutes_ago)).isoformat()
        return {
            "ticker": ticker,
            "side": "yes",
            "avg_price": 20,
            "contracts": 5,
            "order_id": f"order-{ticker}",
            "status": "resting",
            "entry_time": entry,
            "notes": [],
        }

    def _run(self, coro):
        return asyncio.run(coro)

    @patch("position_monitor.send_discord_alert", new_callable=AsyncMock)
    def test_no_resting_orders(self, mock_alert, mock_client):
        from position_monitor import _cancel_stale_resting_orders
        positions = [{"status": "open", "ticker": "T1"}]
        now = datetime.now(ET)
        actions = self._run(_cancel_stale_resting_orders(positions, mock_client, now))
        assert actions == []
        mock_client.cancel_order.assert_not_called()

    @patch("position_monitor.send_discord_alert", new_callable=AsyncMock)
    def test_fresh_resting_not_cancelled(self, mock_alert, mock_client):
        """Resting order placed 30 min ago should NOT be cancelled."""
        from position_monitor import _cancel_stale_resting_orders
        pos = self._make_resting_pos(minutes_ago=30)
        positions = [pos]
        now = datetime.now(ET)
        actions = self._run(_cancel_stale_resting_orders(positions, mock_client, now))
        assert actions == []
        assert pos["status"] == "resting"
        mock_client.cancel_order.assert_not_called()

    @patch("position_monitor.send_discord_alert", new_callable=AsyncMock)
    def test_stale_resting_cancelled(self, mock_alert, mock_client):
        """Resting order placed 150 min ago should be cancelled (>120 timeout)."""
        from position_monitor import _cancel_stale_resting_orders
        pos = self._make_resting_pos(minutes_ago=150)
        positions = [pos]
        now = datetime.now(ET)
        actions = self._run(_cancel_stale_resting_orders(positions, mock_client, now))
        assert len(actions) == 1
        assert "STALE CANCEL" in actions[0]
        assert pos["status"] == "cancelled"
        mock_client.cancel_order.assert_called_once_with(pos["order_id"])

    @patch("position_monitor.send_discord_alert", new_callable=AsyncMock)
    def test_stale_at_exact_boundary(self, mock_alert, mock_client):
        """Resting order placed exactly at timeout boundary should be cancelled."""
        from position_monitor import _cancel_stale_resting_orders
        from config import RESTING_ORDER_TIMEOUT_MINUTES
        pos = self._make_resting_pos(minutes_ago=RESTING_ORDER_TIMEOUT_MINUTES)
        positions = [pos]
        now = datetime.now(ET)
        actions = self._run(_cancel_stale_resting_orders(positions, mock_client, now))
        assert len(actions) == 1
        assert pos["status"] == "cancelled"

    @patch("position_monitor.send_discord_alert", new_callable=AsyncMock)
    def test_just_under_boundary(self, mock_alert, mock_client):
        """Order placed 1 min before timeout should NOT be cancelled."""
        from position_monitor import _cancel_stale_resting_orders
        from config import RESTING_ORDER_TIMEOUT_MINUTES
        pos = self._make_resting_pos(minutes_ago=RESTING_ORDER_TIMEOUT_MINUTES - 1)
        positions = [pos]
        now = datetime.now(ET)
        actions = self._run(_cancel_stale_resting_orders(positions, mock_client, now))
        assert actions == []
        assert pos["status"] == "resting"

    @patch("position_monitor.send_discord_alert", new_callable=AsyncMock)
    def test_multiple_positions_mixed(self, mock_alert, mock_client):
        """Mix of fresh and stale resting orders — only stale ones cancelled."""
        from position_monitor import _cancel_stale_resting_orders
        fresh = self._make_resting_pos(ticker="T-FRESH", minutes_ago=30)
        stale = self._make_resting_pos(ticker="T-STALE", minutes_ago=200)
        positions = [fresh, stale]
        now = datetime.now(ET)
        actions = self._run(_cancel_stale_resting_orders(positions, mock_client, now))
        assert len(actions) == 1
        assert fresh["status"] == "resting"
        assert stale["status"] == "cancelled"

    @patch("position_monitor.send_discord_alert", new_callable=AsyncMock)
    def test_ignores_non_resting(self, mock_alert, mock_client):
        """Only resting positions are checked — open/closed/settled ignored."""
        from position_monitor import _cancel_stale_resting_orders
        pos = self._make_resting_pos(minutes_ago=200)
        pos["status"] = "open"  # Not resting
        positions = [pos]
        now = datetime.now(ET)
        actions = self._run(_cancel_stale_resting_orders(positions, mock_client, now))
        assert actions == []

    @patch("position_monitor.send_discord_alert", new_callable=AsyncMock)
    def test_notes_appended(self, mock_alert, mock_client):
        """Cancellation note is appended to position notes."""
        from position_monitor import _cancel_stale_resting_orders
        pos = self._make_resting_pos(minutes_ago=150)
        positions = [pos]
        now = datetime.now(ET)
        self._run(_cancel_stale_resting_orders(positions, mock_client, now))
        assert len(pos["notes"]) == 1
        assert "Stale order cancelled" in pos["notes"][0]

    @patch("position_monitor.send_discord_alert", new_callable=AsyncMock)
    def test_discord_alert_sent(self, mock_alert, mock_client):
        """Discord alert sent on stale cancellation."""
        from position_monitor import _cancel_stale_resting_orders
        pos = self._make_resting_pos(minutes_ago=150)
        positions = [pos]
        now = datetime.now(ET)
        self._run(_cancel_stale_resting_orders(positions, mock_client, now))
        mock_alert.assert_called_once()
        alert_title = mock_alert.call_args[0][0]
        assert "STALE" in alert_title

    @patch("position_monitor.send_discord_alert", new_callable=AsyncMock)
    def test_cancel_failure_still_marks_cancelled(self, mock_alert, mock_client):
        """If Kalshi cancel_order fails, position is still marked cancelled locally."""
        from position_monitor import _cancel_stale_resting_orders
        mock_client.cancel_order = AsyncMock(side_effect=Exception("API error"))
        pos = self._make_resting_pos(minutes_ago=150)
        positions = [pos]
        now = datetime.now(ET)
        actions = self._run(_cancel_stale_resting_orders(positions, mock_client, now))
        assert len(actions) == 1
        assert pos["status"] == "cancelled"

    @patch("position_monitor.send_discord_alert", new_callable=AsyncMock)
    def test_missing_entry_time_skipped(self, mock_alert, mock_client):
        """Position with no entry_time is skipped."""
        from position_monitor import _cancel_stale_resting_orders
        pos = {"status": "resting", "ticker": "T1", "order_id": "o1", "notes": []}
        positions = [pos]
        now = datetime.now(ET)
        actions = self._run(_cancel_stale_resting_orders(positions, mock_client, now))
        assert actions == []
        assert pos["status"] == "resting"


# ═══════════════════════════════════════════════════════════════
# DUPLICATE ORDER GUARD (auto_trader.py)
# ═══════════════════════════════════════════════════════════════

class TestDuplicateOrderGuard:
    """Verify that auto_trader skips trades when a resting/open position exists."""

    def test_resting_position_blocks_new_order(self):
        """If a resting position exists for the same ticker, skip the new trade."""
        positions = [
            {"ticker": "KXHIGHNY-26FEB16-B36.5", "status": "resting", "order_id": "o1"},
        ]
        ticker = "KXHIGHNY-26FEB16-B36.5"
        existing = [p for p in positions
                    if p.get("ticker") == ticker and p.get("status") in ("resting", "open")]
        assert len(existing) == 1
        assert existing[0]["status"] == "resting"

    def test_open_position_blocks_new_order(self):
        """If an open position exists for the same ticker, skip."""
        positions = [
            {"ticker": "KXHIGHNY-26FEB16-B36.5", "status": "open"},
        ]
        ticker = "KXHIGHNY-26FEB16-B36.5"
        existing = [p for p in positions
                    if p.get("ticker") == ticker and p.get("status") in ("resting", "open")]
        assert len(existing) == 1

    def test_closed_position_does_not_block(self):
        """Closed/settled positions should NOT block new orders."""
        positions = [
            {"ticker": "KXHIGHNY-26FEB16-B36.5", "status": "closed"},
            {"ticker": "KXHIGHNY-26FEB16-B36.5", "status": "settled"},
            {"ticker": "KXHIGHNY-26FEB16-B36.5", "status": "cancelled"},
        ]
        ticker = "KXHIGHNY-26FEB16-B36.5"
        existing = [p for p in positions
                    if p.get("ticker") == ticker and p.get("status") in ("resting", "open")]
        assert len(existing) == 0

    def test_different_ticker_does_not_block(self):
        """Different ticker resting position should NOT block."""
        positions = [
            {"ticker": "KXHIGHCHI-26FEB16-B28.5", "status": "resting"},
        ]
        ticker = "KXHIGHNY-26FEB16-B36.5"
        existing = [p for p in positions
                    if p.get("ticker") == ticker and p.get("status") in ("resting", "open")]
        assert len(existing) == 0


# ═══════════════════════════════════════════════════════════════
# CONFIG VALIDATION
# ═══════════════════════════════════════════════════════════════

class TestFillMonitorConfig:
    def test_resting_timeout_exists(self):
        from config import RESTING_ORDER_TIMEOUT_MINUTES
        assert isinstance(RESTING_ORDER_TIMEOUT_MINUTES, (int, float))
        assert RESTING_ORDER_TIMEOUT_MINUTES > 0

    def test_resting_timeout_reasonable(self):
        """Timeout should be between 30 min and 24 hours."""
        from config import RESTING_ORDER_TIMEOUT_MINUTES
        assert 30 <= RESTING_ORDER_TIMEOUT_MINUTES <= 1440

    def test_resting_timeout_longer_than_sell_timeout(self):
        """Buy order timeout should be >= sell order timeout (buys are more patient)."""
        from config import RESTING_ORDER_TIMEOUT_MINUTES, PENDING_SELL_EXPIRY_MINUTES
        assert RESTING_ORDER_TIMEOUT_MINUTES >= PENDING_SELL_EXPIRY_MINUTES

    def test_stale_order_event_exists(self):
        from trade_events import TradeEvent
        assert hasattr(TradeEvent, "STALE_ORDER_CANCELLED")
