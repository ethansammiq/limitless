#!/usr/bin/env python3
"""Tests for position_monitor.py — exit logic, pricing helpers, trailing stops."""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

ET = ZoneInfo("America/New_York")


# ═══════════════════════════════════════════════════════════════
# DERIVED ASK (yes_ask = 100 - best NO bid)
# ═══════════════════════════════════════════════════════════════

class TestBestDerivedAsk:
    """_best_derived_ask: ask implied by the opposite side's bids.

    Regression for the min-vs-max drift: the ask must come from the
    BEST (highest) opposite bid, matching core.broker._bid_ask_for_side
    and edge_scanner_v2 — not the lowest, which inflates the ask.
    """

    def test_uses_highest_opposite_bid(self):
        from position_monitor import _best_derived_ask
        # Best NO bid is 58, not 55 → yes_ask = 42, not 45
        assert _best_derived_ask([[55, 8], [58, 3]]) == 42

    def test_single_level(self):
        from position_monitor import _best_derived_ask
        assert _best_derived_ask([[55, 3]]) == 45

    def test_empty_levels(self):
        from position_monitor import _best_derived_ask
        assert _best_derived_ask([]) == 0

    def test_zero_qty_levels_filtered(self):
        from position_monitor import _best_derived_ask
        # 58 has qty=0 → best live bid is 55 → ask = 45
        assert _best_derived_ask([[58, 0], [55, 2]]) == 45

    def test_matches_paper_broker_derivation(self):
        """Must agree with the fill simulator's bid/ask math."""
        from position_monitor import _best_derived_ask
        from core.broker import _bid_ask_for_side
        book = {"yes": [[40, 10], [38, 5]], "no": [[55, 8], [58, 3]]}
        _, yes_ask = _bid_ask_for_side(book, "yes")
        assert _best_derived_ask(book["no"]) == yes_ask

    def test_extract_sell_prices_yes_ask_consistent(self):
        from position_monitor import _best_derived_ask, _extract_sell_prices
        ob = {"yes": [[40, 10]], "no": [[55, 8], [58, 3]]}
        _, ask, _ = _extract_sell_prices(ob, "yes")
        assert ask == _best_derived_ask(ob["no"]) == 42

    def test_extract_sell_prices_no_ask_consistent(self):
        from position_monitor import _best_derived_ask, _extract_sell_prices
        ob = {"yes": [[40, 10], [42, 5]], "no": [[55, 8]]}
        _, ask, _ = _extract_sell_prices(ob, "no")
        assert ask == _best_derived_ask(ob["yes"]) == 58


# ═══════════════════════════════════════════════════════════════
# EXIT SELL PLACEMENT HELPER
# ═══════════════════════════════════════════════════════════════

class TestPlaceExitSell:
    """_place_exit_sell: shared place-sell + pending_sell bookkeeping."""

    def _make_open_pos(self, contracts=10, avg_price=40):
        return {
            "ticker": "KXHIGHNY-26FEB16-B36.5",
            "side": "yes",
            "avg_price": avg_price,
            "contracts": contracts,
            "status": "open",
            "pnl_realized": 0.0,
            "notes": [],
        }

    def _place(self, client, pos, **overrides):
        from position_monitor import _place_exit_sell
        from trade_events import TradeEvent
        actions = overrides.pop("actions_taken", [])
        kwargs = dict(
            qty=5, price=48, remaining_qty=5,
            event=TradeEvent.EXIT_FREEROLL,
            note="FREEROLL sell 5x @ 48c placed (order: {order_id})",
            action="FREEROLL: Sell 5x placed",
            actions_taken=actions,
            log_payload={"ticker": pos["ticker"], "price": 48},
            alert_title="FREEROLL — SELL PLACED",
            alert_body="body",
            alert_color=0x00FF00,
        )
        kwargs.update(overrides)
        now = datetime.now(ET)
        with patch("position_monitor.send_discord_alert", new_callable=AsyncMock) as alert, \
             patch("position_monitor.log_event") as log:
            ok = asyncio.run(_place_exit_sell(client, pos, now, **kwargs))
        return ok, actions, alert, log

    def test_marks_pending_sell_fields(self):
        client = AsyncMock()
        client.place_order = AsyncMock(return_value={"order": {"order_id": "ord-42"}})
        pos = self._make_open_pos()
        ok, actions, _, _ = self._place(client, pos)
        assert ok is True
        assert pos["status"] == "pending_sell"
        assert pos["sell_order_id"] == "ord-42"
        assert pos["_pending_remaining_qty"] == 5
        assert pos["_pre_sell_qty"] == 10
        assert pos["_sell_price_placed"] == 48
        assert pos["sell_placed_at"]
        assert actions == ["FREEROLL: Sell 5x placed"]
        client.place_order.assert_awaited_once_with(
            ticker=pos["ticker"], side="yes", action="sell",
            count=5, price=48, order_type="limit",
        )

    def test_does_not_book_pnl_at_placement(self):
        """Regression: realized P&L must NOT be booked when the sell is
        merely placed — only on confirmed fill in _check_pending_sells."""
        client = AsyncMock()
        client.place_order = AsyncMock(return_value={"order": {"order_id": "o1"}})
        pos = self._make_open_pos()
        ok, _, _, _ = self._place(client, pos)
        assert ok is True
        assert pos["pnl_realized"] == 0.0

    def test_note_order_id_substitution(self):
        client = AsyncMock()
        client.place_order = AsyncMock(return_value={"order": {"order_id": "ord-7"}})
        pos = self._make_open_pos()
        self._place(client, pos)
        assert len(pos["notes"]) == 1
        assert "(order: ord-7)" in pos["notes"][0]

    def test_extra_fields_applied_on_success(self):
        client = AsyncMock()
        client.place_order = AsyncMock(return_value={"order": {"order_id": "o1"}})
        pos = self._make_open_pos()
        self._place(client, pos, extra_fields={"freerolled": True, "peak_price": 48})
        assert pos["freerolled"] is True
        assert pos["peak_price"] == 48

    def test_failed_order_leaves_position_untouched(self):
        client = AsyncMock()
        client.place_order = AsyncMock(return_value=None)
        pos = self._make_open_pos()
        ok, actions, alert, log = self._place(client, pos, extra_fields={"freerolled": True})
        assert ok is False
        assert pos["status"] == "open"
        assert "sell_order_id" not in pos
        assert "freerolled" not in pos
        assert pos["notes"] == []
        assert actions == []
        alert.assert_not_awaited()
        log.assert_not_called()

    def test_log_event_and_alert_emitted(self):
        client = AsyncMock()
        client.place_order = AsyncMock(return_value={"order": {"order_id": "o9"}})
        pos = self._make_open_pos()
        _, _, alert, log = self._place(client, pos)
        alert.assert_awaited_once()
        assert alert.call_args[0][0] == "FREEROLL — SELL PLACED"
        assert alert.call_args.kwargs.get("color") == 0x00FF00
        log.assert_called_once()
        payload = log.call_args[0][2]
        assert payload["order_id"] == "o9"
        assert payload["price"] == 48


