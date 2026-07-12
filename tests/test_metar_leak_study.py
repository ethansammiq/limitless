"""backtest/metar_leak_study pure helpers — no network."""
from datetime import datetime, timezone

from backtest import metar_leak_study as mls
from core.metar import SixHrExtreme


class TestArchivedStamp:
    def test_floor_same_day(self):
        t = mls.archived_stamp_to_utc("112230", "2026-07-11")
        assert t == datetime(2026, 7, 11, 22, 30, tzinfo=timezone.utc)

    def test_final_next_day(self):
        t = mls.archived_stamp_to_utc("120851", "2026-07-11")
        assert t == datetime(2026, 7, 12, 8, 51, tzinfo=timezone.utc)

    def test_month_rollover(self):
        t = mls.archived_stamp_to_utc("010913", "2026-06-30")
        assert t == datetime(2026, 7, 1, 9, 13, tzinfo=timezone.utc)

    def test_unresolvable_is_none(self):
        assert mls.archived_stamp_to_utc("250913", "2026-06-30") is None
        assert mls.archived_stamp_to_utc("bogus1", "2026-06-30") is None


FLOOR = """000
CDUS43 KMPX 112230

CLIMSP

CLIMATE SUMMARY FOR JULY 11 2026...VALID TODAY AS OF 0400 PM LOCAL TIME.

TEMPERATURE (F)
 MAXIMUM         88
 MINIMUM         69
"""
FINAL = """000
CDUS43 KMPX 120851

CLIMSP

CLIMATE SUMMARY FOR JULY 11 2026.

TEMPERATURE (F)
 MAXIMUM         90
 MINIMUM         68
"""


class TestParseCliArchive:
    def test_floor_final_split(self):
        days = mls.parse_cli_archive(FLOOR + "\x01" + FINAL, "America/Chicago")
        d = days["2026-07-11"]
        assert d["floor_max"] == 88 and d["final_max"] == 90
        assert d["floor_min"] == 69 and d["final_min"] == 68
        assert d["final_issued"].hour == 8

    def test_pacific_late_floor_stays_floor(self):
        # 17:41 PDT = 0041Z next UTC day — local calendar keeps it a floor
        pac = FLOOR.replace("112230", "120041").replace("CLIMSP", "CLILAX")
        days = mls.parse_cli_archive(pac, "America/Los_Angeles")
        assert "floor_max" in days["2026-07-11"]
        assert "final_max" not in days["2026-07-11"]


def _ext(day, hour, kind, tenths):
    return SixHrExtreme(station="KMSP",
                        obs_time_utc=datetime(2026, 7, day, hour, 53,
                                              tzinfo=timezone.utc),
                        kind=kind, tenths_c=tenths)


class TestDailyExtremes:
    def test_max_of_maxes_and_pm_group(self):
        # 1153Z morning group 25.0°C=77, 2353Z afternoon 32.2°C=90
        days = mls.daily_metar_extremes(
            [_ext(11, 11, "max", 250), _ext(11, 23, "max", 322)],
            "America/Chicago", "max")
        d = days["2026-07-11"]
        assert d["value"] == 90
        assert d["pm_value"] == 90         # 2353Z = 18:53 CDT, ≥15:00 local
        assert d["obs_time"].hour == 23

    def test_straddle_dropped(self):
        # 0553Z window straddles CDT midnight — contributes nothing
        assert mls.daily_metar_extremes(
            [_ext(12, 5, "max", 322)], "America/Chicago", "max") == {}

    def test_min_of_mins(self):
        days = mls.daily_metar_extremes(
            [_ext(11, 11, "min", 206), _ext(11, 23, "min", 250)],
            "America/Chicago", "min")
        assert days["2026-07-11"]["value"] == 69   # 20.6°C = 69.08 → 69


class TestSummarize:
    def test_drift_resolution_counted(self):
        rows = [
            {"awips": "MSP", "kind": "high", "final": 90, "floor": 88,
             "metar": 90, "metar_pm": 90, "lead_min": 400},
            {"awips": "MSP", "kind": "high", "final": 88, "floor": 88,
             "metar": 88, "metar_pm": 88, "lead_min": 500},
        ]
        s = mls.summarize(rows)["high"]
        assert s["n"] == 2 and s["exact"] == 2
        assert s["drift_days"] == 1 and s["drift_called"] == 1
        assert s["median_lead_min"] == 500
