"""Tests for core.obs pure helpers (no network)."""
from core import obs


class TestCertainSettleBounds:
    def test_exact_ob_holds(self):
        # 2026-07-02 live case: 100.0°F ob -> CLI settles >= 100
        assert obs.certain_min_settle(100.0) == 100

    def test_half_degree_backs_off(self):
        # 99.5°F reported could be a true 99.41 -> only >= 99 is certain
        assert obs.certain_min_settle(99.5) == 99

    def test_low_mirror(self):
        assert obs.certain_max_settle(63.0) == 63
        assert obs.certain_max_settle(62.5) == 63

    def test_negative_low(self):
        assert obs.certain_max_settle(-5.0) == -5


class TestCorroboratedExtreme:
    def test_corroborated_max(self):
        assert obs.corroborated_extreme([84.9, 96.1, 100.0, 99.0], "high") == 100.0

    def test_lone_spike_rejected(self):
        assert obs.corroborated_extreme([84.9, 85.1, 100.0], "high") is None

    def test_hourly_warmup_gap_accepted(self):
        # KDEN 2026-07-02: 81.0 -> 84.9 between hourly obs is real warming
        assert obs.corroborated_extreme([75.9, 81.0, 84.9], "high") == 84.9

    def test_min_side(self):
        assert obs.corroborated_extreme([70.0, 63.2, 63.0], "low") == 63.0

    def test_single_ob_rejected(self):
        assert obs.corroborated_extreme([100.0], "high") is None
        assert obs.corroborated_extreme([], "high") is None
