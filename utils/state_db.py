#!/usr/bin/env python3
"""
STATE DB — SQLite-backed audit log and idempotency key registry.

Runs alongside position_store.py (does NOT replace it). Owns two concerns:

  1. Audit log  — structured, rotating record of every trade event.
                  Replaces the unbounded sniper_trades.jsonl pattern.
                  Pruned automatically to AUDIT_RETAIN_DAYS.

  2. Order registry — maps client_order_id (UUID we generate pre-flight)
                      to Kalshi order_id (returned post-fill).
                      Guards against duplicate orders on network timeout.

WAL journal mode allows concurrent readers while a writer holds the lock,
which is safe for the async + cron process mix in this project.

Usage:
    from utils.state_db import get_db

    db = get_db()
    db.write_audit("ORDER_PLACED", ticker="KXHIGHNY-...", payload={...})
    db.register_order(client_order_id="<uuid>", ticker="...", side="yes",
                      count=20, price=18, is_paper=False)
    db.confirm_order(client_order_id="<uuid>", kalshi_order_id="ord_...")
    db.is_duplicate(client_order_id="<uuid>")  -> bool
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from log_setup import get_logger

logger = get_logger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / "weather_edge.db"

AUDIT_RETAIN_DAYS = 90


class StateDB:
    def __init__(self, db_path: Path = _DEFAULT_DB_PATH) -> None:
        self.db_path = db_path
        self._init_schema()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")   # crash-safe with WAL; faster than FULL
        conn.execute("PRAGMA foreign_keys=ON;")
        try:
            yield conn
            conn.commit()
        except Exception as exc:
            conn.rollback()
            if "locked" in str(exc).lower():
                logger.warning("StateDB contention (database locked): %s", exc)
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS order_registry (
                    client_order_id  TEXT PRIMARY KEY,
                    kalshi_order_id  TEXT,
                    ticker           TEXT NOT NULL,
                    side             TEXT NOT NULL CHECK(side IN ('yes','no')),
                    count            INTEGER NOT NULL,
                    price            INTEGER NOT NULL,
                    is_paper         INTEGER NOT NULL DEFAULT 0,
                    status           TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','resting','open','rejected','closed')),
                    created_at       TEXT NOT NULL,
                    updated_at       TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS audit_log (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts         TEXT NOT NULL,
                    event      TEXT NOT NULL,
                    ticker     TEXT NOT NULL,
                    payload    TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts  ON audit_log(ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_reg_ticker ON order_registry(ticker, status)")

    # ── Idempotency / order registry ─────────────────────────────────────────

    def is_duplicate(self, client_order_id: str) -> bool:
        """Return True if this client_order_id was already submitted.

        Call before every place_order(). If True, skip — Kalshi already has
        (or had) this order. Do not retry.
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM order_registry WHERE client_order_id = ?",
                (client_order_id,),
            ).fetchone()
        return row is not None

    def register_order(
        self,
        *,
        client_order_id: str,
        ticker: str,
        side: str,
        count: int,
        price: int,
        is_paper: bool = False,
    ) -> None:
        """Insert a new pending order record before calling place_order().

        If the entry already exists (duplicate UUID — should never happen),
        this is a no-op. The caller should check is_duplicate() first.
        """
        now = _now()
        with self._conn() as conn:
            conn.execute("""
                INSERT OR IGNORE INTO order_registry
                    (client_order_id, ticker, side, count, price, is_paper,
                     status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """, (client_order_id, ticker, side, count, price, int(is_paper), now, now))

    def confirm_order(
        self,
        client_order_id: str,
        kalshi_order_id: str,
        status: str = "open",
    ) -> None:
        """Record the Kalshi-assigned order_id after a successful API response."""
        now = _now()
        with self._conn() as conn:
            conn.execute("""
                UPDATE order_registry
                SET kalshi_order_id = ?, status = ?, updated_at = ?
                WHERE client_order_id = ?
            """, (kalshi_order_id, status, now, client_order_id))

    def reject_order(self, client_order_id: str) -> None:
        """Mark an order as rejected so it doesn't block future UUIDs."""
        now = _now()
        with self._conn() as conn:
            conn.execute("""
                UPDATE order_registry
                SET status = 'rejected', updated_at = ?
                WHERE client_order_id = ?
            """, (now, client_order_id))

    # ── Audit log ─────────────────────────────────────────────────────────────

    def write_audit(
        self,
        event: str,
        ticker: str,
        payload: dict[str, Any],
    ) -> None:
        """Append a structured audit record. Events are free-form strings.

        Suggested conventions:
            ORDER_PLACED, ORDER_CONFIRMED, ORDER_REJECTED, ORPHANED_ORDER,
            POSITION_CLOSED, EXIT_FREEROLL, EXIT_EFFICIENCY, EXIT_TRAILING,
            EXIT_THESIS_BREAK, SCAN_RESULT
        """
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO audit_log (ts, event, ticker, payload)
                VALUES (?, ?, ?, ?)
            """, (_now(), event, ticker, json.dumps(payload, default=str)))

    def prune_audit(self, retain_days: int = AUDIT_RETAIN_DAYS) -> int:
        """Delete audit records older than retain_days. Returns rows deleted."""
        cutoff = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        from datetime import timedelta
        cutoff -= timedelta(days=retain_days)
        cutoff_str = cutoff.isoformat()
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM audit_log WHERE ts < ?", (cutoff_str,)
            )
            deleted = cur.rowcount
        if deleted:
            logger.info("Pruned %d audit records older than %d days", deleted, retain_days)
        return deleted


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Module-level singleton — import and use directly.
# Tests can instantiate StateDB(db_path=tmp_path) independently.
_db: StateDB | None = None


def get_db(db_path: Path = _DEFAULT_DB_PATH) -> StateDB:
    global _db
    if _db is None:
        _db = StateDB(db_path)
    return _db
