"""Tests for cli_sniper pure helpers — real product fixtures, no network."""
import json
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
        # Window measured from real issuance times (01:13-04:51 local across
        # 16 offices, backtest/cli_timing.py 2026-07-05).
        assert cs.window_kind(1.0) == "morning"
        assert cs.window_kind(1.5) == "morning"   # LOX/SEW finals ~01:30
        assert cs.window_kind(4.9) == "morning"   # FFC straggler 04:51
        assert cs.window_kind(0.9) is None
        assert cs.window_kind(5.5) is None        # old window start — now closed
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


class TestObsAnnotation:
    """The 2026-07-12 DAL/AUS trap class: post-print obs already beat the
    floor bracket. The manual check that killed them, as code."""

    def _entry(self, subtitle="95° to 96°", final=False, ladder_kind="high"):
        return {"kind": "buy_winner", "ticker": "KXHIGHTDAL-26JUL12-B95.5",
                "subtitle": subtitle, "printed": 96, "final": final,
                "ladder_kind": ladder_kind, "ask": 1, "ask_depth": 60134}

    def test_corroborated_exceedance_stamps_hard_kill(self):
        e = self._entry()   # bracket 95-96
        cs._annotate_obs_context([e], 96.98, 96.98)
        assert e["obs_max_f"] == 97.0
        assert "settle ≥97°" in e["obs_kill"]
        assert "obs_warn" not in e

    def test_lone_spike_exceedance_stamps_soft_warn(self):
        # KDFW's REAL 2026-07-12 day: 96.98 peak, next hourly ob 93.92 —
        # corroboration returns None, but the spike named the final (97)
        e = self._entry()
        cs._annotate_obs_context([e], None, 96.98)
        assert e["obs_max_f"] == 97.0
        assert "obs_kill" not in e
        assert "uncorroborated" in e["obs_warn"]

    def test_obs_inside_bracket_annotates_without_kill(self):
        e = self._entry(subtitle="90° or below")
        cs._annotate_obs_context([e], 89.96, 89.96)   # MSP T91: held by 0.04°F
        assert e["obs_max_f"] == 90.0
        assert "obs_kill" not in e and "obs_warn" not in e

    def test_final_low_and_missing_obs_untouched(self):
        e_final = self._entry(final=True)
        e_low = self._entry(ladder_kind="low")
        cs._annotate_obs_context([e_final, e_low], 99.0, 99.0)
        assert "obs_max_f" not in e_final and "obs_max_f" not in e_low
        e = self._entry()
        cs._annotate_obs_context([e], None, None)
        assert "obs_max_f" not in e


class TestWallFlag:
    class _Client:
        def __init__(self, books):
            self._books = books

        async def get_orderbook(self, ticker):
            return self._books[ticker]

    def _find(self):
        return {"ticker": "KXHIGHCHI-26JUL04-B84.5", "subtitle": "84° to 85°",
                "series": "KXHIGHCHI", "ladder_kind": "high", "printed": 85,
                "final": False, "kind": "buy_winner"}

    def test_deep_ask_flags_wall(self):
        import asyncio as aio
        books = {"KXHIGHCHI-26JUL04-B84.5": {"yes": [], "no": [[99, 154899]]}}
        out = aio.run(cs._price_findings(self._Client(books), [self._find()]))
        assert out[0]["wall_ask"] is True

    def test_thin_ask_has_no_flag(self):
        import asyncio as aio
        books = {"KXHIGHCHI-26JUL04-B84.5": {"yes": [], "no": [[84, 40]]}}
        out = aio.run(cs._price_findings(self._Client(books), [self._find()]))
        assert "wall_ask" not in out[0]


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

    def test_obs_kill_and_wall_render_in_alert(self):
        opps = [{"kind": "buy_winner", "ticker": "KXHIGHTDAL-26JUL12-B95.5",
                 "subtitle": "95° to 96°", "printed": 96, "final": False,
                 "ladder_kind": "high", "ask": 1, "ask_depth": 60134.0,
                 "wall_ask": True, "obs_max_f": 97.0,
                 "obs_kill": "obs already 97.0° ⇒ settle ≥97° — bracket dead",
                 "cmd": ".venv/bin/python scripts/take.py KXHIGHTDAL-26JUL12-B95.5 buy yes 60134 1"}]
        _, body = cs.format_alert(opps)
        assert "🚫" in body and "bracket dead" in body
        assert "🧱" in body and "never fade" in body

    def test_wall_suppresses_the_ev_line(self):
        # MIN T94 2026-07-14: "EV +87¢" printed beside "never fade" — the
        # wall invalidates the drift base rate, so the stats are withheld.
        opps = [{"kind": "buy_winner", "ticker": "KXHIGHTMIN-26JUL14-T94",
                 "subtitle": "93° or below", "printed": 93, "final": False,
                 "ladder_kind": "high", "ask": 1, "ask_depth": 47723.0,
                 "wall_ask": True, "drift_prob": 0.89, "drift_n": 184,
                 "drift_ev_c": 87.0,
                 "cmd": ".venv/bin/python scripts/take.py KXHIGHTMIN-26JUL14-T94 buy yes 47723 1"}]
        _, body = cs.format_alert(opps)
        assert "🧱" in body and "never fade" in body
        assert "EV" not in body and "drift 89%" not in body

    def test_unwalled_alert_keeps_the_ev_line(self):
        opps = [{"kind": "buy_winner", "ticker": "T", "subtitle": "81° or below",
                 "printed": 80, "final": False, "ladder_kind": "high",
                 "ask": 34, "ask_depth": 9.0, "drift_prob": 0.98,
                 "drift_n": 184, "drift_ev_c": 62.0,
                 "cmd": ".venv/bin/python scripts/take.py T buy yes 9 34"}]
        _, body = cs.format_alert(opps)
        assert "drift 98% win" in body and "EV +62¢" in body

    def test_safe_obs_renders_inline(self):
        opps = [{"kind": "buy_winner", "ticker": "T", "subtitle": "90° or below",
                 "printed": 90, "final": False, "ladder_kind": "high",
                 "ask": 18, "ask_depth": 23.0, "obs_max_f": 90.0,
                 "cmd": ".venv/bin/python scripts/take.py T buy yes 23 18"}]
        _, body = cs.format_alert(opps)
        assert "obs so far 90.0°" in body and "🚫" not in body


