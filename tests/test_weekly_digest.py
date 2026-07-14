"""Tests for weekly_digest aggregation (no network, no Discord)."""
from datetime import datetime, timedelta, timezone

import weekly_digest as wd

NOW = datetime.now(timezone.utc)
RECENT = (NOW - timedelta(days=1)).isoformat()
OLD = (NOW - timedelta(days=30)).isoformat()
SINCE = NOW - timedelta(days=7)


class TestLiveSummary:
    def test_fees_notional_delta(self):
        fills = [
            {"created_time": RECENT, "fee_cost": "0.10", "count_fp": "20.00", "yes_price_dollars": "0.0800"},
            {"created_time": OLD, "fee_cost": "9.99", "count_fp": "1.00", "yes_price_dollars": "0.5000"},
        ]
        balances = [
            {"ts": RECENT, "balance": 100.0},
            {"ts": (NOW - timedelta(hours=1)).isoformat(), "balance": 97.8},
        ]
        s = wd.live_summary(fills, balances, SINCE)
        assert s["fills"] == 1
        assert s["fees"] == 0.10
        assert s["notional"] == 1.60
        assert s["balance"] == 97.8
        assert s["balance_delta"] == -2.2

    def test_empty(self):
        s = wd.live_summary([], [], SINCE)
        assert s["fills"] == 0 and s["balance"] is None


class TestDigestBuilds:
    def test_returns_title_and_body(self):
        title, body = wd.build_digest(7)
        assert "digest" in title.lower()
        assert "Paper" not in body      # KDE paper section retired 2026-07-06
        assert "Live account" in body
        assert "Dead-bracket base rate" in body


class TestMetarScorecardLine:
    def test_missing_verdict(self, monkeypatch, tmp_path):
        import weekly_digest as wd
        monkeypatch.setattr(wd, "METAR_VERDICT", tmp_path / "nope.json")
        assert "no METAR verdict yet" in wd.metar_scorecard_line()

    def test_headline_with_ladder_split_and_ci(self, monkeypatch, tmp_path):
        import json
        import weekly_digest as wd
        verdict = {"overall": {"n": 96, "hit_rate": 0.052,
                               "mean_per_contract_cents": 0.9,
                               "total_dollars": -5555.87,
                               "ci80": {"lo": -2.0, "hi": 4.0, "clusters": 41}},
                   "pending": 12,
                   "by_ladder": {"high": {"n": 51, "mean_per_contract_cents": 2.1},
                                 "low": {"n": 45, "mean_per_contract_cents": -0.4}}}
        p = tmp_path / "metar_scorecard_verdict.json"
        p.write_text(json.dumps(verdict))
        monkeypatch.setattr(wd, "METAR_VERDICT", p)
        line = wd.metar_scorecard_line()
        assert "96 settled" in line and "hit 5%" in line
        assert "high +2¢×51" in line and "low -0¢×45" in line
        assert "CI80 [-2,+4]¢ (41 stn-nights)" in line

    def test_digest_includes_metar_section(self, monkeypatch, tmp_path):
        import weekly_digest as wd
        monkeypatch.setattr(wd, "METAR_VERDICT", tmp_path / "nope.json")
        _, body = wd.build_digest(7)
        assert "METAR sniper scorecard" in body
