#!/usr/bin/env python3
"""Tests for exit strategy refinements: smart pegging, scaled trailing,
quantitative settlement override, and re-entry logic."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

ET = ZoneInfo("America/New_York")


# ═══════════════════════════════════════════════════════════════
# SMART SELL PEGGING
# ═══════════════════════════════════════════════════════════════

from position_monitor import _smart_sell_price, _extract_sell_prices


class TestSmartSellPrice:
    def test_urgent_uses_bid(self):
        """Urgent sells always hit the bid."""
        assert _smart_sell_price(bid=40, ask=48, spread=8, urgent=True) == 40

    def test_non_urgent_pegs_ask_minus_one(self):
        """Non-urgent sells peg to ask-1 when spread >= 3."""
        assert _smart_sell_price(bid=40, ask=48, spread=8, urgent=False) == 47

    def test_non_urgent_tight_spread_uses_bid(self):
        """Non-urgent sells with tight spread (< 3) use bid."""
        assert _smart_sell_price(bid=40, ask=42, spread=2, urgent=False) == 40

    def test_zero_bid_returns_zero(self):
        """Zero bid means no liquidity — return 0."""
        assert _smart_sell_price(bid=0, ask=5, spread=5, urgent=False) == 0

    def test_ask_minus_one_never_below_bid(self):
        """ask-1 should never return below bid."""
        assert _smart_sell_price(bid=45, ask=46, spread=1, urgent=False) == 45

    def test_spread_exactly_3(self):
        """Spread of exactly 3 triggers ask-peg."""
        assert _smart_sell_price(bid=40, ask=43, spread=3, urgent=False) == 42


class TestExtractSellPrices:
    def test_yes_side_bid_and_ask(self):
        """YES side: bid from yes bids, ask derived from no bids."""
        ob = {
            "yes": [[40, 5], [38, 10]],
            "no": [[55, 3]],  # no_bid=55 → yes_ask = 100-55 = 45
        }
        bid, ask, spread = _extract_sell_prices(ob, "yes")
        assert bid == 40
        assert ask == 45
        assert spread == 5

    def test_yes_ask_uses_best_no_bid(self):
        """Regression: yes_ask must derive from the HIGHEST NO bid.

        The old code used min(no_bids), inflating the ask and pushing
        non-urgent ask-peg sells above the true best offer.
        """
        ob = {
            "yes": [[40, 5]],
            "no": [[55, 3], [58, 2]],  # best NO bid=58 → yes_ask = 42
        }
        bid, ask, spread = _extract_sell_prices(ob, "yes")
        assert ask == 42
        assert spread == 2

    def test_no_side_bid_and_ask(self):
        """NO side: bid from no bids, ask derived from yes bids."""
        ob = {
            "yes": [[40, 5]],   # yes_bid=40 → no_ask = 100-40 = 60
            "no": [[55, 3], [50, 10]],
        }
        bid, ask, spread = _extract_sell_prices(ob, "no")
        assert bid == 55
        assert ask == 60
        assert spread == 5

    def test_empty_orderbook(self):
        """Empty orderbook returns zeros."""
        bid, ask, spread = _extract_sell_prices({}, "yes")
        assert bid == 0 and ask == 0 and spread == 0

    def test_no_side_fallback_from_yes_ask(self):
        """NO bid derived from YES asks when no NO bids."""
        ob = {
            "yes": [[40, 5]],  # NO bid via fallback = 100-40=60? No — yes_bid → NO ask
            "no": [],
        }
        # When no NO bids, fallback: bid from yes asks (100 - min(yes_ask))
        # But we only have yes bids, not asks. The fallback path is:
        # `elif orderbook.get("yes"):` → asks = yes entries → bid = 100 - min(price)
        bid, ask, spread = _extract_sell_prices(ob, "no")
        assert bid == 60  # 100 - 40


# ═══════════════════════════════════════════════════════════════
# SCALED TRAILING STOP
# ═══════════════════════════════════════════════════════════════

from position_monitor import _trailing_offset_for_price


class TestScaledTrailing:
    def test_low_price_wide_offset(self):
        """Prices 0-20 get 10¢ offset."""
        assert _trailing_offset_for_price(15) == 10

    def test_low_mid_price(self):
        """Prices 20-40 get 7¢ offset."""
        assert _trailing_offset_for_price(30) == 7

    def test_mid_price(self):
        """Prices 40-60 get 6¢ offset."""
        assert _trailing_offset_for_price(50) == 6

    def test_high_price(self):
        """Prices 60-80 get 5¢ offset."""
        assert _trailing_offset_for_price(70) == 5

    def test_near_certain(self):
        """Prices 80-100 get 3¢ offset."""
        assert _trailing_offset_for_price(90) == 3

    def test_boundary_20(self):
        """Price 20 falls in [20,40) zone (7¢)."""
        assert _trailing_offset_for_price(20) == 7

    def test_boundary_80(self):
        """Price 80 falls in [80,100) zone (3¢)."""
        assert _trailing_offset_for_price(80) == 3

    def test_out_of_range_fallback(self):
        """Price >= 100 falls back to TRAILING_OFFSET_CENTS."""
        from config import TRAILING_OFFSET_CENTS
        assert _trailing_offset_for_price(105) == TRAILING_OFFSET_CENTS


# ═══════════════════════════════════════════════════════════════
# RE-ENTRY LOGIC
# ═══════════════════════════════════════════════════════════════

from auto_trader import _find_reentry_candidates
from dataclasses import dataclass, field


@dataclass
class MockOpp:
    """Minimal Opportunity mock for testing re-entry logic."""
    ticker: str = "KXHIGHNY-26FEB14-B36.5"
    confidence_score: float = 92.0
    city: str = "NYC"
    bracket_title: str = "36-37°F"
    low: float = 36.0
    high: float = 38.0
    side: str = "yes"
    yes_bid: int = 35
    yes_ask: int = 40
    suggested_contracts: int = 5
    kde_prob: float = 0.5
    edge_after_fees: float = 0.10
    confidence: str = "HIGH"
    strategies: list = field(default_factory=list)
    rationale: str = ""
    entry_window: str = ""
    volume: int = 100
    histogram_prob: float = 0.5
    weighted_prob: float = 0.5
    edge_raw: float = 0.15
    kelly: float = 0.05


class TestReentryLogic:
    @pytest.fixture(autouse=True)
    def _disable_trade_score(self, monkeypatch):
        """These tests target the legacy confidence gate; disable trade score."""
        monkeypatch.setattr("auto_trader.TRADE_SCORE_ENABLED", False)

    def _make_exited_pos(self, ticker="KXHIGHNY-26FEB14-B36.5", minutes_ago=60):
        now = datetime.now(ET)
        exit_time = (now - timedelta(minutes=minutes_ago)).isoformat()
        return {
            "ticker": ticker,
            "status": "closed",
            "side": "yes",
            "avg_price": 30,
            "contracts": 0,
            "sell_placed_at": exit_time,
            "notes": [
                f"{exit_time}: TRAILING STOP sell placed at 45c"
            ],
        }

    def test_finds_valid_reentry(self):
        """Recently trailing-stop exited + scanner still confident → re-enter."""
        pos = self._make_exited_pos(minutes_ago=60)
        opp = MockOpp(confidence_score=92.0)
        candidates = _find_reentry_candidates([pos], [opp], datetime.now(ET))
        assert len(candidates) == 1
        assert candidates[0][0] is opp
        assert candidates[0][1] is pos

    def test_no_reentry_if_low_confidence(self):
        """Scanner confidence below REENTRY_MIN_CONFIDENCE → no re-entry."""
        pos = self._make_exited_pos()
        opp = MockOpp(confidence_score=75.0)  # Below 90 threshold
        candidates = _find_reentry_candidates([pos], [opp], datetime.now(ET))
        assert len(candidates) == 0

    def test_no_reentry_if_too_recent(self):
        """Exited less than REENTRY_COOLDOWN_MINUTES ago → too soon."""
        pos = self._make_exited_pos(minutes_ago=5)  # 5 min < 30 min cooldown
        opp = MockOpp(confidence_score=95.0)
        candidates = _find_reentry_candidates([pos], [opp], datetime.now(ET))
        assert len(candidates) == 0

    def test_no_reentry_after_thesis_break(self):
        """Thesis break exit → no re-entry (thesis was wrong, not just volatility)."""
        pos = self._make_exited_pos(minutes_ago=60)
        pos["notes"].append("THESIS BREAK sell at 15c (conf=30)")
        opp = MockOpp(confidence_score=92.0)
        candidates = _find_reentry_candidates([pos], [opp], datetime.now(ET))
        assert len(candidates) == 0

    def test_no_reentry_after_roi_backstop(self):
        """ROI backstop exit → no re-entry."""
        pos = self._make_exited_pos(minutes_ago=60)
        pos["notes"].append("ROI BACKSTOP sell at 10c (-55% ROI)")
        opp = MockOpp(confidence_score=92.0)
        candidates = _find_reentry_candidates([pos], [opp], datetime.now(ET))
        assert len(candidates) == 0

    def test_no_reentry_open_position(self):
        """Open positions are not re-entry candidates."""
        pos = self._make_exited_pos(minutes_ago=60)
        pos["status"] = "open"
        opp = MockOpp(confidence_score=95.0)
        candidates = _find_reentry_candidates([pos], [opp], datetime.now(ET))
        assert len(candidates) == 0

    def test_no_reentry_different_ticker(self):
        """Exited ticker doesn't match scanner opp ticker → no re-entry."""
        pos = self._make_exited_pos(minutes_ago=60)
        pos["ticker"] = "KXHIGHDEN-26FEB14-B30.5"
        opp = MockOpp(ticker="KXHIGHNY-26FEB14-B36.5", confidence_score=95.0)
        candidates = _find_reentry_candidates([pos], [opp], datetime.now(ET))
        assert len(candidates) == 0

    def test_daily_cap_respected(self):
        """Max REENTRY_MAX_PER_TICKER_PER_DAY re-entries per ticker per day."""
        now = datetime.now(ET)
        pos = self._make_exited_pos(minutes_ago=60)
        today_str = now.strftime("%Y-%m-%d")
        pos["notes"].append(f"{today_str}T14:00:00: RE-ENTRY at 35c")
        opp = MockOpp(confidence_score=95.0)
        candidates = _find_reentry_candidates([pos], [opp], now)
        assert len(candidates) == 0  # Already re-entered once today

    def test_empty_opps_returns_empty(self):
        """No scanner opps → no candidates."""
        pos = self._make_exited_pos()
        candidates = _find_reentry_candidates([pos], [], datetime.now(ET))
        assert len(candidates) == 0
