"""core/walls.py — certainty-wall detection (competitor dossier, 2026-07-07)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.walls import detect_wall, scan_rows  # noqa: E402


class TestDetectWall:
    def test_mia_style_ladder(self):
        # The observed 2026-07-06 signature: 2,500/level every 3¢.
        levels = [[89, 2500], [86, 2500], [83, 2500], [80, 2500], [77, 2500]]
        w = detect_wall(levels)
        assert w is not None
        assert w["total"] == 12500
        assert w["max_level"] == 2500
        assert w["ladder_levels"] == 5
        assert w["band"] == [77, 89]

    def test_single_big_level(self):
        w = detect_wall([[42, 1500], [40, 12]])
        assert w is not None and w["max_level"] == 1500

    def test_three_mid_levels_is_ladder_wall(self):
        assert detect_wall([[50, 600], [48, 700], [46, 550]]) is not None

    def test_retail_book_is_not_a_wall(self):
        # The 2026-07-02 NY dead-bracket prey: 432 contracts of scattered bids.
        assert detect_wall([[42, 45], [38, 5], [26, 297], [22, 85]]) is None

    def test_two_mid_levels_not_enough(self):
        assert detect_wall([[50, 600], [48, 700]]) is None

    def test_empty_and_none(self):
        assert detect_wall([]) is None
        assert detect_wall(None) is None


class TestScanRows:
    def _row(self, ts, ticker, yes=None, no=None):
        return {"ts": ts, "ticker": ticker, "series": "KXHIGHMIA",
                "target_date": "2026-07-06", "yes_levels": yes, "no_levels": no,
                "yes_bid": 89, "yes_ask": 92, "vol24": 1000}

    def test_first_seen_is_earliest_wall_snapshot(self):
        rows = [
            self._row("T1", "TK", yes=[[10, 5]]),                # no wall yet
            self._row("T2", "TK", yes=[[89, 2500], [86, 2500], [83, 2500]]),
            self._row("T3", "TK", yes=[[89, 2500], [86, 2500], [83, 2500]]),
        ]
        out = scan_rows(rows)
        assert out["TK"]["first_seen_yes"] == "T2"
        assert out["TK"]["ts"] == "T3"

    def test_wall_gone_by_latest_snapshot_drops_ticker(self):
        rows = [
            self._row("T1", "TK", yes=[[89, 2500], [86, 2500], [83, 2500]]),
            self._row("T2", "TK", yes=[[10, 5]]),                 # wall pulled
        ]
        assert scan_rows(rows) == {}

    def test_sides_tracked_independently(self):
        rows = [self._row("T1", "TK", yes=[[89, 2500], [86, 2500], [83, 2500]],
                          no=[[4, 20]])]
        out = scan_rows(rows)
        assert out["TK"]["yes_wall"] is not None
        assert out["TK"]["no_wall"] is None
        assert out["TK"]["first_seen_no"] is None


class TestWallKind:
    def test_penny_farm(self):
        from core.walls import detect_wall
        w = detect_wall([[1, 173000], [2, 4000]])
        assert w["kind"] == "penny_farm"

    def test_defense(self):
        from core.walls import detect_wall
        w = detect_wall([[89, 2500], [86, 2500], [83, 2500]])
        assert w["kind"] == "defense"

    def test_mid(self):
        from core.walls import detect_wall
        w = detect_wall([[30, 1500], [28, 40]])
        assert w["kind"] == "mid"
