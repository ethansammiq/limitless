"""Tests for stale_price_detector.py — Ensemble shift tracking."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from stale_price_detector import (
    StaleAlert,
    ScanSnapshot,
    build_snapshot,
    detect_stale_prices,
    format_stale_alerts,
    load_previous_state,
    save_current_state,
)

ET = ZoneInfo("America/New_York")


# ─── Helpers ───────────────────────────────────────────


def _snap(
    mean: float = 73.0,
    std: float = 1.5,
    bids: dict | None = None,
) -> ScanSnapshot:
    """Create a ScanSnapshot with sensible defaults."""
    if bids is None:
        bids = {
            "T-72-73": {"bid": 45, "title": "72° to 73°F"},
            "T-74-75": {"bid": 30, "title": "74° to 75°F"},
            "T-70-71": {"bid": 20, "title": "70° to 71°F"},
        }
    return ScanSnapshot(
        mean=mean,
        std=std,
        timestamp=datetime.now(ET).isoformat(),
        bracket_bids=bids,
    )


# ─── Test: ScanSnapshot serialization ─────────────────


class TestScanSnapshot:
    """ScanSnapshot round-trip serialization."""

    def test_to_dict_and_back(self):
        snap = _snap(mean=74.5, std=1.2)
        d = snap.to_dict()
        restored = ScanSnapshot.from_dict(d)
        assert restored.mean == 74.5
        assert restored.std == 1.2
        assert "T-72-73" in restored.bracket_bids
        assert restored.bracket_bids["T-72-73"]["bid"] == 45

    def test_from_dict_missing_fields(self):
        snap = ScanSnapshot.from_dict({})
        assert snap.mean == 0
        assert snap.std == 0
        assert snap.bracket_bids == {}
        assert snap.timestamp == ""

    def test_from_dict_extra_fields_ignored(self):
        d = {"mean": 70.0, "std": 1.0, "timestamp": "ts", "bracket_bids": {}, "extra": "junk"}
        snap = ScanSnapshot.from_dict(d)
        assert snap.mean == 70.0


# ─── Test: build_snapshot ─────────────────────────────


class TestBuildSnapshot:
    """build_snapshot correctly extracts bracket bids."""

    def test_basic_build(self):
        brackets = [
            {"ticker": "T-72-73", "title": "72° to 73°F", "yes_bid": 45, "yes_ask": 55},
            {"ticker": "T-74-75", "title": "74° to 75°F", "yes_bid": 30, "yes_ask": 40},
        ]
        snap = build_snapshot("NYC", 73.0, 1.5, brackets)
        assert snap.mean == 73.0
        assert snap.std == 1.5
        assert len(snap.bracket_bids) == 2
        assert snap.bracket_bids["T-72-73"]["bid"] == 45
        assert snap.bracket_bids["T-74-75"]["title"] == "74° to 75°F"

    def test_empty_brackets(self):
        snap = build_snapshot("NYC", 73.0, 1.5, [])
        assert snap.bracket_bids == {}

    def test_missing_ticker_skipped(self):
        brackets = [
            {"ticker": "", "title": "bad", "yes_bid": 50},
            {"ticker": "T-72-73", "title": "72° to 73°F", "yes_bid": 45},
        ]
        snap = build_snapshot("NYC", 73.0, 1.5, brackets)
        assert len(snap.bracket_bids) == 1
        assert "T-72-73" in snap.bracket_bids

    def test_uses_subtitle_fallback(self):
        """If title is empty, should use subtitle."""
        brackets = [
            {"ticker": "T-72-73", "title": "", "subtitle": "72-73 subtitle", "yes_bid": 45},
        ]
        snap = build_snapshot("NYC", 73.0, 1.5, brackets)
        assert snap.bracket_bids["T-72-73"]["title"] == "72-73 subtitle"


# ─── Test: detect_stale_prices ─────────────────────────


@pytest.fixture
def stale_config(monkeypatch):
    """Pin detection thresholds to production defaults for determinism."""
    monkeypatch.setattr("stale_price_detector.STALE_PRICE_ENABLED", True)
    monkeypatch.setattr("stale_price_detector.STALE_PRICE_MIN_SHIFT_F", 1.5)
    monkeypatch.setattr("stale_price_detector.STALE_PRICE_MIN_GAP_CENTS", 8)


class TestDetectStalePrices:
    """Core stale price detection logic.

    Expected repricing is the model-implied probability change of each
    bracket between the two normal-approximated ensembles (mean/std), so
    the numbers in comments come from NormalDist CDFs.
    """

    def test_no_previous_returns_empty(self):
        current = _snap()
        alerts = detect_stale_prices("NYC", current, None)
        assert alerts == []

    def test_small_shift_returns_empty(self, stale_config):
        """Shift of 1.0°F < 1.5°F threshold → no alerts, even for flat bids."""
        prev = _snap(mean=73.0)
        curr = _snap(mean=74.0)  # identical default bids → all flat
        alerts = detect_stale_prices("NYC", curr, prev)
        assert alerts == []

    def test_stale_unmoved_bid_at_min_shift_warmer(self, stale_config):
        """Canonical stale case: +1.5°F shift, above-mean bid did not move → alert."""
        prev = _snap(mean=73.0, std=1.5, bids={
            "T-74-75": {"bid": 30, "title": "74° to 75°F"},
        })
        curr = _snap(mean=74.5, std=1.5, bids={
            "T-74-75": {"bid": 30, "title": "74° to 75°F"},  # bid didn't move!
        })
        alerts = detect_stale_prices("NYC", curr, prev)
        # P(74-76) goes 0.230 → 0.472 → expected_change = +24¢
        # bid_change = 0 <= 24 - 8 = 16 → stale
        assert len(alerts) == 1
        assert alerts[0].direction == "warmer"
        assert alerts[0].mean_shift_f == 1.5
        assert alerts[0].ticker == "T-74-75"
        assert alerts[0].expected_bid_change == 24
        assert alerts[0].prev_bid == 30
        assert alerts[0].actual_bid == 30

    def test_stale_unmoved_bid_cooler(self, stale_config):
        """Cooler shift: above-mean bracket should have collapsed but sat still → alert."""
        prev = _snap(mean=75.0, std=1.5, bids={
            "T-75-76": {"bid": 40, "title": "75° to 76°F"},
        })
        curr = _snap(mean=73.5, std=1.5, bids={
            "T-75-76": {"bid": 40, "title": "75° to 76°F"},  # bid didn't move!
        })
        alerts = detect_stale_prices("NYC", curr, prev)
        # P(75-77) goes 0.409 → 0.149 → expected_change = -26¢
        # bid_change = 0 >= -26 + 8 = -18 → stale
        assert len(alerts) == 1
        assert alerts[0].direction == "cooler"
        assert alerts[0].mean_shift_f == -1.5
        assert alerts[0].expected_bid_change == -26

    def test_correctly_repriced_no_alert(self, stale_config):
        """Bid moved commensurately with the warranted change → no alert."""
        prev = _snap(mean=73.0, std=1.5, bids={
            "T-74-75": {"bid": 30, "title": "74° to 75°F"},
        })
        curr = _snap(mean=74.5, std=1.5, bids={
            "T-74-75": {"bid": 52, "title": "74° to 75°F"},  # +22¢ vs +24¢ warranted
        })
        alerts = detect_stale_prices("NYC", curr, prev)
        # bid_change = 22 > 24 - 8 = 16 → healthy
        assert alerts == []

    def test_correctly_repriced_cooler_no_alert(self, stale_config):
        """Cooler shift, bid collapsed as warranted → no alert."""
        prev = _snap(mean=75.0, std=1.5, bids={
            "T-75-76": {"bid": 40, "title": "75° to 76°F"},
        })
        curr = _snap(mean=73.5, std=1.5, bids={
            "T-75-76": {"bid": 16, "title": "75° to 76°F"},  # -24¢ vs -26¢ warranted
        })
        alerts = detect_stale_prices("NYC", curr, prev)
        # bid_change = -24 < -26 + 8 = -18 → healthy
        assert alerts == []

    def test_below_mean_bracket_collapsing_on_warm_shift_not_flagged(self, stale_config):
        """Regression for inverted logic: a below-mean bracket that correctly
        collapsed on a warmer shift must NOT be flagged, while an above-mean
        bracket that sat still MUST be."""
        prev = _snap(mean=73.0, std=1.5, bids={
            "T-70-71": {"bid": 38, "title": "70° to 71°F"},  # below mean
            "T-77-78": {"bid": 20, "title": "77° to 78°F"},  # above new mean
        })
        curr = _snap(mean=76.0, std=1.5, bids={
            "T-70-71": {"bid": 16, "title": "70° to 71°F"},  # collapsed -22¢ (correct)
            "T-77-78": {"bid": 20, "title": "77° to 78°F"},  # sat still (stale)
        })
        alerts = detect_stale_prices("NYC", curr, prev)
        # T-70-71: expected -23¢, moved -22¢ → healthy
        # T-77-78: expected +23¢, moved 0¢ → stale
        assert len(alerts) == 1
        assert alerts[0].ticker == "T-77-78"
        assert alerts[0].expected_bid_change == 23

    def test_below_mean_bracket_flat_on_warm_shift_is_stale(self, stale_config):
        """Position-aware direction: a below-mean bracket that should have
        collapsed on a warmer shift but didn't move is stale (NO-side edge)."""
        prev = _snap(mean=73.0, std=1.5, bids={
            "T-70-71": {"bid": 38, "title": "70° to 71°F"},
        })
        curr = _snap(mean=76.0, std=1.5, bids={
            "T-70-71": {"bid": 38, "title": "70° to 71°F"},  # should have collapsed
        })
        alerts = detect_stale_prices("NYC", curr, prev)
        # expected_change = -23¢, bid_change = 0 >= -23 + 8 = -15 → stale
        assert len(alerts) == 1
        assert alerts[0].ticker == "T-70-71"
        assert alerts[0].expected_bid_change == -23
        assert alerts[0].direction == "warmer"

    def test_far_bracket_small_warranted_change_skipped(self, stale_config):
        """Bracket far from both means: shift passes the threshold but the
        warranted repricing is < MIN_GAP → an unmoved bid is not stale."""
        prev = _snap(mean=73.0, std=1.5, bids={
            "T-60-61": {"bid": 20, "title": "60° to 61°F"},
        })
        curr = _snap(mean=74.5, std=1.5, bids={
            "T-60-61": {"bid": 20, "title": "60° to 61°F"},
        })
        alerts = detect_stale_prices("NYC", curr, prev)
        assert alerts == []

    def test_tail_bracket_uses_open_ended_bounds(self, stale_config):
        """'X or above' tail brackets get a position-aware expectation too."""
        prev = _snap(mean=73.0, std=1.5, bids={
            "T-78UP": {"bid": 15, "title": "78° or above"},
        })
        curr = _snap(mean=76.0, std=1.5, bids={
            "T-78UP": {"bid": 15, "title": "78° or above"},  # should have risen
        })
        alerts = detect_stale_prices("NYC", curr, prev)
        # P(>=78) goes 0.0004 → 0.091 → expected_change = +9¢; flat bid → stale
        assert len(alerts) == 1
        assert alerts[0].expected_bid_change == 9

    def test_unparseable_title_skipped(self, stale_config):
        """Brackets whose title can't be parsed into a range are skipped."""
        prev = _snap(mean=73.0, std=1.5, bids={
            "T-???": {"bid": 30, "title": "???"},
        })
        curr = _snap(mean=76.0, std=1.5, bids={
            "T-???": {"bid": 30, "title": "???"},
        })
        alerts = detect_stale_prices("NYC", curr, prev)
        assert alerts == []

    def test_zero_std_falls_back_to_default(self, stale_config):
        """Legacy state with std=0 must not crash NormalDist; fallback σ used."""
        prev = _snap(mean=73.0, std=0, bids={
            "T-74-75": {"bid": 30, "title": "74° to 75°F"},
        })
        curr = _snap(mean=76.0, std=0, bids={
            "T-74-75": {"bid": 30, "title": "74° to 75°F"},
        })
        alerts = detect_stale_prices("NYC", curr, prev)
        # With fallback σ=2.0: P(74-76) goes 0.242 → 0.341 → expected +10¢
        assert len(alerts) == 1
        assert alerts[0].expected_bid_change == 10

    def test_disabled_returns_empty(self, monkeypatch):
        """Feature flag off → no alerts."""
        monkeypatch.setattr("stale_price_detector.STALE_PRICE_ENABLED", False)
        prev = _snap(mean=70.0)
        curr = _snap(mean=75.0)
        alerts = detect_stale_prices("NYC", curr, prev)
        assert alerts == []

    def test_zero_mean_returns_empty(self):
        """Zero mean in either snapshot → skip."""
        prev = _snap(mean=0)
        curr = _snap(mean=73.0)
        alerts = detect_stale_prices("NYC", curr, prev)
        assert alerts == []

        prev2 = _snap(mean=73.0)
        curr2 = _snap(mean=0)
        alerts2 = detect_stale_prices("NYC", curr2, prev2)
        assert alerts2 == []

    def test_new_ticker_not_in_previous(self, stale_config):
        """Bracket in current but not in previous → skipped, no crash."""
        prev = _snap(mean=70.0, bids={
            "T-70-71": {"bid": 40, "title": "70° to 71°F"},
        })
        curr = _snap(mean=72.0, bids={
            "T-72-73": {"bid": 45, "title": "72° to 73°F"},  # new ticker
        })
        alerts = detect_stale_prices("NYC", curr, prev)
        assert alerts == []  # skipped because T-72-73 not in previous

    def test_zero_bid_skipped(self, stale_config):
        """Brackets with zero bid are skipped."""
        prev = _snap(mean=70.0, bids={
            "T-72-73": {"bid": 0, "title": "72° to 73°F"},
        })
        curr = _snap(mean=73.0, bids={
            "T-72-73": {"bid": 40, "title": "72° to 73°F"},
        })
        alerts = detect_stale_prices("NYC", curr, prev)
        assert alerts == []

    def test_bid_outside_tradeable_range_skipped(self, stale_config):
        """Bids below 15¢ or above 85¢ are skipped (not tradeable)."""
        prev = _snap(mean=73.0, std=1.5, bids={
            "T-76-77": {"bid": 5, "title": "76° to 77°F"},
            "T-77-78": {"bid": 90, "title": "77° to 78°F"},
        })
        curr = _snap(mean=76.0, std=1.5, bids={
            "T-76-77": {"bid": 5, "title": "76° to 77°F"},   # stale but < 15¢
            "T-77-78": {"bid": 90, "title": "77° to 78°F"},  # stale but > 85¢
        })
        alerts = detect_stale_prices("NYC", curr, prev)
        assert alerts == []

    def test_multiple_stale_brackets(self, stale_config):
        """Multiple brackets stale in same shift → multiple alerts."""
        prev = _snap(mean=73.0, std=1.5, bids={
            "T-76-77": {"bid": 20, "title": "76° to 77°F"},
            "T-77-78": {"bid": 20, "title": "77° to 78°F"},
        })
        curr = _snap(mean=76.0, std=1.5, bids={
            "T-76-77": {"bid": 20, "title": "76° to 77°F"},  # didn't move
            "T-77-78": {"bid": 20, "title": "77° to 78°F"},  # didn't move
        })
        alerts = detect_stale_prices("NYC", curr, prev)
        assert len(alerts) == 2
        tickers = {a.ticker for a in alerts}
        assert "T-76-77" in tickers
        assert "T-77-78" in tickers


