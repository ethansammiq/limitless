#!/usr/bin/env python3
"""
Tests for edge_scanner_v2 core functions.

Run: python3 -m pytest tests/ -v
"""

import json
import math
import tempfile
from unittest.mock import patch

import numpy as np
import pytest

from edge_scanner_v2 import (
    EnsembleV2,
    ModelGroup,
    NWSData,
    kde_probability,
    silverman_bandwidth,
    weight_ensemble_members,
    build_member_weights,
    parse_bracket_range,
    kelly_fraction,
    taker_fee_cents,
    compute_confidence_score,
    shorten_bracket_title,
    is_tomorrow_ticker,
)


# ═══════════════════════════════════════════════════════════
#  KDE PROBABILITY
# ═══════════════════════════════════════════════════════════

class TestKDEProbability:
    """Test Gaussian KDE bracket probability computation."""

    def test_normal_distribution_center(self):
        """Members centered at 40°F → bracket 39-41 should have high probability."""
        members = list(np.random.normal(40, 1, 200))
        prob = kde_probability(members, 39, 41)
        assert 0.5 < prob < 0.9  # ~68% within 1 std

    def test_normal_distribution_tail(self):
        """Tail bracket should have low probability."""
        members = list(np.random.normal(40, 1, 200))
        prob = kde_probability(members, 45, 47)
        assert prob < 0.05

    def test_all_members_in_bracket(self):
        """All members in bracket → probability near 1.0."""
        members = [35.0 + i * 0.01 for i in range(100)]  # 35.00 to 35.99
        prob = kde_probability(members, 34, 37)
        assert prob > 0.95

    def test_no_members_in_bracket(self):
        """All members far from bracket → probability near 0."""
        members = [60.0 + i * 0.1 for i in range(50)]  # 60-65°F
        prob = kde_probability(members, 30, 35)
        assert prob < 0.01

    def test_empty_members(self):
        """Empty member list → 0 probability."""
        assert kde_probability([], 30, 35) == 0.0

    def test_single_member(self):
        """Single member → 0 (requires at least 2)."""
        assert kde_probability([40.0], 39, 41) == 0.0

    def test_two_identical_members(self):
        """Two identical members (std=0) → 1.0 if in bracket, 0.0 if not."""
        assert kde_probability([40.0, 40.0], 39, 41) == 1.0
        assert kde_probability([40.0, 40.0], 45, 47) == 0.0

    def test_probability_bounded_0_1(self):
        """Result should always be in [0, 1]."""
        members = list(np.random.normal(40, 2, 100))
        for low, high in [(30, 50), (39, 41), (0, 100), (45, 46)]:
            prob = kde_probability(members, low, high)
            assert 0.0 <= prob <= 1.0

    def test_wide_bracket_near_one(self):
        """Very wide bracket covering all members → near 1.0."""
        members = list(np.random.normal(40, 2, 100))
        prob = kde_probability(members, 20, 60)
        assert prob > 0.99

    def test_custom_bandwidth(self):
        """Custom bandwidth should be respected."""
        members = list(np.random.normal(40, 1, 100))
        prob_narrow = kde_probability(members, 39, 41, bandwidth=0.3)
        prob_wide = kde_probability(members, 39, 41, bandwidth=3.0)
        # Wider bandwidth → more smoothing → lower peak probability in center bracket
        assert prob_narrow > prob_wide

    def test_weighted_kde(self):
        """Higher-weighted members should shift probability toward their values."""
        # Two clusters: 40°F (high weight) and 50°F (low weight)
        members = [40.0] * 10 + [50.0] * 10
        # Equal weights → roughly equal probability in each bracket
        prob_40_equal = kde_probability(members, 39, 41, bandwidth=1.0)
        prob_50_equal = kde_probability(members, 49, 51, bandwidth=1.0)

        # Heavy weight on 40°F cluster → more probability there
        weights = [3.0] * 10 + [0.5] * 10
        prob_40_weighted = kde_probability(members, 39, 41, bandwidth=1.0, weights=weights)
        prob_50_weighted = kde_probability(members, 49, 51, bandwidth=1.0, weights=weights)

        assert prob_40_weighted > prob_40_equal
        assert prob_50_weighted < prob_50_equal


