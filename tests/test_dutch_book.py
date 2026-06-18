"""Tests for dutch_book.py — bracket-sum Dutch-book detection."""

import math

from dutch_book import (
    check_dutch_book,
    format_dutch_book_alerts,
    kalshi_taker_fee_cents,
    _ladder_legs,
)


# ─── Helpers ───────────────────────────────────────────


def _mkt(ticker: str, title: str, yes_bid: int, yes_ask: int,
         event: str = "KXHIGHNY-26JUN13") -> dict:
    return {
        "ticker": ticker,
        "event_ticker": event,
        "title": title,
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
    }


def _ladder(bids: list[int], asks: list[int], event: str = "KXHIGHNY-26JUN13") -> list[dict]:
    """Exhaustive 4-leg ladder: low tail, two ranges, high tail."""
    titles = ["61° or below", "62° to 63°", "64° to 65°", "66° or above"]
    tickers = [f"{event}-T61", f"{event}-B62.5", f"{event}-B64.5", f"{event}-T66"]
    return [
        _mkt(t, title, b, a, event)
        for t, title, b, a in zip(tickers, titles, bids, asks)
    ]


# ─── Fee math ──────────────────────────────────────────


class TestFeeFunction:
    """ceil(0.07 * P * (100 - P) / 100) cents."""

    def test_midpoint_rounds_up(self):
        # 0.07 * 50 * 50 / 100 = 1.75 → 2
        assert kalshi_taker_fee_cents(50) == 2

    def test_extreme_prices_round_up_to_one(self):
        # 0.07 * 1 * 99 / 100 = 0.0693 → 1
        assert kalshi_taker_fee_cents(1) == 1
        assert kalshi_taker_fee_cents(99) == 1

    def test_symmetry(self):
        for p in (5, 20, 35, 50):
            assert kalshi_taker_fee_cents(p) == kalshi_taker_fee_cents(100 - p)

    def test_matches_formula(self):
        for p in range(1, 100):
            assert kalshi_taker_fee_cents(p) == math.ceil(0.07 * p * (100 - p) / 100)


# ─── Ladder preconditions ──────────────────────────────


class TestLadderPreconditions:
    """No arb is ever reported from a non-exhaustive or one-sided ladder."""

    # Prices absurd enough that sums would trip both directions if the
    # precondition check were skipped.
    HOT_BIDS = [40, 40, 40, 40]
    HOT_ASKS = [41, 41, 41, 41]

    def test_valid_ladder_accepted(self):
        legs = _ladder_legs(_ladder([20, 40, 35, 15], [22, 42, 37, 17]))
        assert legs is not None
        assert [m["ticker"].rsplit("-", 1)[1] for m in legs] == ["T61", "B62.5", "B64.5", "T66"]

    def test_missing_low_tail_rejected(self):
        markets = _ladder(self.HOT_BIDS, self.HOT_ASKS)[1:]  # drop "61 or below"
        assert _ladder_legs(markets) is None
        assert check_dutch_book(markets) == []

    def test_missing_high_tail_rejected(self):
        markets = _ladder(self.HOT_BIDS, self.HOT_ASKS)[:-1]
        assert _ladder_legs(markets) is None
        assert check_dutch_book(markets) == []

    def test_duplicate_low_tail_rejected(self):
        markets = _ladder(self.HOT_BIDS, self.HOT_ASKS)
        markets.append(_mkt("KXHIGHNY-26JUN13-T59", "59° or below", 40, 41))
        assert _ladder_legs(markets) is None
        assert check_dutch_book(markets) == []

    def test_gap_rejected(self):
        markets = _ladder(self.HOT_BIDS, self.HOT_ASKS)
        markets[2]["title"] = "65° to 66°"  # leaves 64 uncovered
        assert _ladder_legs(markets) is None
        assert check_dutch_book(markets) == []

    def test_overlap_rejected(self):
        markets = _ladder(self.HOT_BIDS, self.HOT_ASKS)
        markets[2]["title"] = "63° to 65°"  # 63 covered twice
        assert _ladder_legs(markets) is None
        assert check_dutch_book(markets) == []

    def test_unparseable_title_rejected(self):
        markets = _ladder(self.HOT_BIDS, self.HOT_ASKS)
        markets[1]["title"] = "something weird"
        assert _ladder_legs(markets) is None
        assert check_dutch_book(markets) == []

    def test_one_sided_quotes_rejected(self):
        for field, value in (("yes_bid", 0), ("yes_ask", 0), ("yes_ask", 100)):
            markets = _ladder(self.HOT_BIDS, self.HOT_ASKS)
            markets[1][field] = value
            assert _ladder_legs(markets) is None, f"{field}={value} should fail two-sided check"
            assert check_dutch_book(markets) == []

    def test_single_market_rejected(self):
        assert _ladder_legs([_mkt("KXHIGHNY-26JUN13-T61", "61° or below", 40, 41)]) is None

    def test_empty_input(self):
        assert check_dutch_book([]) == []


# ─── NO basket ─────────────────────────────────────────


