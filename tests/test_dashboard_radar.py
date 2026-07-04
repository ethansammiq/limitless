"""Tests for the dashboard opportunity-radar classification (no network)."""
import dashboard_server as ds


class TestRadarStatus:
    def test_no_verdict_yet(self):
        assert ds._radar_status(99.0, 100.0, None) == "open"

    def test_dead_below_certain(self):
        # 2026-07-02 live case: "98 or below" vs certain >= 100
        assert ds._radar_status(None, 98.0, 100) == "dead"

    def test_leader_contains_certain(self):
        assert ds._radar_status(99.0, 100.0, 100) == "leader"

    def test_target_contains_certain_plus_one(self):
        # CLI offset bracket: certain 100 -> 101-102 is the +1 target
        assert ds._radar_status(101.0, 102.0, 100) == "target"

    def test_open_above(self):
        assert ds._radar_status(103.0, 104.0, 100) == "open"

    def test_open_tail_never_dead(self):
        assert ds._radar_status(107.0, None, 100) == "open"

    def test_leader_wins_when_bracket_holds_both(self):
        # 100-101 contains certain AND certain+1 -> leader
        assert ds._radar_status(100.0, 101.0, 100) == "leader"


class TestRadarIngest:
    def test_keeps_full_ladder_today_only(self):
        code = ds._today_ticker_code("NYC")
        markets = [
            {"ticker": f"KXHIGHNY-{code}-T99", "subtitle": "98° or below",
             "yes_bid": 42, "yes_ask": 100, "volume": 3},
            {"ticker": f"KXHIGHNY-{code}-B99.5", "subtitle": "99° to 100°",
             "yes_bid": 76, "yes_ask": 81, "volume": 500},
            {"ticker": "KXHIGHNY-26DEC25-B99.5", "subtitle": "99° to 100°",
             "yes_bid": 1, "yes_ask": 2, "volume": 9},
        ]
        ds._radar_ingest("NYC", markets, "2026-07-04T12:00:00")
        snap = ds._radar["NYC"]
        tickers = [b["ticker"] for b in snap["brackets"]]
        assert len(tickers) == 2
        assert all(f"-{code}-" in t for t in tickers)
        # low-volume tail KEPT (the whole point vs _ingest's top-N)
        assert any(t.endswith("T99") for t in tickers)
        # sorted by lower bound, open-bottom tail first
        assert snap["brackets"][0]["ticker"].endswith("T99")
        assert snap["brackets"][0]["hi"] == 98.0