class TestEffectiveFinality:
    """Regression for 2026-07-05: 07:31-local 'so far' products (AUS/SAT/DEN)
    were regex-marked final and alerted false 1c certain-winners."""

    NOW = datetime(2026, 7, 5, 20, 30, tzinfo=timezone.utc)

    def _p(self, stamp, summary, is_final=True):
        return cs.ParsedCLI(awips="AUS", stamp=stamp, summary_date=summary,
                            is_final=is_final, max_f=80, min_f=74)

    def test_same_day_morning_product_skips(self):
        # AUS stamp 051231 = 07:31 CDT Jul 5, summary Jul 5 -> intraday junk
        assert cs.effective_finality(
            self._p("051231", "2026-07-05"), "America/Chicago", self.NOW) == "skip"

    def test_same_day_mountain_morning_skips(self):
        # DEN stamp 051229 = 06:29 MDT Jul 5, summary Jul 5
        assert cs.effective_finality(
            self._p("051229", "2026-07-05"), "America/Denver", self.NOW) == "skip"

    def test_same_day_afternoon_is_floor_even_if_regex_said_final(self):
        # NOLA stamp 052150 = 16:50 CDT Jul 5, summary Jul 5 -> floor
        assert cs.effective_finality(
            self._p("052150", "2026-07-05"), "America/Chicago", self.NOW) == "floor"

    def test_yesterday_summary_is_final(self):
        # 06:31 CDT Jul 5 product summarizing Jul 4 -> genuine morning final
        assert cs.effective_finality(
            self._p("051131", "2026-07-04"), "America/Chicago", self.NOW) == "final"

    def test_final_even_if_regex_said_floor(self):
        assert cs.effective_finality(
            self._p("051131", "2026-07-04", is_final=False),
            "America/Chicago", self.NOW) == "final"

    def test_bad_stamp_skips(self):
        assert cs.effective_finality(
            self._p("9999xx", "2026-07-05"), "America/Chicago", self.NOW) == "skip"


