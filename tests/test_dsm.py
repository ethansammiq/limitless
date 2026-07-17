"""core.dsm — parsing against real IEM AFOS products, no network."""
import json

import pytest

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


class TestFetchFallback:
    """IEM primary → NWS fallback → fail open. Network stubbed via dsm._get."""

    NWS_LISTING = json.dumps({"@graph": [
        {"@id": "https://api.weather.gov/products/p1"},
        {},  # entry without @id is skipped
        {"@id": "https://api.weather.gov/products/p2"},
    ]})
    NWS_P1 = json.dumps(
        {"productText": "KMIA DS 06/07 931344/ 771818//"})
    NWS_P2 = json.dumps({"productText": None})

    @pytest.fixture
    def stub(self, monkeypatch):
        calls = []

        def install(responses):
            def fake_get(url, timeout):
                calls.append(url)
                result = responses[len(calls) - 1]
                if isinstance(result, Exception):
                    raise result
                return result
            monkeypatch.setattr(dsm, "_get", fake_get)
            return calls
        return install

    def test_iem_success_skips_nws(self, stub):
        calls = stub([MIA_BLOB])
        assert len(dsm.fetch_dsm_reports("MIA")) == 2
        assert len(calls) == 1 and "mesonet" in calls[0]

    def test_rate_limited_iem_falls_through_to_nws(self, stub):
        calls = stub(["Too many requests from your IP",
                      self.NWS_LISTING, self.NWS_P1, self.NWS_P2])
        reports = dsm.fetch_dsm_reports("MIA")
        assert [r.max_f for r in reports] == [93]
        assert "api.weather.gov/products/types/DSM/locations/MIA" in calls[1]

    def test_iem_transport_error_falls_through_to_nws(self, stub):
        stub([OSError("timeout"), self.NWS_LISTING, self.NWS_P1, self.NWS_P2])
        assert [r.max_f for r in dsm.fetch_dsm_reports("mia")] == [93]

    def test_bad_nws_product_skipped_not_fatal(self, stub):
        stub(["", self.NWS_LISTING, OSError("500"), self.NWS_P1])
        assert [r.max_f for r in dsm.fetch_dsm_reports("MIA")] == [93]

    def test_both_feeds_down_fails_open(self, stub):
        stub([OSError("429"), OSError("503")])
        assert dsm.fetch_dsm_reports("MIA") == []

    def test_nws_station_with_no_products_fails_open(self, stub):
        stub(["Too many requests", json.dumps({"@graph": []})])
        assert dsm.fetch_dsm_reports("MDW") == []


class TestContradicts:
    def test_high_vetoes_only_above(self):
        assert dsm.contradicts("high", 92, 93)          # the MIA case
        assert not dsm.contradicts("high", 92, 92)      # agreement
        assert not dsm.contradicts("high", 92, 91)      # earlier snapshot

    def test_low_mirrors(self):
        assert dsm.contradicts("low", 74, 73)
        assert not dsm.contradicts("low", 74, 74)
        assert not dsm.contradicts("low", 74, 75)


class TestDateOrderUnambiguous:
    """The DD/MM assumption validated on a real mid-June archive product
    (day > 12 disambiguates). IEM AFOS retrieve.py sdate/edate fetch,
    2026-07-08: KMIA daily DSM for June 14 prints '14/06'. If this were
    MM/DD the veto would silently die every month from the 13th (fail-open
    masks it as dsm: unchecked)."""

    JUNE_14_DAILY = ("KMIA DS 14/06 931300/ 771559// 93/ 80//9990417/T/M/M/"
                     "M/M/M/M/M/M/M/M/M/M/M/M/M/00/T/00/00/00/00/00/00/00/"
                     "34/22211520/22341513/3/NN/N/N/NN/EP EW=")

    def test_day_gt_12_parses_as_dd_mm(self):
        r = dsm.parse_dsm_text(self.JUNE_14_DAILY)[0]
        assert (r.day, r.month) == (14, 6)
        assert r.max_f == 93 and r.max_time_lst == "1300"
        assert r.min_f == 77 and r.min_time_lst == "1559"

    def test_reports_for_date_matches_unambiguous_day(self):
        reports = dsm.parse_dsm_text(self.JUNE_14_DAILY)
        assert dsm.reports_for_date(reports, "2026-06-14")[0].max_f == 93
        assert dsm.reports_for_date(reports, "2026-12-06") == []  # MM/DD read


