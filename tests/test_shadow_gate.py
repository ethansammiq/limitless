"""Tests for shadow_gate pure helpers (no network)."""
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backtest"))

import shadow_gate as sg


class TestRounding:
    def test_half_goes_up_not_bankers(self):
        assert sg.round_half_up(89.5) == 90
        assert sg.round_half_up(88.5) == 89  # round() would give 88

    def test_plain_cases(self):
        assert sg.round_half_up(89.4) == 89
        assert sg.round_half_up(89.6) == 90


class TestContains:
    def test_integer_bracket(self):
        assert sg.contains((88.0, 89.0), 88.7)
        assert sg.contains((88.0, 89.0), 89.4)
        assert not sg.contains((88.0, 89.0), 89.6)  # rounds to 90

    def test_open_ended(self):
        assert sg.contains((90.0, float("inf")), 103.0)
        assert sg.contains((float("-inf"), 65.0), 12.0)


class TestDepthDollars:
    def test_band_limited(self):
        # best ask 40c; band 5c includes 40+44, excludes 60
        lvls = [[40.0, 100.0], [44.0, 50.0], [60.0, 500.0]]
        assert sg.depth_dollars(lvls) == 40.0 * 100 / 100 + 44.0 * 50 / 100

    def test_empty(self):
        assert sg.depth_dollars([]) == 0.0
        assert sg.depth_dollars(None) == 0.0


class TestEv:
    def test_win_and_loss(self):
        assert sg.ev_cents(54.0, True) == 46.0
        assert sg.ev_cents(54.0, False) == -54.0


class TestRunmax:
    def test_no_lookahead(self):
        obs = [{"minutes": 60, "temp_f": 70.0}, {"minutes": 900, "temp_f": 95.0}]
        assert sg.runmax_at(obs, 120) == 70.0
        assert sg.runmax_at(obs, 900) == 95.0
        assert sg.runmax_at(obs, 30) is None


def snap(minute, on_signal=True, live=True, ask=50.0):
    return {"min": minute, "bracket": (88.0, 89.0), "ask": ask,
            "ask_levels": [[ask, 10]], "on_signal": on_signal, "live": live}


class TestPickEntry:
    def test_latest_on_signal_wins(self):
        got = sg.pick_entry([snap(960), snap(990), snap(1005)], 1020, 45)
        assert got["min"] == 1005

    def test_stale_discarded(self):
        assert sg.pick_entry([snap(900)], 1020, 45) is None

    def test_future_off_signal_and_quote_only_ignored(self):
        snaps = [snap(1015, on_signal=False), snap(1010, live=False),
                 snap(1000), snap(1030)]
        assert sg.pick_entry(snaps, 1020, 45)["min"] == 1000

    def test_no_ask_ignored(self):
        assert sg.pick_entry([snap(1000, ask=None)], 1020, 45) is None


class TestGateVerdict:
    def entry(self, ev, depth):
        return {"ev_c": ev, "depth_d": depth}

    def test_pass(self):
        entries = [self.entry(20.0, 80.0)] * 5
        v = sg.gate_verdict(entries)
        assert v["passed"] and all(v["checks"].values())

    def test_fails_on_each_leg(self):
        good = [self.entry(20.0, 80.0)] * 5
        assert not sg.gate_verdict(good[:4])["checks"]["n"]
        low_ev = [self.entry(5.0, 80.0)] * 5
        assert not sg.gate_verdict(low_ev)["checks"]["ev"]
        thin = [self.entry(20.0, 10.0)] * 5
        assert not sg.gate_verdict(thin)["checks"]["depth"]

    def test_empty_is_fail_not_crash(self):
        v = sg.gate_verdict([])
        assert not v["passed"] and v["mean_ev_c"] is None


class TestToSnapshot:
    def test_local_day_and_legacy_live(self, monkeypatch):
        tz = ZoneInfo("America/New_York")
        row = {"ticker": "Will the highest temperature in New York City be "
                         "between 88-89°F on July 4?",
               "ts": "2026-07-04T20:00:00+00:00", "target_date": "2026-07-04",
               "yes_ask": 54.0, "ask_levels": [[54.0, 20.0]]}
        s = sg.to_snapshot(row, tz)
        assert s["min"] == 16 * 60  # 20:00Z = 16:00 ET
        assert s["bracket"] == (88.0, 89.0)
        assert s["live"] is True  # legacy row without flag, has a book

    def test_quote_only_row_not_live(self):
        tz = ZoneInfo("America/New_York")
        row = {"ticker": "Will the highest temperature in New York City be "
                         "between 88-89°F on July 4?",
               "ts": "2026-07-04T20:00:00+00:00", "target_date": "2026-07-04",
               "yes_ask": 98.0, "live": False}
        assert sg.to_snapshot(row, tz)["live"] is False

    def test_wrong_local_day_dropped(self):
        tz = ZoneInfo("America/New_York")
        row = {"ticker": "Will the highest temperature in New York City be "
                         "between 88-89°F on July 4?",
               "ts": "2026-07-05T01:00:00+00:00",  # 21:00 ET on the 4th... no:
               "target_date": "2026-07-05"}        # row says the 5th, ts is the 4th
        assert sg.to_snapshot(row, tz) is None
