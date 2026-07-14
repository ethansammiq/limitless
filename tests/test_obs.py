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


class TestPreciseCelsius:
    """Integer-°C readings (±0.9°F) can't support certainty math (KAUS 2026-07-04)."""

    def test_integer_celsius_rejected(self):
        assert not obs.is_precise_celsius(24.0)   # the 75.2°F=24.0°C false min
        assert not obs.is_precise_celsius(-3.0)

    def test_tenth_celsius_accepted(self):
        assert obs.is_precise_celsius(24.4)       # 11:53Z METAR 75.9°F
        assert obs.is_precise_celsius(23.9)


class TestTrendClass:
    def _series(self, *pairs):
        from datetime import datetime, timezone
        return [(datetime(2026, 7, 14, h, m, tzinfo=timezone.utc), f)
                for h, m, f in pairs]

    AT = None  # set per-test

    def test_post_peak(self):
        from datetime import datetime, timezone
        from core.obs import trend_class
        s = self._series((15, 53, 88.0), (16, 53, 90.1), (17, 53, 90.1),
                         (18, 53, 89.2), (19, 53, 88.3), (20, 53, 87.4))
        at = datetime(2026, 7, 14, 21, 40, tzinfo=timezone.utc)
        t = trend_class(s, at)
        # peak = LAST occurrence of 90.1 (17:53); lag 227 min, drop 2.7°F
        assert t["klass"] == "post_peak"
        assert t["lag_min"] == 227 and t["drop_f"] == 2.7
        assert t["drift_p"] == 0.031 and t["drift_n"] == 327

    def test_plateau_is_still_hot(self):
        from datetime import datetime, timezone
        from core.obs import trend_class
        s = self._series((15, 53, 88.0), (16, 53, 90.1), (17, 53, 90.1),
                         (18, 53, 90.1), (19, 53, 90.1), (20, 53, 90.1))
        at = datetime(2026, 7, 14, 21, 40, tzinfo=timezone.utc)
        t = trend_class(s, at)
        assert t["klass"] == "still_hot"    # sitting ON the max: no drop

    def test_recent_peak_is_still_hot(self):
        from datetime import datetime, timezone
        from core.obs import trend_class
        s = self._series((15, 53, 86.0), (16, 53, 87.0), (17, 53, 88.0),
                         (18, 53, 89.0), (19, 53, 90.0), (21, 20, 90.5))
        at = datetime(2026, 7, 14, 21, 40, tzinfo=timezone.utc)
        assert trend_class(s, at)["klass"] == "still_hot"   # peak 20 min ago

    def test_thin_obs_none(self):
        from datetime import datetime, timezone
        from core.obs import trend_class
        s = self._series((19, 53, 88.3), (20, 53, 87.4))
        at = datetime(2026, 7, 14, 21, 40, tzinfo=timezone.utc)
        assert trend_class(s, at) is None

    def test_future_obs_excluded(self):
        from datetime import datetime, timezone
        from core.obs import trend_class
        s = self._series((15, 53, 88.0), (16, 53, 90.1), (17, 53, 89.0),
                         (18, 53, 88.5), (19, 53, 88.0), (20, 53, 87.5),
                         (22, 53, 95.0))   # after the print — invisible
        at = datetime(2026, 7, 14, 21, 40, tzinfo=timezone.utc)
        assert trend_class(s, at)["klass"] == "post_peak"


class TestAnnotateTrend:
    def test_trend_stamped_on_floor_high_buys_only(self):
        from core.obs import annotate_floor_buys
        entries = [
            {"kind": "buy_winner", "final": False, "ladder_kind": "high",
             "subtitle": "88° to 89°"},
            {"kind": "sell_dead", "final": False, "ladder_kind": "high"},
        ]
        trend = {"klass": "still_hot", "lag_min": 10, "drop_f": 0.2,
                 "drift_p": 0.086, "drift_n": 490}
        annotate_floor_buys(entries, 88.0, 88.2, trend=trend)
        assert entries[0]["obs_trend"] == "still_hot"
        assert entries[0]["trend_drift_p"] == 0.086
        assert "obs_trend" not in entries[1]

    def test_no_trend_no_stamp(self):
        from core.obs import annotate_floor_buys
        entries = [{"kind": "buy_winner", "final": False,
                    "ladder_kind": "high", "subtitle": "88° to 89°"}]
        annotate_floor_buys(entries, 88.0, 88.2, trend=None)
        assert "obs_trend" not in entries[0]
