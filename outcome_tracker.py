#!/usr/bin/env python3
"""
OUTCOME TRACKER — Logs predicted vs actual for calibration.

Records every trade decision (entry) with full prediction context:
  - confidence_score, trade_score, edge, kde_prob
  - ticker, bracket, city, side, entry_price
  - hours_to_settlement

After settlement, backtest_collector.py records actual_high.
Joining on (date, city) gives predicted-vs-actual pairs for:
  1. Confidence calibration (does 90 confidence = 90% win rate?)
  2. Trade score validation (does TS=0.70 beat TS=0.55?)
  3. Edge accuracy (is predicted 15c edge actually realized?)

Usage:
  from outcome_tracker import log_trade_prediction
  log_trade_prediction(opp, trade_score_obj, hours_to_settlement, entry_price)

Data: backtest/trade_predictions.jsonl
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from log_setup import get_logger

logger = get_logger(__name__)

ET = ZoneInfo("America/New_York")
PROJECT_ROOT = Path(__file__).resolve().parent
PREDICTIONS_FILE = PROJECT_ROOT / "backtest" / "trade_predictions.jsonl"


def log_trade_prediction(
    opp,
    trade_score=None,
    hours_to_settlement: float = 0.0,
    entry_price: int = 0,
    action: str = "entry",
) -> None:
    """Append a trade prediction record for later calibration.

    Parameters
    ----------
    opp : Opportunity-like object
        Must have: ticker, city, bracket_title, confidence_score,
        kde_prob, edge_after_fees, side, yes_bid, yes_ask, volume.
    trade_score : TradeScore, optional
        If TRADE_SCORE_ENABLED, the computed TradeScore object.
    hours_to_settlement : float
        Hours until settlement at time of trade.
    entry_price : int
        Actual entry price in cents.
    action : str
        "entry" for new trade, "reentry" for re-entry after trailing stop.
    """
    now = datetime.now(ET)
    record = {
        "timestamp": now.isoformat(),
        "date": now.strftime("%Y-%m-%d"),
        "action": action,
        # Identification
        "ticker": getattr(opp, "ticker", ""),
        "city": getattr(opp, "city", ""),
        "bracket_title": getattr(opp, "bracket_title", ""),
        "side": getattr(opp, "side", ""),
        "bracket_low": getattr(opp, "low", 0),
        "bracket_high": getattr(opp, "high", 0),
        # Predicted signals
        "confidence_score": round(getattr(opp, "confidence_score", 0), 1),
        "kde_prob": round(getattr(opp, "kde_prob", 0), 4),
        "edge_after_fees": round(getattr(opp, "edge_after_fees", 0), 4),
        "histogram_prob": round(getattr(opp, "histogram_prob", 0), 4),
        "weighted_prob": round(getattr(opp, "weighted_prob", 0), 4),
        # Market context
        "yes_bid": getattr(opp, "yes_bid", 0),
        "yes_ask": getattr(opp, "yes_ask", 0),
        "volume": getattr(opp, "volume", 0),
        "entry_price": entry_price,
        # Timing
        "hours_to_settlement": round(hours_to_settlement, 2),
    }

    # Trade score breakdown (if available)
    if trade_score is not None:
        record["trade_score"] = round(getattr(trade_score, "score", 0), 4)
        record["ts_confidence_signal"] = round(getattr(trade_score, "confidence_signal", 0), 4)
        record["ts_edge_signal"] = round(getattr(trade_score, "edge_signal", 0), 4)
        record["ts_urgency_signal"] = round(getattr(trade_score, "urgency_signal", 0), 4)
        record["ts_liquidity_penalty"] = round(getattr(trade_score, "liquidity_penalty", 0), 4)
        record["ts_entry_price_penalty"] = round(getattr(trade_score, "entry_price_penalty", 0), 4)
        record["ts_w_confidence"] = round(getattr(trade_score, "w_confidence", 0), 4)
        record["ts_w_edge"] = round(getattr(trade_score, "w_edge", 0), 4)
        record["ts_w_urgency"] = round(getattr(trade_score, "w_urgency", 0), 4)

    try:
        PREDICTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(PREDICTIONS_FILE, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        logger.warning("Failed to log trade prediction: %s", e)