# ═══════════════════════════════════════════════════════════
#  SILVERMAN BANDWIDTH
# ═══════════════════════════════════════════════════════════

class TestSilvermanBandwidth:
    def test_known_std(self):
        """Members with known std → predictable bandwidth (uses sample std, ddof=1)."""
        members = list(np.random.normal(40, 2, 100))
        bw = silverman_bandwidth(members)
        # Silverman: 1.06 * std(ddof=1) * n^(-0.2) * bandwidth_factor
        from edge_scanner_v2 import _BANDWIDTH_FACTOR
        expected = 1.06 * np.std(members, ddof=1) * 100 ** (-0.2) * _BANDWIDTH_FACTOR
        assert abs(bw - expected) < 0.1

    def test_single_member(self):
        """< 2 members → default bandwidth of 1.0."""
        assert silverman_bandwidth([40.0]) == 1.0

    def test_zero_std(self):
        """All identical → min bandwidth of 0.3 (floor prevents under-smoothing)."""
        assert silverman_bandwidth([40.0, 40.0, 40.0]) == 0.3

    def test_positive_result(self):
        """Bandwidth should always be positive."""
        members = [30 + i for i in range(20)]
        bw = silverman_bandwidth(members)
        assert bw > 0


# ═══════════════════════════════════════════════════════════
#  WEIGHT ENSEMBLE MEMBERS
# ═══════════════════════════════════════════════════════════

class TestWeightEnsembleMembers:
    """weight_ensemble_members returns all raw members sorted (no resampling)."""

    def test_single_model_weight_1(self):
        """Weight 1.0 → same number of members (all raw)."""
        mg = ModelGroup(name="GFS", members=[30, 31, 32, 33, 34], weight=1.0)
        result = weight_ensemble_members([mg])
        assert len(result) == 5

    def test_high_weight_returns_all(self):
        """Weight 1.3 with 10 members → still 10 (no resampling)."""
        mg = ModelGroup(name="AIFS", members=list(range(10)), weight=1.3)
        result = weight_ensemble_members([mg])
        assert len(result) == 10  # All raw members, not oversampled

    def test_low_weight_returns_all(self):
        """Weight 0.5 with 10 members → still 10 (weighting is in KDE, not here)."""
        mg = ModelGroup(name="GEM", members=list(range(10)), weight=0.5)
        result = weight_ensemble_members([mg])
        assert len(result) == 10

    def test_empty_model(self):
        """Empty members → nothing contributed."""
        mg = ModelGroup(name="EMPTY", members=[], weight=1.0)
        result = weight_ensemble_members([mg])
        assert len(result) == 0

    def test_multi_model(self):
        """Multiple models → all raw members combined and sorted."""
        models = [
            ModelGroup(name="AIFS", members=[40.0] * 10, weight=1.3),
            ModelGroup(name="GFS", members=[41.0] * 10, weight=1.0),
            ModelGroup(name="GEM", members=[42.0] * 10, weight=0.8),
        ]
        result = weight_ensemble_members(models)
        assert len(result) == 30  # 10 + 10 + 10 (all raw)
        assert result == sorted(result)  # Should be sorted


