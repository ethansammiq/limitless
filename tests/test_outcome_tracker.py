#!/usr/bin/env python3
"""Tests for outcome_tracker.py — prediction logging for calibration."""

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from outcome_tracker import log_trade_prediction, PREDICTIONS_FILE


def _make_opp(**overrides):
    """Create a minimal opportunity-like object."""
    defaults = {
        "ticker": "KXHIGHNY-26FEB15-B36.5",
        "city": "NYC",
        "bracket_title": "36-38°F",
        "side": "yes",
        "confidence_score": 92.5,
        "kde_prob": 0.45,
        "edge_after_fees": 0.18,
        "histogram_prob": 0.42,
        "weighted_prob": 0.44,
        "yes_bid": 30,
        "yes_ask": 35,
        "volume": 120,
        "low": 36,
        "high": 38,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_trade_score(**overrides):
    """Create a minimal TradeScore-like object."""
    defaults = {
        "score": 0.72,
        "confidence_signal": 0.85,
        "edge_signal": 0.65,
        "urgency_signal": 0.70,
        "liquidity_penalty": 0.05,
        "entry_price_penalty": 0.02,
        "w_confidence": 0.40,
        "w_edge": 0.35,
        "w_urgency": 0.25,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class TestLogTradePrediction:
    """Test prediction logging writes correct JSONL records."""

    def test_basic_entry(self, tmp_path):
        """Basic entry prediction is logged with all fields."""
        pred_file = tmp_path / "trade_predictions.jsonl"
        opp = _make_opp()

        with patch("outcome_tracker.PREDICTIONS_FILE", pred_file):
            log_trade_prediction(opp, entry_price=32, hours_to_settlement=8.5)

        lines = pred_file.read_text().strip().split("\n")
        assert len(lines) == 1

        record = json.loads(lines[0])
        assert record["ticker"] == "KXHIGHNY-26FEB15-B36.5"
        assert record["city"] == "NYC"
        assert record["side"] == "yes"
        assert record["confidence_score"] == 92.5
        assert record["kde_prob"] == 0.45
        assert record["edge_after_fees"] == 0.18
        assert record["entry_price"] == 32
        assert record["hours_to_settlement"] == 8.5
        assert record["action"] == "entry"
        assert record["bracket_low"] == 36
        assert record["bracket_high"] == 38

    def test_with_trade_score(self, tmp_path):
        """Trade score components are included when provided."""
        pred_file = tmp_path / "trade_predictions.jsonl"
        opp = _make_opp()
        ts = _make_trade_score()

        with patch("outcome_tracker.PREDICTIONS_FILE", pred_file):
            log_trade_prediction(opp, trade_score=ts, entry_price=32)

        record = json.loads(pred_file.read_text().strip())
        assert record["trade_score"] == 0.72
        assert record["ts_confidence_signal"] == 0.85
        assert record["ts_edge_signal"] == 0.65
        assert record["ts_urgency_signal"] == 0.70
        assert record["ts_liquidity_penalty"] == 0.05
        assert record["ts_entry_price_penalty"] == 0.02
        assert record["ts_w_confidence"] == 0.40
        assert record["ts_w_edge"] == 0.35
        assert record["ts_w_urgency"] == 0.25

    def test_without_trade_score(self, tmp_path):
        """Without trade score, ts_ fields are absent."""
        pred_file = tmp_path / "trade_predictions.jsonl"
        opp = _make_opp()

        with patch("outcome_tracker.PREDICTIONS_FILE", pred_file):
            log_trade_prediction(opp, entry_price=32)

        record = json.loads(pred_file.read_text().strip())
        assert "trade_score" not in record
        assert "ts_confidence_signal" not in record

    def test_reentry_action(self, tmp_path):
        """Re-entry trades are logged with action='reentry'."""
        pred_file = tmp_path / "trade_predictions.jsonl"
        opp = _make_opp()

        with patch("outcome_tracker.PREDICTIONS_FILE", pred_file):
            log_trade_prediction(opp, entry_price=35, action="reentry")

        record = json.loads(pred_file.read_text().strip())
        assert record["action"] == "reentry"

    def test_appends_multiple_records(self, tmp_path):
        """Multiple calls append separate lines."""
        pred_file = tmp_path / "trade_predictions.jsonl"
        opp1 = _make_opp(ticker="KXHIGHNY-26FEB15-B36.5")
        opp2 = _make_opp(ticker="KXHIGHCHI-26FEB15-B28.5")

        with patch("outcome_tracker.PREDICTIONS_FILE", pred_file):
            log_trade_prediction(opp1, entry_price=30)
            log_trade_prediction(opp2, entry_price=25)

        lines = pred_file.read_text().strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["ticker"].endswith("NY-26FEB15-B36.5")
        assert json.loads(lines[1])["ticker"].endswith("CHI-26FEB15-B28.5")

    def test_has_timestamp_and_date(self, tmp_path):
        """Records include timestamp and date fields."""
        pred_file = tmp_path / "trade_predictions.jsonl"
        opp = _make_opp()

        with patch("outcome_tracker.PREDICTIONS_FILE", pred_file):
            log_trade_prediction(opp, entry_price=32)

        record = json.loads(pred_file.read_text().strip())
        assert "timestamp" in record
        assert "date" in record
        # Date should be YYYY-MM-DD format
        assert len(record["date"]) == 10
        assert record["date"].count("-") == 2

    def test_market_context_fields(self, tmp_path):
        """Market context (bid/ask/volume) is logged."""
        pred_file = tmp_path / "trade_predictions.jsonl"
        opp = _make_opp(yes_bid=28, yes_ask=33, volume=250)

        with patch("outcome_tracker.PREDICTIONS_FILE", pred_file):
            log_trade_prediction(opp, entry_price=30)

        record = json.loads(pred_file.read_text().strip())
        assert record["yes_bid"] == 28
        assert record["yes_ask"] == 33
        assert record["volume"] == 250

    def test_creates_directory(self, tmp_path):
        """Creates parent directory if it doesn't exist."""
        pred_file = tmp_path / "subdir" / "trade_predictions.jsonl"
        opp = _make_opp()

        with patch("outcome_tracker.PREDICTIONS_FILE", pred_file):
            log_trade_prediction(opp, entry_price=32)

        assert pred_file.exists()

    def test_handles_missing_attrs_gracefully(self, tmp_path):
        """Missing opp attributes default to safe values."""
        pred_file = tmp_path / "trade_predictions.jsonl"
        # Minimal object with almost no attributes
        opp = SimpleNamespace(ticker="TEST", city="NYC")

        with patch("outcome_tracker.PREDICTIONS_FILE", pred_file):
            log_trade_prediction(opp, entry_price=10)

        record = json.loads(pred_file.read_text().strip())
        assert record["ticker"] == "TEST"
        assert record["confidence_score"] == 0
        assert record["kde_prob"] == 0
