"""Tests for live_watch pure helpers (no network)."""
import json

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
