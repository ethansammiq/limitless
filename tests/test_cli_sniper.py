"""Tests for cli_sniper pure helpers — real product fixtures, no network."""
from datetime import datetime, timezone
from pathlib import Path

import cli_sniper as cs
from ladders import Ladder

FIXTURES = Path(__file__).parent / "fixtures"
AFTERNOON = (FIXTURES / "climdw_afternoon.txt").read_text()
MORNING = (FIXTURES / "climdw_morning.txt").read_text()

MDW_HIGH = Ladder(series="KXHIGHCHI", kind="high", awips="MDW", wfo="LOT",
                  station_icao="KMDW", tz="America/Chicago")
MDW_LOW = Ladder(series="KXLOWTCHI", kind="low", awips="MDW", wfo="LOT",
                 station_icao="KMDW", tz="America/Chicago")


class TestParseProduct:
    def test_afternoon_floor(self):
        p = cs.parse_product(AFTERNOON)
        assert p is not None
        assert p.awips == "MDW"
        assert p.summary_date == "2026-07-04"
        assert p.is_final is False           # "VALID TODAY AS OF 0400 PM"
        assert p.max_f == 85                 # the print that paid +$18.24
        assert p.min_f is not None

    def test_morning_final(self):
        p = cs.parse_product(MORNING)
        assert p is not None
        assert p.summary_date == "2026-07-03"
        assert p.is_final is True            # no VALID TODAY line
        assert p.max_f == 91

    def test_garbage_is_none(self):
        assert cs.parse_product("") is None
        assert cs.parse_product("random text\nno structure") is None

    def test_stamp_dedup_key_differs(self):
        a, m = cs.parse_product(AFTERNOON), cs.parse_product(MORNING)
        assert a.stamp != m.stamp


class TestWindows:
    def test_afternoon(self):
        assert cs.window_kind(15.5) == "afternoon"
        assert cs.window_kind(18.4) == "afternoon"
        assert cs.window_kind(18.5) is None

    def test_morning(self):
        assert cs.window_kind(5.5) == "morning"
        assert cs.window_kind(8.6) is None

    def test_midday_closed(self):
        assert cs.window_kind(12.0) is None

    def test_stations_in_window_tz_aware(self):
        groups = {"MDW": [MDW_HIGH], "NYC": [Ladder(
            series="KXHIGHNY", kind="high", awips="NYC", wfo="OKX",
            station_icao="KNYC", tz="America/New_York")]}
        # 21:00Z = 16:00 CDT (in afternoon window), 17:00 EDT (in window too)
        now = datetime(2026, 7, 4, 21, 0, tzinfo=timezone.utc)
        assert cs.stations_in_window(now, groups) == ["MDW", "NYC"]
        # 23:45Z = 18:45 CDT (closed), 19:45 EDT (closed)
        now = datetime(2026, 7, 4, 23, 45, tzinfo=timezone.utc)
        assert cs.stations_in_window(now, groups) == []


def _mkt(ticker: str, subtitle: str) -> dict:
    return {"ticker": ticker, "subtitle": subtitle}


CHI_MARKETS = [
    _mkt("KXHIGHCHI-26JUL04-T84", "83° or below"),
    _mkt("KXHIGHCHI-26JUL04-B84.5", "84° to 85°"),
    _mkt("KXHIGHCHI-26JUL04-B86.5", "86° to 87°"),
    _mkt("KXHIGHCHI-26JUL04-T88", "88° or above"),
    _mkt("KXHIGHCHI-26JUL05-B84.5", "84° to 85°"),   # tomorrow — ignored
]


class TestClassify:
    def test_afternoon_floor_classification(self):
        p = cs.parse_product(AFTERNOON)          # max 85, floor
        found = cs.classify(p, MDW_HIGH, CHI_MARKETS)
        by = {f["ticker"]: f["kind"] for f in found}
        assert by["KXHIGHCHI-26JUL04-T84"] == "sell_dead"      # hi 83 < 85
        assert by["KXHIGHCHI-26JUL04-B84.5"] == "buy_winner"   # contains 85
        assert "KXHIGHCHI-26JUL04-B86.5" not in by             # still reachable
        assert "KXHIGHCHI-26JUL05-B84.5" not in by             # wrong day

    def test_final_flag_propagates(self):
        p = cs.parse_product(MORNING)
        markets = [_mkt("KXHIGHCHI-26JUL03-B90.5", "90° to 91°")]
        found = cs.classify(p, MDW_HIGH, markets)
        assert found[0]["kind"] == "buy_winner" and found[0]["final"] is True

    def test_low_ladder_mirrors(self):
        p = cs.parse_product(AFTERNOON)
        m = p.min_f
        markets = [
            _mkt("KXLOWTCHI-26JUL04-X1", f"{m + 2}° to {m + 3}°"),   # lo > m: dead
            _mkt("KXLOWTCHI-26JUL04-X2", f"{m - 1}° to {m}°"),       # contains m
        ]
        found = cs.classify(p, MDW_LOW, markets)
        by = {f["ticker"]: f["kind"] for f in found}
        assert by["KXLOWTCHI-26JUL04-X1"] == "sell_dead"
        assert by["KXLOWTCHI-26JUL04-X2"] == "buy_winner"

    def test_no_printed_value_no_findings(self):
        p = cs.ParsedCLI(awips="MDW", stamp="042136", summary_date="2026-07-04",
                         is_final=False, max_f=None, min_f=None)
        assert cs.classify(p, MDW_HIGH, CHI_MARKETS) == []


class TestFormatAlert:
    def test_alert_carries_command(self):
        opps = [{"kind": "buy_winner", "ticker": "KXHIGHCHI-26JUL04-B84.5",
                 "subtitle": "84° to 85°", "printed": 85, "final": False,
                 "ladder_kind": "high",
                 "ask": 16, "ask_depth": 40.0,
                 "cmd": ".venv/bin/python scripts/take.py KXHIGHCHI-26JUL04-B84.5 buy yes 40 16"}]
        title, body = cs.format_alert(opps)
        assert "1 winner buy" in title
        assert "take.py KXHIGHCHI-26JUL04-B84.5 buy yes 40 16" in body
        assert "floor" in body and "warming" in body
