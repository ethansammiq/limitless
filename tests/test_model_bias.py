#!/usr/bin/env python3
"""Tests for model_bias.py — per-model bias tracking, rolling bias, weight suggestions."""



# ─── Record builder ──────────────────────────────────────────────────────────


def _make_record(
    city="NYC",
    date="2026-02-14",
    actual_high=42.0,
    per_model_means=None,
):
    """Build a minimal backtest record dict for model_bias functions."""
    if per_model_means is None:
        per_model_means = {
            "ecmwf_aifs025": actual_high + 1.0,   # runs +1F hot
            "ecmwf_ifs025": actual_high - 0.5,     # runs -0.5F cold
            "gfs_seamless": actual_high + 0.2,
            "icon_seamless": actual_high + 2.0,     # runs +2F hot
            "gem_global": actual_high - 1.0,
        }
    return {
        "date": date,
        "city": city,
        "actual_high": actual_high,
        "per_model_means": per_model_means,
    }


def _make_batch(n=10, city="NYC", base_actual=42.0, **per_model_offsets):
    """Generate n records with consistent model offsets for bias testing."""
    offsets = {
        "ecmwf_aifs025": 1.0,
        "ecmwf_ifs025": -0.5,
        "gfs_seamless": 0.2,
        "icon_seamless": 2.0,
        "gem_global": -1.0,
        "bom_access_global_ensemble": -1.5,
        "ukmo_global_ensemble_20km": 0.8,
    }
    offsets.update(per_model_offsets)

    records = []
    for i in range(n):
        actual = base_actual + (i % 5) * 0.5  # slight variation
        pmm = {m: actual + off for m, off in offsets.items()}
        records.append(_make_record(
            city=city,
            date=f"2026-02-{i+1:02d}",
            actual_high=actual,
            per_model_means=pmm,
        ))
    return records


# ─── compute_model_biases ───────────────────────────────────────────────────


class TestComputeModelBiases:
    """Test full bias computation across model/city combos."""

    def test_basic_bias_values(self):
        from model_bias import compute_model_biases

        records = _make_batch(n=10, city="NYC")
        biases = compute_model_biases(records)

        # Should have per-city entries plus aggregate ("ALL")
        nyc_aifs = [b for b in biases if b.model_name == "ecmwf_aifs025" and b.city == "NYC"]
        assert len(nyc_aifs) == 1
        assert abs(nyc_aifs[0].bias - 1.0) < 0.01  # +1.0F hot
        assert nyc_aifs[0].mae == 1.0

        nyc_icon = [b for b in biases if b.model_name == "icon_seamless" and b.city == "NYC"]
        assert len(nyc_icon) == 1
        assert abs(nyc_icon[0].bias - 2.0) < 0.01  # +2.0F hot

    def test_aggregate_entries_present(self):
        from model_bias import compute_model_biases

        records = _make_batch(n=10, city="NYC")
        biases = compute_model_biases(records)
        all_entries = [b for b in biases if b.city == "ALL"]
        assert len(all_entries) >= 1  # at least one model has ALL

    def test_insufficient_records_excluded(self):
        from model_bias import compute_model_biases

        # Only 3 records — below the threshold of 5
        records = _make_batch(n=3, city="NYC")
        biases = compute_model_biases(records)
        # No per-city entries should appear (MIN_RECORDS_FOR_BIAS=5)
        nyc_entries = [b for b in biases if b.city == "NYC"]
        assert len(nyc_entries) == 0

    def test_multi_city(self):
        from model_bias import compute_model_biases

        records = _make_batch(n=8, city="NYC") + _make_batch(n=8, city="CHI")
        # Adjust CHI dates to avoid NYC date collision
        for i, r in enumerate(records[8:]):
            r["date"] = f"2026-01-{i+1:02d}"

        biases = compute_model_biases(records)
        cities = {b.city for b in biases}
        assert "NYC" in cities
        assert "CHI" in cities
        assert "ALL" in cities


# ─── compute_rolling_bias ────────────────────────────────────────────────────


class TestComputeRollingBias:
    """Test exponentially-weighted recent bias calculation."""

    def test_basic_rolling_bias(self):
        from model_bias import compute_rolling_bias

        records = _make_batch(n=10, city="NYC")
        bias, mae, count = compute_rolling_bias(records, "ecmwf_aifs025", "NYC", window=7)

        assert count == 7  # windowed to last 7
        # All records have +1.0F offset, so rolling bias should be ~+1.0
        assert abs(bias - 1.0) < 0.1

    def test_exponential_weighting(self):
        """More recent records should dominate the rolling bias."""
        from model_bias import compute_rolling_bias

        # First 5 records: model offset = +1.0; last 5: offset = +3.0
        records = []
        for i in range(5):
            records.append(_make_record(
                city="NYC", date=f"2026-02-{i+1:02d}", actual_high=40.0,
                per_model_means={"ecmwf_aifs025": 41.0,
                                 "ecmwf_ifs025": 40.0, "gfs_seamless": 40.0,
                                 "icon_seamless": 40.0, "gem_global": 40.0},
            ))
        for i in range(5):
            records.append(_make_record(
                city="NYC", date=f"2026-02-{i+6:02d}", actual_high=40.0,
                per_model_means={"ecmwf_aifs025": 43.0,
                                 "ecmwf_ifs025": 40.0, "gfs_seamless": 40.0,
                                 "icon_seamless": 40.0, "gem_global": 40.0},
            ))

        bias, _, _ = compute_rolling_bias(records, "ecmwf_aifs025", "NYC", window=10)
        # Should be closer to +3.0 than to +1.0 due to exponential weighting
        assert bias > 2.0

    def test_no_data_returns_zero(self):
        from model_bias import compute_rolling_bias

        bias, mae, count = compute_rolling_bias([], "ecmwf_aifs025", "NYC")
        assert bias == 0.0
        assert mae == 0.0
        assert count == 0

    def test_all_city_aggregate(self):
        from model_bias import compute_rolling_bias

        records = _make_batch(n=10, city="NYC") + _make_batch(n=10, city="CHI")
        for i, r in enumerate(records[10:]):
            r["date"] = f"2026-01-{i+1:02d}"

        bias, mae, count = compute_rolling_bias(records, "ecmwf_aifs025", "ALL", window=7)
        assert count == 7
        assert abs(bias - 1.0) < 0.1  # same offset for both cities


