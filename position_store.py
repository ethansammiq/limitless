#!/usr/bin/env python3
"""
POSITION STORE — Atomic, locked position file operations.

Shared module used by execute_trade.py, position_monitor.py, and the broker
factory to safely read/write positions files without race conditions.

Paper and live mode use DIFFERENT positions files:
  - live mode: positions.json       (LIVE_POSITIONS_FILE)
  - paper:     positions_paper.json (PAPER_POSITIONS_FILE)

Callers that want the active mode can omit positions_file=... — the default
resolves via config.get_positions_file() which honors PAPER_TRADING_MODE.
Tests and administrative tools can pass positions_file=... explicitly to
target a specific file.

Features:
  - fcntl file locking (prevents concurrent writes from cron + manual runs)
  - Atomic writes (write to temp file, then os.rename)
  - Schema validation (catches corrupted or malformed entries)
  - Stale-lock recovery after LOCK_TIMEOUT_SEC
"""

import fcntl
import json
import os
import signal
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional, TypedDict
from zoneinfo import ZoneInfo

from log_setup import get_logger
from config import LIVE_POSITIONS_FILE
import config as _config  # for runtime access to PAPER_TRADING_MODE

logger = get_logger(__name__)

__all__ = [
    "PositionDict", "LockTimeoutError", "LOCK_TIMEOUT_SEC",
    "load_positions", "save_positions",
    "position_transaction", "register_position",
    "POSITIONS_FILE", "LOCK_FILE",
]


class ExitRulesDict(TypedDict, total=False):
    """Exit rule thresholds attached to each position."""
    freeroll_at: int
    efficiency_exit: int
    trailing_offset: int


class PositionDict(TypedDict, total=False):
    """Canonical schema for a position entry."""
    # ── Core (required) ──
    ticker: str
    side: str
    avg_price: float
    contracts: int
    status: str

    # ── Lifecycle ──
    original_contracts: int
    order_id: str
    entry_time: str
    freerolled: bool
    peak_price: int
    trailing_floor: int
    pnl_realized: float
    exit_rules: ExitRulesDict
    notes: list[str]

    # ── Set by auto_trader confidence updates ──
    last_confidence: float
    bracket_low: float
    bracket_high: float
    current_obs_temp: float
    trend: str

    # ── Sell tracking ──
    sell_placed_at: str
    sell_price: int

    # ── Averaging ──
    averaged_in: bool

    # ── Idempotency (set at order-placement time, never changes) ──
    client_order_id: str

    # ── Attribution (set at open time; legacy records lack it → "untagged") ──
    strategy: str


ET = ZoneInfo("America/New_York")
PROJECT_ROOT = Path(__file__).resolve().parent

# Backward-compat module-level constants. Point at the LIVE file; new code
# should call get_positions_file() or pass positions_file=... explicitly.
POSITIONS_FILE = LIVE_POSITIONS_FILE

REQUIRED_KEYS = {"ticker", "side", "avg_price", "contracts", "status"}
LOCK_TIMEOUT_SEC = 10


class LockTimeoutError(Exception):
    """Raised when file lock acquisition exceeds LOCK_TIMEOUT_SEC."""


def _lock_file_for(positions_file: Path) -> Path:
    """Derive a lock-file path from the positions-file path.

    positions.json       -> .positions.lock
    positions_paper.json -> .positions_paper.lock
    """
    return positions_file.parent / f".{positions_file.stem}.lock"


LOCK_FILE = _lock_file_for(POSITIONS_FILE)


def _default_positions_file() -> Path:
    """Resolve the default positions file, honoring runtime state.

    In paper mode, returns the paper file. Otherwise returns the module-level
    POSITIONS_FILE — which tests can monkeypatch to a temp path without
    needing to patch config internals.
    """
    if getattr(_config, "PAPER_TRADING_MODE", False):
        return _config.PAPER_POSITIONS_FILE
    return POSITIONS_FILE


def _validate_position(pos: dict) -> bool:
    if not isinstance(pos, dict):
        return False
    return REQUIRED_KEYS.issubset(pos.keys())


