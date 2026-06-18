#!/usr/bin/env python3
"""Tests for calibration_tracker.py — save, enrich, and load calibration records."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest


# ─── Fixtures ────────────────────────────────────────────────────────────────


def _make_scan_result(mean=42.0, std=1.1, **overrides):
    """Build a minimal scan_result dict for testing."""
    base = {
        "mean": mean,
        "std": std,
        "total_count": 194,
        "kde_bandwidth": 0.8,
        "is_bimodal": False,
        "p10": mean - 1.5,
        "p25": mean - 0.8,
        "p50": mean,
        "p75": mean + 0.8,
        "p90": mean + 1.5,
        "per_model_means": {
            "ecmwf_aifs025": mean + 0.2,
            "ecmwf_ifs025": mean - 0.1,
            "gfs_seamless": mean,
            "icon_seamless": mean + 0.5,
            "gem_global": mean - 0.3,
        },
        "per_model_stds": {
            "ecmwf_aifs025": 0.9,
            "ecmwf_ifs025": 1.0,
            "gfs_seamless": 1.2,
            "icon_seamless": 1.1,
            "gem_global": 1.3,
        },
        "per_model_counts": {
            "ecmwf_aifs025": 51,
            "ecmwf_ifs025": 51,
            "gfs_seamless": 31,
            "icon_seamless": 40,
            "gem_global": 21,
        },
        "nws_forecast_high": mean + 1.0,
        "nws_physics_high": mean + 0.5,
        "nws_current_temp": mean - 5.0,
        "nws_wind_penalty": 0.0,
        "nws_wet_bulb_penalty": 0.0,
        "nws_temp_trend": "on_track",
        "bracket_prices": {},
    }
    base.update(overrides)
    return base


def _make_opp(low=42, high=44, kde_prob=0.45, edge_after_fees=0.15,
              confidence_score=92, trade_score=0.72, side="yes"):
    """Build a mock opportunity SimpleNamespace."""
    return SimpleNamespace(
        low=low,
        high=high,
        kde_prob=kde_prob,
        edge_after_fees=edge_after_fees,
        confidence_score=confidence_score,
        trade_score=trade_score,
        trade_score_components={"tradeable": True},
        side=side,
        strategies=["B:WIND"],
    )


@pytest.fixture(autouse=True)
def _redirect_paths(tmp_path, monkeypatch):
    """Redirect calibration_tracker file I/O to tmp_path."""
    import calibration_tracker
    monkeypatch.setattr(calibration_tracker, "CALIBRATION_DIR", tmp_path / "cal")
    monkeypatch.setattr(calibration_tracker, "CALIBRATION_LOG", tmp_path / "cal_log.jsonl")
    yield


# ─── CalibrationRecord dataclass ─────────────────────────────────────────────


class TestCalibrationRecord:
    """Verify dataclass construction and defaults."""

    def test_create_record(self):
        from calibration_tracker import CalibrationRecord
        rec = CalibrationRecord(
            date="2026-02-14", city="NYC", scan_time="2026-02-14T10:00:00Z",
            lead_time_hours=18.5,
            mean=42.0, std=1.1, total_count=194, kde_bandwidth=0.8, is_bimodal=False,
            p10=40.5, p25=41.2, p50=42.0, p75=42.8, p90=43.5,
            per_model_means={}, per_model_stds={}, per_model_counts={},
            nws_forecast_high=43.0, nws_physics_high=42.5, nws_current_temp=37.0,
            nws_wind_penalty=0.0, nws_wet_bulb_penalty=0.0, nws_temp_trend="on_track",
            bracket_prices={},
            confidence_score=92, trade_score=0.72, trade_score_tradeable=True,
            best_bracket="42-43", best_kde_prob=0.45, best_edge_cents=15.0,
            best_side="yes", strategies_active=["B:WIND"],
            aifs_ifs_divergence_f=0.3,
        )
        assert rec.city == "NYC"
        assert rec.actual_high is None  # default
        assert rec.prediction_correct is None

    def test_optional_fields_default_none(self):
        from calibration_tracker import CalibrationRecord
        rec = CalibrationRecord(
            date="2026-02-14", city="CHI", scan_time="T", lead_time_hours=10.0,
            mean=40.0, std=1.0, total_count=194, kde_bandwidth=0.8, is_bimodal=False,
            p10=38.5, p25=39.2, p50=40.0, p75=40.8, p90=41.5,
            per_model_means={}, per_model_stds={}, per_model_counts={},
            nws_forecast_high=41.0, nws_physics_high=40.5, nws_current_temp=35.0,
            nws_wind_penalty=0.0, nws_wet_bulb_penalty=0.0, nws_temp_trend="on_track",
            bracket_prices={},
            confidence_score=0, trade_score=0.0, trade_score_tradeable=False,
            best_bracket="", best_kde_prob=0.0, best_edge_cents=0.0,
            best_side="", strategies_active=[],
            aifs_ifs_divergence_f=0.0,
        )
        assert rec.actual_high is None
        assert rec.model_errors is None


# ─── save_calibration_record ─────────────────────────────────────────────────


class TestSaveCalibrationRecord:
    """Test saving scan results to disk."""

    def test_save_with_opportunity(self, tmp_path):
        from calibration_tracker import save_calibration_record, CALIBRATION_DIR, CALIBRATION_LOG

        scan = _make_scan_result()
        opp = _make_opp()
        result = save_calibration_record("NYC", scan, [opp], [], hours_to_settlement=18.0)

        assert result is not None
        assert result.city == "NYC"
        assert result.confidence_score == 92
        assert result.best_bracket == "42-43"

        # Verify JSON file was written
        json_files = list(CALIBRATION_DIR.glob("*.json"))
        assert len(json_files) == 1
        data = json.loads(json_files[0].read_text())
        assert data["city"] == "NYC"
        assert data["mean"] == 42.0

        # Verify JSONL log was appended
        assert CALIBRATION_LOG.exists()
        lines = CALIBRATION_LOG.read_text().strip().split("\n")
        assert len(lines) == 1

    def test_save_no_opportunities(self, tmp_path):
        from calibration_tracker import save_calibration_record

        scan = _make_scan_result()
        result = save_calibration_record("DEN", scan, [], [], hours_to_settlement=20.0)

        assert result is not None
        assert result.confidence_score == 0
        assert result.best_bracket == ""
        assert result.best_side == ""

    def test_save_picks_highest_trade_score(self, tmp_path):
        from calibration_tracker import save_calibration_record

        scan = _make_scan_result()
        opp_low = _make_opp(trade_score=0.40, confidence_score=80)
        opp_high = _make_opp(trade_score=0.85, confidence_score=95, low=44, high=46)

        result = save_calibration_record("NYC", scan, [opp_low, opp_high], [], hours_to_settlement=12.0)
        assert result is not None
        assert result.trade_score == 0.85
        assert result.best_bracket == "44-45"

    def test_aifs_ifs_divergence_computed(self, tmp_path):
        from calibration_tracker import save_calibration_record

        scan = _make_scan_result()
        scan["per_model_means"]["ecmwf_aifs025"] = 44.0
        scan["per_model_means"]["ecmwf_ifs025"] = 41.5

        result = save_calibration_record("NYC", scan, [], [], hours_to_settlement=15.0)
        assert result is not None
        assert result.aifs_ifs_divergence_f == 2.5


# ─── enrich_with_settlement ──────────────────────────────────────────────────


class TestEnrichWithSettlement:
    """Test post-settlement enrichment of saved records."""

    def test_enrich_correct_prediction(self, tmp_path):
        from calibration_tracker import save_calibration_record, enrich_with_settlement

        scan = _make_scan_result(mean=42.0)
        opp = _make_opp(low=42, high=44)  # bracket "42-43", range [42, 44)
        result = save_calibration_record("NYC", scan, [opp], [], hours_to_settlement=18.0)
        assert result is not None

        date_str = result.date
        enriched = enrich_with_settlement(date_str, "NYC", actual_high=42.5)
        assert enriched is not None
        assert enriched["actual_high"] == 42.5
        assert enriched["prediction_correct"] is True
        assert enriched["actual_bracket"] == "42-43"

    def test_enrich_incorrect_prediction(self, tmp_path):
        from calibration_tracker import save_calibration_record, enrich_with_settlement

        scan = _make_scan_result(mean=42.0)
        opp = _make_opp(low=42, high=44)  # bracket "42-43"
        result = save_calibration_record("NYC", scan, [opp], [], hours_to_settlement=18.0)
        assert result is not None

        enriched = enrich_with_settlement(result.date, "NYC", actual_high=38.0)
        assert enriched is not None
        assert enriched["prediction_correct"] is False

    def test_enrich_computes_model_errors(self, tmp_path):
        from calibration_tracker import save_calibration_record, enrich_with_settlement

        scan = _make_scan_result(mean=42.0)
        result = save_calibration_record("MIA", scan, [], [], hours_to_settlement=20.0)
        assert result is not None

        enriched = enrich_with_settlement(result.date, "MIA", actual_high=40.0)
        assert enriched is not None
        errors = enriched["model_errors"]
        # ecmwf_aifs025 mean was 42.2, actual 40.0 -> error = +2.2
        assert abs(errors["ecmwf_aifs025"] - 2.2) < 0.01

    def test_enrich_missing_record_returns_none(self, tmp_path):
        from calibration_tracker import enrich_with_settlement
        result = enrich_with_settlement("2099-01-01", "NYC", actual_high=50.0)
        assert result is None


# ─── load_calibration_records ────────────────────────────────────────────────


class TestLoadCalibrationRecords:
    """Test loading and filtering from the JSONL log."""

    def _seed_records(self, cities_dates):
        """Save multiple records for testing load/filter."""
        from calibration_tracker import save_calibration_record
        import calibration_tracker
        from datetime import datetime

        for city, date_str in cities_dates:
            scan = _make_scan_result()
            # Patch the "today" to match desired date
            fake_now = datetime.fromisoformat(f"{date_str}T12:00:00+00:00")
            with patch.object(calibration_tracker, "datetime") as mock_dt:
                mock_dt.now.return_value = fake_now
                mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
                save_calibration_record(city, scan, [], [], hours_to_settlement=10.0)

    def test_load_all(self, tmp_path):
        from calibration_tracker import load_calibration_records
        self._seed_records([("NYC", "2026-02-10"), ("CHI", "2026-02-11")])

        records = load_calibration_records()
        assert len(records) == 2

    def test_load_filter_city(self, tmp_path):
        from calibration_tracker import load_calibration_records
        self._seed_records([("NYC", "2026-02-10"), ("CHI", "2026-02-11")])

        records = load_calibration_records(city_key="NYC")
        assert len(records) == 1
        assert records[0]["city"] == "NYC"

    def test_load_filter_date_range(self, tmp_path):
        from calibration_tracker import load_calibration_records
        self._seed_records([
            ("NYC", "2026-02-08"),
            ("NYC", "2026-02-10"),
            ("NYC", "2026-02-12"),
        ])

        records = load_calibration_records(start_date="2026-02-09", end_date="2026-02-11")
        assert len(records) == 1
        assert records[0]["date"] == "2026-02-10"

    def test_load_empty_log(self, tmp_path):
        from calibration_tracker import load_calibration_records
        records = load_calibration_records()
        assert records == []

    def test_deduplication_keeps_latest(self, tmp_path):
        """When the same (date, city) appears twice, keep the last entry."""
        from calibration_tracker import load_calibration_records, CALIBRATION_LOG
        CALIBRATION_LOG.parent.mkdir(parents=True, exist_ok=True)

        # Write two entries for same date/city, second one enriched
        base = {"date": "2026-02-14", "city": "NYC", "actual_high": None}
        enriched = {"date": "2026-02-14", "city": "NYC", "actual_high": 42.0}
        with open(CALIBRATION_LOG, "w") as f:
            f.write(json.dumps(base) + "\n")
            f.write(json.dumps(enriched) + "\n")

        records = load_calibration_records()
        assert len(records) == 1
        assert records[0]["actual_high"] == 42.0


# ─── _temp_to_bracket ────────────────────────────────────────────────────────


class TestTempToBracket:
    """Test temperature to bracket conversion."""

    def test_even_temp(self):
        from calibration_tracker import _temp_to_bracket
        assert _temp_to_bracket(42.0) == "42-43"

    def test_odd_temp(self):
        from calibration_tracker import _temp_to_bracket
        assert _temp_to_bracket(43.5) == "42-43"

    def test_boundary_temp(self):
        from calibration_tracker import _temp_to_bracket
        assert _temp_to_bracket(44.0) == "44-45"