# ═══════════════════════════════════════════════════════════════
# PENDING SELL — REALIZED P&L BOOKING (fill-time, not placement)
# ═══════════════════════════════════════════════════════════════

class TestPendingSellPnlBooking:
    """_check_pending_sells: P&L is booked on confirmed fill and never
    survives an expired/cancelled sell (regression for the phantom-profit
    bug that corrupted circuit-breaker/drawdown inputs)."""

    TICKER = "KXHIGHNY-26FEB16-B36.5"

    def _make_pending_pos(self, *, contracts=10, remaining=0, price_placed=80,
                          avg_price=40, minutes_ago=5, pnl_realized=0.0):
        placed = (datetime.now(ET) - timedelta(minutes=minutes_ago)).isoformat()
        return {
            "ticker": self.TICKER,
            "side": "yes",
            "avg_price": avg_price,
            "contracts": contracts,
            "status": "pending_sell",
            "sell_order_id": "ord-1",
            "sell_placed_at": placed,
            "_pending_remaining_qty": remaining,
            "_pre_sell_qty": contracts,
            "_sell_price_placed": price_placed,
            "pnl_realized": pnl_realized,
            "notes": [],
        }

    def _run(self, positions, client):
        from position_monitor import _check_pending_sells
        now = datetime.now(ET)
        with patch("position_monitor.send_discord_alert", new_callable=AsyncMock), \
             patch("position_monitor.log_event"):
            return asyncio.run(_check_pending_sells(positions, client, now))

    def _make_client(self, *, api_qty=None, fills=None):
        client = AsyncMock()
        api_positions = []
        if api_qty is not None:
            # Kalshi fixed-point shape: contracts as a 2-decimal string.
            api_positions = [{"ticker": self.TICKER, "position_fp": f"{api_qty:.2f}"}]
        client.get_positions = AsyncMock(return_value=api_positions)
        client.get_fills = AsyncMock(return_value=fills or [])
        client.get_orderbook = AsyncMock(return_value={})
        client.cancel_order = AsyncMock(return_value={})
        return client

    def test_full_fill_books_pnl_and_closes(self):
        pos = self._make_pending_pos()  # sell 10 @ 80c, entry 40c
        client = self._make_client(api_qty=None)  # gone from API → filled
        actions = self._run([pos], client)
        assert pos["status"] == "closed"
        assert pos["pnl_realized"] == pytest.approx(4.0)  # (80-40)/100*10
        assert any("CONFIRMED FILL" in a for a in actions)
        client.get_fills.assert_not_awaited()  # qty check already confirmed
        # Sell-cycle bookkeeping cleared
        for key in ("_pending_remaining_qty", "_pre_sell_qty", "_sell_price_placed"):
            assert key not in pos

    def test_partial_fill_books_sold_portion_only(self):
        pos = self._make_pending_pos(contracts=10, remaining=4)  # sold 6 @ 80c
        client = self._make_client(api_qty=4)
        actions = self._run([pos], client)
        assert pos["status"] == "open"
        assert pos["contracts"] == 4
        assert pos["pnl_realized"] == pytest.approx(2.4)  # (80-40)/100*6
        assert any("PARTIAL FILL" in a for a in actions)

    def test_fill_confirmed_via_fills_when_positions_stale(self):
        """PaperBroker positions mirror the local store, so fills are the
        only signal — booking must still happen."""
        pos = self._make_pending_pos()
        client = self._make_client(
            api_qty=10,  # store mirror: still shows pre-sell quantity
            fills=[{"order_id": "ord-1", "count_fp": "10.00", "action": "sell"}],
        )
        self._run([pos], client)
        assert pos["status"] == "closed"
        assert pos["pnl_realized"] == pytest.approx(4.0)

    def test_expired_sell_reverts_without_phantom_pnl(self):
        """THE regression: an unfilled sell that expires must leave
        pnl_realized untouched after reverting to open."""
        pos = self._make_pending_pos(minutes_ago=45)  # > 30 min expiry
        client = self._make_client(api_qty=10)  # never filled
        actions = self._run([pos], client)
        assert pos["status"] == "open"
        assert pos["contracts"] == 10
        assert pos["pnl_realized"] == 0.0
        assert any("STALE ORDER" in a for a in actions)
        client.cancel_order.assert_awaited_once_with("ord-1")
        for key in ("sell_order_id", "_pre_sell_qty", "_sell_price_placed"):
            assert key not in pos

    def test_fill_accumulates_onto_prior_realized(self):
        """A second exit adds to pnl_realized from an earlier partial."""
        pos = self._make_pending_pos(pnl_realized=1.0)
        client = self._make_client(api_qty=None)
        self._run([pos], client)
        assert pos["pnl_realized"] == pytest.approx(5.0)  # 1.0 + 4.0

    def test_no_booking_without_placed_price(self):
        """Corrupted/zero _sell_price_placed → close without booking."""
        pos = self._make_pending_pos(price_placed=0)
        client = self._make_client(api_qty=None)
        self._run([pos], client)
        assert pos["status"] == "closed"
        assert pos["pnl_realized"] == 0.0

    def test_unfilled_fresh_sell_left_pending(self):
        """Recent unfilled sell: stays pending, nothing booked."""
        pos = self._make_pending_pos(minutes_ago=2)
        client = self._make_client(api_qty=10)
        actions = self._run([pos], client)
        assert pos["status"] == "pending_sell"
        assert pos["pnl_realized"] == 0.0
        assert actions == []


# ═══════════════════════════════════════════════════════════════
# TRAILING OFFSET ZONES
# ═══════════════════════════════════════════════════════════════

