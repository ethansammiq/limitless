#!/usr/bin/env python3
"""
Tests for CPI scanner core functions.

Run: python3 -m pytest tests/test_cpi_scanner.py -v
"""

import numpy as np

from edge_scanner_v2 import kde_probability, silverman_bandwidth
from cpi_scanner import (
    CPISourceEstimate,
    CPIEnsemble,
    build_cpi_ensemble,
    build_bls_prior_source,
    build_yoy_ensemble,
    parse_cpi_bracket,
)


# ═══════════════════════════════════════════════════════════
#  KDE BANDWIDTH — CPI SCALE
# ═══════════════════════════════════════════════════════════

class TestCPIBandwidth:
    """Verify KDE bandwidth is appropriate for CPI percentage scale."""

    def test_silverman_cpi_scale(self):
        """82 CPI members with std=0.04% → bandwidth ~0.02%, NOT 0.3."""
        rng = np.random.default_rng(42)
        members = rng.normal(0.30, 0.04, 82).tolist()
        bw = silverman_bandwidth(members, min_bandwidth=0.005)
        assert 0.005 < bw < 0.1, f"CPI bandwidth {bw} out of range (expected ~0.02)"
        assert bw < 0.3, f"CPI bandwidth {bw} hit weather floor — bug!"

    def test_silverman_weather_default_unchanged(self):
        """Weather members (default min_bandwidth=0.3) → unchanged behavior."""
        rng = np.random.default_rng(42)
        members = rng.normal(40, 0.8, 194).tolist()
        bw = silverman_bandwidth(members)  # Default min_bandwidth=0.3
        assert bw >= 0.3, f"Weather bandwidth {bw} below 0.3 floor"

    def test_silverman_backward_compat(self):
        """Old callers with no min_bandwidth arg still get 0.3 floor."""
        members = [40.0, 40.1, 40.2, 40.3]  # Very tight → Silverman < 0.3
        bw = silverman_bandwidth(members)
        assert bw == 0.3, f"Expected 0.3 floor, got {bw}"


# ═══════════════════════════════════════════════════════════
#  KDE PROBABILITY — CPI SCALE
# ═══════════════════════════════════════════════════════════

class TestCPIKDEProbability:
    """Verify KDE probability works correctly at CPI percentage scale."""

    def test_cpi_center_bracket(self):
        """82 members at N(0.30, 0.04) → bracket [0.25, 0.35] should get high prob."""
        rng = np.random.default_rng(42)
        members = rng.normal(0.30, 0.04, 82).tolist()
        bw = silverman_bandwidth(members, min_bandwidth=0.005)
        prob = kde_probability(members, 0.25, 0.35, bandwidth=bw, min_bandwidth=0.005)
        assert prob > 0.70, f"Center bracket prob {prob:.3f} too low (expected >0.70)"

    def test_cpi_tail_bracket(self):
        """Members at 0.30% → bracket [0.50, 0.60] should have very low prob."""
        rng = np.random.default_rng(42)
        members = rng.normal(0.30, 0.04, 82).tolist()
        bw = silverman_bandwidth(members, min_bandwidth=0.005)
        prob = kde_probability(members, 0.50, 0.60, bandwidth=bw, min_bandwidth=0.005)
        assert prob < 0.05, f"Tail bracket prob {prob:.3f} too high (expected <0.05)"

    def test_cpi_above_threshold(self):
        """P(CPI > 0.30) for members at N(0.30, 0.04) → ~50%."""
        rng = np.random.default_rng(42)
        members = rng.normal(0.30, 0.04, 82).tolist()
        bw = silverman_bandwidth(members, min_bandwidth=0.005)
        # P(CPI > 0.30) = 1 - P(CPI < 0.30)
        p_below = kde_probability(members, -999, 0.30, bandwidth=bw, min_bandwidth=0.005)
        p_above = 1.0 - p_below
        assert 0.35 < p_above < 0.65, f"P(>0.30) = {p_above:.3f}, expected ~0.50"

    def test_cpi_vs_broken_bandwidth(self):
        """With broken 0.3 floor, CPI probs should be much worse (smeared)."""
        rng = np.random.default_rng(42)
        members = rng.normal(0.30, 0.04, 82).tolist()

        # Good bandwidth (CPI-appropriate)
        bw_good = silverman_bandwidth(members, min_bandwidth=0.005)
        prob_good = kde_probability(members, 0.25, 0.35, bandwidth=bw_good, min_bandwidth=0.005)

        # Bad bandwidth (old weather floor)
        prob_bad = kde_probability(members, 0.25, 0.35, bandwidth=0.3, min_bandwidth=0.3)

        # Good bandwidth should give MUCH higher prob for the center bracket
        assert prob_good > prob_bad * 1.5, (
            f"Good bw prob ({prob_good:.3f}) should be much higher than bad ({prob_bad:.3f})"
        )