# ─── get_bias_correction ────────────────────────────────────────────────────


class TestGetBiasCorrection:
    """Test the correction value returned for a model+city combo."""

    def test_returns_negative_of_bias(self):
        from model_bias import get_bias_correction

        records = _make_batch(n=10, city="NYC")
        # icon_seamless runs +2.0F hot -> correction should be -2.0
        corr = get_bias_correction("icon_seamless", "NYC", records)
        assert abs(corr - (-2.0)) < 0.05

    def test_insufficient_data_returns_zero(self):
        from model_bias import get_bias_correction

        records = _make_batch(n=3, city="NYC")  # below MIN_RECORDS_FOR_BIAS
        corr = get_bias_correction("ecmwf_aifs025", "NYC", records)
        assert corr == 0.0

    def test_cold_model_positive_correction(self):
        from model_bias import get_bias_correction

        records = _make_batch(n=10, city="NYC")
        # gem_global runs -1.0F cold -> correction should be +1.0
        corr = get_bias_correction("gem_global", "NYC", records)
        assert abs(corr - 1.0) < 0.05

    def test_filters_by_city(self):
        from model_bias import get_bias_correction

        nyc = _make_batch(n=10, city="NYC")
        chi = _make_batch(n=10, city="CHI")
        for i, r in enumerate(chi):
            r["date"] = f"2026-01-{i+1:02d}"
            # Override CHI ICON to be cold instead of hot
            r["per_model_means"]["icon_seamless"] = r["actual_high"] - 3.0

        all_records = nyc + chi
        nyc_corr = get_bias_correction("icon_seamless", "NYC", all_records)
        chi_corr = get_bias_correction("icon_seamless", "CHI", all_records)
        # NYC: icon runs hot (+2) -> correction ~ -2
        # CHI: icon runs cold (-3) -> correction ~ +3
        assert nyc_corr < 0
        assert chi_corr > 0


# ─── suggest_weights ─────────────────────────────────────────────────────────


class TestSuggestWeights:
    """Test inverse-MAE weight suggestions."""

    def test_canonical_weights_from_config(self):
        """DEFAULT_MODEL_WEIGHTS must be the canonical config.py table (all 7 models)."""
        import config
        from model_bias import DEFAULT_MODEL_WEIGHTS, TOTAL_DEFAULT_WEIGHT

        assert DEFAULT_MODEL_WEIGHTS is config.DEFAULT_MODEL_WEIGHTS
        assert DEFAULT_MODEL_WEIGHTS == {
            "ecmwf_aifs025": 1.30,
            "ecmwf_ifs025": 1.15,
            "gfs_seamless": 1.00,
            "icon_seamless": 0.95,
            "gem_global": 0.85,
            "bom_access_global_ensemble": 0.80,
            "ukmo_global_ensemble_20km": 0.85,
        }
        assert abs(TOTAL_DEFAULT_WEIGHT - 6.90) < 1e-9

    def test_weights_sum_to_target(self):
        from model_bias import suggest_weights, TOTAL_DEFAULT_WEIGHT

        records = _make_batch(n=10, city="NYC")
        result = suggest_weights(records)

        agg = result["aggregate"]
        total = sum(agg.values())
        assert abs(total - TOTAL_DEFAULT_WEIGHT) < 0.05

    def test_missing_models_get_default_weights_not_one(self):
        """Models without backtest data must keep canonical defaults, not 1.0."""
        from model_bias import suggest_weights

        # Records cover only the original 5 models — bom/ukmo have no data
        per_model_means_5 = {
            "ecmwf_aifs025": 43.0,
            "ecmwf_ifs025": 41.5,
            "gfs_seamless": 42.2,
            "icon_seamless": 44.0,
            "gem_global": 41.0,
        }
        records = [
            _make_record(
                city="NYC", date=f"2026-02-{i+1:02d}", actual_high=42.0,
                per_model_means=dict(per_model_means_5),
            )
            for i in range(10)
        ]
        result = suggest_weights(records)
        agg = result["aggregate"]
        assert agg["bom_access_global_ensemble"] == 0.80
        assert agg["ukmo_global_ensemble_20km"] == 0.85

    def test_lower_mae_gets_higher_weight(self):
        from model_bias import suggest_weights

        records = _make_batch(n=10, city="NYC")
        result = suggest_weights(records)
        agg = result["aggregate"]

        # gfs_seamless offset = +0.2 (lowest MAE)
        # icon_seamless offset = +2.0 (highest MAE)
        assert agg["gfs_seamless"] > agg["icon_seamless"]

    def test_by_city_present(self):
        from model_bias import suggest_weights

        records = _make_batch(n=10, city="NYC")
        result = suggest_weights(records)
        assert "by_city" in result
        assert "NYC" in result["by_city"]