class TestBuildMemberWeights:
    """build_member_weights returns parallel (members, weights) for weighted KDE."""

    def test_single_model(self):
        """Single model → weights all equal to model weight."""
        mg = ModelGroup(name="GFS", members=[30, 31, 32], weight=1.0)
        members, weights = build_member_weights([mg])
        assert len(members) == 3
        assert len(weights) == 3
        assert all(w == 1.0 for w in weights)

    def test_weights_match_model(self):
        """Each member's weight equals its model's weight."""
        models = [
            ModelGroup(name="AIFS", members=[40.0, 40.5], weight=1.3),
            ModelGroup(name="GFS", members=[41.0], weight=1.0),
        ]
        members, weights = build_member_weights(models)
        assert len(members) == 3
        # Sorted by value: 40.0, 40.5, 41.0
        assert weights[0] == 1.3  # AIFS member (40.0)
        assert weights[1] == 1.3  # AIFS member (40.5)
        assert weights[2] == 1.0  # GFS member (41.0)

    def test_empty_models(self):
        """Empty models → empty output."""
        mg = ModelGroup(name="EMPTY", members=[], weight=1.0)
        members, weights = build_member_weights([mg])
        assert members == []
        assert weights == []

    def test_sorted_output(self):
        """Output members are sorted by value."""
        models = [
            ModelGroup(name="GFS", members=[42.0, 38.0], weight=1.0),
            ModelGroup(name="AIFS", members=[40.0], weight=1.3),
        ]
        members, _ = build_member_weights(models)
        assert members == sorted(members)


# ═══════════════════════════════════════════════════════════
#  PARSE BRACKET RANGE
# ═══════════════════════════════════════════════════════════

class TestParseBracketRange:
    def test_range_bracket(self):
        """Normal range: '35° to 36°F' → (35, 37, 'range')."""
        low, high, btype = parse_bracket_range("35° to 36°F")
        assert low == 35.0
        assert high == 37.0  # +1 for inclusive
        assert btype == "range"

    def test_range_with_dash(self):
        """Dash format: '35-36°F'."""
        low, high, btype = parse_bracket_range("35-36°F")
        assert low == 35.0
        assert high == 37.0
        assert btype == "range"

    def test_decimal_range(self):
        """Decimal bracket: '35.5 to 36.5°F'."""
        low, high, btype = parse_bracket_range("35.5 to 36.5°F")
        assert low == 35.5
        assert high == 37.5

    def test_low_tail(self):
        """Low tail: '34°F or less' → (-999, 34, 'low_tail')."""
        low, high, btype = parse_bracket_range("34°F or less")
        assert low == -999
        assert high == 34.0
        assert btype == "low_tail"

    def test_below_variant(self):
        """'below 34°F'."""
        low, high, btype = parse_bracket_range("below 34°F")
        assert btype == "low_tail"
        assert high == 34.0

    def test_high_tail(self):
        """High tail: '40°F or more' → (40, 999, 'high_tail')."""
        low, high, btype = parse_bracket_range("40°F or more")
        assert low == 40.0
        assert high == 999
        assert btype == "high_tail"

    def test_above_variant(self):
        """'above 40°F'."""
        low, high, btype = parse_bracket_range("above 40°F")
        assert btype == "high_tail"
        assert low == 40.0

    def test_unknown_format(self):
        """Unrecognized format → (0, 0, 'unknown')."""
        low, high, btype = parse_bracket_range("some random text")
        assert btype == "unknown"


# ═══════════════════════════════════════════════════════════
#  KELLY FRACTION
# ═══════════════════════════════════════════════════════════

class TestKellyFraction:
    def test_positive_edge(self):
        """Model prob > market price → positive Kelly fraction."""
        f = kelly_fraction(0.50, 0.30)
        assert f > 0

    def test_negative_edge(self):
        """Model prob < market price → 0 (no bet)."""
        f = kelly_fraction(0.20, 0.50)
        assert f == 0.0

    def test_half_kelly(self):
        """Result should be half-Kelly (max 0.5 * full Kelly)."""
        # Full Kelly for prob=0.60, price=0.30: b=2.33, f=(2.33*0.6-0.4)/2.33=0.43
        f = kelly_fraction(0.60, 0.30)
        assert f > 0
        assert f < 0.5  # Half-Kelly caps at 50%

    def test_zero_prob(self):
        """Zero probability → 0."""
        assert kelly_fraction(0.0, 0.30) == 0.0

    def test_zero_price(self):
        """Zero price → 0."""
        assert kelly_fraction(0.50, 0.0) == 0.0

    def test_price_at_one(self):
        """Price at 1.0 → 0 (no edge possible)."""
        assert kelly_fraction(0.50, 1.0) == 0.0

    def test_certainty(self):
        """Prob=1.0 at cheap price → max half-Kelly."""
        f = kelly_fraction(1.0, 0.10)
        assert f == 0.5  # Half-Kelly of full-Kelly 1.0


