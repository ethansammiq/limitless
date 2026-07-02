#!/usr/bin/env python3
"""Tests for calibration_analyzer.py — calibration curve, model accuracy, weights, thresholds."""



# ─── Synthetic record builders ───────────────────────────────────────────────


def _make_record(
    confidence_score=75,
    prediction_correct=True,
    actual_high=42.0,
    model_errors=None,
    trade_score=0.55,
    best_edge_cents=12.0,
    city="NYC",
    date="2026-02-14",
    ensemble_std=1.2,
    ensemble_mean=42.0,
):
    """Build a minimal calibration record dict for testing."""
    if model_errors is None:
        model_errors = {
            "ecmwf_aifs025": 0.5,
            "ecmwf_ifs025": -0.3,
            "gfs_seamless": 1.0,
            "icon_seamless": -0.8,
            "gem_global": 1.5,
        }
    return {
        "date": date,
        "city": city,
        "confidence_score": confidence_score,
        "prediction_correct": prediction_correct,
        "actual_high": actual_high,
        "model_errors": model_errors,
        "trade_score": trade_score,
        "best_edge_cents": best_edge_cents,
        "ensemble_std": ensemble_std,
        "ensemble_mean": ensemble_mean,
    }


# ─── calibration_curve ───────────────────────────────────────────────────────


class TestCalibrationCurve:
    """Test binning records by confidence and computing accuracy."""

    def test_single_bin(self):
        from calibration_analyzer import calibration_curve

        records = [
            _make_record(confidence_score=75, prediction_correct=True),
            _make_record(confidence_score=72, prediction_correct=False),
            _make_record(confidence_score=78, prediction_correct=True),
        ]
        result = calibration_curve(records, bin_size=10)
        assert len(result) == 1
        entry = result[0]
        assert entry["bin"] == "70-79"
        assert entry["count"] == 3
        assert abs(entry["accuracy"] - 2.0 / 3.0) < 0.01

    def test_multiple_bins(self):
        from calibration_analyzer import calibration_curve

        records = [
            _make_record(confidence_score=55, prediction_correct=False),
            _make_record(confidence_score=75, prediction_correct=True),
            _make_record(confidence_score=92, prediction_correct=True),
        ]
        result = calibration_curve(records, bin_size=10)
        bins = {r["bin"] for r in result}
        assert "50-59" in bins
        assert "70-79" in bins
        assert "90-99" in bins

    def test_empty_records(self):
        from calibration_analyzer import calibration_curve
        assert calibration_curve([]) == []

    def test_records_without_confidence_skipped(self):
        from calibration_analyzer import calibration_curve

        records = [
            _make_record(confidence_score=None, prediction_correct=True),
            _make_record(confidence_score=80, prediction_correct=None),
        ]
        assert calibration_curve(records) == []

    def test_accuracy_bounds(self):
        """All correct => accuracy 1.0; all wrong => accuracy 0.0."""
        from calibration_analyzer import calibration_curve

        all_correct = [_make_record(confidence_score=85, prediction_correct=True) for _ in range(5)]
        result = calibration_curve(all_correct)
        assert result[0]["accuracy"] == 1.0

        all_wrong = [_make_record(confidence_score=85, prediction_correct=False) for _ in range(5)]
        result = calibration_curve(all_wrong)
        assert result[0]["accuracy"] == 0.0


# ─── model_accuracy_report ───────────────────────────────────────────────────


