"""Tests for live_watch pure helpers (no network)."""
import json
from datetime import datetime, timezone

import live_watch as lw


class TestFillDedup:
    def test_new_fills_filtered_by_known_ids(self):
        fills = [{"fill_id": "a", "ticker": "X"}, {"fill_id": "b", "ticker": "Y"}]
        assert lw.new_fills(fills, {"a"}) == [{"fill_id": "b", "ticker": "Y"}]

    def test_fills_without_id_dropped(self):
        assert lw.new_fills([{"ticker": "X"}], set()) == []

    def test_empty_inputs(self):
        assert lw.new_fills([], set()) == []
        assert lw.new_fills(None, {"a"}) == []


class TestOpenLongs:
    def test_positive_fp_kept(self):
        pos = [{"ticker": "A", "position_fp": "50.00"},
               {"ticker": "B", "position_fp": "0.00"},
               {"ticker": "C", "position_fp": "-10.00"}]
        longs = lw.open_long_positions(pos)
        assert [p["ticker"] for p in longs] == ["A"]
        assert longs[0]["qty"] == 50.0

    def test_garbage_fp_skipped(self):
        assert lw.open_long_positions([{"position_fp": "n/a"}]) == []


class TestStrengthAlert:
    def test_below_threshold_never(self):
        assert not lw.should_alert_strength({}, "T", 84, 85)

    def test_first_crossing_alerts(self):
        assert lw.should_alert_strength({}, "T", 85, 85)

    def test_same_level_deduped(self):
        state = {"T": {"bid": 85}}
        assert not lw.should_alert_strength(state, "T", 86, 85)

    def test_realerts_on_climb(self):
        state = {"T": {"bid": 85}}
        assert lw.should_alert_strength(state, "T", 88, 85)

    def test_phantom_thin_bid_suppressed(self):
        # 2026-07-04: a 1-lot 99c flicker on an 18c market must not ping
        assert not lw.should_alert_strength({}, "T", 99, 85, depth=1)

    def test_depth_near_best(self):
        bids = [[99, 1], [97, 3], [90, 500]]
        assert lw.bid_depth_near_best(bids) == 4
        assert lw.bid_depth_near_best([]) == 0


class TestAccountSnapshot:
    def test_splits_open_closed_and_totals_realized(self):
        positions = [
            {"ticker": "MIA", "position_fp": "50.00", "realized_pnl_dollars": "0.00",
             "market_exposure_dollars": "0.50"},
            {"ticker": "CHI", "position_fp": "0.00", "realized_pnl_dollars": "18.24",
             "market_exposure_dollars": "0.00"},
            {"ticker": "OLD", "position_fp": "0.00", "realized_pnl_dollars": "0.00",
             "market_exposure_dollars": "0.00"},
        ]
        fills = [{"created_time": "2026-07-04T21:20:45Z", "ticker": "CHI",
                  "action": "sell", "yes_price_dollars": "0.9900", "count_fp": "20.00",
                  "is_taker": True}]
        snap = lw.account_snapshot(117.59, positions, fills, "2026-07-04T21:25:00+00:00")
        assert snap["balance"] == 117.59
        assert snap["realized_total"] == 18.24
        assert [p["ticker"] for p in snap["open_positions"]] == ["MIA"]
        # closed with zero realized (OLD) is dropped; only CHI kept
        assert [p["ticker"] for p in snap["closed_positions"]] == ["CHI"]
        assert snap["recent_fills"][0]["price_c"] == 99

    def test_handles_none_balance_and_empty(self):
        snap = lw.account_snapshot(None, [], [], "2026-07-04T21:25:00+00:00")
        assert snap["balance"] is None
        assert snap["realized_total"] == 0.0
        assert snap["open_positions"] == []


class TestJournalReads:
    def test_known_ids_from_file(self, tmp_path, monkeypatch):
        log = tmp_path / "live_fills.jsonl"
        log.write_text(json.dumps({"fill_id": "x"}) + "\nnot json\n")
        monkeypatch.setattr(lw, "FILLS_LOG", log)
        assert lw.known_fill_ids() == {"x"}

    def test_last_balance(self, tmp_path, monkeypatch):
        log = tmp_path / "live_balance.jsonl"
        log.write_text('{"ts":"t1","balance":100.0}\n{"ts":"t2","balance":97.8}\n')
        monkeypatch.setattr(lw, "BALANCE_LOG", log)
        assert lw.last_logged_balance() == 97.8

    def test_missing_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr(lw, "FILLS_LOG", tmp_path / "none.jsonl")
        monkeypatch.setattr(lw, "BALANCE_LOG", tmp_path / "none2.jsonl")
        assert lw.known_fill_ids() == set()
        assert lw.last_logged_balance() is None


class TestReadsDegraded:
    def test_error_degraded_shapes_are_degraded(self):
        assert lw.reads_degraded(None) is True
        assert lw.reads_degraded({}) is True          # _req_safe swallowed a 401
        assert lw.reads_degraded("not a dict") is True

    def test_real_responses_are_not_degraded(self):
        assert lw.reads_degraded({"fills": []}) is False
        assert lw.reads_degraded({"fills": [{"fill_id": "a"}]}) is False


