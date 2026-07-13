"""Tests for scripts/position_brief.py pure helpers (no network)."""
import json

from scripts.position_brief import fmt_book, journal_rows, relevant_prints, resolve_ladder

TICKER = "KXHIGHTDAL-26JUL12-B95.5"

CLI_ROW = {"ts": "2026-07-12T21:42:01+00:00", "awips": "DFW", "stamp": "122138",
           "summary_date": "2026-07-12", "is_final": False, "max_f": 96, "min_f": 78,
           "findings": [{"ticker": TICKER, "kind": "buy_winner", "ask": 1,
                         "ask_depth": 60134, "drift_prob": 0.875}]}
METAR_ROW = {"ts": "2026-07-13T00:05:00+00:00", "station": "KDFW", "kind": "max",
             "tenths_c": 361, "temp_f": 96.98, "rounded_f": 97, "findings": []}
OTHER_ROW = {"ts": "2026-07-12T20:00:00+00:00", "awips": "MSP", "stamp": "122126",
             "summary_date": "2026-07-12", "is_final": False, "max_f": 90, "min_f": 75,
             "findings": [{"ticker": "KXHIGHTMIN-26JUL12-T91", "kind": "buy_winner"}]}


class TestRelevantPrints:
    def test_station_day_and_ticker_rows_kept_others_dropped(self):
        rows = [CLI_ROW, METAR_ROW, OTHER_ROW]
        out = relevant_prints(rows, TICKER, "DFW", "KDFW", "2026-07-12")
        assert len(out) == 2
        assert any("CLI DFW" in line and "max=96" in line for line in out)
        assert any("METAR KDFW" in line and "→ 97°" in line for line in out)

    def test_ticker_finding_details_inlined(self):
        out = relevant_prints([CLI_ROW], TICKER, "DFW", "KDFW", "2026-07-12")
        assert "this ticker" in out[0]
        assert "drift_prob=0.875" in out[0]

    def test_wrong_day_rows_excluded_unless_ticker_matches(self):
        plain = {**CLI_ROW, "findings": []}   # same station, no ticker finding
        assert relevant_prints([plain], TICKER, "DFW", "KDFW", "2026-07-11") == []
        # a finding on the exact ticker forces inclusion regardless of date
        assert len(relevant_prints([CLI_ROW], TICKER, "MSP", "KMSP",
                                   "2026-07-11")) == 1


class TestJournalRows:
    def test_reads_last_n_day_files(self, tmp_path):
        for d in ("2026-07-10", "2026-07-11", "2026-07-12"):
            (tmp_path / f"{d}.jsonl").write_text(json.dumps({"ts": d}) + "\n")
        assert len(journal_rows(tmp_path, days=2)) == 2
        assert journal_rows(tmp_path / "missing", days=2) == []


class TestResolveAndBook:
    def test_resolves_real_ladder(self):
        lad = resolve_ladder("KXHIGHTDAL-26JUL12-B95.5")
        assert lad.awips == "DFW" and lad.station_icao == "KDFW"
        assert resolve_ladder("KXNOPE-26JUL12-T1") is None

    def test_book_formatting(self):
        s = fmt_book({"yes": [[3, 10]], "no": [[99, 154899]]})
        assert "bid 3¢ / ask 1¢" in s
        assert fmt_book({}) .startswith("bid None¢ / ask None¢")
