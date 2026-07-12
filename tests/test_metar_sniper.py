"""metar_sniper pure helpers — no network."""
from datetime import datetime, timezone

import metar_sniper as ms
from core.metar import SixHrExtreme
from ladders import Ladder

MSP_HIGH = Ladder(series="KXHIGHTMIN", kind="high", awips="MSP", wfo="MPX",
                  station_icao="KMSP", tz="America/Chicago")
MSP_LOW = Ladder(series="KXLOWTMIN", kind="low", awips="MSP", wfo="MPX",
                 station_icao="KMSP", tz="America/Chicago")

# The live 2026-07-11 KMSP leak: 6-hr max 32.2°C → 90°F
MAX_EXTREME = SixHrExtreme(
    station="KMSP",
    obs_time_utc=datetime(2026, 7, 11, 23, 53, tzinfo=timezone.utc),
    kind="max", tenths_c=322)
MIN_EXTREME = SixHrExtreme(
    station="KMSP",
    obs_time_utc=datetime(2026, 7, 11, 23, 53, tzinfo=timezone.utc),
    kind="min", tenths_c=206)


def _mkt(ticker: str, subtitle: str) -> dict:
    return {"ticker": ticker, "subtitle": subtitle}


MSP_MARKETS = [
    _mkt("KXHIGHTMIN-26JUL11-B84.5", "84° to 85°"),
    _mkt("KXHIGHTMIN-26JUL11-B86.5", "86° to 87°"),
    _mkt("KXHIGHTMIN-26JUL11-B88.5", "88° to 89°"),
    _mkt("KXHIGHTMIN-26JUL11-B90.5", "90° to 91°"),
    _mkt("KXHIGHTMIN-26JUL11-B92.5", "92° or above"),
    _mkt("KXHIGHTMIN-26JUL12-B88.5", "88° to 89°"),   # wrong day — ignored
]


class TestFetchWindow:
    def _at(self, h, m):
        return datetime(2026, 7, 11, h, m, tzinfo=timezone.utc)

    def test_around_2353z(self):
        assert ms.in_fetch_window(self._at(23, 43))
        assert ms.in_fetch_window(self._at(23, 59))
        assert not ms.in_fetch_window(self._at(23, 30))

    def test_after_synoptic_hour(self):
        assert ms.in_fetch_window(self._at(0, 30))
        assert ms.in_fetch_window(self._at(0, 45))
        assert not ms.in_fetch_window(self._at(0, 46))

    def test_all_four_windows(self):
        for h in (5, 11, 17, 23):
            assert ms.in_fetch_window(self._at(h, 50))
        for h in (3, 9, 15, 21):
            assert not ms.in_fetch_window(self._at(h, 50))


class TestClassify:
    def test_max_floor_kills_below_and_leads_containing(self):
        out = ms.classify(MAX_EXTREME, MSP_HIGH, MSP_MARKETS)
        by = {f["ticker"]: f["kind"] for f in out}
        # 90°F floor: 84-85, 86-87, 88-89 dead; 90-91 contains; 92+ open (absent)
        assert by == {
            "KXHIGHTMIN-26JUL11-B84.5": "sell_dead",
            "KXHIGHTMIN-26JUL11-B86.5": "sell_dead",
            "KXHIGHTMIN-26JUL11-B88.5": "sell_dead",
            "KXHIGHTMIN-26JUL11-B90.5": "buy_winner",
        }
        winner = next(f for f in out if f["kind"] == "buy_winner")
        assert winner["printed"] == 90
        assert winner["precise_f"] == 89.96
        assert winner["final"] is False

    def test_kind_mismatch_classifies_nothing(self):
        assert ms.classify(MIN_EXTREME, MSP_HIGH, MSP_MARKETS) == []
        assert ms.classify(MAX_EXTREME, MSP_LOW, MSP_MARKETS) == []

    def test_min_ceiling_on_low_ladder(self):
        low_markets = [
            _mkt("KXLOWTMIN-26JUL11-B72.5", "72° to 73°"),   # lo > 69 → dead
            _mkt("KXLOWTMIN-26JUL11-B68.5", "68° to 69°"),   # contains 69
            _mkt("KXLOWTMIN-26JUL11-B66.5", "66° to 67°"),   # still reachable
        ]
        # 20.6°C = 69.08°F → 69 ceiling on the low
        out = ms.classify(MIN_EXTREME, MSP_LOW, low_markets)
        by = {f["ticker"]: f["kind"] for f in out}
        assert by == {
            "KXLOWTMIN-26JUL11-B72.5": "sell_dead",
            "KXLOWTMIN-26JUL11-B68.5": "buy_winner",
        }

    def test_midnight_straddle_classifies_nothing(self):
        straddle = SixHrExtreme(
            station="KMSP",
            obs_time_utc=datetime(2026, 7, 12, 5, 53, tzinfo=timezone.utc),
            kind="max", tenths_c=322)
        assert ms.classify(straddle, MSP_HIGH, MSP_MARKETS) == []


class TestSeenKey:
    def test_distinct_per_kind_and_time(self):
        assert ms._seen_key(MAX_EXTREME) == "KMSP:112353:max"
        assert ms._seen_key(MIN_EXTREME) == "KMSP:112353:min"


class TestFormatAlert:
    def test_buy_and_sell_lines(self):
        opps = [
            {"kind": "buy_winner", "ticker": "KXHIGHTMIN-26JUL11-B90.5",
             "subtitle": "90° to 91°", "ladder_kind": "high", "printed": 90,
             "precise_c": 32.2, "precise_f": 89.96,
             "obs_time": "2026-07-11T23:53+00:00", "final": False,
             "ask": 14, "ask_depth": 250, "fee_c": 1,
             "cmd": ".venv/bin/python scripts/take.py KXHIGHTMIN-26JUL11-B90.5 buy yes 250 14"},
            {"kind": "sell_dead", "ticker": "KXHIGHTMIN-26JUL11-B88.5",
             "subtitle": "88° to 89°", "ladder_kind": "high", "printed": 90,
             "precise_c": 32.2, "precise_f": 89.96,
             "obs_time": "2026-07-11T23:53+00:00", "final": False,
             "net_cents": 450, "contracts": 10, "levels": [[50, 10]],
             "cmd": ".venv/bin/python scripts/take.py KXHIGHTMIN-26JUL11-B88.5 sell yes 10 50"},
        ]
        title, body = ms.format_alert(opps)
        assert "1 winner buy(s), 1 dead-bid sell(s)" in title
        assert "32.2°C = 89.96°F → **90°**" in body
        assert "take.py KXHIGHTMIN-26JUL11-B90.5 buy yes 250 14" in body
        assert "Alert only" in body


class TestGates:
    def test_max_ask_is_the_standing_rule(self):
        assert ms.MAX_BUY_ASK_C == 20