class TestModelAccuracyReport:
    """Test per-model MAE, bias, RMSE computation."""

    def test_basic_report(self):
        from calibration_analyzer import model_accuracy_report

        records = [
            _make_record(model_errors={
                "ecmwf_aifs025": 1.0, "ecmwf_ifs025": -1.0,
                "gfs_seamless": 2.0, "icon_seamless": -2.0, "gem_global": 0.5,
            }, city="NYC"),
            _make_record(model_errors={
                "ecmwf_aifs025": 1.0, "ecmwf_ifs025": -1.0,
                "gfs_seamless": 2.0, "icon_seamless": -2.0, "gem_global": 0.5,
            }, city="CHI"),
        ]
        report = model_accuracy_report(records)

        assert "ecmwf_aifs025" in report
        # MAE for aifs should be 1.0 (|1.0| both records)
        assert report["ecmwf_aifs025"]["mae"] == 1.0
        # Bias for aifs should be +1.0 (runs hot)
        assert report["ecmwf_aifs025"]["bias"] == 1.0
        assert report["ecmwf_aifs025"]["count"] == 2

    def test_empty_records(self):
        from calibration_analyzer import model_accuracy_report

        report = model_accuracy_report([])
        for model, info in report.items():
            assert info["mae"] is None
            assert info["count"] == 0

    def test_best_worst_city(self):
        from calibration_analyzer import model_accuracy_report

        records = [
            _make_record(model_errors={"ecmwf_aifs025": 0.1, "ecmwf_ifs025": 0.0,
                                       "gfs_seamless": 0.0, "icon_seamless": 0.0,
                                       "gem_global": 0.0}, city="NYC"),
            _make_record(model_errors={"ecmwf_aifs025": 3.0, "ecmwf_ifs025": 0.0,
                                       "gfs_seamless": 0.0, "icon_seamless": 0.0,
                                       "gem_global": 0.0}, city="CHI"),
        ]
        report = model_accuracy_report(records)
        assert report["ecmwf_aifs025"]["best_city"] == "NYC"
        assert report["ecmwf_aifs025"]["worst_city"] == "CHI"


# ─── optimal_weights ─────────────────────────────────────────────────────────


class TestOptimalWeights:
    """Test inverse-MAE weight computation."""

    def test_canonical_weights_from_config(self):
        """CURRENT_MODEL_WEIGHTS must be the canonical config.py table (all 7 models)."""
        import config
        from calibration_analyzer import CURRENT_MODEL_WEIGHTS

        assert CURRENT_MODEL_WEIGHTS is config.DEFAULT_MODEL_WEIGHTS
        assert CURRENT_MODEL_WEIGHTS == {
            "ecmwf_aifs025": 1.30,
            "ecmwf_ifs025": 1.15,
            "gfs_seamless": 1.00,
            "icon_seamless": 0.95,
            "gem_global": 0.85,
            "bom_access_global_ensemble": 0.80,
            "ukmo_global_ensemble_20km": 0.85,
        }

    def test_weights_sum_to_target(self):
        from calibration_analyzer import optimal_weights, CURRENT_TOTAL_WEIGHT

        # Need at least 5 records per model to trigger weight computation
        records = []
        for i in range(10):
            records.append(_make_record(
                model_errors={
                    "ecmwf_aifs025": 0.5, "ecmwf_ifs025": 1.0,
                    "gfs_seamless": 1.5, "icon_seamless": 2.0, "gem_global": 2.5,
                    "bom_access_global_ensemble": 3.0, "ukmo_global_ensemble_20km": 2.8,
                },
                date=f"2026-02-{i+1:02d}",
            ))
        weights = optimal_weights(records)
        assert len(weights) == 7
        total = sum(weights.values())
        assert abs(total - CURRENT_TOTAL_WEIGHT) < 0.05  # close to 6.90

    def test_missing_models_get_default_weights_not_one(self):
        """Models without backtest data must fall back to canonical defaults, not 1.0."""
        from calibration_analyzer import optimal_weights

        # Only the original 5 models have error data — bom/ukmo missing
        records = []
        for i in range(10):
            records.append(_make_record(
                model_errors={
                    "ecmwf_aifs025": 0.5, "ecmwf_ifs025": 1.0,
                    "gfs_seamless": 1.5, "icon_seamless": 2.0, "gem_global": 2.5,
                },
                date=f"2026-02-{i+1:02d}",
            ))
        weights = optimal_weights(records)
        assert weights["bom_access_global_ensemble"] == 0.80
        assert weights["ukmo_global_ensemble_20km"] == 0.85

    def test_lower_mae_gets_higher_weight(self):
        from calibration_analyzer import optimal_weights

        records = []
        for i in range(10):
            records.append(_make_record(
                model_errors={
                    "ecmwf_aifs025": 0.5,   # best
                    "ecmwf_ifs025": 1.0,
                    "gfs_seamless": 1.5,
                    "icon_seamless": 2.0,
                    "gem_global": 3.0,       # worst
                },
                date=f"2026-02-{i+1:02d}",
            ))
        weights = optimal_weights(records)
        assert weights["ecmwf_aifs025"] > weights["gem_global"]

    def test_insufficient_data_returns_empty(self):
        from calibration_analyzer import optimal_weights

        # Only 2 records — not enough (need 5 per model)
        records = [_make_record(), _make_record(date="2026-02-15")]
        weights = optimal_weights(records)
        assert weights == {}