class TestTrailingOffsetForPrice:
    """_trailing_offset_for_price: zone-based trailing stop offset.

    Zones (updated 2026-02-15):
        [0,20)  → 10   deep value
        [20,40) → 7    low-mid (most weather trades)
        [40,60) → 6    mid
        [60,80) → 5    high
        [80,100)→ 3    near-certain
    """

    def test_deep_value_zone(self):
        from position_monitor import _trailing_offset_for_price
        assert _trailing_offset_for_price(10) == 10
        assert _trailing_offset_for_price(0) == 10
        assert _trailing_offset_for_price(19) == 10

    def test_low_mid_zone(self):
        from position_monitor import _trailing_offset_for_price
        assert _trailing_offset_for_price(20) == 7
        assert _trailing_offset_for_price(30) == 7
        assert _trailing_offset_for_price(39) == 7

    def test_mid_zone(self):
        from position_monitor import _trailing_offset_for_price
        assert _trailing_offset_for_price(40) == 6
        assert _trailing_offset_for_price(50) == 6
        assert _trailing_offset_for_price(59) == 6

    def test_high_zone(self):
        from position_monitor import _trailing_offset_for_price
        assert _trailing_offset_for_price(60) == 5
        assert _trailing_offset_for_price(70) == 5
        assert _trailing_offset_for_price(79) == 5

    def test_near_certain_zone(self):
        from position_monitor import _trailing_offset_for_price
        assert _trailing_offset_for_price(80) == 3
        assert _trailing_offset_for_price(90) == 3
        assert _trailing_offset_for_price(99) == 3

    def test_fallback_above_all_zones(self):
        """100+ should fall through to legacy TRAILING_OFFSET_CENTS."""
        from position_monitor import _trailing_offset_for_price
        from config import TRAILING_OFFSET_CENTS
        assert _trailing_offset_for_price(100) == TRAILING_OFFSET_CENTS
        assert _trailing_offset_for_price(150) == TRAILING_OFFSET_CENTS

    def test_zone_boundaries_are_exclusive(self):
        """Upper bound is exclusive: 20 is in [20,40), not [0,20)."""
        from position_monitor import _trailing_offset_for_price
        assert _trailing_offset_for_price(20) != _trailing_offset_for_price(19)

    def test_offsets_decrease_as_price_rises(self):
        """Higher price = tighter trailing stop (less room to fall)."""
        from position_monitor import _trailing_offset_for_price
        offsets = [_trailing_offset_for_price(p) for p in [10, 25, 45, 65, 85]]
        assert offsets == sorted(offsets, reverse=True)


# ═══════════════════════════════════════════════════════════════
# ORDERBOOK EXTRACTION
# ═══════════════════════════════════════════════════════════════

class TestExtractSellPrices:
    """_extract_sell_prices: orderbook → (bid, ask, spread)."""

    def test_yes_side_normal_book(self):
        from position_monitor import _extract_sell_prices
        ob = {
            "yes": [[40, 10], [38, 5]],  # bid=40
            "no":  [[55, 8], [58, 3]],   # best NO bid=58 → yes_ask=100-58=42
        }
        bid, ask, spread = _extract_sell_prices(ob, "yes")
        assert bid == 40
        assert ask == 42
        assert spread == 2

    def test_yes_side_no_ask(self):
        """When no NO-side data, ask falls back to bid."""
        from position_monitor import _extract_sell_prices
        ob = {"yes": [[35, 5]], "no": []}
        bid, ask, spread = _extract_sell_prices(ob, "yes")
        assert bid == 35
        assert ask == 35
        assert spread == 0

    def test_no_side_normal_book(self):
        from position_monitor import _extract_sell_prices
        ob = {
            "yes": [[40, 10], [42, 5]],  # yes_bids max=42 → no_ask=100-42=58
            "no":  [[55, 8], [60, 3]],    # no_bids max=60 → bid=60
        }
        bid, ask, spread = _extract_sell_prices(ob, "no")
        assert bid == 60
        assert ask == 58  # 100 - max(yes_bids) = 100 - 42
        # ask < bid is valid (crossed book for NO side)
        assert spread == 0  # max(0, 58-60) = 0

    def test_no_side_fallback_from_yes(self):
        """When no NO bids, derive from YES ask."""
        from position_monitor import _extract_sell_prices
        ob = {
            "yes": [[40, 10], [45, 5]],  # min=40 → no_bid = 100-40=60
            "no": [],
        }
        bid, ask, spread = _extract_sell_prices(ob, "no")
        assert bid == 60  # 100 - min(yes_prices)=100-40

    def test_empty_orderbook(self):
        from position_monitor import _extract_sell_prices
        bid, ask, spread = _extract_sell_prices({}, "yes")
        assert bid == 0
        assert ask == 0
        assert spread == 0

    def test_zero_quantity_entries_filtered(self):
        """Entries with qty=0 should be ignored."""
        from position_monitor import _extract_sell_prices
        ob = {
            "yes": [[50, 0], [40, 5]],  # 50 has qty=0, should use 40
            "no": [[55, 3]],
        }
        bid, ask, spread = _extract_sell_prices(ob, "yes")
        assert bid == 40


# ═══════════════════════════════════════════════════════════════
# SMART SELL PRICE
# ═══════════════════════════════════════════════════════════════

class TestSmartSellPrice:
    """_smart_sell_price: urgency-based price selection."""

    def test_urgent_returns_bid(self):
        from position_monitor import _smart_sell_price
        assert _smart_sell_price(bid=40, ask=48, spread=8, urgent=True) == 40

    def test_non_urgent_wide_spread_pegs_ask_minus_1(self):
        from position_monitor import _smart_sell_price
        # spread=8 >= 3, ask=48 → return max(40, 48-1) = 47
        assert _smart_sell_price(bid=40, ask=48, spread=8, urgent=False) == 47

    def test_non_urgent_tight_spread_returns_bid(self):
        from position_monitor import _smart_sell_price
        # spread=2 < 3 → not worth pegging, return bid
        assert _smart_sell_price(bid=40, ask=42, spread=2, urgent=False) == 40

    def test_non_urgent_zero_ask_returns_bid(self):
        from position_monitor import _smart_sell_price
        assert _smart_sell_price(bid=40, ask=0, spread=0, urgent=False) == 40

    def test_zero_bid_returns_zero(self):
        from position_monitor import _smart_sell_price
        assert _smart_sell_price(bid=0, ask=45, spread=5, urgent=False) == 0

    def test_peg_never_below_bid(self):
        """ask-1 should never be below bid (max(bid, ask-1))."""
        from position_monitor import _smart_sell_price
        # Weird case: ask=42, bid=45, spread=3 → max(45, 41) = 45
        assert _smart_sell_price(bid=45, ask=42, spread=3, urgent=False) == 45

    def test_exact_spread_threshold(self):
        """spread=3 should trigger ask-peg (>= 3, not > 3)."""
        from position_monitor import _smart_sell_price
        result = _smart_sell_price(bid=40, ask=43, spread=3, urgent=False)
        assert result == 42  # max(40, 43-1) = 42


# ═══════════════════════════════════════════════════════════════
# EXIT RULE DECISION LOGIC (unit-testable conditions)
# ═══════════════════════════════════════════════════════════════

