#!/usr/bin/env python3
"""Tests for execute_trade.py — order verification, fill handling."""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_tmpdir = tempfile.mkdtemp()
_test_positions = Path(_tmpdir) / "positions.json"
_test_lock = Path(_tmpdir) / ".positions.lock"


@pytest.fixture(autouse=True)
def _patch_paths(monkeypatch):
    """Redirect position_store to temp files."""
    import position_store
    monkeypatch.setattr(position_store, "POSITIONS_FILE", _test_positions)
    monkeypatch.setattr(position_store, "LOCK_FILE", _test_lock)
    if _test_positions.exists():
        _test_positions.unlink()
    if _test_lock.exists():
        _test_lock.unlink()
    yield


def _mock_client(order_response=None, balance=100.0, orderbook=None):
    """Create a mock KalshiClient."""
    client = AsyncMock()
    client.start = AsyncMock()
    client.stop = AsyncMock()
    client.get_balance = AsyncMock(return_value=balance)
    client.get_orderbook = AsyncMock(return_value=orderbook or {"yes": [], "no": []})
    client.place_order = AsyncMock(return_value=order_response)
    return client


class TestExecuteAutoVerification:
    """execute_auto() order verification before position registration."""

    def test_successful_order_registers_position(self):
        """Good order with valid order_id → position registered."""
        from execute_trade import execute_auto
        from position_store import load_positions

        client = _mock_client(
            order_response={"order": {"order_id": "abc123", "status": "resting"}},
            balance=100.0,
        )

        with patch("execute_trade.send_discord_confirmation", new_callable=AsyncMock):
            with patch("execute_trade.send_discord_alert", new_callable=AsyncMock):
                result = asyncio.run(execute_auto("TICKER1", "yes", 25, 5, client=client))

        assert result["success"] is True
        assert result["order_id"] == "abc123"
        positions = load_positions()
        assert len(positions) == 1
        assert positions[0]["ticker"] == "TICKER1"

    def test_missing_order_id_does_not_register(self):
        """Order response without order_id → position NOT registered."""
        from execute_trade import execute_auto
        from position_store import load_positions

        client = _mock_client(
            order_response={"order": {"status": "unknown"}},  # No order_id
            balance=100.0,
        )

        with patch("execute_trade.send_discord_confirmation", new_callable=AsyncMock):
            with patch("execute_trade.send_discord_alert", new_callable=AsyncMock) as mock_alert:
                result = asyncio.run(execute_auto("TICKER1", "yes", 25, 5, client=client))

        assert result["success"] is False
        assert "missing order_id" in result["error"]
        positions = load_positions()
        assert len(positions) == 0
        # Should have sent alert
        mock_alert.assert_called_once()

    def test_rejected_order_does_not_register(self):
        """Kalshi rejects order → position NOT registered."""
        from execute_trade import execute_auto
        from position_store import load_positions

        client = _mock_client(
            order_response={"order": {"order_id": "abc123", "status": "rejected"}},
            balance=100.0,
        )

        with patch("execute_trade.send_discord_confirmation", new_callable=AsyncMock):
            with patch("execute_trade.send_discord_alert", new_callable=AsyncMock):
                result = asyncio.run(execute_auto("TICKER1", "yes", 25, 5, client=client))

        assert result["success"] is False
        assert "rejected" in result["error"].lower()
        positions = load_positions()
        assert len(positions) == 0

    def test_empty_result_does_not_register(self):
        """place_order returns None → position NOT registered."""
        from execute_trade import execute_auto
        from position_store import load_positions

        client = _mock_client(order_response=None, balance=100.0)

        with patch("execute_trade.send_discord_confirmation", new_callable=AsyncMock):
            result = asyncio.run(execute_auto("TICKER1", "yes", 25, 5, client=client))

        assert result["success"] is False
        positions = load_positions()
        assert len(positions) == 0

    def test_insufficient_balance_rejects(self):
        """Not enough balance → order never placed."""
        from execute_trade import execute_auto

        client = _mock_client(balance=1.0)  # Only $1

        result = asyncio.run(execute_auto("TICKER1", "yes", 25, 100, client=client))
        assert result["success"] is False
        assert "balance" in result["error"].lower() or "NLV" in result["error"]
        # place_order should NOT have been called
        client.place_order.assert_not_called()

    def test_invalid_side_rejects(self):
        """Invalid side → immediate rejection."""
        from execute_trade import execute_auto
        result = asyncio.run(execute_auto("TICKER1", "maybe", 25, 5))
        assert result["success"] is False
        assert "side" in result["error"].lower()

    def test_price_too_high_rejects(self):
        """Price above MAX_ENTRY_PRICE → rejection."""
        from execute_trade import execute_auto
        result = asyncio.run(execute_auto("TICKER1", "yes", 99, 5))
        assert result["success"] is False
        assert "MAX" in result["error"]

    def test_na_order_id_does_not_register(self):
        """Literal 'N/A' order_id → treated as missing, position NOT registered."""
        from execute_trade import execute_auto
        from position_store import load_positions

        client = _mock_client(
            order_response={"order": {"order_id": "N/A", "status": "resting"}},
            balance=100.0,
        )

        with patch("execute_trade.send_discord_confirmation", new_callable=AsyncMock):
            with patch("execute_trade.send_discord_alert", new_callable=AsyncMock):
                result = asyncio.run(execute_auto("TICKER1", "yes", 25, 5, client=client))

        assert result["success"] is False
        assert "missing order_id" in result["error"]
        assert len(load_positions()) == 0

    def test_register_failure_sends_orphan_alert(self):
        """register_position fails → orphaned order Discord alert sent."""
        from execute_trade import execute_auto

        client = _mock_client(
            order_response={"order": {"order_id": "abc123", "status": "executed"}},
            balance=100.0,
        )

        with patch("execute_trade.register_position", side_effect=Exception("DB error")):
            with patch("execute_trade.send_discord_confirmation", new_callable=AsyncMock):
                with patch("execute_trade.send_discord_alert", new_callable=AsyncMock) as mock_alert:
                    result = asyncio.run(execute_auto("TICKER1", "yes", 25, 5, client=client))

        # Order succeeded even though registration failed
        assert result["success"] is True
        # Orphaned order alert should have been sent
        mock_alert.assert_called_once()
        call_kwargs = mock_alert.call_args
        assert "ORPHANED" in call_kwargs.kwargs.get("title", "") or "ORPHANED" in str(call_kwargs)