# ─── threshold_analysis ──────────────────────────────────────────────────────


class TestThresholdAnalysis:
    """Test confidence/score threshold sweep."""

    def test_basic_output_structure(self):
        from calibration_analyzer import threshold_analysis

        records = [
            _make_record(confidence_score=90, trade_score=0.55, prediction_correct=True),
            _make_record(confidence_score=60, trade_score=0.45, prediction_correct=False),
        ]
        results = threshold_analysis(records)
        assert len(results) > 0

        # Each entry should have expected keys
        for r in results:
            assert "confidence_threshold" in r
            assert "score_threshold" in r
            assert "trade_count" in r
            assert "win_rate" in r

    def test_higher_threshold_fewer_trades(self):
        from calibration_analyzer import threshold_analysis

        records = [
            _make_record(confidence_score=90, trade_score=0.55, prediction_correct=True),
            _make_record(confidence_score=60, trade_score=0.55, prediction_correct=False),
            _make_record(confidence_score=50, trade_score=0.55, prediction_correct=True),
        ]
        results = threshold_analysis(records)

        # Find results at score=0.55
        at_55 = [r for r in results if abs(r["score_threshold"] - 0.55) < 0.001]
        # Conf 50 should include all 3; conf 90 should include only 1
        conf50 = next(r for r in at_55 if r["confidence_threshold"] == 50)
        conf90 = next(r for r in at_55 if r["confidence_threshold"] == 90)
        assert conf50["trade_count"] >= conf90["trade_count"]


# ─── sigma_accuracy ──────────────────────────────────────────────────────────


class TestSigmaAccuracy:
    """Test ensemble spread vs accuracy grouping."""

    def test_buckets_present(self):
        from calibration_analyzer import sigma_accuracy

        records = [
            _make_record(ensemble_std=0.8, prediction_correct=True),
            _make_record(ensemble_std=1.2, prediction_correct=False),
            _make_record(ensemble_std=2.5, prediction_correct=True),
        ]
        results = sigma_accuracy(records)
        labels = [r["sigma_range"] for r in results]
        assert "<1.0" in labels
        assert "1.0-1.5" in labels
        assert "2.0-3.0" in labels

    def test_accuracy_computed_correctly(self):
        from calibration_analyzer import sigma_accuracy

        records = [
            _make_record(ensemble_std=0.5, prediction_correct=True),
            _make_record(ensemble_std=0.7, prediction_correct=True),
            _make_record(ensemble_std=0.9, prediction_correct=False),
        ]
        results = sigma_accuracy(records)
        tight = next(r for r in results if r["sigma_range"] == "<1.0")
        assert tight["count"] == 3
        assert abs(tight["accuracy"] - 2.0 / 3.0) < 0.01

    def test_empty_records(self):
        from calibration_analyzer import sigma_accuracy
        results = sigma_accuracy([])
        assert all(r["count"] == 0 for r in results)