class TestOverdueSettlements:
    """KXLOWTMIA-26JUL07 sat unsettled 50+ hours (2026-07-10) — the watcher
    now pings instead of a human happening to look."""

    from datetime import date
    TODAY = date(2026, 7, 10)

    def _pos(self, ticker, qty="100.00"):
        return {"ticker": ticker, "position_fp": qty,
                "market_exposure_dollars": "34.94"}

    def test_old_event_flagged_with_age(self):
        out = lw.overdue_settlements([self._pos("KXLOWTMIA-26JUL07-B78.5")], self.TODAY)
        assert len(out) == 1
        assert out[0][1] == 3

    def test_recent_event_not_flagged(self):
        out = lw.overdue_settlements([self._pos("KXHIGHMIA-26JUL09-B92.5")], self.TODAY)
        assert out == []

    def test_zero_position_ignored(self):
        out = lw.overdue_settlements([self._pos("KXLOWTMIA-26JUL07-B78.5", qty="0.00")], self.TODAY)
        assert out == []

    def test_unparseable_ticker_ignored(self):
        out = lw.overdue_settlements([self._pos("NOT-A-TICKER")], self.TODAY)
        assert out == []

    def test_one_ping_per_day(self):
        state = {}
        assert lw.should_alert_overdue(state, "T1", "2026-07-10") is True
        state["overdue:T1"] = "2026-07-10"
        assert lw.should_alert_overdue(state, "T1", "2026-07-10") is False
        assert lw.should_alert_overdue(state, "T1", "2026-07-11") is True


class TestBoundaryWatch:
    """BOS-26JUL16 shape: overnight min 69.1, evening push toward the
    leading bracket's 69° floor, boundary (midnight LST) at 05:00Z."""

    DAY_END = datetime(2026, 7, 17, 5, 0, tzinfo=timezone.utc)
    MARKETS = [
        {"ticker": "KXLOWTBOS-26JUL16-T68", "yes_sub_title": "69° or above"},
        {"ticker": "KXLOWTBOS-26JUL16-B67.5", "yes_sub_title": "67° to 68°"},
        {"ticker": "KXLOWTBOS-26JUL16-T61", "yes_sub_title": "60° or below"},
    ]

    @staticmethod
    def _obs(*rows):
        return [(datetime(2026, 7, d, h, m, tzinfo=timezone.utc), f)
                for d, h, m, f in rows]

    def _evening(self, latest_f):
        return self._obs((16, 8, 54, 69.1), (16, 19, 54, 88.0),
                         (17, 2, 54, 73.0), (17, 3, 54, latest_f))

    def test_leading_bracket_within_margin(self):
        now = datetime(2026, 7, 17, 4, 10, tzinfo=timezone.utc)
        cands = lw.boundary_candidates(self.MARKETS, self._evening(69.5),
                                       now, self.DAY_END)
        assert [c["ticker"] for c in cands] == ["KXLOWTBOS-26JUL16-T68"]
        c = cands[0]
        assert c["floor_f"] == 69 and c["gap_f"] == 0.5
        assert c["run_min_f"] == 69.1 and c["minutes_left"] == 50

    def test_wide_gap_stays_silent(self):
        now = datetime(2026, 7, 17, 4, 10, tzinfo=timezone.utc)
        assert lw.boundary_candidates(self.MARKETS, self._evening(72.0),
                                      now, self.DAY_END) == []

    def test_or_below_bracket_has_no_floor(self):
        now = datetime(2026, 7, 17, 4, 10, tzinfo=timezone.utc)
        assert lw.boundary_candidates([self.MARKETS[2]], self._evening(69.5),
                                      now, self.DAY_END) == []

    def test_past_boundary_empty(self):
        now = datetime(2026, 7, 17, 5, 1, tzinfo=timezone.utc)
        assert lw.boundary_candidates(self.MARKETS, self._evening(69.5),
                                      now, self.DAY_END) == []

    def test_no_obs_empty(self):
        now = datetime(2026, 7, 17, 4, 10, tzinfo=timezone.utc)
        assert lw.boundary_candidates(self.MARKETS, [], now, self.DAY_END) == []

    def test_corroborated_dead_is_the_sweepers_job(self):
        # Two obs at 66.5/66.8 lock the settle <= 67: T68 (floor 69) drops
        # out; the watch follows the ladder down to B67.5 (floor 67).
        obs = self._obs((16, 8, 54, 69.1), (17, 2, 54, 66.5), (17, 3, 54, 66.8))
        now = datetime(2026, 7, 17, 4, 10, tzinfo=timezone.utc)
        cands = lw.boundary_candidates(self.MARKETS, obs, now, self.DAY_END)
        assert [c["ticker"] for c in cands] == ["KXLOWTBOS-26JUL16-B67.5"]

    def test_lone_spike_keeps_watching(self):
        # An uncorroborated 66.0 down-spike (2026-07-04 KMSY class) must not
        # silently kill the watch on the 83c bracket.
        obs = self._obs((16, 8, 54, 69.1), (17, 2, 54, 66.0), (17, 3, 54, 69.5))
        now = datetime(2026, 7, 17, 4, 10, tzinfo=timezone.utc)
        cands = lw.boundary_candidates(self.MARKETS, obs, now, self.DAY_END)
        assert "KXLOWTBOS-26JUL16-T68" in [c["ticker"] for c in cands]

    def test_dedupe_first_then_closer_step(self):
        assert lw.should_alert_boundary({}, "T", 1.9)
        state = {"boundary:T": {"gap_f": 1.9}}
        assert not lw.should_alert_boundary(state, "T", 1.5)
        assert lw.should_alert_boundary(state, "T", 0.9)
