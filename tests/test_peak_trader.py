"""Tests for peak_trader.py — Strategy G: Peak → Trade Pipeline."""

import asyncio

import pytest
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock, MagicMock

from peak_trader import compute_peak_trade, _hours_until_settlement


# ─── Helpers ───────────────────────────────────────────


def _bracket(bid=70, ask=80, ticker="KXHIGHNY-26FEB13-B72.5", title="72° to 73°F", volume=500):
    """Create a mock bracket_info dict."""
    return {
        "yes_bid": bid,
        "yes_ask": ask,
        "ticker": ticker,
        "title": title,
        "volume": volume,
    }


# ─── Test: compute_peak_trade ─────────────────────────


class TestComputePeakTrade:
    """Core peak trade evaluation logic."""

    def test_basic_trade_passes(self, monkeypatch):
        """Standard case: 70¢ bid → 25¢ edge → should trade."""
        monkeypatch.setattr("peak_trader.PEAK_TRADE_ENABLED", True)
        monkeypatch.setattr("peak_trader._hours_until_settlement", lambda: 5.0)

        result = compute_peak_trade(_bracket(bid=70), balance=100.0)
        assert result["execute"] is True
        assert result["edge_cents"] == 25  # 95 - 70
        assert result["entry_price"] == 71  # bid+1
        assert result["contracts"] > 0
        assert result["side"] == "yes"

    def test_disabled_flag(self, monkeypatch):
        """Feature flag off → never execute."""
        monkeypatch.setattr("peak_trader.PEAK_TRADE_ENABLED", False)
        result = compute_peak_trade(_bracket(bid=50), balance=100.0)
        assert result["execute"] is False
        assert "PEAK_TRADE_ENABLED" in result["reason"]

    def test_insufficient_edge(self, monkeypatch):
        """Bid at 88¢ → edge = 7¢ < 10¢ min → no trade."""
        monkeypatch.setattr("peak_trader.PEAK_TRADE_ENABLED", True)
        monkeypatch.setattr("peak_trader._hours_until_settlement", lambda: 5.0)
        result = compute_peak_trade(_bracket(bid=88), balance=100.0)
        assert result["execute"] is False
        assert "Edge" in result["reason"]

    def test_price_too_high(self, monkeypatch):
        """Bid at 86¢ → exceeds PEAK_TRADE_MAX_PRICE_CENTS (85)."""
        monkeypatch.setattr("peak_trader.PEAK_TRADE_ENABLED", True)
        monkeypatch.setattr("peak_trader.PEAK_TRADE_MAX_PRICE_CENTS", 85)
        monkeypatch.setattr("peak_trader._hours_until_settlement", lambda: 5.0)
        # 86¢ bid → edge = 9¢ (fails edge) and also >= max price
        result = compute_peak_trade(_bracket(bid=86), balance=100.0)
        assert result["execute"] is False

    def test_too_close_to_settlement(self, monkeypatch):
        """Only 0.5h to settlement → no trade."""
        monkeypatch.setattr("peak_trader.PEAK_TRADE_ENABLED", True)
        monkeypatch.setattr("peak_trader._hours_until_settlement", lambda: 0.5)
        result = compute_peak_trade(_bracket(bid=70), balance=100.0)
        assert result["execute"] is False
        assert "settlement" in result["reason"].lower()

    def test_zero_balance(self, monkeypatch):
        """Zero balance → no trade."""
        monkeypatch.setattr("peak_trader.PEAK_TRADE_ENABLED", True)
        monkeypatch.setattr("peak_trader._hours_until_settlement", lambda: 5.0)
        result = compute_peak_trade(_bracket(bid=70), balance=0.0)
        assert result["execute"] is False
        assert "balance" in result["reason"].lower()

    def test_no_ticker(self, monkeypatch):
        """Empty ticker → no trade."""
        monkeypatch.setattr("peak_trader.PEAK_TRADE_ENABLED", True)
        monkeypatch.setattr("peak_trader._hours_until_settlement", lambda: 5.0)
        result = compute_peak_trade(_bracket(ticker=""), balance=100.0)
        assert result["execute"] is False

    def test_contract_sizing(self, monkeypatch):
        """Contracts should be min(MAX_CONTRACTS, budget / entry_price)."""
        monkeypatch.setattr("peak_trader.PEAK_TRADE_ENABLED", True)
        monkeypatch.setattr("peak_trader._hours_until_settlement", lambda: 5.0)
        monkeypatch.setattr("peak_trader.MAX_POSITION_PCT", 0.10)
        monkeypatch.setattr("peak_trader.PEAK_TRADE_MAX_CONTRACTS", 20)

        result = compute_peak_trade(_bracket(bid=50), balance=100.0)
        assert result["execute"] is True
        # Budget: $100 * 0.10 = $10.00
        # Entry: 51¢ → 10.00 / 0.51 ≈ 19 contracts
        assert result["contracts"] == 19
        assert result["entry_price"] == 51

    def test_max_contracts_cap(self, monkeypatch):
        """Large balance → capped at PEAK_TRADE_MAX_CONTRACTS."""
        monkeypatch.setattr("peak_trader.PEAK_TRADE_ENABLED", True)
        monkeypatch.setattr("peak_trader._hours_until_settlement", lambda: 5.0)
        monkeypatch.setattr("peak_trader.MAX_POSITION_PCT", 0.50)  # 50% for test
        monkeypatch.setattr("peak_trader.PEAK_TRADE_MAX_CONTRACTS", 20)

        result = compute_peak_trade(_bracket(bid=50), balance=10000.0)
        assert result["execute"] is True
        assert result["contracts"] == 20  # Capped

    def test_entry_price_capped_at_max(self, monkeypatch):
        """Entry price = bid+1, but capped at PEAK_TRADE_MAX_PRICE_CENTS."""
        monkeypatch.setattr("peak_trader.PEAK_TRADE_ENABLED", True)
        monkeypatch.setattr("peak_trader._hours_until_settlement", lambda: 5.0)
        monkeypatch.setattr("peak_trader.PEAK_TRADE_MAX_PRICE_CENTS", 85)

        result = compute_peak_trade(_bracket(bid=84), balance=100.0)
        assert result["execute"] is True
        assert result["entry_price"] == 85  # min(84+1, 85) = 85

    def test_edge_boundary_exactly_10(self, monkeypatch):
        """Bid at 85¢ → edge = 10¢ exactly → should pass (>= threshold)."""
        monkeypatch.setattr("peak_trader.PEAK_TRADE_ENABLED", True)
        monkeypatch.setattr("peak_trader._hours_until_settlement", lambda: 5.0)
        monkeypatch.setattr("peak_trader.PEAK_TRADE_MIN_EDGE_CENTS", 10)
        monkeypatch.setattr("peak_trader.PEAK_TRADE_MAX_PRICE_CENTS", 86)

        result = compute_peak_trade(_bracket(bid=85), balance=100.0)
        # edge = 95-85 = 10 >= 10 → passes. bid < max_price → passes
        assert result["execute"] is True

    def test_edge_boundary_just_below(self, monkeypatch):
        """Bid at 86¢ → edge = 9¢ < 10¢ → should fail."""
        monkeypatch.setattr("peak_trader.PEAK_TRADE_ENABLED", True)
        monkeypatch.setattr("peak_trader._hours_until_settlement", lambda: 5.0)
        monkeypatch.setattr("peak_trader.PEAK_TRADE_MIN_EDGE_CENTS", 10)

        result = compute_peak_trade(_bracket(bid=86), balance=100.0)
        assert result["execute"] is False

    def test_low_bid_huge_edge(self, monkeypatch):
        """Bid at 20¢ → edge = 75¢ → great trade."""
        monkeypatch.setattr("peak_trader.PEAK_TRADE_ENABLED", True)
        monkeypatch.setattr("peak_trader._hours_until_settlement", lambda: 5.0)

        result = compute_peak_trade(_bracket(bid=20), balance=100.0)
        assert result["execute"] is True
        assert result["edge_cents"] == 75
        assert result["entry_price"] == 21


