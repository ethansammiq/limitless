"""Tests for audit_coverage gap detectors (no network)."""
from scripts.audit_coverage import (
    build_report,
    missing_series,
    parse_health_gaps,
    silent_offices,
)


class TestMissingSeries:
    def test_finds_live_not_laddered(self):
        live = {"KXHIGHCHI", "KXHIGHBUF", "KXLOWTNYC", "SOMEOTHER"}
        laddered = {"KXHIGHCHI", "KXLOWTNYC"}
        assert missing_series(live, laddered) == ["KXHIGHBUF"]

    def test_ignores_non_weather(self):
        assert missing_series({"KXBTC", "MLBGAME"}, set()) == []

    def test_none_missing(self):
        assert missing_series({"KXHIGHCHI"}, {"KXHIGHCHI"}) == []

    def test_known_aliases_and_incompatibles_not_flagged(self):
        # Dormant alias shells + structurally different series (verified
        # 2026-07-05) must not re-alarm every Sunday audit.
        from scripts.audit_coverage import IGNORED_SERIES
        live = set(IGNORED_SERIES) | {"KXHIGHCHI"}
        assert missing_series(live, {"KXHIGHCHI"}) == []


class TestParseHealth:
    def test_flags_no_temp_products(self):
        products = [
            {"awips": "MDW", "max_f": 85, "min_f": 70},
            {"awips": "XYZ", "max_f": None, "min_f": None},
        ]
        assert parse_health_gaps(products) == ["XYZ"]

    def test_partial_temp_is_ok(self):
        # min present, max missing — still a usable parse
        assert parse_health_gaps([{"awips": "MDW", "max_f": None, "min_f": 70}]) == []


class TestSilentOffices:
    def test_flags_offices_with_no_products(self):
        products = [{"awips": "MDW"}]
        laddered_wfos = {"LOT", "OKX", "LOX"}
        awips_to_wfo = {"MDW": "LOT", "NYC": "OKX", "LAX": "LOX"}
        assert silent_offices(products, laddered_wfos, awips_to_wfo) == ["LOX", "OKX"]

    def test_all_covered(self):
        products = [{"awips": "MDW"}, {"awips": "NYC"}]
        assert silent_offices(products, {"LOT", "OKX"},
                              {"MDW": "LOT", "NYC": "OKX"}) == []


class TestBuildReport:
    def test_gaps_flag_true_when_any(self):
        text, gaps = build_report(["KXHIGHHOU"], [], [], 8, 7)
        assert gaps is True
        assert "KXHIGHHOU" in text and "series drift: 1" in text

    def test_clean_when_none(self):
        text, gaps = build_report([], [], [], 8, 7)
        assert gaps is False
        assert text.count("✓") == 3
