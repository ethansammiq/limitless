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

    def test_hourly_warmup_gap_gives_no_verdict(self):
        # KDEN-style 3.9°F hourly warm-up: conservative None until the next
        # ob lands near the peak (reversed 2026-07-04 after the KMSY spike)
        assert obs.corroborated_extreme([75.9, 81.0, 84.9], "high") is None

    def test_msy_down_spike_rejected(self):
        # KMSY 2026-07-04: lone 75.2 between continuous 77.0s — sensor tick,
        # not a real min (CLI printed 76; the naive verdict cost $195)
        temps = [77.0] * 6 + [75.2] + [77.0] * 6
        assert obs.corroborated_extreme(temps, "low") is None

    def test_corroborated_min_within_degree(self):
        assert obs.corroborated_extreme([77.0, 76.8, 78.1], "low") == 76.8

    def test_min_side(self):
        assert obs.corroborated_extreme([70.0, 63.2, 63.0], "low") == 63.0

    def test_single_ob_rejected(self):
        assert obs.corroborated_extreme([100.0], "high") is None
        assert obs.corroborated_extreme([], "high") is None


class TestClimateDayStart:
    """CLI climate days run midnight LOCAL STANDARD TIME (2026-07-04 MSY bug)."""

    def test_dst_zone_starts_at_0100_wall_clock(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/Chicago")
        now = datetime(2026, 7, 4, 12, 0, tzinfo=tz)          # CDT
        start = obs.climate_day_start(tz, now)
        assert (start.hour, start.minute) == (1, 0)

    def test_standard_time_starts_at_midnight(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/Chicago")
        now = datetime(2026, 1, 15, 12, 0, tzinfo=tz)         # CST
        assert obs.climate_day_start(tz, now).hour == 0

    def test_phoenix_never_shifts(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/Phoenix")
        now = datetime(2026, 7, 4, 12, 0, tzinfo=tz)
        assert obs.climate_day_start(tz, now).hour == 0
