#!/usr/bin/env python3
"""Tests for market_timeseries.py — snapshot loading, price analysis, bot detection, entry windows."""

import json

import pytest


# ─── Snapshot builders ───────────────────────────────────────────────────────


def _make_snapshot(
    ts="2026-02-14T14:00:00-05:00",
    ts_utc="2026-02-14T19:00:00Z",
    city="NYC",
    target_date="2026-02-14",
    hours_to_settlement=12.0,
    brackets=None,
):
    """Build a minimal snapshot dict."""
    if brackets is None:
        brackets = {
            "KXHIGHNY-26FEB14-B42.5": {
                "yes_bid": 30,
                "yes_ask": 35,
                "volume": 100,
                "bracket": "42-43",
            },
            "KXHIGHNY-26FEB14-B44.5": {
                "yes_bid": 20,
                "yes_ask": 25,
                "volume": 50,
                "bracket": "44-45",
            },
        }
    return {
        "ts": ts,
        "ts_utc": ts_utc,
        "city": city,
        "target_date": target_date,
        "hours_to_settlement": hours_to_settlement,
        "brackets": brackets,
    }


def _write_snapshots(tmp_path, city, date_str, snapshots):
    """Write snapshot list to the expected JSONL path."""
    snap_dir = tmp_path / "backtest" / "market_snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    path = snap_dir / f"{date_str}_{city}.jsonl"
    with open(path, "w") as f:
        for s in snapshots:
            f.write(json.dumps(s) + "\n")
    return path


@pytest.fixture(autouse=True)
def _redirect_snapshot_dir(tmp_path, monkeypatch):
    """Redirect SNAPSHOT_DIR to tmp_path for all tests."""
    import market_timeseries
    monkeypatch.setattr(market_timeseries, "SNAPSHOT_DIR", tmp_path / "backtest" / "market_snapshots")
    yield


# ─── load_snapshots ─────────────────────────────────────────────────────────


class TestLoadSnapshots:
    """Test loading snapshots from JSONL files."""

    def test_load_valid_snapshots(self, tmp_path):
        from market_timeseries import load_snapshots

        snaps = [
            _make_snapshot(ts_utc="2026-02-14T18:00:00Z"),
            _make_snapshot(ts_utc="2026-02-14T19:00:00Z"),
            _make_snapshot(ts_utc="2026-02-14T20:00:00Z"),
        ]
        _write_snapshots(tmp_path, "NYC", "2026-02-14", snaps)

        loaded = load_snapshots("NYC", "2026-02-14")
        assert len(loaded) == 3
        # Should be sorted by ts_utc
        assert loaded[0]["ts_utc"] == "2026-02-14T18:00:00Z"
        assert loaded[2]["ts_utc"] == "2026-02-14T20:00:00Z"

    def test_load_missing_file(self, tmp_path):
        from market_timeseries import load_snapshots
        assert load_snapshots("NYC", "2099-01-01") == []

    def test_load_skips_malformed_lines(self, tmp_path):
        from market_timeseries import load_snapshots

        snap_dir = tmp_path / "backtest" / "market_snapshots"
        snap_dir.mkdir(parents=True, exist_ok=True)
        path = snap_dir / "2026-02-14_NYC.jsonl"
        with open(path, "w") as f:
            f.write(json.dumps(_make_snapshot()) + "\n")
            f.write("{bad json\n")
            f.write(json.dumps(_make_snapshot(ts_utc="2026-02-14T20:00:00Z")) + "\n")

        loaded = load_snapshots("NYC", "2026-02-14")
        assert len(loaded) == 2  # skips malformed line

    def test_load_empty_file(self, tmp_path):
        from market_timeseries import load_snapshots

        snap_dir = tmp_path / "backtest" / "market_snapshots"
        snap_dir.mkdir(parents=True, exist_ok=True)
        (snap_dir / "2026-02-14_NYC.jsonl").write_text("")

        assert load_snapshots("NYC", "2026-02-14") == []


# ─── analyze_price_series ───────────────────────────────────────────────────