class TestEfficiencyExitConditions:
    """Efficiency exit logic: price >= 90c, with settlement hold override."""

    def test_efficiency_threshold_from_config(self):
        from position_monitor import EFFICIENCY_EXIT_CENTS
        assert EFFICIENCY_EXIT_CENTS == 90

    def test_settlement_hold_when_obs_inside_bracket(self):
        """Near settlement + obs inside bracket → should hold for $1."""
        # Simulating the should_hold logic from check_and_manage_positions
        sell_price = 92
        near_settlement = True
        settlement_hold_threshold = 80
        side = "yes"
        obs_temp = 42.0
        bracket_lo = 40.0
        bracket_hi = 44.0

        should_hold = False
        if near_settlement and sell_price >= settlement_hold_threshold:
            if obs_temp > 0 and bracket_lo > 0 and bracket_hi > 0:
                if side == "yes":
                    if bracket_lo <= obs_temp < bracket_hi:
                        should_hold = True  # inside bracket

        assert should_hold is True

    def test_settlement_hold_when_obs_far_outside_bracket(self):
        """Obs well outside bracket → should NOT hold, sell at 90+."""
        sell_price = 92
        near_settlement = True
        settlement_hold_threshold = 80
        side = "yes"
        obs_temp = 48.0  # 4°F above bracket
        bracket_lo = 40.0
        bracket_hi = 44.0

        should_hold = False
        if near_settlement and sell_price >= settlement_hold_threshold:
            if obs_temp > 0 and bracket_lo > 0 and bracket_hi > 0:
                if side == "yes":
                    if obs_temp >= bracket_hi:
                        dist = obs_temp - bracket_hi + 1
                        should_hold = dist <= 2.0  # 5.0 > 2.0

        assert should_hold is False

    def test_settlement_hold_when_obs_barely_below_bracket(self):
        """Obs just below bracket (within 2°F) → hold (could still reach)."""
        sell_price = 92
        near_settlement = True
        settlement_hold_threshold = 80
        side = "yes"
        obs_temp = 39.0
        bracket_lo = 40.0
        bracket_hi = 44.0

        should_hold = False
        if near_settlement and sell_price >= settlement_hold_threshold:
            if obs_temp > 0 and bracket_lo > 0 and bracket_hi > 0:
                if side == "yes":
                    if obs_temp < bracket_lo:
                        dist = bracket_lo - obs_temp  # 1.0
                        should_hold = dist <= 2.0

        assert should_hold is True

    def test_no_hold_when_not_near_settlement(self):
        """Far from settlement → no special hold logic, sell at efficiency."""
        sell_price = 92
        near_settlement = False
        settlement_hold_threshold = 80

        should_hold = False
        if near_settlement and sell_price >= settlement_hold_threshold:
            should_hold = True  # Would hold

        assert should_hold is False


class TestFreerollConditions:
    """Freeroll trigger: price >= 2x entry, not already freerolled, contracts > 1."""

    def test_freeroll_triggers(self):
        entry = 25
        sell_price = 50  # 2x entry
        freerolled = False
        contracts = 10
        from config import FREEROLL_MULTIPLIER

        freeroll_price = entry * FREEROLL_MULTIPLIER
        should_freeroll = (
            not freerolled
            and sell_price >= freeroll_price
            and contracts > 1
        )
        assert should_freeroll is True

    def test_freeroll_blocked_if_already_freerolled(self):
        entry = 25
        sell_price = 50
        freerolled = True
        contracts = 10
        from config import FREEROLL_MULTIPLIER

        freeroll_price = entry * FREEROLL_MULTIPLIER
        should_freeroll = (
            not freerolled
            and sell_price >= freeroll_price
            and contracts > 1
        )
        assert should_freeroll is False

    def test_freeroll_blocked_single_contract(self):
        entry = 25
        sell_price = 55
        freerolled = False
        contracts = 1
        from config import FREEROLL_MULTIPLIER

        freeroll_price = entry * FREEROLL_MULTIPLIER
        should_freeroll = (
            not freerolled
            and sell_price >= freeroll_price
            and contracts > 1
        )
        assert should_freeroll is False

    def test_freeroll_blocked_below_threshold(self):
        entry = 25
        sell_price = 49  # Just under 2x
        freerolled = False
        contracts = 10
        from config import FREEROLL_MULTIPLIER

        freeroll_price = entry * FREEROLL_MULTIPLIER
        should_freeroll = (
            not freerolled
            and sell_price >= freeroll_price
            and contracts > 1
        )
        assert should_freeroll is False

    def test_freeroll_sell_qty_is_half(self):
        contracts = 10
        sell_qty = contracts // 2
        assert sell_qty == 5

    def test_freeroll_sell_qty_odd_contracts(self):
        contracts = 7
        sell_qty = contracts // 2
        assert sell_qty == 3  # Floor division


class TestThesisBreakConditions:
    """Thesis break: confidence dropped below threshold, not freerolled."""

    def test_thesis_break_triggers(self):
        from config import THESIS_BREAK_CONFIDENCE, STOP_LOSS_FLOOR_CENTS
        last_conf = 30  # Below 40
        freerolled = False
        sell_price = 20
        thin_book = False

        should_exit = (
            not freerolled
            and last_conf is not None
            and last_conf < THESIS_BREAK_CONFIDENCE
            and sell_price > STOP_LOSS_FLOOR_CENTS
            and not thin_book
        )
        assert should_exit is True

    def test_thesis_break_blocked_if_freerolled(self):
        """Freerolled positions use trailing stop, not thesis break."""
        from config import THESIS_BREAK_CONFIDENCE, STOP_LOSS_FLOOR_CENTS
        last_conf = 30
        freerolled = True
        sell_price = 20
        thin_book = False

        should_exit = (
            not freerolled
            and last_conf is not None
            and last_conf < THESIS_BREAK_CONFIDENCE
            and sell_price > STOP_LOSS_FLOOR_CENTS
            and not thin_book
        )
        assert should_exit is False

    def test_thesis_break_blocked_no_confidence_data(self):
        from config import THESIS_BREAK_CONFIDENCE, STOP_LOSS_FLOOR_CENTS
        last_conf = None
        freerolled = False
        sell_price = 20
        thin_book = False

        should_exit = (
            not freerolled
            and last_conf is not None
            and last_conf < THESIS_BREAK_CONFIDENCE
            and sell_price > STOP_LOSS_FLOOR_CENTS
            and not thin_book
        )
        assert should_exit is False

    def test_thesis_break_blocked_thin_book(self):
        """Don't sell into illiquid market."""
        from config import THESIS_BREAK_CONFIDENCE, STOP_LOSS_FLOOR_CENTS
        last_conf = 30
        freerolled = False
        sell_price = 20
        thin_book = True

        should_exit = (
            not freerolled
            and last_conf is not None
            and last_conf < THESIS_BREAK_CONFIDENCE
            and sell_price > STOP_LOSS_FLOOR_CENTS
            and not thin_book
        )
        assert should_exit is False

    def test_thesis_break_blocked_price_at_floor(self):
        """Don't sell at 1-2c — too illiquid."""
        from config import THESIS_BREAK_CONFIDENCE, STOP_LOSS_FLOOR_CENTS
        last_conf = 30
        freerolled = False
        sell_price = STOP_LOSS_FLOOR_CENTS  # 2c
        thin_book = False

        should_exit = (
            not freerolled
            and last_conf is not None
            and last_conf < THESIS_BREAK_CONFIDENCE
            and sell_price > STOP_LOSS_FLOOR_CENTS
            and not thin_book
        )
        assert should_exit is False


