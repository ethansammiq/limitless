"""core.dsm — parsing against real IEM AFOS products, no network."""
from core import dsm

# Verbatim IEM AFOS retrieve.py output for pil=DSMMIA, fetched 2026-07-07.
# Second report is THE 2026-07-06 product from the certainty-wall trade:
# max 93 @ 13:44 LST while the prelim CLI printed 92.
MIA_BLOB = """
KMIA DS 0200 07/07 810159/ 740050// 93/ 78//0050159/00/00/00/-/-/-/-/
-/-/-/-/-/-/-/-/-/-/-/-/-/-/-/-/-/-/-/10070044/12090016/N/NN/N/N/NN/
ET=



KMIA DS 06/07 931344/ 771818// 93/ 78//0010422/26/00/12/00/00/00/T/T/
00/00/00/00/00/T/00/T/11/02/T/01/T/00/00/00/00/43/19181410/19351409/
138/NN/N/N/NN/ET EW=
"""


class TestParse:
    def test_parses_both_reports(self):
        reports = dsm.parse_dsm_text(MIA_BLOB)
        assert len(reports) == 2
        intraday, daily = reports
        # intraday snapshot: DS HHMM DD/MM
        assert (intraday.day, intraday.month) == (7, 7)
        assert intraday.max_f == 81 and intraday.max_time_lst == "0159"
        assert intraday.min_f == 74 and intraday.min_time_lst == "0050"
        # the Jul-6 daily — the settlement-deciding value
        assert (daily.day, daily.month) == (6, 7)
        assert daily.max_f == 93 and daily.max_time_lst == "1344"
        assert daily.min_f == 77 and daily.min_time_lst == "1818"

    def test_three_digit_and_negative_temps(self):
        assert dsm.parse_dsm_text(
            "KLAS DS 06/07 1081512/ 850430//")[0].max_f == 108
        assert dsm.parse_dsm_text(
            "KMSP DS 06/01 -051512/ -220430//")[0].min_f == -22

    def test_missing_value_is_none(self):
        r = dsm.parse_dsm_text("KMIA DS 06/07 M/ 771818//")[0]
        assert r.max_f is None and r.min_f == 77

    def test_garbage_is_empty(self):
        assert dsm.parse_dsm_text("") == []
        assert dsm.parse_dsm_text("Too many requests from your IP") == []


class TestSelection:
    REPORTS = dsm.parse_dsm_text(MIA_BLOB)

    def test_reports_for_date_matches_dd_mm(self):
        assert [r.max_f for r in dsm.reports_for_date(
            self.REPORTS, "2026-07-06")] == [93]
        assert [r.max_f for r in dsm.reports_for_date(
            self.REPORTS, "2026-07-07")] == [81]
        assert dsm.reports_for_date(self.REPORTS, "2026-07-05") == []
        assert dsm.reports_for_date(self.REPORTS, "junk") == []

    def test_extreme_takes_worst_across_reports(self):
        two = dsm.parse_dsm_text(
            "KMIA DS 0200 06/07 901000/ 780200//\n"
            "KMIA DS 06/07 931344/ 771818//")
        assert dsm.dsm_extreme(two, "high") == (93, "1344")
        assert dsm.dsm_extreme(two, "low") == (77, "1818")
        assert dsm.dsm_extreme([], "high") is None


class TestContradicts:
    def test_high_vetoes_only_above(self):
        assert dsm.contradicts("high", 92, 93)          # the MIA case
        assert not dsm.contradicts("high", 92, 92)      # agreement
        assert not dsm.contradicts("high", 92, 91)      # earlier snapshot

    def test_low_mirrors(self):
        assert dsm.contradicts("low", 74, 73)
        assert not dsm.contradicts("low", 74, 74)
        assert not dsm.contradicts("low", 74, 75)