class TestEdgeCases:
    """Edge cases and boundary conditions."""

    def test_cost_calculation(self, monkeypatch):
        """Cost = (entry_price / 100) * contracts."""
        monkeypatch.setattr("peak_trader.PEAK_TRADE_ENABLED", True)
        monkeypatch.setattr("peak_trader._hours_until_settlement", lambda: 5.0)
        monkeypatch.setattr("peak_trader.MAX_POSITION_PCT", 0.10)
        monkeypatch.setattr("peak_trader.PEAK_TRADE_MAX_CONTRACTS", 100)

        result = compute_peak_trade(_bracket(bid=50), balance=100.0)
        assert result["execute"] is True
        expected_cost = (result["entry_price"] / 100) * result["contracts"]
        assert abs(result["cost"] - expected_cost) < 0.01

    def test_hours_to_settlement_field(self, monkeypatch):
        """Result includes hours_to_settlement."""
        monkeypatch.setattr("peak_trader.PEAK_TRADE_ENABLED", True)
        monkeypatch.setattr("peak_trader._hours_until_settlement", lambda: 3.5)

        result = compute_peak_trade(_bracket(bid=70), balance=100.0)
        assert result["hours_to_settlement"] == 3.5

    def test_reason_contains_info(self, monkeypatch):
        """Reason string contains trade details."""
        monkeypatch.setattr("peak_trader.PEAK_TRADE_ENABLED", True)
        monkeypatch.setattr("peak_trader._hours_until_settlement", lambda: 5.0)

        result = compute_peak_trade(_bracket(bid=70), balance=100.0)
        assert "edge=" in result["reason"]
        assert "settle" in result["reason"]