class TestNoBasketArb:
    """sum(yes_bid) > 100 + fees → buy NO every leg at (100 - yes_bid)."""

    def test_detects_no_basket_with_fee_math(self):
        # bids sum to 110; NO prices 80/60/65/85 → fees 2+2+2+1 = 7
        markets = _ladder([20, 40, 35, 15], [22, 42, 37, 17])
        arbs = check_dutch_book(markets)
        assert len(arbs) == 1
        arb = arbs[0]
        assert arb.side == "no"
        assert arb.event_ticker == "KXHIGHNY-26JUN13"
        assert arb.sum_cents == 110
        assert arb.total_fee_cents == 7
        assert arb.profit_cents == 110 - 100 - 7 == 3
        assert [leg.price_cents for leg in arb.legs] == [80, 60, 65, 85]
        assert [leg.fee_cents for leg in arb.legs] == [2, 2, 2, 1]
        assert all(leg.side == "no" for leg in arb.legs)
        assert len(arb.legs) == 4

    def test_sum_above_100_but_below_fees_is_not_arb(self):
        # bids sum to 104; NO price 74 each → fee 2 each = 8 > 4 gross
        markets = _ladder([26, 26, 26, 26], [28, 28, 28, 28])
        assert check_dutch_book(markets) == []

    def test_custom_fee_fn(self):
        markets = _ladder([26, 26, 26, 26], [28, 28, 28, 28])
        arbs = check_dutch_book(markets, fee_fn=lambda p: 0)
        assert len(arbs) == 1
        assert arbs[0].side == "no"
        assert arbs[0].profit_cents == 4


# ─── YES basket ────────────────────────────────────────


class TestYesBasketArb:
    """sum(yes_ask) < 100 - fees → buy YES every leg at yes_ask."""

    def test_detects_yes_basket_with_fee_math(self):
        # asks sum to 80 → fees 1+2+2+1 = 6; profit 100 - 80 - 6 = 14
        markets = _ladder([8, 28, 23, 13], [10, 30, 25, 15])
        arbs = check_dutch_book(markets)
        assert len(arbs) == 1
        arb = arbs[0]
        assert arb.side == "yes"
        assert arb.sum_cents == 80
        assert arb.total_fee_cents == 6
        assert arb.profit_cents == 14
        assert [leg.price_cents for leg in arb.legs] == [10, 30, 25, 15]
        assert [leg.fee_cents for leg in arb.legs] == [1, 2, 2, 1]
        assert all(leg.side == "yes" for leg in arb.legs)

    def test_sum_below_100_but_above_fees_is_not_arb(self):
        # asks sum to 97, fees ≥ 4 → 100 - 97 - fees < 0
        markets = _ladder([22, 23, 24, 24], [24, 24, 25, 24])
        assert check_dutch_book(markets) == []


# ─── Fair ladder / grouping ────────────────────────────


class TestFairLadderAndGrouping:
    def test_fair_ladder_no_false_positive(self):
        # bids sum 93, asks sum 101 — both inside the no-arb band
        markets = _ladder([5, 30, 40, 18], [7, 32, 42, 20])
        assert check_dutch_book(markets) == []

    def test_groups_by_event(self):
        fair = _ladder([5, 30, 40, 18], [7, 32, 42, 20], event="KXHIGHNY-26JUN12")
        rich = _ladder([20, 40, 35, 15], [22, 42, 37, 17], event="KXHIGHNY-26JUN13")
        arbs = check_dutch_book(fair + rich)
        assert len(arbs) == 1
        assert arbs[0].event_ticker == "KXHIGHNY-26JUN13"

    def test_event_key_falls_back_to_ticker_prefix(self):
        markets = _ladder([20, 40, 35, 15], [22, 42, 37, 17])
        for m in markets:
            m.pop("event_ticker")
        arbs = check_dutch_book(markets)
        assert len(arbs) == 1
        assert arbs[0].event_ticker == "KXHIGHNY-26JUN13"

    def test_incomplete_sibling_event_does_not_block_detection(self):
        partial = _ladder([40, 40, 40, 40], [41, 41, 41, 41], event="KXHIGHNY-26JUN12")[1:]
        rich = _ladder([20, 40, 35, 15], [22, 42, 37, 17], event="KXHIGHNY-26JUN13")
        arbs = check_dutch_book(partial + rich)
        assert [a.event_ticker for a in arbs] == ["KXHIGHNY-26JUN13"]


# ─── Formatting ────────────────────────────────────────


class TestFormatting:
    def test_empty(self):
        assert format_dutch_book_alerts([]) == ""

    def test_contains_legs_prices_and_profit(self):
        markets = _ladder([20, 40, 35, 15], [22, 42, 37, 17])
        arbs = check_dutch_book(markets)
        text = format_dutch_book_alerts(arbs)
        assert "KXHIGHNY-26JUN13" in text
        assert "+3¢ per set" in text
        assert "KXHIGHNY-26JUN13-T61" in text
        assert "NO @ 80¢" in text
        assert "NOT auto-executed" in text