class TestRoiBackstopConditions:
    """ROI backstop: secondary stop-loss when re-scan can't update confidence."""

    def test_roi_backstop_triggers(self):
        from config import STOP_LOSS_ROI_PCT, STOP_LOSS_FLOOR_CENTS
        freerolled = False
        entry_price = 40
        sell_price = 18  # ROI = (18-40)/40*100 = -55%
        roi = (sell_price - entry_price) / entry_price * 100
        thin_book = False

        should_exit = (
            not freerolled
            and sell_price > 0
            and roi <= STOP_LOSS_ROI_PCT
            and sell_price > STOP_LOSS_FLOOR_CENTS
            and not thin_book
        )
        assert roi == pytest.approx(-55.0)
        assert should_exit is True

    def test_roi_backstop_blocked_if_freerolled(self):
        from config import STOP_LOSS_ROI_PCT, STOP_LOSS_FLOOR_CENTS
        freerolled = True
        entry_price = 40
        sell_price = 18
        roi = (sell_price - entry_price) / entry_price * 100
        thin_book = False

        should_exit = (
            not freerolled
            and sell_price > 0
            and roi <= STOP_LOSS_ROI_PCT
            and sell_price > STOP_LOSS_FLOOR_CENTS
            and not thin_book
        )
        assert should_exit is False

    def test_roi_backstop_not_triggered_mild_loss(self):
        from config import STOP_LOSS_ROI_PCT, STOP_LOSS_FLOOR_CENTS
        freerolled = False
        entry_price = 40
        sell_price = 30  # ROI = -25%
        roi = (sell_price - entry_price) / entry_price * 100
        thin_book = False

        should_exit = (
            not freerolled
            and sell_price > 0
            and roi <= STOP_LOSS_ROI_PCT
            and sell_price > STOP_LOSS_FLOOR_CENTS
            and not thin_book
        )
        assert roi == -25.0
        assert should_exit is False


class TestMidProfitConditions:
    """Mid-range profit take: after freeroll, at 65c, sell half of remaining."""

    def test_mid_profit_triggers(self):
        from config import MID_PROFIT_THRESHOLD_CENTS, MID_PROFIT_SELL_FRACTION
        freerolled = True
        mid_profit_taken = False
        sell_price = 68
        contracts = 5

        should_mid = (
            freerolled
            and not mid_profit_taken
            and sell_price >= MID_PROFIT_THRESHOLD_CENTS
            and contracts > 1
        )
        assert should_mid is True

        sell_qty = max(1, int(contracts * MID_PROFIT_SELL_FRACTION))
        assert sell_qty == 2  # int(5 * 0.50) = 2

    def test_mid_profit_blocked_before_freeroll(self):
        from config import MID_PROFIT_THRESHOLD_CENTS
        freerolled = False
        mid_profit_taken = False
        sell_price = 70
        contracts = 5

        should_mid = (
            freerolled
            and not mid_profit_taken
            and sell_price >= MID_PROFIT_THRESHOLD_CENTS
            and contracts > 1
        )
        assert should_mid is False

    def test_mid_profit_blocked_already_taken(self):
        from config import MID_PROFIT_THRESHOLD_CENTS
        freerolled = True
        mid_profit_taken = True
        sell_price = 70
        contracts = 5

        should_mid = (
            freerolled
            and not mid_profit_taken
            and sell_price >= MID_PROFIT_THRESHOLD_CENTS
            and contracts > 1
        )
        assert should_mid is False


class TestTrailingStopConditions:
    """Trailing stop: after freeroll, sell when price drops below floor."""

    def test_trailing_stop_triggers(self):
        freerolled = True
        sell_price = 35
        peak = 50
        floor = 38  # 50 - 12 (mid-low zone)
        thin_book = False

        should_trail = (
            freerolled
            and sell_price <= floor
            and sell_price > 0
            and not thin_book
        )
        assert should_trail is True

    def test_trailing_stop_skipped_thin_book(self):
        freerolled = True
        sell_price = 3
        floor = 5
        bid_volume = 2
        thin_book = (bid_volume < 3 and sell_price <= 5)

        should_trail = (
            freerolled
            and sell_price <= floor
            and sell_price > 0
            and not thin_book
        )
        assert thin_book is True
        assert should_trail is False

    def test_trailing_floor_ratchets_up(self):
        """Floor should only increase, never decrease."""
        from position_monitor import _trailing_offset_for_price
        entry_price = 20
        peak = 35
        floor = max(entry_price, peak - _trailing_offset_for_price(35))
        assert floor == 28  # 35 - 7

        # Price rises to 55
        new_peak = 55
        new_offset = _trailing_offset_for_price(55)
        new_floor = max(entry_price, new_peak - new_offset)
        assert new_floor == 49  # 55 - 6

        # Floor should only go up
        assert new_floor > floor

    def test_trailing_floor_never_below_entry(self):
        """Trailing floor is always >= entry price."""
        from position_monitor import _trailing_offset_for_price
        entry_price = 20
        sell_price = 25
        offset = _trailing_offset_for_price(sell_price)  # 7 (low-mid zone [20,40))
        floor = max(entry_price, sell_price - offset)  # max(20, 25-7) = max(20, 18) = 20
        assert floor == entry_price


class TestPendingSellExpiry:
    """PENDING_SELL_EXPIRY_MINUTES now comes from config."""

    def test_expiry_from_config(self):
        from config import PENDING_SELL_EXPIRY_MINUTES
        from position_monitor import PENDING_SELL_EXPIRY_MINUTES as PM_EXPIRY
        assert PENDING_SELL_EXPIRY_MINUTES == PM_EXPIRY == 30


# ═══════════════════════════════════════════════════════════════
# UPGRADE 1: TIME-DECAY URGENCY SCALING
# ═══════════════════════════════════════════════════════════════

class TestTimeDecayFactor:
    """_time_decay_factor: sigmoid decay from 1.0 (far) to 0.0 (near settlement)."""

    def test_far_from_settlement(self):
        from position_monitor import _time_decay_factor
        # 20+ hours → f ≈ 1.0
        assert _time_decay_factor(20) > 0.99

    def test_at_midpoint(self):
        from position_monitor import _time_decay_factor
        # At midpoint (6h) → f ≈ 0.5
        assert _time_decay_factor(6.0) == pytest.approx(0.5, abs=0.01)

    def test_near_settlement(self):
        from position_monitor import _time_decay_factor
        # 1h → f should be very small
        assert _time_decay_factor(1.0) < 0.1

    def test_at_settlement(self):
        from position_monitor import _time_decay_factor
        # 0h → near zero
        assert _time_decay_factor(0.0) < 0.05

    def test_monotonically_increasing(self):
        from position_monitor import _time_decay_factor
        hours = [0, 1, 3, 6, 10, 15, 20]
        values = [_time_decay_factor(h) for h in hours]
        assert values == sorted(values)

    def test_negative_hours(self):
        """Negative hours (past settlement) → near 0."""
        from position_monitor import _time_decay_factor
        assert _time_decay_factor(-2) < 0.01

    def test_returns_float(self):
        from position_monitor import _time_decay_factor
        assert isinstance(_time_decay_factor(10), float)