class TestAfosGetter:
    def test_afos_text_builds_the_shared_url(self, monkeypatch):
        seen = {}

        def fake_get(url, timeout):
            seen["url"], seen["timeout"] = url, timeout
            return "blob"
        monkeypatch.setattr(dsm, "_get", fake_get)
        assert dsm.afos_text("CLIBOS", limit=5, timeout=11) == "blob"
        assert "pil=CLIBOS" in seen["url"] and "limit=5" in seen["url"]
        assert seen["timeout"] == 11

    def test_429_backs_off_once_then_succeeds(self, monkeypatch):
        import io
        import urllib.error
        import urllib.request

        calls = {"n": 0}
        naps = []

        def fake_urlopen(req, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.HTTPError(
                    "u", 429, "Too Many Requests", None, io.BytesIO(b""))

            class R:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def read(self):
                    return b"payload"
            return R()
        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(dsm, "_throttle", lambda: None)
        monkeypatch.setattr(dsm.time, "sleep", naps.append)
        assert dsm._get("http://x", 5) == "payload"
        assert calls["n"] == 2
        # one backoff, in the jittered band around the base delay
        assert len(naps) == 1
        assert 0.5 * dsm.IEM_429_BACKOFF_S <= naps[0] <= 1.5 * dsm.IEM_429_BACKOFF_S

    def test_exhausted_429s_and_other_codes_raise(self, monkeypatch):
        import io
        import urllib.error
        import urllib.request

        calls = {"n": 0}

        def always_429(req, timeout):
            calls["n"] += 1
            raise urllib.error.HTTPError(
                "u", 429, "Too Many Requests", None, io.BytesIO(b""))
        monkeypatch.setattr(urllib.request, "urlopen", always_429)
        monkeypatch.setattr(dsm, "_throttle", lambda: None)
        monkeypatch.setattr(dsm.time, "sleep", lambda s: None)
        with pytest.raises(urllib.error.HTTPError):
            dsm._get("http://x", 5)
        assert calls["n"] == dsm.IEM_429_ATTEMPTS  # tried, then gave up

        def forbidden(req, timeout):
            raise urllib.error.HTTPError(
                "u", 403, "Forbidden", None, io.BytesIO(b""))
        monkeypatch.setattr(urllib.request, "urlopen", forbidden)
        with pytest.raises(urllib.error.HTTPError):
            dsm._get("http://x", 5)  # a non-429 never retries

    def test_backoff_grows_and_is_jittered(self, monkeypatch):
        # Fixed delays make the aligned crons (*/2, */5, */10, */15 all fire
        # at :00) retry in lockstep and collide again — each retry must land
        # in a wider, randomised band than the last.
        for retry in (0, 1, 2):
            base = dsm.IEM_429_BACKOFF_S * (2 ** retry)
            draws = {dsm.iem_backoff_delay(retry) for _ in range(50)}
            assert len(draws) > 1, "delay must be jittered, not fixed"
            assert all(0.5 * base <= d <= 1.5 * base for d in draws)
        assert min(dsm.iem_backoff_delay(1) for _ in range(50)) \
            >= dsm.IEM_429_BACKOFF_S * 0.5  # retry 1 never undercuts retry 0's floor

    def test_throttle_spaces_requests_and_never_sleeps_when_idle(self, monkeypatch):
        # peak_monitor's 5-city sweep was arriving as five same-second hits.
        naps = []
        clock = {"t": 100.0}
        monkeypatch.setattr(dsm.time, "monotonic", lambda: clock["t"])
        monkeypatch.setattr(dsm.time, "sleep", naps.append)
        monkeypatch.setattr(dsm, "_last_request_monotonic", 0.0)

        dsm._throttle()          # first call after a long idle: no wait
        assert naps == []
        dsm._throttle()          # immediately again: must wait the full gap
        assert naps and naps[0] == pytest.approx(dsm.IEM_MIN_INTERVAL_S)

        naps.clear()
        clock["t"] += dsm.IEM_MIN_INTERVAL_S * 2   # enough time has passed
        dsm._throttle()
        assert naps == []
