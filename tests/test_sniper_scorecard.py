"""Tests for sniper_scorecard scoring/join (no network)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backtest"))
import sniper_scorecard as sc


class TestScoreBuyWinner:
    def _f(self, ask=48, depth=4, series="KXLOWTCHI", final=False):
        # mirrors the real 2026-07-04 KXLOWTCHI-26JUL04-B70.5 finding
        return {"ticker": "KXLOWTCHI-26JUL04-B70.5", "kind": "buy_winner",
                "ask": ask, "ask_depth": depth, "series": series,
                "is_final": final, "awips": "MDW"}

    def test_win_realizes_100_minus_ask_minus_fee(self):
        s = sc.score_finding(self._f(ask=48), "yes")
        assert s["won"] is True
        # 100 - 48 - taker_fee(48)
        assert s["per_contract_cents"] == 100 - 48 - sc.kalshi_taker_fee_cents(48)
        assert s["size"] == 4
        assert s["ladder"] == "low"

    def test_loss_is_negative_ask_plus_fee(self):
        s = sc.score_finding(self._f(ask=48), "no")
        assert s["won"] is False
        assert s["per_contract_cents"] == 0 - 48 - sc.kalshi_taker_fee_cents(48)

    def test_pending_when_unsettled(self):
        assert sc.score_finding(self._f(), None) is None
        assert sc.score_finding(self._f(), "") is None


class TestScoreSellDead:
    def _f(self, net=10828, contracts=437):
        return {"ticker": "KXHIGHNY-26JUL02-T99", "kind": "sell_dead",
                "net_cents": net, "contracts": contracts, "series": "KXHIGHNY",
                "is_final": False, "awips": "NYC"}

    def test_win_books_net(self):
        s = sc.score_finding(self._f(net=10828, contracts=437), "no")
        assert s["won"] is True
        assert s["realized_dollars"] == round(10828 / 100 * 1, 2)  # per*size/100 == net/100
        assert s["ladder"] == "high"

    def test_dead_bracket_that_actually_wins_is_a_big_loss(self):
        # the KAUS/$348 settlement-source misfire class: sold 387 for net,
        # they settle YES -> pay 100 each
        s = sc.score_finding(self._f(net=34889, contracts=387), "yes")
        assert s["won"] is False
        # -(387*100 - 34889) cents = -(38700-34889) = -3811c = -$38.11
        assert s["realized_dollars"] == round(-(387 * 100 - 34889) / 100, 2)


class TestAggregateAndSplit:
    def _scored(self):
        return [
            {"won": True, "per_contract_cents": 44, "realized_dollars": 1.76,
             "is_final": False, "kind": "buy_winner", "ladder": "low", "awips": "MDW"},
            {"won": False, "per_contract_cents": -50, "realized_dollars": -2.0,
             "is_final": True, "kind": "buy_winner", "ladder": "high", "awips": "NYC"},
        ]

    def test_aggregate(self):
        a = sc.aggregate(self._scored())
        assert a["n"] == 2 and a["hit_rate"] == 0.5
        assert a["mean_per_contract_cents"] == -3.0
        assert a["total_dollars"] == -0.24

    def test_empty_aggregate(self):
        assert sc.aggregate([])["n"] == 0

    def test_split_by_certainty(self):
        split = sc.split_by(self._scored(), lambda s: "final" if s["is_final"] else "floor")
        assert set(split) == {"final", "floor"}
        assert split["floor"]["n"] == 1


class TestBuildAndLoad:
    def test_build_buckets_pending(self):
        findings = [
            {"ticker": "A", "kind": "buy_winner", "ask": 48, "ask_depth": 4,
             "series": "KXLOWTCHI", "is_final": False, "awips": "MDW"},
            {"ticker": "B", "kind": "buy_winner", "ask": 20, "ask_depth": 1,
             "series": "KXHIGHNY", "is_final": True, "awips": "NYC"},
        ]
        result = sc.build(findings, {"A": "yes", "B": None})
        assert result["overall"]["n"] == 1
        assert result["pending"] == 1

    def test_load_findings_flattens(self, tmp_path):
        import json
        d = tmp_path / "cli_sniper"
        d.mkdir()
        (d / "2026-07-05.jsonl").write_text(json.dumps({
            "ts": "2026-07-05T01:06:00+00:00", "awips": "MDW",
            "summary_date": "2026-07-04", "is_final": False,
            "findings": [{"ticker": "T1", "kind": "buy_winner", "ask": 48}],
        }) + "\n")
        out = sc.load_findings(d)
        assert len(out) == 1
        assert out[0]["awips"] == "MDW" and out[0]["is_final"] is False
        assert out[0]["ticker"] == "T1"