# ═══════════════════════════════════════════════════════════
#  TAKER FEE
# ═══════════════════════════════════════════════════════════

class TestTakerFee:
    def test_midpoint(self):
        """50¢ → max fee."""
        fee = taker_fee_cents(50)
        assert fee == round(0.07 * 0.5 * 0.5 * 100, 2)  # 1.75¢

    def test_low_price(self):
        """5¢ → small fee."""
        fee = taker_fee_cents(5)
        assert fee < 0.5

    def test_high_price(self):
        """95¢ → small fee (symmetric with 5¢)."""
        fee = taker_fee_cents(95)
        assert abs(fee - taker_fee_cents(5)) < 0.01  # Symmetric


# ═══════════════════════════════════════════════════════════
#  CONFIDENCE SCORE
# ═══════════════════════════════════════════════════════════

class TestComputeConfidenceScore:
    def _make_ensemble(self, mean=40.0, std=0.8, n_members=194):
        """Helper: create a realistic ensemble with weighted members."""
        members = list(np.random.normal(mean, std, n_members))
        models = [
            ModelGroup(name="ecmwf_aifs025", members=members[:51], weight=1.3,
                      mean=float(np.mean(members[:51])), std=float(np.std(members[:51], ddof=1)) if len(members[:51]) > 1 else 0),
            ModelGroup(name="ecmwf_ifs025", members=members[51:102], weight=1.15,
                      mean=float(np.mean(members[51:102])), std=float(np.std(members[51:102], ddof=1))),
            ModelGroup(name="gfs_seamless", members=members[102:133], weight=1.0,
                      mean=float(np.mean(members[102:133])), std=float(np.std(members[102:133], ddof=1))),
            ModelGroup(name="icon_seamless", members=members[133:173], weight=0.95,
                      mean=float(np.mean(members[133:173])), std=float(np.std(members[133:173], ddof=1))),
            ModelGroup(name="gem_global", members=members[173:], weight=0.85,
                      mean=float(np.mean(members[173:])), std=float(np.std(members[173:], ddof=1))),
        ]
        wm, mw = build_member_weights(models)
        wm_arr = np.asarray(wm)
        w_arr = np.asarray(mw)
        w_norm = w_arr / w_arr.sum()
        weighted_mean = float(np.average(wm_arr, weights=w_norm))
        v1 = w_norm.sum()
        v2 = (w_norm ** 2).sum()
        denom = v1 * v1 - v2
        weighted_std = float(np.sqrt((w_norm * (wm_arr - weighted_mean) ** 2).sum() / denom)) if denom > 0 else float(np.std(wm_arr, ddof=1))
        return EnsembleV2(
            models=models,
            all_members=members,
            weighted_members=wm,
            member_weights=mw,
            total_count=n_members,
            mean=weighted_mean,
            median=float(np.median(wm_arr)),
            std=weighted_std,
            min_val=float(np.min(wm_arr)),
            max_val=float(np.max(wm_arr)),
            p10=float(np.percentile(wm_arr, 10)),
            p25=float(np.percentile(wm_arr, 25)),
            p50=float(np.percentile(wm_arr, 50)),
            p75=float(np.percentile(wm_arr, 75)),
            p90=float(np.percentile(wm_arr, 90)),
            kde_bandwidth=silverman_bandwidth(wm),
        )

    def _make_nws(self, forecast_high=40.0, trend="on_track"):
        return NWSData(
            forecast_high=forecast_high,
            physics_high=forecast_high,
            temp_trend=trend,
        )

    def test_high_confidence(self):
        """Tight ensemble + aligned NWS → high confidence."""
        ens = self._make_ensemble(mean=40.0, std=0.5)
        nws = self._make_nws(forecast_high=40.0)
        label, score, _ = compute_confidence_score(ens, nws)
        assert score >= 70

    def test_wide_ensemble_penalty(self):
        """Wide ensemble (high σ) → lower confidence."""
        ens_tight = self._make_ensemble(mean=40.0, std=0.5)
        ens_wide = self._make_ensemble(mean=40.0, std=3.0)
        nws = self._make_nws(forecast_high=40.0)

        _, score_tight, _ = compute_confidence_score(ens_tight, nws)
        _, score_wide, _ = compute_confidence_score(ens_wide, nws)
        assert score_tight > score_wide

    def test_nws_divergence_penalty(self):
        """NWS far from ensemble → lower confidence."""
        ens = self._make_ensemble(mean=40.0, std=0.8)
        nws_aligned = self._make_nws(forecast_high=40.0)
        nws_diverged = self._make_nws(forecast_high=46.0)

        _, score_aligned, _ = compute_confidence_score(ens, nws_aligned)
        _, score_diverged, _ = compute_confidence_score(ens, nws_diverged)
        assert score_aligned > score_diverged

    def test_returns_label_and_score(self):
        """Should return (label, score, details) tuple."""
        ens = self._make_ensemble()
        nws = self._make_nws()
        result = compute_confidence_score(ens, nws)
        assert len(result) == 3
        assert isinstance(result[0], str)
        assert isinstance(result[1], (int, float))
        assert isinstance(result[2], list)