class TestScaledTrailingOffset:
    """_scaled_trailing_offset: time-decayed trailing stop offset."""

    def test_far_from_settlement_unchanged(self):
        from position_monitor import _scaled_trailing_offset
        # 20h out → factor ≈ 1.0, offset unchanged
        assert _scaled_trailing_offset(10, 20.0) == 10

    def test_near_settlement_tightens(self):
        from position_monitor import _scaled_trailing_offset
        # 1h out → offset should be near min_factor * base
        result = _scaled_trailing_offset(10, 1.0)
        assert result < 10
        assert result >= 2  # Minimum floor

    def test_minimum_floor_of_2(self):
        from position_monitor import _scaled_trailing_offset
        # Even with extreme decay, never below 2
        assert _scaled_trailing_offset(3, 0.0) >= 2

    def test_midpoint_intermediate(self):
        from position_monitor import _scaled_trailing_offset
        # 6h → factor ≈ 0.5, offset should be between min and max
        result = _scaled_trailing_offset(10, 6.0)
        assert 6 <= result <= 10

    def test_large_offset(self):
        from position_monitor import _scaled_trailing_offset
        result = _scaled_trailing_offset(14, 0.5)
        # At near-settlement: 14 * ~0.6 = ~8.4 → 8
        assert result < 14
        assert result >= 2


class TestScaledFreerollMultiplier:
    """_scaled_freeroll_multiplier: time-decayed freeroll trigger."""

    def test_far_from_settlement_unchanged(self):
        from position_monitor import _scaled_freeroll_multiplier
        result = _scaled_freeroll_multiplier(2.0, 20.0)
        assert result == pytest.approx(2.0, abs=0.05)

    def test_near_settlement_lowers(self):
        from position_monitor import _scaled_freeroll_multiplier
        result = _scaled_freeroll_multiplier(2.0, 1.0)
        assert result < 2.0
        # min_factor=0.75, so 2.0 * 0.75 = 1.5 minimum
        assert result >= 1.5

    def test_midpoint_intermediate(self):
        from position_monitor import _scaled_freeroll_multiplier
        result = _scaled_freeroll_multiplier(2.0, 6.0)
        assert 1.5 < result < 2.0


class TestScaledMidProfitThreshold:
    """_scaled_mid_profit_threshold: time-decayed mid-profit trigger."""

    def test_far_from_settlement_unchanged(self):
        from position_monitor import _scaled_mid_profit_threshold
        assert _scaled_mid_profit_threshold(65, 20.0) == 65

    def test_near_settlement_lowers(self):
        from position_monitor import _scaled_mid_profit_threshold
        result = _scaled_mid_profit_threshold(65, 1.0)
        assert result < 65
        # min_factor=0.80, so 65 * 0.80 = 52 minimum
        assert result >= 50

    def test_minimum_floor_of_50(self):
        from position_monitor import _scaled_mid_profit_threshold
        # Even small base, never below 50
        assert _scaled_mid_profit_threshold(55, 0.0) >= 50


# ═══════════════════════════════════════════════════════════════
# UPGRADE 2: OBS-AWARE DYNAMIC TRAILING
# ═══════════════════════════════════════════════════════════════

class TestObsAdjustedTrailingOffset:
    """_obs_adjusted_trailing_offset: observation-based trail adjustment."""

    def test_yes_inside_bracket_widens(self):
        from position_monitor import _obs_adjusted_trailing_offset
        # Obs=42, bracket=[40,44), side=yes → inside → widen by 1.3x
        result = _obs_adjusted_trailing_offset(10, 42.0, 40.0, 44.0, "yes")
        assert result == 13  # 10 * 1.3 = 13

    def test_yes_outside_far_tightens(self):
        from position_monitor import _obs_adjusted_trailing_offset
        # Obs=35, bracket=[40,44), side=yes → 5°F below → tighten by 0.6x
        result = _obs_adjusted_trailing_offset(10, 35.0, 40.0, 44.0, "yes")
        assert result == 6  # 10 * 0.6 = 6

    def test_yes_mild_divergence_unchanged(self):
        from position_monitor import _obs_adjusted_trailing_offset
        # Obs=38, bracket=[40,44), side=yes → 2°F below (< 3°F threshold) → unchanged
        result = _obs_adjusted_trailing_offset(10, 38.0, 40.0, 44.0, "yes")
        assert result == 10

    def test_no_inside_bracket_tightens(self):
        from position_monitor import _obs_adjusted_trailing_offset
        # NO side: obs inside bracket = bad → tighten
        result = _obs_adjusted_trailing_offset(10, 42.0, 40.0, 44.0, "no")
        assert result == 6  # 10 * 0.6

    def test_no_outside_far_widens(self):
        from position_monitor import _obs_adjusted_trailing_offset
        # NO side: obs far outside = good → widen
        result = _obs_adjusted_trailing_offset(10, 35.0, 40.0, 44.0, "no")
        assert result == 13  # 10 * 1.3

    def test_no_obs_returns_base(self):
        from position_monitor import _obs_adjusted_trailing_offset
        # No obs data → base unchanged
        assert _obs_adjusted_trailing_offset(10, 0.0, 40.0, 44.0, "yes") == 10
        assert _obs_adjusted_trailing_offset(10, -1.0, 40.0, 44.0, "yes") == 10

    def test_no_bracket_data_returns_base(self):
        from position_monitor import _obs_adjusted_trailing_offset
        assert _obs_adjusted_trailing_offset(10, 42.0, 0.0, 0.0, "yes") == 10

    def test_minimum_floor_of_2(self):
        from position_monitor import _obs_adjusted_trailing_offset
        # Even with tightening on small offset, never below 2
        result = _obs_adjusted_trailing_offset(3, 30.0, 40.0, 44.0, "yes")
        assert result >= 2


# ═══════════════════════════════════════════════════════════════
# UPGRADE 3: ADAPTIVE FREEROLL MULTIPLIER
# ═══════════════════════════════════════════════════════════════

