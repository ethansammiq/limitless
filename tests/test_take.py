"""Tests for scripts/take.py guards (no network, no orders)."""
import argparse

from scripts.take import order_cost_dollars, summarize_order_fills, validate


def _args(action="buy", side="yes", count=10, price_c=20):
    return argparse.Namespace(action=action, side=side, count=count, price_c=price_c)


class TestCostModel:
    def test_buy_yes_costs_price(self):
        assert order_cost_dollars("buy", "yes", 40, 16) == 6.40

    def test_sell_collateralizes_complement(self):
        # selling YES at 22c: worst case is the 78c complement per contract
        assert order_cost_dollars("sell", "yes", 20, 22) == 15.60


class TestValidate:
    def test_ok_order_passes(self):
        assert validate(_args(), 50.0) is None

    def test_price_bounds(self):
        assert "outside" in validate(_args(price_c=0), 50.0)
        assert "outside" in validate(_args(price_c=100), 50.0)

    def test_count_floor(self):
        assert "count" in validate(_args(count=0), 50.0)

    def test_notional_cap(self):
        # 500 x 90c = $450 > $50 cap
        assert "exceeds cap" in validate(_args(count=500, price_c=90), 50.0)

    def test_cap_override(self):
        assert validate(_args(count=500, price_c=90), 1000.0) is None


class TestSummarizeOrderFills:
    """place_order can report status=resting for an IOC that filled nothing
    (2026-07-10, two live orders) — fills are the ground truth."""

    FILLS = [
        {"order_id": "A", "count_fp": "50.00", "yes_price_dollars": "0.62"},
        {"order_id": "A", "count_fp": "44.00", "yes_price_dollars": "0.68"},
        {"order_id": "B", "count_fp": "6.00", "yes_price": 69},
    ]

    def test_sums_only_this_order(self):
        filled, avg = summarize_order_fills(self.FILLS, "A")
        assert filled == 94
        assert 62 < avg < 68

    def test_integer_cent_fills(self):
        filled, avg = summarize_order_fills(self.FILLS, "B")
        assert filled == 6
        assert avg == 69

    def test_no_fills_is_zero(self):
        assert summarize_order_fills(self.FILLS, "Z") == (0, 0.0)
        assert summarize_order_fills([], "A") == (0, 0.0)
        assert summarize_order_fills(None, "A") == (0, 0.0)
