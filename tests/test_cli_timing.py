"""Tests for cli_timing decode/summary (no network)."""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backtest"))
import cli_timing as ct


class TestStampDecode:
    def test_basic(self):
        ref = datetime(2026, 7, 4, 21, 40, tzinfo=timezone.utc)
        dt = ct.stamp_to_utc("042136", ref)
        assert dt == datetime(2026, 7, 4, 21, 36, tzinfo=timezone.utc)

    def test_month_rollover(self):
        # stamp day 31 seen when ref is early in the next month
        ref = datetime(2026, 8, 1, 7, 0, tzinfo=timezone.utc)
        dt = ct.stamp_to_utc("310650", ref)
        assert dt == datetime(2026, 7, 31, 6, 50, tzinfo=timezone.utc)

    def test_garbage(self):
        ref = datetime(2026, 7, 4, 21, 40, tzinfo=timezone.utc)
        assert ct.stamp_to_utc("", ref) is None
        assert ct.stamp_to_utc("99zz99", ref) is None
        assert ct.stamp_to_utc("049999", ref) is None


class TestLocalHour:
    def test_cdt_conversion(self):
        # 21:36 UTC = 16:36 CDT (the real MDW afternoon product)
        dt = datetime(2026, 7, 4, 21, 36, tzinfo=timezone.utc)
        assert abs(ct.local_hour(dt, "America/Chicago") - 16.6) < 0.01

    def test_window_membership(self):
        assert ct.in_window(16.6) == "afternoon"
        assert ct.in_window(6.5) == "morning"
        assert ct.in_window(12.0) is None


class TestFormatHHMM:
    def test_round_trip(self):
        assert ct._fmt_hhmm(16.6) == "16:36"
        assert ct._fmt_hhmm(6.0) == "06:00"


class TestSummarize:
    def test_groups_by_wfo_kind(self):
        # MDW afternoon at 16:36 CDT — must land in the LOT/afternoon group
        obs = [{"awips": "MDW", "is_final": False,
                "issue_utc": datetime(2026, 7, 4, 21, 36, tzinfo=timezone.utc),
                "run_ts": datetime(2026, 7, 4, 21, 40, tzinfo=timezone.utc)}]
        s = ct.summarize(obs)
        assert s["n"] == 1
        assert "LOT/afternoon" in s["per_office"]
        assert s["per_office"]["LOT/afternoon"]["median"] == "16:36"
        assert s["median_detect_latency_min"] == 4.0

    def test_empty(self):
        s = ct.summarize([])
        assert s["n"] == 0 and s["per_office"] == {}
