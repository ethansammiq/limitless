"""Tests for trade_autopsy pure helpers (no network, no journals on disk).

Fixtures mirror the real record shapes: live_watch fills
(yes_price_dollars/count_fp/action/is_taker/created_time), the
live_account.json snapshot (open/closed positions with `realized`), and a
cli_sniper buy_winner finding.
"""
import trade_autopsy as ta

# A winning Chicago high: sniper flagged B85.5 as a certain winner, we bought
# 40 @ 60¢ and it settled YES.
FILLS = [
    {"fill_id": "1", "ticker": "KXHIGHCHI-26JUL04-B85.5", "action": "buy",
     "yes_price_dollars": "0.60", "count_fp": "25", "is_taker": True,
     "created_time": "2026-07-04T21:50:00+00:00"},
    {"fill_id": "2", "ticker": "KXHIGHCHI-26JUL04-B85.5", "action": "buy",
     "yes_price_dollars": "0.60", "count_fp": "15", "is_taker": True,
     "created_time": "2026-07-04T21:51:00+00:00"},
    # an unrelated ticker's fill, must be ignored
    {"fill_id": "3", "ticker": "KXHIGHNY-26JUL04-B90.5", "action": "buy",
     "yes_price_dollars": "0.30", "count_fp": "10", "is_taker": False,
     "created_time": "2026-07-04T15:00:00+00:00"},
]

ACCOUNT = {
    "balance": 512.34,
    "open_positions": [],
    "closed_positions": [
        {"ticker": "KXHIGHCHI-26JUL04-B85.5", "qty": 0, "realized": 16.00,
         "exposure": 0},
        {"ticker": "KXHIGHNY-26JUL04-B90.5", "qty": 0, "realized": -3.00,
         "exposure": 0},
    ],
}

FINDING = {
    "ts": "2026-07-04T21:45:00+00:00", "awips": "MDW",
    "summary_date": "2026-07-04", "is_final": False,
    "ticker": "KXHIGHCHI-26JUL04-B85.5", "subtitle": "85° to 86°",
    "series": "KXHIGHCHI", "ladder_kind": "high", "printed": 85,
    "final": False, "kind": "buy_winner", "ask": 58, "ask_depth": 40,
}


class TestFillMath:
    def test_price_from_yes_dollars(self):
        assert ta.fill_price_cents({"yes_price_dollars": "0.60"}) == 60

    def test_price_from_no_leg(self):
        assert ta.fill_price_cents({"no_price_dollars": "0.40"}) == 60

    def test_price_missing_is_none(self):
        assert ta.fill_price_cents({}) is None

    def test_count_parsing_and_garbage(self):
        assert ta.fill_count({"count_fp": "25"}) == 25.0
        assert ta.fill_count({"count_fp": "n/a"}) == 0.0


class TestLegSummary:
    def test_buy_leg_weighted_avg_and_taker(self):
        fills = [f for f in FILLS if f["ticker"].startswith("KXHIGHCHI")]
        buys = ta.leg_summary(fills, "buy")
        assert buys["qty"] == 40
        assert buys["avg_cents"] == 60.0
        assert buys["taker_share"] == 1.0
        assert buys["first_ts"] == "2026-07-04T21:50:00+00:00"

    def test_empty_side(self):
        sells = ta.leg_summary(FILLS, "sell")
        assert sells["qty"] == 0
        assert sells["avg_cents"] is None


class TestReconstruct:
    def test_only_target_ticker_counted(self):
        t = ta.reconstruct_trade(FILLS, "KXHIGHCHI-26JUL04-B85.5")
        assert t["n_fills"] == 2
        assert t["net_qty"] == 40
        assert t["cost_basis_dollars"] == 24.0
        assert t["proceeds_dollars"] == 0.0


class TestAccount:
    def test_realized_lookup(self):
        assert ta.realized_for_ticker(ACCOUNT, "KXHIGHCHI-26JUL04-B85.5") == 16.0
        assert ta.realized_for_ticker(ACCOUNT, "NOPE") is None

    def test_pick_biggest_win(self):
        assert ta.pick_ticker(ACCOUNT) == "KXHIGHCHI-26JUL04-B85.5"

    def test_pick_biggest_loss(self):
        assert ta.pick_ticker(ACCOUNT, want_loss=True) == "KXHIGHNY-26JUL04-B90.5"

    def test_pick_none_when_all_flat(self):
        assert ta.pick_ticker({"closed_positions": [
            {"ticker": "X", "realized": 0}]}) is None


class TestFindingMatch:
    def test_matches_buy_winner(self):
        f = ta.match_finding([FINDING], "KXHIGHCHI-26JUL04-B85.5")
        assert f["kind"] == "buy_winner"

    def test_earliest_buy_winner_wins(self):
        late = {**FINDING, "ts": "2026-07-04T22:10:00+00:00", "ask": 92}
        f = ta.match_finding([late, FINDING], "KXHIGHCHI-26JUL04-B85.5")
        assert f["ask"] == 58  # the earlier alert

    def test_no_match(self):
        assert ta.match_finding([FINDING], "OTHER") is None


class TestOutcome:
    def test_settlement_beats_realized(self):
        assert ta.classify_outcome("yes", 40, -5) == "win"
        assert ta.classify_outcome("no", 0, 5) == "loss"

    def test_falls_back_to_realized(self):
        assert ta.classify_outcome(None, 0, 16.0) == "win"
        assert ta.classify_outcome(None, 0, -3.0) == "loss"

    def test_open_vs_flat(self):
        assert ta.classify_outcome(None, 40, None) == "open"
        assert ta.classify_outcome(None, 0, None) == "flat"


class TestBuildAutopsy:
    def test_winning_trade_full_story(self):
        a = ta.build_autopsy("KXHIGHCHI-26JUL04-B85.5", FILLS, ACCOUNT,
                             FINDING, "yes")
        assert a["outcome"] == "win"
        assert a["target_date"] == "2026-07-04"
        assert a["realized_dollars"] == 16.0
        # settled yes, held 40, entry 60¢: proceeds 0 + 40*100 - 40*60 = 1600c
        assert a["est_pnl_dollars"] == 16.0
        # edge = 100 - 60 - taker_fee(60); fee = ceil(0.07*60*40/100)=ceil(1.68)=2
        assert a["edge_captured_cents"] == 38.0
        assert a["thesis"]["source"] == "cli_sniper"
        # 21:45 alert -> 21:50 first fill = 5 min
        assert a["thesis"]["reaction_minutes"] == 5.0

    def test_no_thesis_when_finding_missing(self):
        a = ta.build_autopsy("KXHIGHCHI-26JUL04-B85.5", FILLS, ACCOUNT,
                             None, "yes")
        assert a["thesis"] is None
        assert a["edge_captured_cents"] == 38.0

    def test_report_renders_win(self):
        a = ta.build_autopsy("KXHIGHCHI-26JUL04-B85.5", FILLS, ACCOUNT,
                             FINDING, "yes")
        text = ta.format_report(a)
        assert "✅ WIN" in text
        assert "printed **85°**" in text
        assert "Edge captured" in text
