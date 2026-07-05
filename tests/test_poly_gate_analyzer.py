"""Tests for poly_gate_analyzer pure helpers (no network)."""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backtest"))
import poly_gate_analyzer as pga


class TestBracketForRunmax:
    def test_contains(self):
        brackets = {"t_low": (-float("inf"), 89.0), "t_mid": (90.0, 91.0),
                    "t_hi": (92.0, float("inf"))}
        assert pga.bracket_for_runmax(brackets, 90.5) == "t_mid"
        assert pga.bracket_for_runmax(brackets, 85.0) == "t_low"
        assert pga.bracket_for_runmax(brackets, 99.0) == "t_hi"

    def test_gap_returns_none(self):
        assert pga.bracket_for_runmax({"a": (90.0, 91.0)}, 100.0) is None


class TestNearestSnapshot:
    def _snaps(self):
        return {
            "2026-07-04T20:00:00+00:00": {"tokA": {"yes_ask": 50}},
            "2026-07-04T21:00:00+00:00": {"tokA": {"yes_ask": 60}},
        }

    def test_picks_closest(self):
        target = datetime(2026, 7, 4, 20, 50, tzinfo=timezone.utc)
        snap = pga.nearest_snapshot(self._snaps(), target)
        assert snap["tokA"]["yes_ask"] == 60

    def test_beyond_tolerance_is_none(self):
        target = datetime(2026, 7, 4, 23, 0, tzinfo=timezone.utc)
        assert pga.nearest_snapshot(self._snaps(), target, tol_min=45) is None


class TestEconomics:
    def test_ev_winner_and_loser(self):
        assert pga.entry_ev_cents(ask=55, won=True, slippage=3) == 42.0
        assert pga.entry_ev_cents(ask=55, won=False, slippage=3) == -58.0

    def test_depth_dollars(self):
        assert pga.depth_dollars(200, 50) == 100.0


class TestGateVerdict:
    def test_pass(self):
        fills = [{"ev_cents": 15, "depth_dollars": 60, "won": True}] * 6
        v = pga.gate_verdict(fills)
        assert v["gate_pass"] is True
        assert v["n_fills"] == 6 and v["win_rate"] == 1.0

    def test_fails_on_thin_depth(self):
        fills = [{"ev_cents": 20, "depth_dollars": 10, "won": True}] * 6
        assert pga.gate_verdict(fills)["gate_pass"] is False

    def test_fails_on_few_fills(self):
        fills = [{"ev_cents": 20, "depth_dollars": 99, "won": True}] * 4
        assert pga.gate_verdict(fills)["gate_pass"] is False

    def test_fails_on_negative_ev(self):
        fills = [{"ev_cents": -5, "depth_dollars": 99, "won": False}] * 8
        assert pga.gate_verdict(fills)["gate_pass"] is False

    def test_empty(self):
        v = pga.gate_verdict([])
        assert v["n_fills"] == 0 and v["gate_pass"] is False


class TestLoadSnapshots:
    def test_groups_poly_only(self, tmp_path):
        f = tmp_path / "2026-07-04.jsonl"
        import json
        rows = [
            {"venue": "poly", "series": "POLY_NYC", "target_date": "2026-07-04",
             "ts": "2026-07-04T20:00:00+00:00", "token_id": "t1", "yes_ask": 50},
            {"venue": "kalshi", "series": "KXHIGHNY", "target_date": "2026-07-04",
             "ts": "2026-07-04T20:00:00+00:00", "ticker": "x", "yes_ask": 40},
        ]
        f.write_text("\n".join(json.dumps(r) for r in rows))
        out = pga.load_poly_snapshots(tmp_path)
        assert list(out) == [("NYC", "2026-07-04")]
        assert out[("NYC", "2026-07-04")]["2026-07-04T20:00:00+00:00"]["t1"]["yes_ask"] == 50