# ═══════════════════════════════════════════════════════════
#  POSITION STORE
# ═══════════════════════════════════════════════════════════

class TestPositionStore:
    def test_load_empty(self, tmp_path):
        """Load from nonexistent file → empty list."""
        from position_store import load_positions, POSITIONS_FILE, LOCK_FILE
        import position_store

        orig_pos = position_store.POSITIONS_FILE
        orig_lock = position_store.LOCK_FILE
        position_store.POSITIONS_FILE = tmp_path / "positions.json"
        position_store.LOCK_FILE = tmp_path / ".positions.lock"

        try:
            result = load_positions()
            assert result == []
        finally:
            position_store.POSITIONS_FILE = orig_pos
            position_store.LOCK_FILE = orig_lock

    def test_save_and_load(self, tmp_path):
        """Save then load → roundtrip preserves data."""
        import position_store

        orig_pos = position_store.POSITIONS_FILE
        orig_lock = position_store.LOCK_FILE
        position_store.POSITIONS_FILE = tmp_path / "positions.json"
        position_store.LOCK_FILE = tmp_path / ".positions.lock"

        try:
            positions = [{
                "ticker": "KXHIGHNY-26FEB11-B36.5",
                "side": "yes",
                "avg_price": 20,
                "contracts": 5,
                "status": "open",
            }]
            position_store.save_positions(positions)
            loaded = position_store.load_positions()
            assert len(loaded) == 1
            assert loaded[0]["ticker"] == "KXHIGHNY-26FEB11-B36.5"
            assert loaded[0]["contracts"] == 5
        finally:
            position_store.POSITIONS_FILE = orig_pos
            position_store.LOCK_FILE = orig_lock

    def test_invalid_entries_filtered(self, tmp_path):
        """Invalid entries (missing keys) are filtered out."""
        import position_store

        orig_pos = position_store.POSITIONS_FILE
        orig_lock = position_store.LOCK_FILE
        position_store.POSITIONS_FILE = tmp_path / "positions.json"
        position_store.LOCK_FILE = tmp_path / ".positions.lock"

        try:
            # Write a mix of valid and invalid entries
            data = [
                {"ticker": "T1", "side": "yes", "avg_price": 10, "contracts": 1, "status": "open"},
                {"ticker": "T2"},  # Missing required keys
                {"bad": "entry"},
            ]
            (tmp_path / "positions.json").write_text(json.dumps(data))
            loaded = position_store.load_positions()
            assert len(loaded) == 1
            assert loaded[0]["ticker"] == "T1"
        finally:
            position_store.POSITIONS_FILE = orig_pos
            position_store.LOCK_FILE = orig_lock

    def test_register_new_position(self, tmp_path):
        """Register creates new position entry."""
        import position_store

        orig_pos = position_store.POSITIONS_FILE
        orig_lock = position_store.LOCK_FILE
        position_store.POSITIONS_FILE = tmp_path / "positions.json"
        position_store.LOCK_FILE = tmp_path / ".positions.lock"

        try:
            # RESTING orders get status "resting" (not "open")
            position_store.register_position(
                ticker="KXHIGHNY-TEST",
                side="yes",
                price=20,
                quantity=5,
                order_id="order-123",
                status="RESTING",
            )
            loaded = position_store.load_positions()
            assert len(loaded) == 1
            assert loaded[0]["ticker"] == "KXHIGHNY-TEST"
            assert loaded[0]["contracts"] == 5
            assert loaded[0]["avg_price"] == 20
            assert loaded[0]["status"] == "resting"

            # EXECUTED orders get status "open"
            position_store.POSITIONS_FILE.unlink()
            position_store.register_position(
                ticker="KXHIGHNY-TEST2",
                side="yes",
                price=15,
                quantity=10,
                order_id="order-456",
                status="EXECUTED",
            )
            loaded = position_store.load_positions()
            assert len(loaded) == 1
            assert loaded[0]["status"] == "open"
            assert loaded[0]["exit_rules"]["freeroll_at"] == 30
        finally:
            position_store.POSITIONS_FILE = orig_pos
            position_store.LOCK_FILE = orig_lock

    def test_register_averaging_in(self, tmp_path):
        """Register into existing position → weighted average price."""
        import position_store

        orig_pos = position_store.POSITIONS_FILE
        orig_lock = position_store.LOCK_FILE
        position_store.POSITIONS_FILE = tmp_path / "positions.json"
        position_store.LOCK_FILE = tmp_path / ".positions.lock"

        try:
            # First position
            position_store.register_position("KXHIGHNY-TEST", "yes", 20, 5, "o1", "EXECUTED")
            # Average in
            position_store.register_position("KXHIGHNY-TEST", "yes", 30, 5, "o2", "EXECUTED")

            loaded = position_store.load_positions()
            assert len(loaded) == 1
            assert loaded[0]["contracts"] == 10
            # Weighted avg: (20*5 + 30*5) / 10 = 25
            assert loaded[0]["avg_price"] == 25.0
        finally:
            position_store.POSITIONS_FILE = orig_pos
            position_store.LOCK_FILE = orig_lock


    def test_position_transaction(self, tmp_path):
        """position_transaction() holds lock across read-modify-write."""
        import position_store

        orig_pos = position_store.POSITIONS_FILE
        orig_lock = position_store.LOCK_FILE
        position_store.POSITIONS_FILE = tmp_path / "positions.json"
        position_store.LOCK_FILE = tmp_path / ".positions.lock"

        try:
            # Seed a position
            position_store.register_position("KXHIGHNY-TEST", "yes", 20, 5, "o1", "EXECUTED")

            # Use transaction to modify in-place
            with position_store.position_transaction() as positions:
                assert len(positions) == 1
                positions[0]["status"] = "closed"
                positions[0]["notes"].append("closed via transaction test")

            # Verify saved correctly
            loaded = position_store.load_positions()
            assert len(loaded) == 1
            assert loaded[0]["status"] == "closed"
            assert "closed via transaction test" in loaded[0]["notes"][-1]
        finally:
            position_store.POSITIONS_FILE = orig_pos
            position_store.LOCK_FILE = orig_lock

    def test_position_dict_type_exists(self):
        """PositionDict TypedDict should be importable and have expected keys."""
        from position_store import PositionDict
        # TypedDict annotations should include our core fields
        annotations = PositionDict.__annotations__
        assert "ticker" in annotations
        assert "side" in annotations
        assert "avg_price" in annotations
        assert "contracts" in annotations
        assert "status" in annotations
        assert "last_confidence" in annotations
        assert "bracket_low" in annotations
        assert "current_obs_temp" in annotations
        assert "sell_placed_at" in annotations

    def test_lock_timeout_error_importable(self):
        """LockTimeoutError should be importable."""
        from position_store import LockTimeoutError
        err = LockTimeoutError("test timeout")
        assert "test timeout" in str(err)

    def test_lock_timeout_constant(self):
        """Lock timeout should be a reasonable value (not 0 or huge)."""
        from position_store import LOCK_TIMEOUT_SEC
        assert 1 <= LOCK_TIMEOUT_SEC <= 60


