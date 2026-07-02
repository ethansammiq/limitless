"""Tests for hybrid trade score engine."""

from types import SimpleNamespace

import pytest

from trade_score import (
    _compute_weights,
    _confidence_signal,
    _edge_signal,
    _entry_price_penalty,
    _liquidity_penalty,
    _urgency_signal,
    compute_trade_score,
    should_trade,
)


# ── Helper ──────────────────────────────────────────────────────────────────


def _make_opp(
    confidence_score=85,
    edge=0.15,
    volume=2000,
    yes_bid=20,
    yes_ask=25,
    kde_prob=0.35,
):
    """Create a minimal Opportunity-like object for testing."""
    return SimpleNamespace(
        confidence_score=confidence_score,
        edge_after_fees=edge,
        volume=volume,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        kde_prob=kde_prob,
    )


# ── Weight Invariant Tests ──────────────────────────────────────────────────


class TestWeights:
    @pytest.mark.parametrize("hours", [0, 1, 4, 8, 12, 16, 20, 24])
    def test_weights_sum_to_one(self, hours):
        """w1 + w2 + w3 must equal 1.0 at all hours_to_settlement."""
        w1, w2, w3 = _compute_weights(hours)
        assert abs(w1 + w2 + w3 - 1.0) < 1e-10

    def test_confidence_dominates_early(self):
        """Far from settlement: confidence weight > edge and urgency."""
        w1, w2, w3 = _compute_weights(20)  # 20h remaining
        assert w1 > w2
        assert w1 > w3

    def test_urgency_dominates_late(self):
        """Near settlement: urgency weight > confidence."""
        w1, w2, w3 = _compute_weights(1)  # 1h remaining
        assert w3 > w1

    def test_weights_monotonic(self):
        """As settlement approaches: w_conf decreases, w_urgency increases."""
        w1_far, _, w3_far = _compute_weights(20)
        w1_near, _, w3_near = _compute_weights(2)
        assert w1_near < w1_far  # confidence decreases
        assert w3_near > w3_far  # urgency increases


# ── Confidence Signal Tests ─────────────────────────────────────────────────


class TestConfidenceSignal:
    def test_floor_gives_zero(self):
        assert _confidence_signal(70) == 0.0

    def test_max_gives_one(self):
        assert _confidence_signal(100) == 1.0

    def test_mid_gives_half(self):
        assert abs(_confidence_signal(85) - 0.5) < 1e-10

    def test_below_floor_clamped(self):
        assert _confidence_signal(50) == 0.0

    def test_above_max_clamped(self):
        assert _confidence_signal(110) == 1.0

    def test_monotonic(self):
        """Higher confidence always produces higher signal."""
        vals = [_confidence_signal(c) for c in range(70, 101)]
        for i in range(1, len(vals)):
            assert vals[i] >= vals[i - 1]


# ── Edge Signal Tests ───────────────────────────────────────────────────────


class TestEdgeSignal:
    def test_zero_edge(self):
        assert _edge_signal(0) == 0.0

    def test_negative_edge(self):
        assert _edge_signal(-5) == 0.0

    def test_half_saturation(self):
        """At 15¢ the signal should be near 0.46."""
        sig = _edge_signal(15)
        assert 0.44 < sig < 0.48

    def test_30c(self):
        """At 30¢ the signal should be near 0.76."""
        sig = _edge_signal(30)
        assert 0.74 < sig < 0.78

    def test_50c(self):
        """At 50¢ the signal should be > 0.93 (soft cap, not hard clip)."""
        sig = _edge_signal(50)
        assert sig > 0.93

    def test_50c_different_from_30c(self):
        """Fix #2: 50¢ must be distinguishably better than 30¢."""
        assert _edge_signal(50) > _edge_signal(30)

    def test_monotonic(self):
        """Higher edge always produces higher signal."""
        vals = [_edge_signal(c) for c in range(0, 61)]
        for i in range(1, len(vals)):
            assert vals[i] >= vals[i - 1]


# ── Urgency Signal Tests ───────────────────────────────────────────────────


class TestUrgencySignal:
    def test_near_settlement_high(self):
        """1h to settlement → urgency near 1.0."""
        assert _urgency_signal(1) > 0.95

    def test_far_from_settlement_low(self):
        """20h to settlement → urgency near 0."""
        assert _urgency_signal(20) < 0.01

    def test_mid_point(self):
        """8h to settlement → urgency ~0.5."""
        sig = _urgency_signal(8)
        assert 0.45 < sig < 0.55

    def test_monotonic_decreasing(self):
        """More hours remaining → lower urgency."""
        for h in range(1, 24):
            assert _urgency_signal(h) > _urgency_signal(h + 1)


# ── Liquidity Penalty Tests ────────────────────────────────────────────────


