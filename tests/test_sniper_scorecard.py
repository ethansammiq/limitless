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

    def test_win_books_net_at_executable_size(self):
        s = sc.score_finding(self._f(net=10828, contracts=437), "no")
        assert s["won"] is True
        # collateral 100 - 10828/437 = 75.22¢/contract → 66 fit under $50;
        # realized = per(24.78¢) × 66 — the wall's other 371 were never takeable
        assert s["size"] == 66
        assert s["realized_dollars"] == round((10828 / 437) * 66 / 100, 2)
        assert s["ladder"] == "high"

    def test_dead_bracket_that_actually_wins_is_a_big_loss(self):
        # the KAUS/$348 settlement-source misfire class: sold 387 for net,
        # they settle YES -> pay 100 each. Collateral is only ~9.8¢/contract
        # (rich bids), so all 387 fit under the clamp — loss stays full-size.
        s = sc.score_finding(self._f(net=34889, contracts=387), "yes")
        assert s["won"] is False
        assert s["size"] == 387
        # -(387*100 - 34889) cents = -(38700-34889) = -3811c = -$38.11
        assert s["realized_dollars"] == round(-(387 * 100 - 34889) / 100, 2)


class TestNotionalClamp:
    def test_wall_depth_buy_caps_at_the_clamp(self):
        # the 2026-07-12 AUS class: 1¢ ask × 154,899 wall — only $50 worth
        # (5000 contracts) was ever executable
        f = {"ticker": "T", "kind": "buy_winner", "ask": 1, "ask_depth": 154899,
             "series": "KXHIGHAUS", "is_final": False, "awips": "AUS"}
        s = sc.score_finding(f, "no")
        assert s["size"] == 5000
        assert s["realized_dollars"] == round(
            (0 - 1 - sc.kalshi_taker_fee_cents(1)) * 5000 / 100, 2)

    def test_small_findings_unaffected(self):
        assert sc.clamp_size(4, 48) == 4
        assert sc.clamp_size(23, 18) == 23

    def test_env_override_matches_take_py(self, monkeypatch):
        monkeypatch.setenv("TAKE_MAX_NOTIONAL", "100")
        assert sc.clamp_size(154899, 1) == 10000
        monkeypatch.delenv("TAKE_MAX_NOTIONAL")
        assert sc.clamp_size(154899, 1) == 5000

    def test_zero_collateral_passes_through(self):
        assert sc.clamp_size(437, 0) == 437


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


class TestClusterBootstrap:
    def _night(self, awips, date, values):
        return [{"won": v > 0, "per_contract_cents": v, "realized_dollars": v / 100,
                 "is_final": False, "kind": "buy_winner", "ladder": "high",
                 "awips": awips, "summary_date": date} for v in values]

    def test_single_cluster_has_no_between_night_variance(self):
        assert sc.cluster_bootstrap_ci(self._night("MDW", "2026-07-04",
                                                   [10, 20, 30])) is None
        assert sc.cluster_bootstrap_ci([]) is None

    def test_two_degenerate_clusters_span_the_cluster_means(self):
        # night A is all +50, night B is all -50: a resample draws whole
        # nights, so the only possible means are -50, 0, +50 — the 80% CI
        # must reach both extremes (each occurs with p=0.25)
        scored = (self._night("MDW", "2026-07-04", [50, 50]) +
                  self._night("NYC", "2026-07-05", [-50, -50]))
        ci = sc.cluster_bootstrap_ci(scored)
        assert ci["lo"] == -50 and ci["hi"] == 50
        assert ci["clusters"] == 2 and ci["level"] == 0.8

    def test_clustering_widens_vs_iid_on_correlated_nights(self):
        # identical values: grouped into 2 correlated nights vs 4 independent
        # singleton nights — the clustered interval must be wider
        two = sc.cluster_bootstrap_ci(
            self._night("MDW", "2026-07-04", [50, 50]) +
            self._night("NYC", "2026-07-05", [-50, -50]))
        four = sc.cluster_bootstrap_ci(
            self._night("MDW", "2026-07-04", [50]) +
            self._night("MDW", "2026-07-05", [50]) +
            self._night("NYC", "2026-07-04", [-50]) +
            self._night("NYC", "2026-07-05", [-50]))
        assert two["hi"] - two["lo"] > four["hi"] - four["lo"]

    def test_fixed_seed_is_reproducible(self):
        scored = (self._night("MDW", "2026-07-04", [44, -10]) +
                  self._night("NYC", "2026-07-05", [-50, 12, 3]))
        assert sc.cluster_bootstrap_ci(scored) == sc.cluster_bootstrap_ci(scored)

    def test_aggregate_carries_the_ci(self):
        scored = (self._night("MDW", "2026-07-04", [50]) +
                  self._night("NYC", "2026-07-05", [-50]))
        a = sc.aggregate(scored)
        assert a["ci80"]["clusters"] == 2
        assert sc.aggregate([])["ci80"] is None


