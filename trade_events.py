#!/usr/bin/env python3
"""
TRADE EVENTS — Structured JSONL event logging for trade lifecycle.

Emits machine-parseable events to logs/trade_events.jsonl for every
important action: trades, exits, scans, decisions, errors.

Design goals:
  - Append-only JSONL (one JSON object per line) for easy grep/jq analysis
  - Every event has: timestamp, event_type, source module, and payload
  - Rotates at 10 MB, keeps 10 backups (more history than general log)
  - Zero side effects beyond file I/O — safe to call from any context

Usage:
    from trade_events import log_event, TradeEvent

    log_event(TradeEvent.TRADE_EXECUTED, "auto_trader", {
        "ticker": "KXHIGHNY-...", "side": "yes", "price": 34, "qty": 5,
    })

Querying (from terminal):
    # All trade executions today
    cat logs/trade_events.jsonl | jq 'select(.event == "trade_executed")'

    # All exits with P&L
    cat logs/trade_events.jsonl | jq 'select(.event | startswith("exit_"))'

    # Errors in last hour
    cat logs/trade_events.jsonl | jq 'select(.event == "error")'
"""

import json
import logging
from datetime import datetime
from enum import Enum
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
PROJECT_ROOT = Path(__file__).resolve().parent
LOG_DIR = PROJECT_ROOT / "logs"
EVENT_LOG_FILE = LOG_DIR / "trade_events.jsonl"

MAX_BYTES = 10 * 1024 * 1024  # 10 MB
BACKUP_COUNT = 10

__all__ = ["TradeEvent", "log_event"]


class TradeEvent(str, Enum):
    """Canonical event types for the trade lifecycle."""

    # ── Trade entry ──
    TRADE_EXECUTED = "trade_executed"
    TRADE_FAILED = "trade_failed"
    TRADE_SKIPPED = "trade_skipped"
    TRADE_REENTRY = "trade_reentry"

    # ── Position exits ──
    EXIT_EFFICIENCY = "exit_efficiency"
    EXIT_FREEROLL = "exit_freeroll"
    EXIT_MID_PROFIT = "exit_mid_profit"
    EXIT_QUICK_PROFIT = "exit_quick_profit"
    EXIT_TRAILING_STOP = "exit_trailing_stop"
    EXIT_THESIS_BREAK = "exit_thesis_break"
    EXIT_ROI_BACKSTOP = "exit_roi_backstop"
    EXIT_SETTLED = "exit_settled"

    # ── Position management ──
    POSITION_PROMOTED = "position_promoted"       # resting -> open
    POSITION_CANCELLED = "position_cancelled"     # resting -> cancelled
    SELL_CONFIRMED = "sell_confirmed"             # pending_sell -> closed
    SELL_EXPIRED = "sell_expired"                 # stale sell cancelled
    SETTLEMENT_HOLD = "settlement_hold"           # efficiency hold for $1
    BOT_WINDOW_PULL = "bot_window_pull"           # order pulled before DSM

    # ── Scans ──
    SCAN_STARTED = "scan_started"
    SCAN_CITY_COMPLETE = "scan_city_complete"
    SCAN_CITY_FAILED = "scan_city_failed"
    SCAN_COMPLETE = "scan_complete"

    # ── Morning check ──
    MORNING_CHECK_DECISION = "morning_check_decision"

    # ── Safety ──
    KILL_SWITCH_ACTIVE = "kill_switch_active"
    SAFETY_CHECK_FAILED = "safety_check_failed"
    CIRCUIT_BREAKER = "circuit_breaker"
    INTRADAY_DRAWDOWN = "intraday_drawdown"

    # ── System ──
    ERROR = "error"
    PREFLIGHT_FAILED = "preflight_failed"
    HEARTBEAT = "heartbeat"

    # ── Confidence updates ──
    CONFIDENCE_UPDATED = "confidence_updated"
    NEAR_MISS = "near_miss"                       # Close to threshold

    # ── Exit strategy upgrades ──
    EXIT_THESIS_TRIM = "exit_thesis_trim"          # Graduated trim (conf 40-70)
    EXIT_MOMENTUM_ALERT = "exit_momentum_alert"    # Large price drop detected
    SELL_REPRICED = "sell_repriced"                 # Pending sell repriced


# ── Logger setup (singleton) ──

_event_logger: logging.Logger | None = None


def _get_event_logger() -> logging.Logger:
    """Get or initialize the JSONL event logger.

    Separate from the main application logger — writes raw JSON lines
    without the standard log format prefix.
    """
    global _event_logger
    if _event_logger is not None:
        return _event_logger

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("trade_events")
    logger.setLevel(logging.INFO)
    logger.propagate = False  # Don't bubble to root (would double-log)

    # Only add handler if none exist (safe for re-import)
    if not logger.handlers:
        handler = RotatingFileHandler(
            EVENT_LOG_FILE,
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setLevel(logging.INFO)
        # Raw format — just the message (which is a JSON line)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)

    _event_logger = logger
    return logger


def log_event(
    event: TradeEvent,
    source: str,
    payload: dict | None = None,
) -> None:
    """Emit a structured event to the JSONL log.

    Parameters
    ----------
    event : TradeEvent
        The event type (e.g., TradeEvent.TRADE_EXECUTED).
    source : str
        The module name emitting the event (e.g., "position_monitor").
    payload : dict, optional
        Arbitrary key-value data for the event. All values must be
        JSON-serializable (use str() for datetimes, Decimals, etc.).
    """
    now = datetime.now(ET)
    record = {
        "ts": now.isoformat(),
        "ts_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "event": event.value,
        "source": source,
        **(payload or {}),
    }
    try:
        line = json.dumps(record, default=str, separators=(",", ":"))
        _get_event_logger().info(line)
    except Exception:
        # Never let event logging break the trading pipeline
        pass