# ═══════════════════════════════════════════════════════════
#  BLS HISTORICAL PRIOR
# ═══════════════════════════════════════════════════════════

class TestBLSPrior:
    """Test BLS historical prior source builder."""

    def _make_bls_history(self, n_months=24):
        """Generate synthetic BLS history for testing."""
        history = []
        base_index = 310.0
        for i in range(n_months):
            month = 12 - (i % 12)
            year = 2025 - (i // 12)
            # Simulate ~0.3% MoM growth with some noise
            mom_change = round(0.30 + np.random.normal(0, 0.08), 3) if i < n_months - 1 else 0.0
            history.append({
                "year": year,
                "month": month,
                "period_name": f"Month {month}",
                "index_value": base_index + (n_months - i) * 0.9,
                "mom_change": mom_change,
            })
        return history

    def test_prior_basic(self):
        """BLS prior with 24 months of data should produce valid source."""
        history = self._make_bls_history(24)
        prior = build_bls_prior_source(history, target_month=1)
        assert prior.data_available is True
        assert prior.name == "bls_prior"
        assert prior.weight == 0.60
        assert 0.0 < prior.point_estimate < 1.0  # Should be ~0.3% MoM
        assert prior.uncertainty_std > 0

    def test_prior_insufficient_data(self):
        """Less than 3 entries should return unavailable."""
        history = [{"mom_change": 0.3, "month": 1, "year": 2025}]
        prior = build_bls_prior_source(history, target_month=1)
        assert prior.data_available is False

    def test_prior_seasonal_weighting(self):
        """Same-month and adjacent entries should be double-weighted."""
        # Create history with distinct monthly patterns
        history = []
        for m in range(1, 13):
            # January has higher CPI than other months
            base = 0.50 if m == 1 else 0.25
            history.append({
                "year": 2025, "month": m,
                "mom_change": base,
                "index_value": 320 + m,
            })
            history.append({
                "year": 2024, "month": m,
                "mom_change": base,
                "index_value": 310 + m,
            })

        prior_jan = build_bls_prior_source(history, target_month=1)
        prior_jul = build_bls_prior_source(history, target_month=7)

        # January target should pull estimate higher (0.50 weighted more)
        # July target should pull estimate lower (~0.25)
        assert prior_jan.point_estimate > prior_jul.point_estimate, (
            f"Jan prior ({prior_jan.point_estimate:.3f}) should be > Jul ({prior_jul.point_estimate:.3f})"
        )


# ═══════════════════════════════════════════════════════════
#  YoY CONVERSION
# ═══════════════════════════════════════════════════════════

class TestYoYConversion:
    """Test MoM→YoY ensemble conversion."""

    def _make_mom_ensemble(self, mean=0.30, std=0.04, n=82):
        """Build a simple MoM ensemble for testing."""
        rng = np.random.default_rng(42)
        members = sorted(rng.normal(mean, std, n).tolist())
        weights = [1.0] * n
        return CPIEnsemble(
            sources=[],
            all_members=members,
            member_weights=weights,
            total_count=n,
            mean=float(np.mean(members)),
            median=float(np.median(members)),
            std=float(np.std(members, ddof=1)),
            min_val=min(members),
            max_val=max(members),
            p10=float(np.percentile(members, 10)),
            p25=float(np.percentile(members, 25)),
            p50=float(np.percentile(members, 50)),
            p75=float(np.percentile(members, 75)),
            p90=float(np.percentile(members, 90)),
            kde_bandwidth=silverman_bandwidth(members, min_bandwidth=0.005),
            sources_available=2,
        )

    def _make_bls_history(self):
        """BLS history with known index values for predictable YoY."""
        history = []
        # Latest: Dec 2025, index=326.0
        # 12 months ago: Dec 2024, index=316.5
        # Expected YoY for MoM=0.30%: (326.0 * 1.003 / 316.5 - 1) * 100 ≈ 3.30%
        base_index = 310.0
        for i in range(24):
            month = 12 - (i % 12) if (i % 12) != 0 else 12
            year = 2025 - (i // 12)
            if i == 0:
                idx = 326.0
            elif i == 12:
                idx = 316.5
            else:
                idx = base_index + (24 - i) * 0.65
            history.append({
                "year": year,
                "month": month,
                "period_name": f"Month {month}",
                "index_value": idx,
                "mom_change": 0.3 if i < 23 else 0.0,
            })
        return history

    def test_yoy_conversion_basic(self):
        """MoM=0.30% + known indices → YoY ≈ 3.3%."""
        mom_ens = self._make_mom_ensemble(mean=0.30, std=0.04)
        bls = self._make_bls_history()
        yoy_ens = build_yoy_ensemble(mom_ens, bls)

        assert yoy_ens.all_members, "YoY ensemble should have members"
        assert len(yoy_ens.all_members) == len(mom_ens.all_members)
        # Expected: (326 * 1.003 / 316.5 - 1) * 100 ≈ 3.30%
        assert 2.5 < yoy_ens.mean < 4.0, f"YoY mean {yoy_ens.mean:.3f}% out of range"

    def test_yoy_member_count_preserved(self):
        """YoY ensemble should have same member count as MoM."""
        mom_ens = self._make_mom_ensemble(n=100)
        bls = self._make_bls_history()
        yoy_ens = build_yoy_ensemble(mom_ens, bls)
        assert yoy_ens.total_count == 100

    def test_yoy_insufficient_history(self):
        """Less than 13 months of BLS data → empty ensemble."""
        mom_ens = self._make_mom_ensemble()
        bls = [{"year": 2025, "month": 12, "index_value": 326.0, "mom_change": 0.3}] * 5
        yoy_ens = build_yoy_ensemble(mom_ens, bls)
        assert not yoy_ens.all_members, "Should return empty ensemble with insufficient history"

    def test_yoy_bandwidth_appropriate(self):
        """YoY ensemble bandwidth should be appropriate for YoY scale (2-4%)."""
        mom_ens = self._make_mom_ensemble()
        bls = self._make_bls_history()
        yoy_ens = build_yoy_ensemble(mom_ens, bls)
        # YoY std is much tighter than range, so bandwidth should be 0.01-0.1
        assert 0.01 <= yoy_ens.kde_bandwidth < 0.5, (
            f"YoY bandwidth {yoy_ens.kde_bandwidth} out of range"
        )


# ═══════════════════════════════════════════════════════════
#  BRACKET PARSING
# ═══════════════════════════════════════════════════════════

class TestCPIBracketParsing:
    """Test CPI market bracket parsing."""

    def test_mom_above(self):
        """CPI MoM 'above' bracket parsing."""
        market = {
            "title": "CPI to increase 0.3% or more",
            "ticker": "KXCPI-26FEB13-T0.3",
            "floor_strike": 0.3,
            "cap_strike": None,
        }
        threshold, direction, _ = parse_cpi_bracket(market)
        assert threshold == 0.3
        assert direction == "above"

    def test_yoy_above(self):
        """CPI YoY 'above' bracket parsing — strikes in natural units."""
        market = {
            "title": "Inflation rate above 2.7%",
            "ticker": "KXCPIYOY-26FEB13-T2.7",
            "floor_strike": 2.7,
            "cap_strike": None,
        }
        threshold, direction, _ = parse_cpi_bracket(market)
        assert threshold == 2.7  # Should be 2.7%, NOT 0.027
        assert direction == "above"

    def test_below_bracket(self):
        """Below bracket parsing."""
        market = {
            "title": "CPI below 0.2%",
            "ticker": "KXCPI-26FEB13-B0.2",
            "floor_strike": None,
            "cap_strike": 0.2,
        }
        threshold, direction, _ = parse_cpi_bracket(market)
        assert threshold == 0.2
        assert direction == "below"


# ═══════════════════════════════════════════════════════════
#  ENSEMBLE BUILDER
# ═══════════════════════════════════════════════════════════

class TestCPIEnsembleBuilder:
    """Test synthetic ensemble construction."""

    def test_build_with_two_sources(self):
        """Two available sources should produce combined ensemble."""
        sources = [
            CPISourceEstimate(
                name="tips_breakeven", display_name="TIPS",
                point_estimate=0.28,
                uncertainty_std=0.06, weight=1.15,
                data_available=True,
            ),
            CPISourceEstimate(
                name="cleveland_fed", display_name="Cleveland Fed",
                point_estimate=0.30,
                uncertainty_std=0.04, weight=1.30,
                data_available=True,
            ),
        ]
        ensemble = build_cpi_ensemble(sources, seed=42)
        assert ensemble.total_count > 50, f"Expected >50 members, got {ensemble.total_count}"
        assert ensemble.kde_bandwidth < 0.3, f"CPI bandwidth {ensemble.kde_bandwidth} hit weather floor"
        assert 0.20 < ensemble.mean < 0.40, f"Mean {ensemble.mean} out of range"

    def test_build_empty_sources(self):
        """No available sources → empty ensemble."""
        sources = [
            CPISourceEstimate(name="tips", display_name="TIPS", data_available=False),
        ]
        ensemble = build_cpi_ensemble(sources, seed=42)
        assert ensemble.total_count == 0

    def test_build_bandwidth_cpi_scale(self):
        """Ensemble bandwidth should be CPI-appropriate (<<0.3)."""
        sources = [
            CPISourceEstimate(
                name="tips_breakeven", display_name="TIPS",
                point_estimate=0.30,
                uncertainty_std=0.04, weight=1.15,
                data_available=True,
            ),
        ]
        ensemble = build_cpi_ensemble(sources, seed=42)
        assert ensemble.kde_bandwidth < 0.15, (
            f"CPI ensemble bandwidth {ensemble.kde_bandwidth} too high"
        )