class TestAdaptiveFreerollMultiplier:
    """_adaptive_freeroll_multiplier: price-level-aware freeroll trigger.

    Tiers (updated 2026-02-15):
        [0,10)  → 2.5  very cheap
        [10,20) → 2.0  cheap
        [20,30) → 1.5  mid-price (was 1.8, unreachable)
        [30,40) → 1.4  upper-mid
        [40,51) → 1.3  expensive
    """

    def test_very_cheap_entry(self):
        from position_monitor import _adaptive_freeroll_multiplier
        assert _adaptive_freeroll_multiplier(5) == 2.5
        assert _adaptive_freeroll_multiplier(0) == 2.5
        assert _adaptive_freeroll_multiplier(9) == 2.5

    def test_cheap_entry(self):
        from position_monitor import _adaptive_freeroll_multiplier
        assert _adaptive_freeroll_multiplier(10) == 2.0
        assert _adaptive_freeroll_multiplier(15) == 2.0
        assert _adaptive_freeroll_multiplier(19) == 2.0

    def test_mid_entry(self):
        from position_monitor import _adaptive_freeroll_multiplier
        assert _adaptive_freeroll_multiplier(20) == 1.5
        assert _adaptive_freeroll_multiplier(26) == 1.5  # The LAX trade that motivated this change
        assert _adaptive_freeroll_multiplier(29) == 1.5

    def test_upper_mid_entry(self):
        from position_monitor import _adaptive_freeroll_multiplier
        assert _adaptive_freeroll_multiplier(30) == 1.4
        assert _adaptive_freeroll_multiplier(35) == 1.4
        assert _adaptive_freeroll_multiplier(39) == 1.4

    def test_expensive_entry(self):
        from position_monitor import _adaptive_freeroll_multiplier
        assert _adaptive_freeroll_multiplier(40) == 1.3
        assert _adaptive_freeroll_multiplier(45) == 1.3
        assert _adaptive_freeroll_multiplier(50) == 1.3

    def test_fallback_above_tiers(self):
        from position_monitor import _adaptive_freeroll_multiplier
        from config import FREEROLL_MULTIPLIER
        assert _adaptive_freeroll_multiplier(51) == FREEROLL_MULTIPLIER
        assert _adaptive_freeroll_multiplier(99) == FREEROLL_MULTIPLIER

    def test_multipliers_decrease_with_price(self):
        """Expensive entries get lower multiplier (lock profit earlier)."""
        from position_monitor import _adaptive_freeroll_multiplier
        mults = [_adaptive_freeroll_multiplier(p) for p in [5, 15, 25, 35, 45]]
        assert mults == sorted(mults, reverse=True)

    def test_lax_feb15_scenario(self):
        """Regression: 26c entry should freeroll at 39c (was 47c, unreachable)."""
        from position_monitor import _adaptive_freeroll_multiplier
        mult = _adaptive_freeroll_multiplier(26)
        assert mult == 1.5
        freeroll_target = int(26 * mult)
        assert freeroll_target == 39  # vs old 47c that never filled


# ═══════════════════════════════════════════════════════════════
# UPGRADE 4: MOMENTUM / VELOCITY EXIT
# ═══════════════════════════════════════════════════════════════

class TestCheckMomentumDrop:
    """_check_momentum_drop: large price drops between cycles."""

    def test_large_drop_detected(self):
        from position_monitor import _check_momentum_drop
        is_drop, amount = _check_momentum_drop(20, 40)
        assert is_drop is True
        assert amount == 20

    def test_exact_threshold(self):
        from position_monitor import _check_momentum_drop
        is_drop, amount = _check_momentum_drop(25, 40)
        assert is_drop is True
        assert amount == 15

    def test_small_drop_not_triggered(self):
        from position_monitor import _check_momentum_drop
        is_drop, amount = _check_momentum_drop(30, 40)
        assert is_drop is False
        assert amount == 10

    def test_price_increase_not_triggered(self):
        from position_monitor import _check_momentum_drop
        is_drop, amount = _check_momentum_drop(50, 40)
        assert is_drop is False

    def test_no_prev_price(self):
        from position_monitor import _check_momentum_drop
        is_drop, amount = _check_momentum_drop(30, 0)
        assert is_drop is False
        assert amount == 0

    def test_same_price(self):
        from position_monitor import _check_momentum_drop
        is_drop, amount = _check_momentum_drop(40, 40)
        assert is_drop is False
        assert amount == 0

    def test_custom_threshold(self):
        from position_monitor import _check_momentum_drop
        is_drop, amount = _check_momentum_drop(35, 40, threshold=5)
        assert is_drop is True
        assert amount == 5


# ═══════════════════════════════════════════════════════════════
# UPGRADE 5: GRADUATED THESIS DETERIORATION
# ═══════════════════════════════════════════════════════════════

class TestThesisDeteriorationAction:
    """_thesis_deterioration_action: hold / trim / exit zones."""

    def test_high_confidence_holds(self):
        from position_monitor import _thesis_deterioration_action
        assert _thesis_deterioration_action(80, False, False, 10) == "hold"
        assert _thesis_deterioration_action(70, False, False, 10) == "hold"

    def test_mid_confidence_trims(self):
        from position_monitor import _thesis_deterioration_action
        assert _thesis_deterioration_action(55, False, False, 5) == "trim"
        assert _thesis_deterioration_action(40, False, False, 5) == "trim"

    def test_low_confidence_exits(self):
        from position_monitor import _thesis_deterioration_action
        assert _thesis_deterioration_action(39, False, False, 5) == "exit"
        assert _thesis_deterioration_action(10, False, False, 5) == "exit"

    def test_freerolled_always_holds(self):
        from position_monitor import _thesis_deterioration_action
        assert _thesis_deterioration_action(30, True, False, 5) == "hold"
        assert _thesis_deterioration_action(10, True, False, 5) == "hold"

    def test_none_confidence_holds(self):
        from position_monitor import _thesis_deterioration_action
        assert _thesis_deterioration_action(None, False, False, 5) == "hold"

    def test_already_trimmed_holds(self):
        from position_monitor import _thesis_deterioration_action
        # Mid-zone but already trimmed → hold
        assert _thesis_deterioration_action(55, False, True, 5) == "hold"

    def test_single_contract_holds(self):
        from position_monitor import _thesis_deterioration_action
        # Can't trim 1 contract → hold
        assert _thesis_deterioration_action(55, False, False, 1) == "hold"

    def test_boundary_at_40(self):
        from position_monitor import _thesis_deterioration_action
        # 40 is in trim zone (>= THESIS_BREAK_CONFIDENCE)
        assert _thesis_deterioration_action(40, False, False, 5) == "trim"
        # 39 is exit
        assert _thesis_deterioration_action(39, False, False, 5) == "exit"

    def test_boundary_at_70(self):
        from position_monitor import _thesis_deterioration_action
        # 70 is hold (>= THESIS_TRIM_CONFIDENCE_HIGH)
        assert _thesis_deterioration_action(70, False, False, 5) == "hold"
        # 69 is trim
        assert _thesis_deterioration_action(69, False, False, 5) == "trim"


# ═══════════════════════════════════════════════════════════════
# UPGRADE 3b: QUICK PROFIT (pre-freeroll partial take)
# ═══════════════════════════════════════════════════════════════

