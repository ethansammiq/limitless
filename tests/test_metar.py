"""core.metar — 6-hourly climate group parsing, rounding, day attribution."""
from datetime import datetime, timezone

from core import metar

NOW = datetime(2026, 7, 12, 0, 10, tzinfo=timezone.utc)

# The live 2026-07-11 leak: 10322 = 32.2°C = 89.96°F → CLI printed 90.
KMSP_LINE = ("KMSP 112353Z 29008KT 10SM FEW250 31/16 A2990 RMK AO2 "
             "SLP117 T03060161 10322 20206 55004")


class TestParse:
    def test_live_kmsp_groups(self):
        out = metar.parse_metars(KMSP_LINE, NOW)
        assert [(e.kind, e.tenths_c) for e in out] == [("max", 322), ("min", 206)]
        e = out[0]
        assert e.station == "KMSP"
        assert e.obs_time_utc == datetime(2026, 7, 11, 23, 53, tzinfo=timezone.utc)
        assert e.temp_c == 32.2

    def test_negative_sign_digit(self):
        out = metar.parse_metars("KMSP 112353Z 00/00 RMK AO2 11005 21022", NOW)
        assert [(e.kind, e.tenths_c) for e in out] == [("max", -5), ("min", -22)]

    def test_no_rmk_section(self):
        assert metar.parse_metars("KMSP 112353Z 31/16 A2990 10322", NOW) == []

    def test_pk_wnd_not_a_group(self):
        out = metar.parse_metars(
            "KMSP 112353Z RMK AO2 PK WND 21035/2312 SLP117 10322", NOW)
        assert [(e.kind, e.tenths_c) for e in out] == [("max", 322)]

    def test_slash_adjacent_rejected(self):
        # 6-group precip 60322/ style and fractional tokens must not match
        out = metar.parse_metars("KMSP 112353Z RMK AO2 10322/ 4/10322", NOW)
        assert out == []

    def test_t_group_and_slp_ignored(self):
        out = metar.parse_metars(
            "KMSP 112353Z RMK AO2 SLP117 T03060161 55004 58033", NOW)
        assert out == []

    def test_multiline_multi_station(self):
        raw = KMSP_LINE + "\nKMIA 112353Z 30/26 RMK AO2 10339 20272\n"
        out = metar.parse_metars(raw, NOW)
        assert {e.station for e in out} == {"KMSP", "KMIA"}
        assert len(out) == 4

    def test_stale_stamp_dropped(self):
        # Day 25 doesn't resolve within 36h of NOW (July 12) in this or last month
        assert metar.parse_metars("KMSP 252353Z RMK AO2 10322", NOW) == []


class TestRounding:
    def test_the_bracket_maker(self):
        # 32.2°C = 89.96°F → 90, NOT 89 (integer-°C conversion would say 89)
        e = metar.parse_metars(KMSP_LINE, NOW)[0]
        assert round(e.temp_f, 2) == 89.96
        assert e.temp_f_rounded == 90

    def test_half_rounds_up(self):
        assert metar.round_f(88.5) == 89
        assert metar.round_f(89.96) == 90
        assert metar.round_f(89.42) == 89

    def test_negative(self):
        assert metar.round_f(-13.5) == -13
        assert metar.round_f(-13.51) == -14


class TestClimateDate:
    def _extreme(self, hour, minute=53, day=11):
        return metar.SixHrExtreme(
            station="KMSP",
            obs_time_utc=datetime(2026, 7, day, hour, minute, tzinfo=timezone.utc),
            kind="max", tenths_c=322)

    def test_afternoon_window_same_day(self):
        # 2353Z = 18:53 CDT, window 12:53-18:53 — one local day
        assert metar.climate_date(self._extreme(23), "America/Chicago") == "2026-07-11"

    def test_midnight_straddle_is_none(self):
        # 0553Z = 00:53 CDT on the 12th, window starts 18:53 on the 11th
        assert metar.climate_date(self._extreme(5, day=12), "America/Chicago") is None

    def test_morning_window_same_day(self):
        # 1153Z = 06:53 CDT, window 00:53-06:53 — one local day
        assert metar.climate_date(self._extreme(11, day=12), "America/Chicago") == "2026-07-12"


class TestSynopticAnchor:
    def test_the_53_ob_before_each_synoptic_hour(self):
        for hh, anchor in ((23, 0), (5, 6), (11, 12), (17, 18)):
            t = datetime(2026, 7, 12, hh, 53, tzinfo=timezone.utc)
            assert metar.synoptic_anchor_utc(t) == anchor

    def test_stragglers_and_corrections_resolve_to_the_same_anchor(self):
        # fetch window runs until HH:45 — a 0015Z correction is still 00Z
        assert metar.synoptic_anchor_utc(
            datetime(2026, 7, 12, 0, 15, tzinfo=timezone.utc)) == 0
        assert metar.synoptic_anchor_utc(
            datetime(2026, 7, 12, 18, 45, tzinfo=timezone.utc)) == 18


class TestStampResolution:
    def test_month_rollover(self):
        now = datetime(2026, 8, 1, 0, 10, tzinfo=timezone.utc)
        t = metar.metar_time_to_utc(31, 23, 53, now)
        assert t == datetime(2026, 7, 31, 23, 53, tzinfo=timezone.utc)

    def test_small_future_skew_ok(self):
        t = metar.metar_time_to_utc(12, 0, 15, NOW)
        assert t == datetime(2026, 7, 12, 0, 15, tzinfo=timezone.utc)