class TestRunSeenMarking:
    """End-to-end run() test of the 2026-07-06 fix: a degraded market read
    must NOT mark a live CLI product 'seen' (which would discard a real
    winner forever); a clean read marks it seen so it isn't reprocessed."""

    import asyncio as _asyncio

    class _FakeClient:
        def __init__(self, ok):
            self._ok = ok
            self.stopped = False

        def __call__(self, *a, **k):   # stand in for KalshiClient(...)
            return self

        async def start(self):
            pass

        async def stop(self):
            self.stopped = True

        async def get_markets_checked(self, series_ticker=None, status="open", limit=100):
            return ([], self._ok)

    def _run(self, monkeypatch, tmp_path, ok):
        import asyncio
        from datetime import datetime, timezone
        import cli_sniper as csm
        import kalshi_client

        fixed = datetime(2026, 7, 4, 21, 38, tzinfo=timezone.utc)

        class FakeDT(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed

        monkeypatch.setattr(csm, "datetime", FakeDT)
        monkeypatch.setattr(csm, "STATE_FILE", tmp_path / "state.json")
        monkeypatch.setattr(csm, "JOURNAL_DIR", tmp_path / "journal")
        monkeypatch.setattr(csm, "_fetch_product", lambda wfo, awips, version=1: AFTERNOON)
        monkeypatch.setattr(csm, "stations_in_window", lambda now, groups: ["MDW"])
        monkeypatch.setattr(kalshi_client, "KalshiClient", self._FakeClient(ok))

        asyncio.run(csm.run(dry_run=False, replay=None))
        return json.loads((tmp_path / "state.json").read_text())

    def test_degraded_read_leaves_unseen(self, monkeypatch, tmp_path):
        state = self._run(monkeypatch, tmp_path, ok=False)
        assert "MDW:042136" not in state["seen"]   # will retry next cron

    def test_clean_read_marks_seen(self, monkeypatch, tmp_path):
        state = self._run(monkeypatch, tmp_path, ok=True)
        assert "MDW:042136" in state["seen"]


class TestDSMVeto:
    """2026-07-06 MIA regression: prelim CLI printed 92 while the DSM had
    93/1344 committed — the final followed the DSM. A floor buy_winner the
    DSM contradicts must be vetoed, never suggested as a buy."""

    from core import dsm as _dsm

    REPORTS = _dsm.parse_dsm_text(
        "KMIA DS 06/07 931344/ 771818//")

    def _finding(self, kind="buy_winner", printed=92, ladder_kind="high"):
        return {"ticker": "KXHIGHMIA-26JUL06-B91.5", "subtitle": "91° to 92°",
                "series": "KXHIGHMIA", "ladder_kind": ladder_kind,
                "printed": printed, "final": False, "kind": kind}

    def test_contradicted_buy_is_vetoed(self):
        kept, vetoed = cs.apply_dsm_veto(
            [self._finding()], self.REPORTS, "2026-07-06")
        assert kept == []
        assert vetoed[0]["kind"] == "dsm_veto"
        assert vetoed[0]["dsm_extreme"] == 93
        assert vetoed[0]["dsm_time_lst"] == "1344"

    def test_agreeing_buy_passes_annotated(self):
        kept, vetoed = cs.apply_dsm_veto(
            [self._finding(printed=93)], self.REPORTS, "2026-07-06")
        assert vetoed == []
        assert kept[0]["kind"] == "buy_winner" and kept[0]["dsm"] == 93

    def test_sell_dead_never_vetoed(self):
        kept, vetoed = cs.apply_dsm_veto(
            [self._finding(kind="sell_dead")], self.REPORTS, "2026-07-06")
        assert vetoed == [] and kept[0]["kind"] == "sell_dead"

    def test_no_dsm_for_date_fails_open(self):
        kept, vetoed = cs.apply_dsm_veto(
            [self._finding()], self.REPORTS, "2026-07-07")
        assert vetoed == []
        assert kept[0]["kind"] == "buy_winner" and kept[0]["dsm"] == "unchecked"

    def test_empty_reports_fail_open(self):
        kept, vetoed = cs.apply_dsm_veto([self._finding()], [], "2026-07-06")
        assert vetoed == [] and kept[0]["dsm"] == "unchecked"

    def test_low_ladder_mirrors(self):
        kept, vetoed = cs.apply_dsm_veto(
            [self._finding(printed=78, ladder_kind="low")],
            self.REPORTS, "2026-07-06")
        assert vetoed[0]["dsm_extreme"] == 77   # DSM min 77 < printed 78

    def test_dsm_inside_bracket_is_not_vetoed(self):
        """2026-07-09 MSP false veto: printed max 83, DSM 84, bracket 83-84 —
        the DSM disagreed with the print but stayed inside the bracket; the
        final CLI was 84 and the bracket won. A DSM extreme inside the
        finding's own bracket must pass, annotated, not vetoed."""
        reports = self._dsm.parse_dsm_text("KMSP DS 09/07 841512/ 660544//")
        finding = {"ticker": "KXHIGHTMIN-26JUL09-B83.5", "subtitle": "83° to 84°",
                   "series": "KXHIGHTMIN", "ladder_kind": "high",
                   "printed": 83, "final": False, "kind": "buy_winner"}
        kept, vetoed = cs.apply_dsm_veto([finding], reports, "2026-07-09")
        assert vetoed == []
        assert kept[0]["kind"] == "buy_winner" and kept[0]["dsm"] == 84

    def test_dsm_outside_bracket_still_vetoes(self):
        """2026-07-09 DEN correct veto: printed low 59, DSM 57, bracket 58-59
        — the DSM extreme escapes the bracket, so the veto must still fire."""
        reports = self._dsm.parse_dsm_text("KDEN DS 09/07 891455/ 570449//")
        finding = {"ticker": "KXLOWTDEN-26JUL09-B58.5", "subtitle": "58° to 59°",
                   "series": "KXLOWTDEN", "ladder_kind": "low",
                   "printed": 59, "final": False, "kind": "buy_winner"}
        kept, vetoed = cs.apply_dsm_veto([finding], reports, "2026-07-09")
        assert kept == []
        assert vetoed[0]["kind"] == "dsm_veto" and vetoed[0]["dsm_extreme"] == 57

    def test_alert_line_has_no_command(self):
        _, vetoed = cs.apply_dsm_veto(
            [self._finding()], self.REPORTS, "2026-07-06")
        title, body = cs.format_alert(vetoed)
        assert "1 DSM veto" in title
        assert "93° @ 1344 LST" in body
        assert "VETOED" in body
        assert "take.py" not in body.split("_Alert only")[0]


def _with_correction(product_text: str, suffix: str = "CCA") -> str:
    """Append a WMO correction suffix to a fixture's stamp line."""
    import re as _re
    return _re.sub(r"(?m)^(\w{6}\s+K\w{3}\s+\d{6})\s*$", rf"\1 {suffix}",
                   product_text, count=1)


class TestCorrections:
    """2026-07-08 regression: a post-final CCA scrubbed the MIA minimum to
    MM and the $-anchored stamp regex rejected the entire product — the
    correction was invisible to the pipeline."""

    def test_corrected_product_parses(self):
        p = cs.parse_product(_with_correction(MORNING))
        assert p is not None
        assert p.correction == "CCA"
        assert p.summary_date == "2026-07-03"

    def test_uncorrected_product_has_no_suffix(self):
        assert cs.parse_product(MORNING).correction is None

    def test_seen_key_distinguishes_correction(self):
        plain = cs.parse_product(MORNING)
        corr = cs.parse_product(_with_correction(MORNING))
        assert cs._seen_key(plain) != cs._seen_key(corr)
        assert cs._seen_key(corr).endswith(":CCA")

    def test_classify_tags_findings_corrected(self):
        p = cs.parse_product(_with_correction(MORNING))
        markets = [_mkt("KXHIGHCHI-26JUL03-B90.5", "90° to 91°")]
        found = cs.classify(p, MDW_HIGH, markets)
        assert found[0]["corrected"] == "CCA"

    def test_correction_notice_alert_has_no_command(self):
        notice = {"kind": "correction_notice",
                  "ticker": "CORR:MIA:2026-07-07:081455:CCA",
                  "awips": "MIA", "summary_date": "2026-07-07",
                  "corrected": "CCA", "final": True,
                  "max_f": 93, "min_f": None}
        title, body = cs.format_alert([notice])
        assert "1 CORRECTION" in title
        assert "MM (removed)" in body
        assert "re-verify" in body.lower()
        assert "take.py" not in body.split("_Alert only")[0]


class TestPriceFindings:
    """_price_findings computes the ask, applies caps, and emits the literal
    command humans run — previously untested (audit 2026-07-08)."""

    class _Client:
        def __init__(self, books):
            self._books = books

        async def get_orderbook(self, ticker):
            return self._books[ticker]

    def _find(self, kind="buy_winner", ladder_kind="high", final=False,
              ticker="KXHIGHCHI-26JUL04-B84.5"):
        return {"ticker": ticker, "subtitle": "84° to 85°",
                "series": "KXHIGHCHI", "ladder_kind": ladder_kind,
                "printed": 85, "final": final, "kind": kind}

    def _run(self, findings, books):
        import asyncio as aio
        return aio.run(cs._price_findings(self._Client(books), findings))

    def test_high_floor_buy_priced_with_command(self):
        books = {"KXHIGHCHI-26JUL04-B84.5": {"yes": [], "no": [[84, 40]]}}
        out = self._run([self._find()], books)
        assert out[0]["ask"] == 16 and out[0]["ask_depth"] == 40
        assert out[0]["cmd"].endswith("KXHIGHCHI-26JUL04-B84.5 buy yes 40 16")
        assert "suppressed" not in out[0]

    def test_floor_ask_cap_filters(self):
        books = {"KXHIGHCHI-26JUL04-B84.5": {"yes": [], "no": [[25, 40]]}}
        assert self._run([self._find(final=False)], books) == []      # ask 75 > 70
        out = self._run([self._find(final=True)], books)              # 75 ≤ 85
        assert out and out[0]["ask"] == 75

    def test_low_floor_buy_suppressed_but_journaled(self):
        books = {"KXLOWTMIA-26JUL07-B74.5": {"yes": [], "no": [[85, 53]]}}
        out = self._run([self._find(ladder_kind="low",
                                    ticker="KXLOWTMIA-26JUL07-B74.5")], books)
        assert out[0]["suppressed"] == "low_floor_forecast"
        assert "cmd" not in out[0]           # never actionable
        assert out[0]["ask"] == 15           # still measured for the scorecard

    def test_low_final_buy_not_suppressed(self):
        books = {"KXLOWTMIA-26JUL07-B74.5": {"yes": [], "no": [[85, 53]]}}
        out = self._run([self._find(ladder_kind="low", final=True,
                                    ticker="KXLOWTMIA-26JUL07-B74.5")], books)
        assert "suppressed" not in out[0] and "cmd" in out[0]


class TestDriftEconomics:
    """Floor buy_winners carry the measured drift probability (2026-07-09:
    three 86-98% brackets went unbought because the alert had no number)."""

    def _entry(self, **over):
        e = {"ticker": "KXHIGHMIA-26JUL09-B92.5", "subtitle": "92° to 93°",
             "ladder_kind": "high", "printed": 92, "final": False, "ask": 56}
        e.update(over)
        return e

    def _with_dist(self, monkeypatch, same=89, up1=13, up2=2):
        from core.drift import DriftDist
        monkeypatch.setattr(cs, "_drift_dist", lambda: DriftDist(same, up1, up2))

    def test_floor_high_gets_probability_and_ev(self, monkeypatch):
        self._with_dist(monkeypatch)
        e = self._entry()
        cs._attach_drift_economics(e)
        assert 0.97 < e["drift_prob"] < 1.0     # floor-at-bottom: same + up1
        assert e["drift_n"] == 104
        assert e["drift_ev_c"] > 35

    def test_final_and_low_ladders_not_priced(self, monkeypatch):
        self._with_dist(monkeypatch)
        fin = self._entry(final=True)
        cs._attach_drift_economics(fin)
        low = self._entry(ladder_kind="low")
        cs._attach_drift_economics(low)
        assert "drift_prob" not in fin
        assert "drift_prob" not in low

    def test_small_sample_attaches_nothing(self, monkeypatch):
        self._with_dist(monkeypatch, same=10, up1=1, up2=0)
        e = self._entry()
        cs._attach_drift_economics(e)
        assert "drift_prob" not in e


# ─── Reissue guard (2026-07-16 BOS: bogus min 51 silently re-issued as 69) ──

BOS_LOW = Ladder(series="KXLOWTBOS", kind="low", awips="BOS", wfo="BOX",
                 station_icao="KBOS", tz="America/New_York")
GUARD_NOW = datetime(2026, 7, 16, 21, 42, 1, tzinfo=timezone.utc)
BOS_MARKETS = [
    _mkt("KXLOWTBOS-26JUL16-T68", "69° or above"),
    _mkt("KXLOWTBOS-26JUL16-B67.5", "67° to 68°"),
    _mkt("KXLOWTBOS-26JUL16-T61", "60° or below"),
]


def _bos_product(stamp: str, min_f, max_f=89) -> str:
    min_line = f"  MINIMUM         {min_f}    509 AM\n" if min_f is not None else ""
    return (f"000\nCDUS41 KBOX {stamp}\nCLIBOS\n\n"
            f"CLIMATE REPORT\nNATIONAL WEATHER SERVICE BOSTON, MA\n\n"
            f"...THE BOSTON MA CLIMATE SUMMARY FOR JULY 16 2026...\n"
            f"VALID TODAY AS OF 0400 PM LOCAL TIME.\n\n"
            f"TEMPERATURE (F)\n TODAY\n"
            f"  MAXIMUM         {max_f}    435 PM\n{min_line}")


def _floor(parsed):
    """Mirror run()'s effective-finality override before classification."""
    parsed.is_final = cs.effective_finality(
        parsed, "America/New_York", GUARD_NOW) == "final"
    return parsed


class TestConflictRule:
    def test_no_move_never_conflicts(self):
        assert not cs._conflicts("sell_dead", "low", False, 51, 51)
        assert not cs._conflicts("buy_winner", "high", True, 94, 94)

    def test_impossible_direction_kills_sell_dead(self):
        # A floor min can only FALL between prints; 51→69 = bogus print.
        assert cs._conflicts("sell_dead", "low", False, 51, 69)
        # A floor max can only RISE; 94→92 = bogus print.
        assert cs._conflicts("sell_dead", "high", False, 94, 92)

    def test_legit_direction_keeps_sell_dead(self):
        # Deadness is monotone: a higher max / lower min only strengthens it.
        assert not cs._conflicts("sell_dead", "high", False, 94, 95)
        assert not cs._conflicts("sell_dead", "low", False, 69, 67)

    def test_any_move_kills_buy_winner(self):
        assert cs._conflicts("buy_winner", "high", False, 94, 95)
        assert cs._conflicts("buy_winner", "low", False, 69, 67)

    def test_any_move_on_a_final_kills_everything(self):
        assert cs._conflicts("sell_dead", "high", True, 94, 95)
        assert cs._conflicts("sell_dead", "low", True, 69, 67)

    def test_scrubbed_to_mm_kills_everything(self):
        assert cs._conflicts("sell_dead", "low", False, 51, None)
        assert cs._conflicts("buy_winner", "high", False, 94, None)


class TestArchiveFetchAndSplit:
    def test_splits_multi_product_blob(self, monkeypatch):
        blob = _bos_product("162139", 69) + "\x01" + _bos_product("162129", 51)
        from core import dsm
        monkeypatch.setattr(dsm, "afos_text", lambda pil, **kw: blob)
        products = cs.fetch_archive_products("BOS")
        assert [(p.stamp, p.min_f) for p in products] == [
            ("162139", 69), ("162129", 51)]

    def test_splits_on_sequence_header_without_control_char(self, monkeypatch):
        blob = _bos_product("162139", 69) + "\n" + _bos_product("162129", 51)
        from core import dsm
        monkeypatch.setattr(dsm, "afos_text", lambda pil, **kw: blob)
        assert len(cs.fetch_archive_products("BOS")) == 2

    def test_fetch_failure_returns_none(self, monkeypatch):
        from core import dsm

        def boom(pil, **kw):
            raise OSError("HTTP Error 429")
        monkeypatch.setattr(dsm, "afos_text", boom)
        assert cs.fetch_archive_products("BOS") is None


class TestNewerArchiveProduct:
    def _parse(self, stamp, min_f):
        return _floor(cs.parse_product(_bos_product(stamp, min_f)))

    def test_finds_the_silent_reissue(self):
        bogus = self._parse("162129", 51)
        archive = [self._parse("162139", 69), self._parse("162129", 51)]
        status, newer = cs.newer_archive_product(
            bogus, "America/New_York", GUARD_NOW, archive)
        assert status == "newer" and newer.stamp == "162139"
        assert newer.min_f == 69

    def test_older_archive_copy_cannot_refute(self):
        # IEM lag: processing the CORRECT reissue while the archive still
        # holds only the bogus original must not suppress its staging.
        good = self._parse("162139", 69)
        archive = [self._parse("162129", 51)]
        status, newer = cs.newer_archive_product(
            good, "America/New_York", GUARD_NOW, archive)
        assert status == "clear" and newer is None

    def test_same_stamp_is_clear(self):
        bogus = self._parse("162129", 51)
        status, newer = cs.newer_archive_product(
            bogus, "America/New_York", GUARD_NOW, [self._parse("162129", 51)])
        assert status == "clear" and newer is None

    def test_unavailable_archive_is_unchecked(self):
        bogus = self._parse("162129", 51)
        status, _ = cs.newer_archive_product(
            bogus, "America/New_York", GUARD_NOW, None)
        assert status == "unchecked"

    def test_other_station_or_date_ignored(self):
        bogus = self._parse("162129", 51)
        other = self._parse("162139", 69)
        other.awips = "NYC"
        status, _ = cs.newer_archive_product(
            bogus, "America/New_York", GUARD_NOW, [other])
        assert status == "clear"


class TestReissueSequence51To69:
    """End-to-end mirror of the live incident: classify off the bogus 51,
    guard against the archived 162139/69, staging must die."""

    def _findings(self):
        bogus = _floor(cs.parse_product(_bos_product("162129", 51)))
        found = cs.classify(bogus, BOS_LOW, BOS_MARKETS)
        by = {f["ticker"]: f for f in found}
        # The falsified premise: the live favorite marked dead, plus a
        # bogus buy_winner on the 60-or-below bracket.
        assert by["KXLOWTBOS-26JUL16-T68"]["kind"] == "sell_dead"
        assert by["KXLOWTBOS-26JUL16-T61"]["kind"] == "buy_winner"
        return bogus, found, by

    def test_findings_carry_the_premise(self):
        _, _, by = self._findings()
        f = by["KXLOWTBOS-26JUL16-T68"]
        assert (f["awips"], f["stamp"], f["summary_date"]) == (
            "BOS", "162129", "2026-07-16")

    def test_conflicts_stamped_and_staging_blocked(self):
        from core import take_queue

        bogus, found, by = self._findings()
        newer = _floor(cs.parse_product(_bos_product("162139", 69)))
        n = cs.apply_reissue_conflicts(found, newer)
        assert n == len(found)          # min ROSE: impossible, all refuted
        sell = by["KXLOWTBOS-26JUL16-T68"]
        assert "51→69" in sell["reissue_conflict"]
        sell["cmd"] = (".venv/bin/python scripts/take.py "
                       "KXLOWTBOS-26JUL16-T68 sell yes 424 31")
        assert take_queue.entry_from_finding(sell, "cli_sniper", GUARD_NOW) is None

    def test_legit_floor_rise_keeps_sell_dead_staging(self):
        high = Ladder(series="KXHIGHNY", kind="high", awips="NYC", wfo="OKX",
                      station_icao="KNYC", tz="America/New_York")
        p = _floor(cs.parse_product(_bos_product("162129", 51, max_f=94)))
        p.awips = "NYC"
        markets = [_mkt("KXHIGHNY-26JUL16-T90", "89° or below"),
                   _mkt("KXHIGHNY-26JUL16-B94.5", "94° to 95°")]
        found = cs.classify(p, high, markets)
        newer = _floor(cs.parse_product(_bos_product("162140", 51, max_f=95)))
        cs.apply_reissue_conflicts(found, newer)
        by = {f["ticker"]: f for f in found}
        assert "reissue_conflict" not in by["KXHIGHNY-26JUL16-T90"]   # still dead
        assert "reissue_conflict" in by["KXHIGHNY-26JUL16-B94.5"]     # bracket premise moved


class TestCheckPremise:
    ENTRY = {"kind": "sell_dead",
             "premise": {"awips": "BOS", "stamp": "162129",
                         "summary_date": "2026-07-16", "printed": 51,
                         "ladder_kind": "low", "final": False}}

    def _products(self, *specs):
        return [_floor(cs.parse_product(_bos_product(s, m))) for s, m in specs]

    def test_moved_premise_refuses_the_fire(self):
        verdict, reason = cs.check_premise(
            self.ENTRY, GUARD_NOW, products=self._products(("162139", 69)))
        assert verdict == "moved"
        assert "51→69" in reason

    def test_unmoved_reissue_is_clear(self):
        verdict, _ = cs.check_premise(
            self.ENTRY, GUARD_NOW, products=self._products(("162139", 51)))
        assert verdict == "clear"

    def test_archive_unavailable_fails_open(self, monkeypatch):
        monkeypatch.setattr(cs, "fetch_archive_products", lambda awips: None)
        verdict, _ = cs.check_premise(self.ENTRY, GUARD_NOW)
        assert verdict == "unchecked"

    def test_entry_without_premise_is_unchecked(self):
        verdict, _ = cs.check_premise({"kind": "sell_dead"}, GUARD_NOW,
                                      products=self._products(("162139", 69)))
        assert verdict == "unchecked"


class TestSupersededEntryIds:
    def _entry(self, **over):
        base = {"source": "cli_sniper", "status": "posted",
                "kind": "sell_dead", "ticker": "KXLOWTBOS-26JUL16-T68",
                "premise": {"awips": "BOS", "stamp": "162129",
                            "summary_date": "2026-07-16", "printed": 51,
                            "ladder_kind": "low", "final": False}}
        base.update(over)
        return base

    def _reissue(self):
        return _floor(cs.parse_product(_bos_product("162139", 69)))

    def test_bos_button_is_superseded(self):
        ids = cs.superseded_entry_ids({"e1": self._entry()}, self._reissue(),
                                      GUARD_NOW)
        assert ids == ["e1"]

    def test_terminal_metar_other_station_and_same_stamp_ignored(self):
        entries = {
            "done": self._entry(status="executed"),
            "metar": self._entry(source="metar_sniper"),
            "other": self._entry(premise={**self._entry()["premise"],
                                          "awips": "NYC"}),
            "same": self._entry(premise={**self._entry()["premise"],
                                         "stamp": "162139"}),
        }
        assert cs.superseded_entry_ids(entries, self._reissue(), GUARD_NOW) == []

    def test_legit_move_keeps_sell_dead_button(self):
        e = self._entry(kind="sell_dead",
                        premise={"awips": "BOS", "stamp": "162129",
                                 "summary_date": "2026-07-16", "printed": 72,
                                 "ladder_kind": "low", "final": False})
        # Reissue min 69 < premise 72: the min legitimately fell — brackets
        # above 72 are still dead, the button stands.
        assert cs.superseded_entry_ids({"e": e}, self._reissue(), GUARD_NOW) == []


class TestPriorJournalAndMoves:
    NOW = datetime(2026, 7, 16, 21, 56, 1, tzinfo=timezone.utc)

    def _write(self, tmp_path, monkeypatch, rows):
        import json as _json
        monkeypatch.setattr(cs, "JOURNAL_DIR", tmp_path)
        with (tmp_path / "2026-07-16.jsonl").open("w") as fh:
            for r in rows:
                fh.write(_json.dumps(r) + "\n")

    FLOOR_ROW = {"ts": "2026-07-16T21:42:01+00:00", "awips": "BOS",
                 "stamp": "162129", "summary_date": "2026-07-16",
                 "is_final": False, "max_f": 89, "min_f": 51,
                 "findings": [{"ticker": "KXLOWTBOS-26JUL16-T68",
                               "kind": "sell_dead", "ladder_kind": "low",
                               "printed": 51}]}

    def test_prior_floor_found_and_moves_detected(self, tmp_path, monkeypatch):
        skipped = {"ts": "2026-07-16T12:30:02+00:00", "awips": "BOS",
                   "stamp": "161229", "summary_date": "2026-07-16",
                   "is_final": True, "max_f": 73, "min_f": 68,
                   "skipped": "intraday", "findings": []}
        self._write(tmp_path, monkeypatch, [skipped, self.FLOOR_ROW])
        prior = cs._prior_journaled_product("BOS", "2026-07-16", False, self.NOW)
        assert prior is not None and prior["stamp"] == "162129"
        reissue = _floor(cs.parse_product(_bos_product("162139", 69)))
        assert cs.reissue_moves(prior, reissue) == {"low": (51, 69)}

    def test_move_on_unpremised_kind_ignored(self, tmp_path, monkeypatch):
        self._write(tmp_path, monkeypatch, [self.FLOOR_ROW])
        prior = cs._prior_journaled_product("BOS", "2026-07-16", False, self.NOW)
        # max also differs (89→88) but no high-ladder finding was premised
        # on it — only the low move is an exit signal.
        reissue = _floor(cs.parse_product(_bos_product("162139", 69, max_f=88)))
        assert cs.reissue_moves(prior, reissue) == {"low": (51, 69)}

    def test_no_prior_without_findings(self, tmp_path, monkeypatch):
        row = dict(self.FLOOR_ROW, findings=[])
        self._write(tmp_path, monkeypatch, [row])
        assert cs._prior_journaled_product(
            "BOS", "2026-07-16", False, self.NOW) is None


class TestReissueAlertFormat:
    def test_notice_line(self):
        notice = {"kind": "reissue_notice", "ticker": "REISSUE:BOS:2026-07-16:162139",
                  "awips": "BOS", "summary_date": "2026-07-16",
                  "prior_stamp": "162129", "stamp": "162139",
                  "moves": {"low": [51, 69]}, "retracted": 1, "final": False}
        title, body = cs.format_alert([notice])
        assert "REISSUE" in title
        assert "min 51→69" in body and "162129→162139" in body
        assert "1 staged button(s) retracted" in body

    def test_conflicted_sell_line_carries_the_stop(self):
        o = {"kind": "sell_dead", "ticker": "KXLOWTBOS-26JUL16-T68",
             "subtitle": "69° or above", "printed": 51, "final": False,
             "ladder_kind": "low", "net_cents": 19430,
             "levels": [[83, 10]], "cmd": "x",
             "reissue_conflict": "reissued 162139: min 51→69"}
        _, body = cs.format_alert([o])
        assert "🛑" in body and "do not trade this print" in body