class TestLiquidityPenalty:
    def test_no_penalty_high_volume(self):
        assert _liquidity_penalty(5000, 2) == 0.0

    def test_heavy_penalty_low_volume(self):
        assert _liquidity_penalty(200, 2) == 0.15

    def test_light_penalty_mid_volume(self):
        assert _liquidity_penalty(800, 2) == 0.08

    def test_spread_penalty(self):
        assert _liquidity_penalty(5000, 6) == 0.05

    def test_penalties_stack(self):
        """Low volume + wide spread = both penalties."""
        pen = _liquidity_penalty(200, 6)
        assert pen == 0.20

    def test_moderate_spread_small_penalty(self):
        """Spread 4 → small penalty."""
        assert _liquidity_penalty(5000, 4) == 0.02


# ── Hard Floor Tests ────────────────────────────────────────────────────────


class TestHardFloors:
    def test_confidence_below_floor(self):
        """conf=65 → tradeable=False even if other signals are great."""
        opp = _make_opp(confidence_score=65, edge=0.40, volume=5000, kde_prob=0.50)
        ts = compute_trade_score(opp, hours_to_settlement=2.0)
        assert not ts.tradeable
        assert any("confidence" in f for f in ts.floor_failures)

    def test_edge_below_floor(self):
        """edge=8¢ → tradeable=False."""
        opp = _make_opp(confidence_score=90, edge=0.08, volume=5000, kde_prob=0.50)
        ts = compute_trade_score(opp, hours_to_settlement=2.0)
        assert not ts.tradeable
        assert any("edge" in f for f in ts.floor_failures)

    def test_kde_below_floor(self):
        """kde=15% → tradeable=False."""
        opp = _make_opp(confidence_score=90, edge=0.30, volume=5000, kde_prob=0.15)
        ts = compute_trade_score(opp, hours_to_settlement=2.0)
        assert not ts.tradeable
        assert any("kde_prob" in f for f in ts.floor_failures)

    def test_all_floors_pass(self):
        """All above floors + high score → tradeable=True."""
        opp = _make_opp(confidence_score=90, edge=0.30, volume=5000, kde_prob=0.50)
        ts = compute_trade_score(opp, hours_to_settlement=2.0)
        assert ts.tradeable
        assert len(ts.floor_failures) == 0


# ── Real Scenario Tests ────────────────────────────────────────────────────


class TestRealScenarios:
    def test_lax_86_conf_19c_edge_4h(self):
        """LAX today: conf=86, edge=19¢, 4h out → should be tradeable."""
        opp = _make_opp(
            confidence_score=86, edge=0.19, volume=2000,
            yes_bid=39, yes_ask=40, kde_prob=0.59,
        )
        ts = compute_trade_score(opp, hours_to_settlement=4.0)
        assert ts.score > 0.55
        assert ts.tradeable

    def test_lax_86_conf_19c_edge_14h(self):
        """Same setup 14h out → score is lower but may still trade."""
        opp = _make_opp(
            confidence_score=86, edge=0.19, volume=2000,
            yes_bid=39, yes_ask=40, kde_prob=0.59,
        )
        ts = compute_trade_score(opp, hours_to_settlement=14.0)
        # At 14h, urgency is very low — score is lower
        assert ts.score < compute_trade_score(opp, 4.0).score

    def test_high_conf_low_edge(self):
        """conf=95, edge=5¢ → blocked by edge floor."""
        opp = _make_opp(confidence_score=95, edge=0.05, volume=5000, kde_prob=0.50)
        ts = compute_trade_score(opp, hours_to_settlement=4.0)
        assert not ts.tradeable

    def test_low_conf_huge_edge(self):
        """conf=72, edge=40¢ near settlement → might trade."""
        opp = _make_opp(
            confidence_score=72, edge=0.40, volume=3000,
            yes_bid=10, yes_ask=12, kde_prob=0.50,
        )
        ts = compute_trade_score(opp, hours_to_settlement=2.0)
        # conf=72: signal = (72-70)/30 = 0.067
        # edge=40¢: signal ≈ 0.87
        # urgency at 2h: ≈ 0.95
        # Weights near settlement: conf≈0.25, edge≈0.35, urgency≈0.40
        # Score ≈ 0.25*0.067 + 0.35*0.87 + 0.40*0.95 = 0.017 + 0.305 + 0.380 = 0.70
        assert ts.score > 0.55
        assert ts.tradeable

    def test_weak_setup_blocked(self):
        """conf=72, edge=12¢, 14h out → should NOT trade."""
        opp = _make_opp(
            confidence_score=72, edge=0.12, volume=1500,
            yes_bid=15, yes_ask=18, kde_prob=0.30,
        )
        ts = compute_trade_score(opp, hours_to_settlement=14.0)
        assert ts.score < 0.55
        assert not ts.tradeable

    def test_illiquid_bracket_penalized(self):
        """High conf/edge but volume=200 → penalty lowers score."""
        opp_liquid = _make_opp(confidence_score=88, edge=0.20, volume=3000, kde_prob=0.40)
        opp_illiquid = _make_opp(confidence_score=88, edge=0.20, volume=200, kde_prob=0.40)
        ts_liquid = compute_trade_score(opp_liquid, hours_to_settlement=4.0)
        ts_illiquid = compute_trade_score(opp_illiquid, hours_to_settlement=4.0)
        assert ts_illiquid.score < ts_liquid.score
        assert ts_illiquid.liquidity_penalty > 0