# ═══════════════════════════════════════════════════════════
#  UTILITY FUNCTIONS
# ═══════════════════════════════════════════════════════════

class TestShorten:
    def test_shorten_bracket(self):
        """Should strip Kalshi verbose title to just the bracket."""
        title = "Will the high temperature in New York be between 35° and 36°F on February 11?"
        short = shorten_bracket_title(title)
        assert len(short) < len(title)
        assert "35" in short
        assert "Will" not in short

    def test_passthrough_simple(self):
        """Simple title without Kalshi prefix → returned as-is."""
        short = shorten_bracket_title("35° to 36°F")
        assert "35" in short


# ═══════════════════════════════════════════════════════════
#  MODEL BIAS CORRECTIONS
# ═══════════════════════════════════════════════════════════

class TestBiasCorrections:
    """Test that bias corrections dict is loaded and applied correctly."""

    def test_bias_corrections_dict_exists(self):
        """_BIAS_CORRECTIONS dict should be importable."""
        from edge_scanner_v2 import _BIAS_CORRECTIONS
        assert isinstance(_BIAS_CORRECTIONS, dict)

    def test_bias_corrections_keyed_by_tuple(self):
        """If populated, keys should be (model_name, city_code) tuples."""
        from edge_scanner_v2 import _BIAS_CORRECTIONS
        for key in _BIAS_CORRECTIONS:
            assert isinstance(key, tuple)
            assert len(key) == 2
            assert isinstance(key[0], str)  # model name
            assert isinstance(key[1], str)  # city code

    def test_bias_corrections_values_are_float(self):
        """Correction values should be floats."""
        from edge_scanner_v2 import _BIAS_CORRECTIONS
        for val in _BIAS_CORRECTIONS.values():
            assert isinstance(val, (int, float))

    def test_bias_correction_shifts_members(self):
        """Verify the correction math: members + correction shifts the distribution."""
        members = [40.0, 41.0, 42.0, 43.0, 44.0]
        bias_corr = -2.0  # model runs 2F hot → correction subtracts 2F
        corrected = [v + bias_corr for v in members]
        assert corrected == [38.0, 39.0, 40.0, 41.0, 42.0]

    def test_zero_correction_no_change(self):
        """Zero correction should leave members unchanged."""
        members = [40.0, 41.0, 42.0]
        bias_corr = 0.0
        corrected = [v + bias_corr for v in members]
        assert corrected == members

    def test_kde_probability_shifts_with_bias(self):
        """Bias correction should shift KDE probability to different bracket."""
        members = list(np.random.normal(40, 0.5, 200))

        # Before correction: bracket 39-41 should be high probability
        prob_before = kde_probability(members, 39, 41)

        # After -3F correction: members shift to ~37F, bracket 36-38 gets probability
        corrected = [v - 3.0 for v in members]
        prob_after_target = kde_probability(corrected, 36, 38)
        prob_after_original = kde_probability(corrected, 39, 41)

        assert prob_after_target > prob_after_original
        assert prob_before > 0.5  # original bracket had high prob
        assert prob_after_original < 0.1  # shifted away from original


class TestIsTomorrowTicker:
    def test_matching_date(self):
        from datetime import date
        d = date(2025, 2, 11)
        assert is_tomorrow_ticker("KXHIGHNY-25FEB11-B36.5", d) is True

    def test_non_matching_date(self):
        from datetime import date
        d = date(2025, 2, 12)
        assert is_tomorrow_ticker("KXHIGHNY-25FEB11-B36.5", d) is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