class TestIsPaperRegistration:
    """register_order records is_paper based on the active broker's mode."""

    def _run_with_mode(self, mode):
        from execute_trade import execute_auto

        client = _mock_client(
            order_response={"order": {"order_id": "abc123", "status": "resting"}},
            balance=100.0,
        )
        client.mode = mode

        mock_db = MagicMock()
        with patch("execute_trade.get_db", return_value=mock_db):
            with patch("execute_trade.send_discord_confirmation", new_callable=AsyncMock):
                result = asyncio.run(execute_auto("TICKER1", "yes", 25, 5, client=client))

        assert result["success"] is True
        return mock_db.register_order.call_args.kwargs

    def test_paper_broker_records_is_paper_true(self):
        """PaperBroker (mode='paper') → order registered with is_paper=True."""
        kwargs = self._run_with_mode("paper")
        assert kwargs["is_paper"] is True

    def test_live_broker_records_is_paper_false(self):
        """Live broker (mode='live') → order registered with is_paper=False."""
        kwargs = self._run_with_mode("live")
        assert kwargs["is_paper"] is False


class TestStrategyAttribution:
    """strategy= threads through to place_order and register_position."""

    def _run(self, **extra):
        from execute_trade import execute_auto

        client = _mock_client(
            order_response={"order": {"order_id": "abc123", "status": "executed"}},
            balance=100.0,
        )
        with patch("execute_trade.send_discord_confirmation", new_callable=AsyncMock):
            with patch("execute_trade.send_discord_alert", new_callable=AsyncMock):
                result = asyncio.run(
                    execute_auto("TICKER1", "yes", 25, 5, client=client, **extra)
                )
        assert result["success"] is True
        return client

    def test_execute_auto_tags_position_with_strategy(self):
        from position_store import load_positions
        client = self._run(strategy="peak_trader")
        positions = load_positions()
        assert positions[0]["strategy"] == "peak_trader"
        # Broker receives the tag too (paper order ledger attribution)
        assert client.place_order.await_args.kwargs["strategy"] == "peak_trader"

    def test_execute_auto_defaults_to_untagged(self):
        from position_store import load_positions
        self._run()
        positions = load_positions()
        assert positions[0]["strategy"] == "untagged"