class TestAnalyzePriceSeries:
    """Test price series analysis from snapshot data."""

    def test_basic_analysis(self, tmp_path):
        from market_timeseries import analyze_price_series

        # 4 snapshots with increasing price on B42.5
        snaps = []
        for i, price in enumerate([30, 32, 35, 33]):
            snaps.append(_make_snapshot(
                ts_utc=f"2026-02-14T{18+i}:00:00Z",
                ts=f"2026-02-14T{13+i}:00:00-05:00",
                brackets={
                    "KXHIGHNY-26FEB14-B42.5": {
                        "yes_bid": price,
                        "yes_ask": price + 5,
                        "volume": 100,
                        "bracket": "42-43",
                    }
                },
            ))
        _write_snapshots(tmp_path, "NYC", "2026-02-14", snaps)

        result = analyze_price_series("NYC", "2026-02-14")
        assert "KXHIGHNY-26FEB14-B42.5" in result

        stats = result["KXHIGHNY-26FEB14-B42.5"]
        assert stats["n_snapshots"] == 4
        assert stats["final_price"] > 0
        assert stats["total_range"] > 0
        assert len(stats["price_changes"]) == 3  # 4 points -> 3 changes

    def test_volatility_computed(self, tmp_path):
        from market_timeseries import analyze_price_series

        # Alternating prices = higher volatility
        snaps = []
        for i, price in enumerate([20, 40, 20, 40, 20]):
            snaps.append(_make_snapshot(
                ts_utc=f"2026-02-14T{18+i}:00:00Z",
                ts=f"2026-02-14T{13+i}:00:00-05:00",
                brackets={
                    "TEST-TICKER": {
                        "yes_bid": price,
                        "yes_ask": price,
                        "volume": 50,
                        "bracket": "42-43",
                    }
                },
            ))
        _write_snapshots(tmp_path, "NYC", "2026-02-14", snaps)

        result = analyze_price_series("NYC", "2026-02-14", ticker="TEST-TICKER")
        assert "TEST-TICKER" in result
        assert result["TEST-TICKER"]["volatility"] > 0

    def test_single_snapshot_no_changes(self, tmp_path):
        from market_timeseries import analyze_price_series

        snaps = [_make_snapshot(ts_utc="2026-02-14T18:00:00Z")]
        _write_snapshots(tmp_path, "NYC", "2026-02-14", snaps)

        result = analyze_price_series("NYC", "2026-02-14")
        for ticker, stats in result.items():
            assert stats["price_changes"] == []
            assert stats["volatility"] == 0.0

    def test_no_data_returns_empty(self, tmp_path):
        from market_timeseries import analyze_price_series
        assert analyze_price_series("NYC", "2099-01-01") == {}

    def test_max_drawdown(self, tmp_path):
        from market_timeseries import analyze_price_series

        # Price goes 50 -> 60 -> 30 -> 40  => max drawdown = 60-30 = 30
        snaps = []
        for i, price in enumerate([50, 60, 30, 40]):
            snaps.append(_make_snapshot(
                ts_utc=f"2026-02-14T{18+i}:00:00Z",
                ts=f"2026-02-14T{13+i}:00:00-05:00",
                brackets={
                    "DD-TEST": {"yes_bid": price, "yes_ask": price, "volume": 10, "bracket": "42-43"}
                },
            ))
        _write_snapshots(tmp_path, "NYC", "2026-02-14", snaps)

        result = analyze_price_series("NYC", "2026-02-14", ticker="DD-TEST")
        assert result["DD-TEST"]["max_drawdown"] == 30.0


# ─── detect_bot_events ──────────────────────────────────────────────────────


class TestDetectBotEvents:
    """Test bot event detection around DSM/6-hour release times."""

    def test_detects_price_jump_at_dsm(self, tmp_path):
        from market_timeseries import detect_bot_events

        # NYC DSM times are at 20:21, 21:21, 05:17 Zulu
        # Place snapshots before and after the 20:21 DSM time
        before = _make_snapshot(
            ts_utc="2026-02-14T20:00:00Z",
            brackets={
                "KXHIGHNY-26FEB14-B42.5": {
                    "yes_bid": 30, "yes_ask": 35, "volume": 100, "bracket": "42-43",
                }
            },
        )
        after = _make_snapshot(
            ts_utc="2026-02-14T20:45:00Z",
            brackets={
                "KXHIGHNY-26FEB14-B42.5": {
                    "yes_bid": 50, "yes_ask": 55, "volume": 200, "bracket": "42-43",
                }
            },
        )
        _write_snapshots(tmp_path, "NYC", "2026-02-14", [before, after])

        events = detect_bot_events("NYC", "2026-02-14")
        # Should detect at least the 20:21 DSM event
        dsm_events = [e for e in events if e["event_type"] == "dsm"]
        assert len(dsm_events) >= 1

        # The price jump should be detected
        found_significant = any(
            e["max_move"] > 5 and e.get("significant")
            for e in dsm_events
        )
        # 20c jump in midpoint (32.5 -> 52.5) is definitely significant
        assert found_significant

    def test_no_snapshots_returns_empty(self, tmp_path):
        from market_timeseries import detect_bot_events
        assert detect_bot_events("NYC", "2099-01-01") == []

    def test_single_snapshot_returns_empty(self, tmp_path):
        from market_timeseries import detect_bot_events

        _write_snapshots(tmp_path, "NYC", "2026-02-14", [_make_snapshot()])
        assert detect_bot_events("NYC", "2026-02-14") == []