# ── Feature Flag Tests ──────────────────────────────────────────────────────


class TestFeatureFlag:
    def test_flag_off_uses_old_gate(self, monkeypatch):
        """TRADE_SCORE_ENABLED=False → should_trade uses conf >= 90."""
        monkeypatch.setattr("trade_score.TRADE_SCORE_ENABLED", False)
        opp_91 = _make_opp(confidence_score=91)
        opp_89 = _make_opp(confidence_score=89)
        assert should_trade(opp_91, 14.0) is True
        assert should_trade(opp_89, 14.0) is False

    def test_flag_on_uses_trade_score(self, monkeypatch):
        """TRADE_SCORE_ENABLED=True → should_trade uses hybrid score."""
        monkeypatch.setattr("trade_score.TRADE_SCORE_ENABLED", True)
        # conf=86, good edge, near settlement → should trade
        opp = _make_opp(
            confidence_score=86, edge=0.25, volume=3000,
            yes_bid=20, yes_ask=22, kde_prob=0.45,
        )
        assert should_trade(opp, 3.0) is True


# ── Entry Price Penalty Tests ──────────────────────────────────────────────


class TestEntryPricePenalty:
    def test_no_penalty_below_threshold(self):
        """Entries at or below 20c get zero penalty."""
        assert _entry_price_penalty(5) == 0.0
        assert _entry_price_penalty(15) == 0.0
        assert _entry_price_penalty(20) == 0.0

    def test_penalty_scales_above_threshold(self):
        """Penalty grows linearly above 20c at 0.004/cent."""
        # 26c: 6 * 0.004 = 0.024
        assert abs(_entry_price_penalty(26) - 0.024) < 1e-10
        # 30c: 10 * 0.004 = 0.04
        assert abs(_entry_price_penalty(30) - 0.04) < 1e-10

    def test_expensive_entry_heavy_penalty(self):
        """50c: 30 * 0.004 = 0.12 — significant discount."""
        assert abs(_entry_price_penalty(50) - 0.12) < 1e-10

    def test_lax_feb15_scenario(self):
        """Regression: LAX Feb 15 entry at 26c should get 0.024 penalty."""
        pen = _entry_price_penalty(26)
        assert abs(pen - 0.024) < 1e-10

    def test_cheap_entry_preferred(self):
        """Cheap entries (5c) score higher than expensive entries (40c)."""
        opp_cheap = _make_opp(confidence_score=86, edge=0.19, yes_bid=5, yes_ask=8, kde_prob=0.45)
        opp_expensive = _make_opp(confidence_score=86, edge=0.19, yes_bid=40, yes_ask=43, kde_prob=0.45)
        ts_cheap = compute_trade_score(opp_cheap, 4.0)
        ts_expensive = compute_trade_score(opp_expensive, 4.0)
        assert ts_cheap.score > ts_expensive.score
        assert ts_cheap.entry_price_penalty == 0.0
        assert ts_expensive.entry_price_penalty > 0.0

    def test_entry_penalty_in_trade_score(self):
        """Entry price penalty is correctly included in TradeScore output."""
        opp = _make_opp(yes_bid=30, yes_ask=33)
        ts = compute_trade_score(opp, 8.0)
        expected_pen = (30 - 20) * 0.004  # 0.04
        assert abs(ts.entry_price_penalty - expected_pen) < 1e-10
        assert any("entry_price_penalty" in r for r in ts.reasons)


# ── TradeScore Dataclass Tests ──────────────────────────────────────────────


class TestTradeScoreDataclass:
    def test_reasons_populated(self):
        opp = _make_opp()
        ts = compute_trade_score(opp, 8.0)
        assert len(ts.reasons) >= 5  # conf, edge, urgency, liquidity, entry_price, verdict

    def test_score_non_negative(self):
        """Score should never be negative."""
        opp = _make_opp(confidence_score=70, edge=0.10, volume=100, kde_prob=0.20)
        ts = compute_trade_score(opp, 20.0)
        assert ts.score >= 0.0

    def test_custom_threshold(self):
        """Custom threshold should override config value."""
        opp = _make_opp(confidence_score=90, edge=0.30, volume=5000, kde_prob=0.50)
        ts_low = compute_trade_score(opp, 4.0, threshold=0.30)
        ts_high = compute_trade_score(opp, 4.0, threshold=0.99)
        assert ts_low.tradeable is True
        assert ts_high.tradeable is False
        assert ts_low.score == ts_high.score  # Same score, different threshold