def _alarm_handler(signum, frame):
    raise LockTimeoutError(f"File lock acquisition timed out after {LOCK_TIMEOUT_SEC}s")


@contextmanager
def _file_lock(lock_file: Path):
    """Acquire exclusive file lock with timeout + stale-lock recovery."""
    lock_fd = open(lock_file, "w")
    try:
        old_handler = signal.signal(signal.SIGALRM, _alarm_handler)
        signal.alarm(LOCK_TIMEOUT_SEC)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
        except LockTimeoutError:
            logger.error(
                "Lock acquisition timed out after %ds on %s — possible stale lock. "
                "Force-removing and retrying.",
                LOCK_TIMEOUT_SEC, lock_file,
            )
            lock_fd.close()
            try:
                lock_file.unlink(missing_ok=True)
            except OSError:
                pass

            acquired = False
            for attempt in range(3):
                lock_fd = open(lock_file, "w")
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except (BlockingIOError, OSError):
                    lock_fd.close()
                    if attempt < 2:
                        time.sleep(0.5)
                        logger.warning("Lock NB retry %d/3 failed, retrying...", attempt + 1)

            if not acquired:
                logger.warning("NB retries exhausted — blocking acquire with fresh timeout")
                lock_fd = open(lock_file, "w")
                signal.alarm(LOCK_TIMEOUT_SEC)
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX)
                except LockTimeoutError:
                    lock_fd.close()
                    raise LockTimeoutError(
                        f"Lock unrecoverable after stale-lock removal and {LOCK_TIMEOUT_SEC}s retry"
                    )
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)
        yield
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except (ValueError, OSError):
            pass
        try:
            lock_fd.close()
        except (ValueError, OSError):
            pass


def _read_positions_unlocked(positions_file: Path) -> list[dict]:
    """Read and validate positions file. Caller MUST hold _file_lock."""
    if not positions_file.exists():
        return []
    try:
        raw = positions_file.read_text().strip()
        if not raw:
            return []
        positions = json.loads(raw)
        if not isinstance(positions, list):
            logger.warning(
                f"{positions_file.name} is not a list, got {type(positions).__name__}"
            )
            return []
        valid = []
        for i, p in enumerate(positions):
            if _validate_position(p):
                valid.append(p)
            else:
                logger.warning(f"Skipping invalid position entry at index {i}: missing keys")
        if len(valid) < len(positions):
            logger.warning(f"Filtered {len(positions) - len(valid)} invalid entries")
        return valid
    except json.JSONDecodeError as e:
        logger.error(f"{positions_file.name} is corrupted: {e}")
        backup = positions_file.with_suffix(f".corrupted.{int(datetime.now().timestamp())}")
        positions_file.rename(backup)
        logger.error(f"Corrupted file saved as {backup}")
        return []
    except Exception as e:
        logger.error(f"Failed to read {positions_file.name}: {e}")
        return []


def _write_positions_unlocked(positions: list[dict], positions_file: Path) -> None:
    """Atomically write positions file. Caller MUST hold _file_lock."""
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(
            dir=positions_file.parent,
            prefix=f".{positions_file.stem}_",
            suffix=".tmp",
        )
        with os.fdopen(fd, "w") as f:
            json.dump(positions, f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp_path, positions_file)
        logger.debug(f"Saved {len(positions)} positions atomically to {positions_file.name}")
    except Exception as e:
        logger.error(f"Failed to save positions to {positions_file.name}: {e}")
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def load_positions(positions_file: Optional[Path] = None) -> list[dict]:
    """Load positions with locking and validation.

    Defaults to the file dictated by PAPER_TRADING_MODE. Pass positions_file=...
    to target a specific file (useful for tests / admin tooling).
    """
    positions_file = positions_file or _default_positions_file()
    with _file_lock(_lock_file_for(positions_file)):
        return _read_positions_unlocked(positions_file)


def save_positions(positions: list[dict], positions_file: Optional[Path] = None) -> None:
    """Atomically save positions with locking."""
    positions_file = positions_file or _default_positions_file()
    with _file_lock(_lock_file_for(positions_file)):
        _write_positions_unlocked(positions, positions_file)


