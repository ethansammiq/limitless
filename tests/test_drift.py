"""Tests for core/drift.py — the floor→final drift model.

Reference distribution measured live 2026-07-09 (84 pairs): final<=floor
85.7%, +1 11.9%, +2 2.4%. The synthetic journal below reproduces those
bucket shapes at small scale; the invariants (containment math, small-n
refusal, EV) are what the sniper relies on.
"""
import json
from pathlib import Path

import pytest

from core import drift


def _journal(tmp_path: Path, entries: list[dict]) -> Path:
    d = tmp_path / "journal"
    d.mkdir()
    (d / "2026-07-09.jsonl").write_text(
        "\n".join(json.dumps(e) for e in entries))
    return d


def _entry(awips, date, max_f, final, skipped=None):
    e = {"awips": awips, "summary_date": date, "max_f": max_f,
         "min_f": 70, "is_final": final}
    if skipped:
        e["skipped"] = skipped
    return e


class TestLoadPairs:
    def test_pairs_floor_and_final(self, tmp_path):
        d = _journal(tmp_path, [
            _entry("MIA", "2026-07-08", 92, False),
            _entry("MIA", "2026-07-08", 93, True),
            _entry("CHI", "2026-07-08", 87, False),   # no final -> excluded
            _entry("DEN", "2026-07-08", 91, True),    # no floor -> excluded
        ])
        pairs = drift.load_pairs(d)
        assert pairs == [("MIA", "2026-07-08", 92, 93)]

    def test_skipped_intraday_excluded(self, tmp_path):
        # AUS printed max=80 at noon (skipped:"intraday") and 98 final —
        # counting the noon print as a floor would fake a +18 drift.
        d = _journal(tmp_path, [
            _entry("AUS", "2026-07-09", 80, False, skipped="intraday"),
            _entry("AUS", "2026-07-09", 98, False),
            _entry("AUS", "2026-07-09", 98, True),
        ])
        assert drift.load_pairs(d) == [("AUS", "2026-07-09", 98, 98)]

    def test_first_prefinal_print_is_floor(self, tmp_path):
        # The alert fires on the first print; a later re-issued higher floor
        # must not launder the drift out of the sample.
        d = _journal(tmp_path, [
            _entry("SAT", "2026-07-09", 94, False),
            _entry("SAT", "2026-07-09", 96, False),   # re-issue
            _entry("SAT", "2026-07-09", 97, True),
        ])
        assert drift.load_pairs(d) == [("SAT", "2026-07-09", 94, 97)]

    def test_last_final_wins_on_correction(self, tmp_path):
        d = _journal(tmp_path, [
            _entry("DEN", "2026-07-09", 89, False),
            _entry("DEN", "2026-07-09", 91, True),
            _entry("DEN", "2026-07-09", 90, True),    # corrected final
        ])
        assert drift.load_pairs(d) == [("DEN", "2026-07-09", 89, 90)]


def _dist(same=72, up1=10, up2=2):
    return drift.DriftDist(same=same, up1=up1, up2=up2)


class TestBracketWinProb:
    def test_floor_at_bottom_gets_plus_one_buffer(self):
        # floor 92 in a 92-93 bracket: wins on same AND +1 (the 2026-07-09
        # MIA case — final printed 92).
        p = drift.bracket_win_prob(92, 92, 93, _dist())
        assert p == pytest.approx((72 + 10) / 84)

    def test_floor_at_top_wins_only_exact(self):
        # floor 87 in an 86-87 bracket: any drift kills it (the CHI case).
        p = drift.bracket_win_prob(87, 86, 87, _dist())
        assert p == pytest.approx(72 / 84)

    def test_open_ended_top_bracket_absorbs_all_drift(self):
        # "98 or above" with floor 98 wins every bucket.
        assert drift.bracket_win_prob(98, 98, None, _dist()) == pytest.approx(1.0)

    def test_bracket_not_containing_floor_is_not_priced(self):
        assert drift.bracket_win_prob(95, 92, 93, _dist()) is None

    def test_small_sample_refuses(self):
        assert drift.bracket_win_prob(92, 92, 93, _dist(10, 2, 0)) is None


class TestEV:
    def test_ev_matches_hand_math(self):
        # 97.6% at 51c: 97.6 - 51 - fee(51) = +45 net of the 2c fee — the
        # OKC 2026-07-09 number a human talked himself out of.
        prob = (72 + 10) / 84
        ev = drift.ev_cents(prob, 51)
        assert 43 < ev < 46