class TestFindingClass:
    def test_labels_kind_by_finality(self):
        assert sc.finding_class({"kind": "buy_winner", "is_final": True}) == "buy_winner/final"
        assert sc.finding_class({"kind": "buy_winner", "is_final": False}) == "buy_winner/floor"
        assert sc.finding_class({"kind": "sell_dead", "is_final": False}) == "sell_dead/floor"

    def test_build_reports_by_class(self):
        findings = [
            {"ticker": "A", "kind": "buy_winner", "ask": 48, "ask_depth": 4,
             "series": "KXLOWTCHI", "is_final": False, "awips": "MDW",
             "summary_date": "2026-07-04"},
            {"ticker": "B", "kind": "buy_winner", "ask": 20, "ask_depth": 1,
             "series": "KXHIGHNY", "is_final": True, "awips": "NYC",
             "summary_date": "2026-07-04"},
        ]
        result = sc.build(findings, {"A": "yes", "B": "no"})
        assert set(result["by_class"]) == {"buy_winner/floor", "buy_winner/final"}
        assert result["by_class"]["buy_winner/floor"]["n"] == 1

    def test_report_carries_ci_class_and_gate_readouts(self):
        findings = [
            {"ticker": "A", "kind": "buy_winner", "ask": 48, "ask_depth": 4,
             "series": "KXLOWTCHI", "is_final": True, "awips": "MDW",
             "summary_date": "2026-07-04"},
            {"ticker": "B", "kind": "buy_winner", "ask": 20, "ask_depth": 1,
             "series": "KXHIGHNY", "is_final": True, "awips": "NYC",
             "summary_date": "2026-07-05"},
        ]
        out = sc.format_report(sc.build(findings, {"A": "yes", "B": "no"}))
        assert "class buy_winner/final" in out
        assert "stn-nights" in out
        assert "pivot-gate readout" in out and "+2¢ threshold" in out
        assert "daemon-gate readout" in out


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


class TestBugEraExclusion:
    """The 2026-07-05 finality bug journaled 3 false 'certain winner'
    findings from same-day 07:31-local products. Scoring them would pollute
    the pivot-gate sample — load_findings must exclude them by recomputing
    finality from the calendar (real journal rows as fixtures)."""

    AUS_BUG_ROW = ('{"ts":"2026-07-05T20:30:02+00:00","awips":"AUS",'
                   '"stamp":"051231","summary_date":"2026-07-05",'
                   '"is_final":true,"max_f":80,"min_f":74,"findings":'
                   '[{"ticker":"KXHIGHAUS-26JUL05-T94","series":"KXHIGHAUS",'
                   '"kind":"buy_winner","ask":1,"ask_depth":155437}]}')
    NOLA_FLOOR_ROW = ('{"ts":"2026-07-05T22:02:01+00:00","awips":"MSY",'
                      '"stamp":"052150","summary_date":"2026-07-05",'
                      '"is_final":false,"max_f":93,"min_f":74,"findings":'
                      '[{"ticker":"KXLOWTNOLA-26JUL05-B74.5","series":"KXLOWTNOLA",'
                      '"kind":"buy_winner","ask":45,"ask_depth":9}]}')

    def test_bug_era_intraday_row_excluded(self, tmp_path):
        d = tmp_path / "j"
        d.mkdir()
        (d / "2026-07-05.jsonl").write_text(self.AUS_BUG_ROW + "\n")
        assert sc.load_findings(d) == []

    def test_legit_afternoon_floor_kept(self, tmp_path):
        d = tmp_path / "j"
        d.mkdir()
        (d / "2026-07-05.jsonl").write_text(self.NOLA_FLOOR_ROW + "\n")
        out = sc.load_findings(d)
        assert len(out) == 1
        assert out[0]["ticker"] == "KXLOWTNOLA-26JUL05-B74.5"
        assert out[0]["is_final"] is False   # calendar says floor

    def test_rows_without_stamp_trusted_as_journaled(self, tmp_path):
        d = tmp_path / "j"
        d.mkdir()
        (d / "x.jsonl").write_text(
            '{"ts":"2026-07-04T21:00:00+00:00","awips":"MDW",'
            '"summary_date":"2026-07-04","is_final":false,"findings":'
            '[{"ticker":"T1","series":"KXHIGHCHI","kind":"buy_winner","ask":10}]}\n')
        out = sc.load_findings(d)
        assert len(out) == 1 and out[0]["is_final"] is False
