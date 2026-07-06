"""Tests for the public-stats exporter — the sanitization contract above all."""
import json
from datetime import datetime, timezone

import pytest

from scripts.export_public_stats import (
    ALLOWED_KEYS,
    assert_sanitized,
    build_best_trade,
    build_equity_curve,
    build_scorecard,
)


def _fill(ticker, action, price_c, count, created="2026-07-04T21:01:42Z", **extra):
    return {"ticker": ticker, "action": action,
            "yes_price_dollars": f"{price_c / 100:.4f}", "count_fp": f"{count}.00",
            "created_time": created, "order_id": "SECRET-UUID", **extra}


class TestBestTrade:
    def test_chi_snipe_wins(self):
        fills = [
            _fill("KXHIGHCHI-26JUL04-B85.5", "buy", 8, 16),
            _fill("KXHIGHCHI-26JUL04-B85.5", "buy", 7, 4),
            _fill("KXHIGHCHI-26JUL04-B85.5", "sell", 99, 20),
            _fill("KXHIGHMIA-26JUL04-B94.5", "buy", 1, 50),  # no sell leg
        ]
        best = build_best_trade(fills)
        assert best["ticker"] == "KXHIGHCHI-26JUL04-B85.5"
        assert best["avg_buy_cents"] == 7.8
        assert best["avg_sell_cents"] == 99.0
        assert best["multiple"] == 12.7
        assert best["contracts"] == 20
        assert "order_id" not in best          # ids never pass through

    def test_no_round_trips_is_none(self):
        assert build_best_trade([_fill("X", "buy", 10, 5)]) is None
        assert build_best_trade([]) is None


class TestEquityCurve:
    def test_appends_fresher_account_balance(self):
        rows = [{"ts": "2026-07-04T21:16:38+00:00", "balance": 97.8}]
        acct = {"balance": 117.59, "updated": "2026-07-05T17:00:00+00:00"}
        curve = build_equity_curve(rows, acct)
        assert [p["usd"] for p in curve] == [97.8, 117.59]

    def test_no_duplicate_terminal_point(self):
        rows = [{"ts": "t1", "balance": 117.59}]
        acct = {"balance": 117.59, "updated": "t2"}
        assert len(build_equity_curve(rows, acct)) == 1


class TestScorecard:
    def test_shapes_verdict(self):
        v = {"overall": {"n": 1, "hit_rate": 0.0, "mean_per_contract_cents": -50.0},
             "pending": 4}
        s = build_scorecard(v, "2026-07-05T21:45:00+00:00")
        assert s == {"settled": 1, "hit_rate_pct": 0.0,
                     "mean_cents_per_contract": -50.0, "pending": 4,
                     "as_of": "2026-07-05T21:45:00+00:00"}

    def test_empty_is_none(self):
        assert build_scorecard({}, None) is None


class TestSanitizationContract:
    def _minimal(self):
        return {"generated_at": "t", "bankroll": {"start_usd": 100.0,
                "current_usd": 117.59, "return_pct": 17.6}}

    def test_clean_payload_passes(self):
        assert_sanitized(self._minimal())

    def test_rogue_key_raises(self):
        bad = self._minimal()
        bad["order_id"] = "abc"
        with pytest.raises(ValueError, match="non-whitelisted"):
            assert_sanitized(bad)

    def test_nested_rogue_key_raises(self):
        bad = self._minimal()
        bad["bankroll"]["api_key_id"] = "abc"
        with pytest.raises(ValueError, match="non-whitelisted"):
            assert_sanitized(bad)

    def test_key_material_pattern_raises(self):
        bad = self._minimal()
        bad["generated_at"] = "-----BEGIN RSA PRIVATE KEY-----"
        with pytest.raises(ValueError, match="forbidden pattern"):
            assert_sanitized(bad)

    def test_allowed_keys_is_deliberately_small(self):
        # Publishing MORE data is a decision, not an accident.
        assert len(ALLOWED_KEYS) < 40