@contextmanager
def position_transaction(positions_file: Optional[Path] = None):
    """Transactional read-modify-write under a SINGLE lock.

    Usage:
        with position_transaction() as positions:
            for p in positions:
                if p["ticker"] == ticker:
                    p["status"] = "closed"
    """
    positions_file = positions_file or _default_positions_file()
    with _file_lock(_lock_file_for(positions_file)):
        positions = _read_positions_unlocked(positions_file)
        yield positions
        _write_positions_unlocked(positions, positions_file)


def register_position(
    ticker: str,
    side: str,
    price: int,
    quantity: int,
    order_id: str,
    status: str,
    positions_file: Optional[Path] = None,
    client_order_id: str = "",
    strategy: str = "untagged",
) -> None:
    """Register a new position (or average into existing) in the positions file.

    Called after a successful order placement. Uses a single lock transaction.
    Honors PAPER_TRADING_MODE unless positions_file is passed explicitly.

    strategy tags which subsystem opened the position ("auto_trader",
    "peak_trader", "manual") so realized P&L can be attributed per strategy.
    Averaging keeps the opener's tag.
    """
    with position_transaction(positions_file) as positions:
        existing = None
        for p in positions:
            if p["ticker"] == ticker and p["side"] == side and p["status"] == "open":
                existing = p
                break

        now = datetime.now(ET)
        freeroll_at = int(price * 2)

        if existing:
            old_qty = existing["contracts"]
            old_price = existing["avg_price"]
            new_total = old_qty + quantity

            if new_total <= 0:
                raise ValueError(
                    f"AVERAGING REJECTED on {ticker}: new_total={new_total} "
                    f"(old={old_qty} + new={quantity}) would be non-positive"
                )
            if old_qty <= 0 or old_price <= 0:
                raise ValueError(
                    f"AVERAGING REJECTED on {ticker}: existing position has "
                    f"invalid data (qty={old_qty}, price={old_price})"
                )

            new_avg = round((old_price * old_qty + price * quantity) / new_total, 1)
            direction = "DOWN" if price < old_price else "UP"
            logger.warning(
                "AVERAGING %s on %s: %dx@%dc → %dx@%.1fc (was %dx@%.1fc)",
                direction, ticker, quantity, price, new_total, new_avg, old_qty, old_price,
            )

            existing["avg_price"] = new_avg
            existing["contracts"] = new_total
            existing["original_contracts"] = new_total
            existing["averaged_in"] = True
            existing.setdefault("strategy", "untagged")
            existing.setdefault("exit_rules", {})["freeroll_at"] = int(new_avg * 2)
            existing.setdefault("notes", []).append(
                f"{now.isoformat()}: ⚠ AVERAGED {direction} — added {quantity}x @ {price}c (avg now {new_avg}c)"
            )
            logger.info(f"Updated existing position: {new_total}x @ {new_avg}c avg")
        else:
            pos_status = "resting" if status.upper() in ("RESTING", "PENDING") else "open"
            position = {
                "ticker": ticker,
                "side": side,
                "avg_price": price,
                "contracts": quantity,
                "original_contracts": quantity,
                "order_id": order_id,
                "client_order_id": client_order_id,
                "strategy": strategy,
                "status": pos_status,
                "entry_time": now.isoformat(),
                "freerolled": False,
                "peak_price": price,
                "trailing_floor": 0,
                "pnl_realized": 0.0,
                "exit_rules": {
                    "freeroll_at": freeroll_at,
                    "efficiency_exit": 90,
                    "trailing_offset": 8,
                },
                "notes": [
                    f"{now.isoformat()}: Opened {quantity}x {side.upper()} @ {price}c "
                    f"(order: {order_id}, cid: {client_order_id}, status: {status})"
                ],
            }
            positions.append(position)
            logger.info(
                f"Position registered: {quantity}x {side.upper()} {ticker} @ {price}c "
                f"(freeroll at {freeroll_at}c)"
            )