# ─── Test: state persistence ──────────────────────────


class TestStatePersistence:
    """State save/load round-trips correctly."""

    def test_round_trip(self, tmp_path, monkeypatch):
        state_file = tmp_path / "stale_state.json"
        monkeypatch.setattr("stale_price_detector.STATE_PATH", state_file)

        states = {
            "NYC": _snap(mean=73.5, std=1.2),
            "CHI": _snap(mean=65.0, std=2.0),
        }
        save_current_state(states)
        loaded = load_previous_state()

        assert "NYC" in loaded
        assert "CHI" in loaded
        assert loaded["NYC"].mean == 73.5
        assert loaded["CHI"].std == 2.0
        assert "T-72-73" in loaded["NYC"].bracket_bids

    def test_empty_state_file(self, tmp_path, monkeypatch):
        state_file = tmp_path / "stale_state.json"
        monkeypatch.setattr("stale_price_detector.STATE_PATH", state_file)
        loaded = load_previous_state()
        assert loaded == {}

    def test_corrupt_state_file(self, tmp_path, monkeypatch):
        state_file = tmp_path / "stale_state.json"
        state_file.write_text("not json {{{")
        monkeypatch.setattr("stale_price_detector.STATE_PATH", state_file)
        loaded = load_previous_state()
        assert loaded == {}

    def test_overwrite_state(self, tmp_path, monkeypatch):
        """Second save overwrites first."""
        state_file = tmp_path / "stale_state.json"
        monkeypatch.setattr("stale_price_detector.STATE_PATH", state_file)

        save_current_state({"NYC": _snap(mean=70.0)})
        save_current_state({"NYC": _snap(mean=75.0)})
        loaded = load_previous_state()
        assert loaded["NYC"].mean == 75.0


