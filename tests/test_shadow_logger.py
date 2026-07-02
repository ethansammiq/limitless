"""Tests for shadow_logger pure helpers (no network)."""
import json

import shadow_logger as sl


class TestWindows:
    def test_high_window_open(self):
        assert sl.in_window("high", 13)
        assert sl.in_window("high", 18)

    def test_high_window_closed(self):
        assert not sl.in_window("high", 12)
        assert not sl.in_window("high", 19)

    def test_low_window(self):
        assert sl.in_window("low", 4)
        assert sl.in_window("low", 9)
        assert not sl.in_window("low", 10)
        assert not sl.in_window("low", 13)


class TestLivePriced:
    def test_band(self):
        assert sl.is_live_priced(5)
        assert sl.is_live_priced(95)
        assert not sl.is_live_priced(4)
        assert not sl.is_live_priced(96)
        assert not sl.is_live_priced(None)


class TestKalshiBookMetrics:
    def test_ask_derived_from_no_bids(self):
        # yes/no sides are both resting bids; YES ask = 100 - best NO bid
        book = {"yes": [[40, 100], [38, 50]], "no": [[55, 30], [52, 200]]}
        m = sl.kalshi_book_metrics(book)
        assert m["yes_bid"] == 40
        assert m["yes_ask"] == 45
        assert m["ask_sz"] == 30
        assert m["bid_sz"] == 100

    def test_cum_depth_band(self):
        # best NO bid 55; band includes levels >= 50
        book = {"yes": [], "no": [[55, 30], [52, 200], [49, 999]]}
        m = sl.kalshi_book_metrics(book)
        assert m["ask_cum5c"] == 230

    def test_empty_book_is_none(self):
        assert sl.kalshi_book_metrics({"yes": [], "no": []}) is None

    def test_unsorted_levels_handled(self):
        book = {"yes": [[38, 50], [40, 100]], "no": [[52, 200], [55, 30]]}
        m = sl.kalshi_book_metrics(book)
        assert m["yes_bid"] == 40
        assert m["yes_ask"] == 45


class TestPolyBookMetrics:
    def test_basic(self):
        book = {"bids": [{"price": "0.40", "size": "120"}],
                "asks": [{"price": "0.45", "size": "80"},
                         {"price": "0.48", "size": "40"},
                         {"price": "0.60", "size": "500"}]}
        m = sl.poly_book_metrics(book)
        assert m["yes_bid"] == 40.0
        assert m["yes_ask"] == 45.0
        assert m["ask_sz"] == 80.0
        # band = asks <= 50c: 80 + 40
        assert m["ask_cum5c"] == 120.0

    def test_malformed_levels_skipped(self):
        book = {"bids": [{"price": "bad", "size": "1"}, {"price": "0.30", "size": "10"}],
                "asks": []}
        m = sl.poly_book_metrics(book)
        assert m["yes_bid"] == 30.0
        assert m["yes_ask"] is None

    def test_empty_is_none(self):
        assert sl.poly_book_metrics({"bids": [], "asks": []}) is None


class TestConfig:
    def test_all_series_have_known_tz_and_kind(self):
        for series, cfg in sl.KALSHI_SERIES.items():
            assert cfg["tz"] in sl._TZ, series
            assert cfg["kind"] in ("high", "low"), series

    def test_forty_ladders(self):
        highs = [s for s, c in sl.KALSHI_SERIES.items() if c["kind"] == "high"]
        lows = [s for s, c in sl.KALSHI_SERIES.items() if c["kind"] == "low"]
        assert len(highs) == 20
        assert len(lows) == 20


class TestWriteRows(object):
    def test_appends_jsonl(self, tmp_path, monkeypatch):
        from datetime import datetime, timezone
        monkeypatch.setattr(sl, "OUT_DIR", tmp_path / "books")
        now = datetime(2026, 7, 2, 21, 0, tzinfo=timezone.utc)
        p1 = sl.write_rows([{"a": 1}], now)
        p2 = sl.write_rows([{"b": 2}], now)
        assert p1 == p2
        lines = [json.loads(x) for x in p1.read_text().splitlines()]
        assert lines == [{"a": 1}, {"b": 2}]