# ─── optimal_entry_windows ──────────────────────────────────────────────────


class TestOptimalEntryWindows:
    """Test time-of-day entry window analysis."""

    def test_groups_by_time_bucket(self, tmp_path):
        from market_timeseries import optimal_entry_windows

        # Build snapshots across different time windows
        snaps = []
        # Morning snapshots (08:xx ET -> 13:xx UTC)
        for i in range(3):
            snaps.append(_make_snapshot(
                ts=f"2026-02-14T08:{i*15:02d}:00-05:00",
                ts_utc=f"2026-02-14T13:{i*15:02d}:00Z",
                brackets={
                    "T1": {"yes_bid": 30 + i, "yes_ask": 35 + i, "volume": 50, "bracket": "42-43"}
                },
            ))
        # Afternoon snapshots (14:xx ET -> 19:xx UTC)
        for i in range(3):
            snaps.append(_make_snapshot(
                ts=f"2026-02-14T14:{i*15:02d}:00-05:00",
                ts_utc=f"2026-02-14T19:{i*15:02d}:00Z",
                brackets={
                    "T1": {"yes_bid": 40 + i * 5, "yes_ask": 45 + i * 5, "volume": 80, "bracket": "42-43"}
                },
            ))
        _write_snapshots(tmp_path, "NYC", "2026-02-14", snaps)

        windows = optimal_entry_windows("NYC", ["2026-02-14"])
        assert len(windows) >= 1
        # Each result should have expected fields
        for w in windows:
            assert "window" in w
            assert "entry_quality" in w
            assert "avg_volatility" in w
            assert "n_observations" in w

    def test_no_data_returns_empty(self, tmp_path):
        from market_timeseries import optimal_entry_windows
        assert optimal_entry_windows("NYC", ["2099-01-01"]) == []

    def test_sorted_by_quality_descending(self, tmp_path):
        from market_timeseries import optimal_entry_windows

        # Create snapshots spanning multiple time windows with varying volatility
        snaps = []
        # Low-vol morning window
        for i in range(4):
            snaps.append(_make_snapshot(
                ts=f"2026-02-14T09:{i*10:02d}:00-05:00",
                ts_utc=f"2026-02-14T14:{i*10:02d}:00Z",
                brackets={
                    "T1": {"yes_bid": 30 + i, "yes_ask": 35 + i, "volume": 50, "bracket": "42-43"}
                },
            ))
        # High-vol evening window
        for i in range(4):
            snaps.append(_make_snapshot(
                ts=f"2026-02-14T18:{i*10:02d}:00-05:00",
                ts_utc=f"2026-02-14T23:{i*10:02d}:00Z",
                brackets={
                    "T1": {"yes_bid": 20 + i * 20, "yes_ask": 25 + i * 20, "volume": 100, "bracket": "42-43"}
                },
            ))
        _write_snapshots(tmp_path, "NYC", "2026-02-14", snaps)

        windows = optimal_entry_windows("NYC", ["2026-02-14"])
        if len(windows) >= 2:
            # Quality should be descending
            for i in range(len(windows) - 1):
                assert windows[i]["entry_quality"] >= windows[i + 1]["entry_quality"]


# ─── Helper functions ────────────────────────────────────────────────────────


class TestParseBracketFromTitle:
    """Test bracket parsing from Kalshi market titles."""

    def test_range_bracket(self):
        from market_timeseries import parse_bracket_from_title
        assert parse_bracket_from_title("Between 42 to 43\u00b0F") == "42-43"

    def test_above_bracket(self):
        from market_timeseries import parse_bracket_from_title
        assert parse_bracket_from_title("50\u00b0F or more") == ">50"

    def test_below_bracket(self):
        from market_timeseries import parse_bracket_from_title
        result = parse_bracket_from_title("Below 30\u00b0F")
        assert result == "<30"

    def test_unknown_format(self):
        from market_timeseries import parse_bracket_from_title
        assert parse_bracket_from_title("some random text") == "unknown"


class TestExtractTargetDate:
    """Test ticker date extraction."""

    def test_valid_ticker(self):
        from market_timeseries import extract_target_date_from_ticker
        assert extract_target_date_from_ticker("KXHIGHNY-26FEB14-B42.5") == "2026-02-14"

    def test_invalid_ticker(self):
        from market_timeseries import extract_target_date_from_ticker
        assert extract_target_date_from_ticker("NO-DATE-HERE") is None