class TestQuickProfitConditions:
    """Quick profit: sell 30% when ROI >= 35% but before freeroll triggers."""

    def test_quick_profit_triggers(self):
        from config import QUICK_PROFIT_ROI_PCT, QUICK_PROFIT_SELL_FRACTION, QUICK_PROFIT_MIN_CONTRACTS
        entry_price = 26
        sell_price = 36  # ROI = (36-26)/26*100 = 38.5%
        roi = (sell_price - entry_price) / entry_price * 100
        freerolled = False
        quick_profit_taken = False
        contracts = 10

        should_qp = (
            not freerolled
            and not quick_profit_taken
            and roi >= QUICK_PROFIT_ROI_PCT
            and contracts >= QUICK_PROFIT_MIN_CONTRACTS
        )
        assert roi > 35
        assert should_qp is True

        sell_qty = max(1, int(contracts * QUICK_PROFIT_SELL_FRACTION))
        assert sell_qty == 3  # int(10 * 0.30) = 3

    def test_quick_profit_blocked_if_freerolled(self):
        from config import QUICK_PROFIT_ROI_PCT, QUICK_PROFIT_MIN_CONTRACTS
        entry_price = 26
        sell_price = 36
        roi = (sell_price - entry_price) / entry_price * 100
        freerolled = True
        quick_profit_taken = False
        contracts = 10

        should_qp = (
            not freerolled
            and not quick_profit_taken
            and roi >= QUICK_PROFIT_ROI_PCT
            and contracts >= QUICK_PROFIT_MIN_CONTRACTS
        )
        assert should_qp is False

    def test_quick_profit_blocked_already_taken(self):
        from config import QUICK_PROFIT_ROI_PCT, QUICK_PROFIT_MIN_CONTRACTS
        entry_price = 26
        sell_price = 37
        roi = (sell_price - entry_price) / entry_price * 100
        freerolled = False
        quick_profit_taken = True
        contracts = 10

        should_qp = (
            not freerolled
            and not quick_profit_taken
            and roi >= QUICK_PROFIT_ROI_PCT
            and contracts >= QUICK_PROFIT_MIN_CONTRACTS
        )
        assert should_qp is False

    def test_quick_profit_blocked_below_roi_threshold(self):
        from config import QUICK_PROFIT_ROI_PCT, QUICK_PROFIT_MIN_CONTRACTS
        entry_price = 26
        sell_price = 33  # ROI = 26.9%
        roi = (sell_price - entry_price) / entry_price * 100
        freerolled = False
        quick_profit_taken = False
        contracts = 10

        should_qp = (
            not freerolled
            and not quick_profit_taken
            and roi >= QUICK_PROFIT_ROI_PCT
            and contracts >= QUICK_PROFIT_MIN_CONTRACTS
        )
        assert roi < 35
        assert should_qp is False

    def test_quick_profit_blocked_too_few_contracts(self):
        from config import QUICK_PROFIT_ROI_PCT, QUICK_PROFIT_MIN_CONTRACTS
        entry_price = 10
        sell_price = 20  # ROI = 100%
        roi = (sell_price - entry_price) / entry_price * 100
        freerolled = False
        quick_profit_taken = False
        contracts = 2  # Below QUICK_PROFIT_MIN_CONTRACTS=3

        should_qp = (
            not freerolled
            and not quick_profit_taken
            and roi >= QUICK_PROFIT_ROI_PCT
            and contracts >= QUICK_PROFIT_MIN_CONTRACTS
        )
        assert should_qp is False

    def test_lax_feb15_scenario(self):
        """Regression: the LAX trade that motivated this feature.

        Entry 26c, peak 37c. Old system: no profit taken (freeroll at 47c).
        New system: quick profit at 35c (ROI=34.6% ≈ 35%), sells 3 of 10.
        """
        from config import QUICK_PROFIT_ROI_PCT, QUICK_PROFIT_SELL_FRACTION
        entry = 26
        # At 36c, ROI = 38.5% → triggers
        sell = 36
        roi = (sell - entry) / entry * 100
        assert roi >= QUICK_PROFIT_ROI_PCT

        contracts = 10
        sell_qty = max(1, int(contracts * QUICK_PROFIT_SELL_FRACTION))
        assert sell_qty == 3
        remaining = contracts - sell_qty
        assert remaining == 7  # 7 contracts still ride toward freeroll


# ═══════════════════════════════════════════════════════════════
# SETTLEMENT PROXIMITY CALCULATION
# ═══════════════════════════════════════════════════════════════

class TestSettlementProximity:
    """Settlement proximity: (SETTLEMENT_HOUR_ET - now.hour) % 24.

    Settlement is at 7 AM ET. The calculation must always be non-negative
    and correctly represent hours until the next 7 AM settlement.
    """

    def test_5am_is_2h_away(self):
        """5 AM ET → 2 hours to settlement."""
        from config import SETTLEMENT_HOUR_ET
        hours = (SETTLEMENT_HOUR_ET - 5) % 24
        assert hours == 2

    def test_7am_is_0h_settling_now(self):
        """7 AM ET → 0 hours (settling now)."""
        from config import SETTLEMENT_HOUR_ET
        hours = (SETTLEMENT_HOUR_ET - 7) % 24
        assert hours == 0

    def test_10am_is_21h_away(self):
        """10 AM ET → 21 hours until next 7 AM (next day).

        This was the CRITICAL bug: old code made this -3 hours.
        """
        from config import SETTLEMENT_HOUR_ET
        hours = (SETTLEMENT_HOUR_ET - 10) % 24
        assert hours == 21
        assert hours >= 0  # MUST be non-negative

    def test_midnight_is_7h_away(self):
        """Midnight → 7 hours to settlement."""
        from config import SETTLEMENT_HOUR_ET
        hours = (SETTLEMENT_HOUR_ET - 0) % 24
        assert hours == 7

    def test_11pm_is_8h_away(self):
        """11 PM → 8 hours to settlement."""
        from config import SETTLEMENT_HOUR_ET
        hours = (SETTLEMENT_HOUR_ET - 23) % 24
        assert hours == 8

    def test_always_non_negative(self):
        """hours_to_settlement must be >= 0 for ALL hours of day."""
        from config import SETTLEMENT_HOUR_ET
        for hour in range(24):
            hours = (SETTLEMENT_HOUR_ET - hour) % 24
            assert hours >= 0, f"Negative at hour={hour}: {hours}"
            assert hours < 24, f"Out of range at hour={hour}: {hours}"

    def test_near_settlement_within_window(self):
        """5 AM (2h away) is near settlement (window=2h)."""
        from config import SETTLEMENT_HOUR_ET, SETTLEMENT_WINDOW_HOURS
        hours = (SETTLEMENT_HOUR_ET - 5) % 24
        assert hours <= SETTLEMENT_WINDOW_HOURS

    def test_not_near_settlement_outside_window(self):
        """10 AM (21h away) is NOT near settlement."""
        from config import SETTLEMENT_HOUR_ET, SETTLEMENT_WINDOW_HOURS
        hours = (SETTLEMENT_HOUR_ET - 10) % 24
        assert hours > SETTLEMENT_WINDOW_HOURS

    def test_4am_is_near_settlement(self):
        """4 AM (3h away) → within window if window <= 3, outside if window < 3."""
        from config import SETTLEMENT_HOUR_ET, SETTLEMENT_WINDOW_HOURS
        hours = (SETTLEMENT_HOUR_ET - 4) % 24
        assert hours == 3
        # With SETTLEMENT_WINDOW_HOURS=2, 3h is NOT near settlement
        assert hours > SETTLEMENT_WINDOW_HOURS
