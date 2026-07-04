#!/usr/bin/env python3
"""
HEARTBEAT — cron-job liveness ledger (write/read only).

Write side:  write_heartbeat(service_name) — called by each cron job on success.
Read side:   read_heartbeats() — raw ledger for checkers and dashboards.

Staleness checking, Discord alerting, and self-healing live in watchdog.py
(the single checker; its EXPECTED_INTERVALS is the canonical service list).
This module used to carry a second checker with its own EXPECTED_SERVICES —
the two lists drifted, so the checker side was folded into watchdog 2026-07.

Status view:
  python3 heartbeat.py --status
"""

import fcntl
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
PROJECT_ROOT = Path(__file__).resolve().parent
HEARTBEAT_FILE = PROJECT_ROOT / "heartbeats.json"
HEARTBEAT_LOCK = PROJECT_ROOT / ".heartbeats.lock"


def write_heartbeat(service_name: str):
    """Record a successful run timestamp for a service.

    Uses file locking to prevent concurrent cron jobs from corrupting
    the heartbeat file during simultaneous read-modify-write cycles.
    """
    lock_fd = open(HEARTBEAT_LOCK, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        heartbeats = {}
        if HEARTBEAT_FILE.exists():
            try:
                heartbeats = json.loads(HEARTBEAT_FILE.read_text())
            except (json.JSONDecodeError, Exception):
                heartbeats = {}

        heartbeats[service_name] = {
            "timestamp": datetime.now(ET).isoformat(),
            "service": service_name,
        }

        # Atomic write: temp file + rename
        fd, tmp_path = tempfile.mkstemp(
            dir=PROJECT_ROOT, prefix=".heartbeats_", suffix=".tmp"
        )
        with os.fdopen(fd, "w") as f:
            json.dump(heartbeats, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp_path, HEARTBEAT_FILE)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


def read_heartbeats() -> dict:
    """Read current heartbeat data. Returns empty dict if file missing."""
    if not HEARTBEAT_FILE.exists():
        return {}
    try:
        return json.loads(HEARTBEAT_FILE.read_text())
    except (json.JSONDecodeError, Exception):
        return {}


def _print_status() -> None:
    """Human status view against watchdog's canonical service list."""
    from watchdog import EXPECTED_INTERVALS  # lazy: watchdog imports this module

    heartbeats = read_heartbeats()
    now = datetime.now(ET)
    print(f"\n  HEARTBEAT STATUS — {now.strftime('%I:%M %p ET')}")
    print(f"  {'─' * 50}")
    for service, max_hours in EXPECTED_INTERVALS.items():
        entry = heartbeats.get(service)
        if entry is None:
            print(f"  {service:<25s} NEVER SEEN")
            continue
        try:
            last = datetime.fromisoformat(entry.get("timestamp", ""))
            if last.tzinfo is None:
                last = last.replace(tzinfo=ET)
            age_h = (now - last).total_seconds() / 3600
            healthy = "✓" if age_h <= max_hours else "✗ STALE"
            age_str = f"{age_h:.1f}h ago" if age_h >= 1 else f"{age_h * 60:.0f}m ago"
            print(f"  {service:<25s} {age_str:<12s} (max {max_hours}h) {healthy}")
        except (ValueError, TypeError):
            print(f"  {service:<25s} PARSE ERROR")
    print(f"  {'─' * 50}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Heartbeat ledger — status view")
    parser.add_argument("--status", action="store_true", help="Print status of all services")
    parser.add_argument("--check", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.check:
        parser.error("checking moved to watchdog.py (run: python3 watchdog.py)")
    elif args.status:
        _print_status()
    else:
        parser.print_help()