# ─── Test: format_stale_alerts ─────────────────────────


class TestFormatStaleAlerts:
    """Alert formatting for Discord."""

    def test_empty_alerts(self):
        assert format_stale_alerts([]) == ""

    def test_single_warmer_alert(self, monkeypatch):
        # Mock shorten_bracket_title to avoid importing full scanner
        monkeypatch.setattr(
            "stale_price_detector.shorten_bracket_title",
            lambda t: t[:10],
            raising=False,
        )
        # Need to also handle the import inside format_stale_alerts
        import stale_price_detector
        monkeypatch.setattr(
            stale_price_detector, "format_stale_alerts",
            stale_price_detector.format_stale_alerts,
        )
        # Provide the imported function reference
        import edge_scanner_v2
        monkeypatch.setattr(
            edge_scanner_v2, "shorten_bracket_title",
            lambda t: t[:10],
            raising=False,
        )

        alert = StaleAlert(
            city="NYC",
            direction="warmer",
            mean_shift_f=2.5,
            prev_mean=70.0,
            curr_mean=72.5,
            bracket_title="72° to 73°F",
            ticker="T-72-73",
            expected_bid_change=5,
            actual_bid=40,
            prev_bid=38,
        )
        result = format_stale_alerts([alert])
        assert "STALE PRICE" in result
        assert "NYC" in result
        assert "🔴" in result  # warmer
        assert "+2.5°F" in result
        assert "T-72-73" in result

    def test_cooler_alert_uses_blue(self, monkeypatch):
        import edge_scanner_v2
        monkeypatch.setattr(
            edge_scanner_v2, "shorten_bracket_title",
            lambda t: t[:10],
            raising=False,
        )

        alert = StaleAlert(
            city="CHI",
            direction="cooler",
            mean_shift_f=-2.0,
            prev_mean=72.0,
            curr_mean=70.0,
            bracket_title="70° to 71°F",
            ticker="T-70-71",
            expected_bid_change=4,
            actual_bid=35,
            prev_bid=36,
        )
        result = format_stale_alerts([alert])
        assert "🔵" in result  # cooler
        assert "CHI" in result

    def test_caps_at_five_alerts(self, monkeypatch):
        import edge_scanner_v2
        monkeypatch.setattr(
            edge_scanner_v2, "shorten_bracket_title",
            lambda t: "short",
            raising=False,
        )

        alerts = [
            StaleAlert(
                city=f"CITY{i}",
                direction="warmer",
                mean_shift_f=2.0,
                prev_mean=70.0,
                curr_mean=72.0,
                bracket_title=f"Bracket {i}",
                ticker=f"T-{i}",
                expected_bid_change=4,
                actual_bid=40,
                prev_bid=38,
            )
            for i in range(8)
        ]
        result = format_stale_alerts(alerts)
        # Should have "1 bracket(s)" in header → no, 8 alerts but only 5 printed
        assert "8 bracket(s)" in result
        # Count city occurrences — should be capped at 5
        assert result.count("CITY") == 5


# ─── Test: StaleAlert dataclass ────────────────────────


class TestStaleAlert:
    """StaleAlert basic field access."""

    def test_field_access(self):
        alert = StaleAlert(
            city="DEN",
            direction="warmer",
            mean_shift_f=1.8,
            prev_mean=60.0,
            curr_mean=61.8,
            bracket_title="62° to 63°F",
            ticker="T-62-63",
            expected_bid_change=3,
            actual_bid=25,
            prev_bid=23,
        )
        assert alert.city == "DEN"
        assert alert.direction == "warmer"
        assert alert.mean_shift_f == 1.8
        assert alert.prev_mean == 60.0
        assert alert.curr_mean == 61.8
        assert alert.ticker == "T-62-63"
        assert alert.expected_bid_change == 3
        assert alert.actual_bid == 25
        assert alert.prev_bid == 23