class TestExecutePeakTrade:
    """Test the full async execution pipeline (mocked)."""

    def test_dry_run_no_real_execution(self, monkeypatch):
        """Dry run sends Discord alert but doesn't call execute_auto."""
        monkeypatch.setattr("peak_trader.PEAK_TRADE_ENABLED", True)
        monkeypatch.setattr("peak_trader._hours_until_settlement", lambda: 5.0)

        # Mock balance fetch — execute_peak_trade does `from kalshi_client import
        # fetch_balance_quick` at call time, so the mock must target the source
        # module (kalshi_client), not peak_trader's namespace.
        mock_balance = AsyncMock(return_value=100.0)
        monkeypatch.setattr("kalshi_client.fetch_balance_quick", mock_balance, raising=False)

        # Import at module level causes issues, so we mock the full import chain
        import peak_trader
        monkeypatch.setattr(peak_trader, "check_kill_switch",
                            lambda: (True, "OK"), raising=False)

        # Mock imports inside execute_peak_trade
        mock_kill = MagicMock(return_value=(True, "OK"))
        monkeypatch.setattr("trading_guards.check_kill_switch", mock_kill, raising=False)

        mock_discord = AsyncMock()
        monkeypatch.setattr("notifications.send_discord_alert", mock_discord, raising=False)

        mock_positions = MagicMock(return_value=[])
        monkeypatch.setattr("position_store.load_positions", mock_positions, raising=False)

        from peak_trader import execute_peak_trade
        result = asyncio.run(execute_peak_trade(
            city_key="NYC",
            peak_temp=73.5,
            bracket_info=_bracket(bid=70),
            dry_run=True,
        ))
        assert result["success"] is True
        assert "DRY RUN" in result["reason"]

    def test_kill_switch_blocks(self, monkeypatch):
        """Kill switch active → trade blocked."""
        from peak_trader import execute_peak_trade

        monkeypatch.setattr("trading_guards.check_kill_switch",
                            lambda: (False, "Kill switch active"), raising=False)

        result = asyncio.run(execute_peak_trade(
            city_key="NYC",
            peak_temp=73.5,
            bracket_info=_bracket(bid=70),
        ))
        assert result["success"] is False
        assert "Kill switch" in result["reason"]
