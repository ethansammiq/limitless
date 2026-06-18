#!/usr/bin/env python3
"""
HEARTBEAT — Cron job health monitoring with staleness detection.

Write side:  write_heartbeat(service_name) — called by each cron job on success.
Read side:   check_heartbeats() — returns list of stale/missing services.
Alert side:  monitor_heartbeats() — sends Discord alert for stale services.

Expected services and their max-age thresholds:
  auto_scan:           90 min  (runs every 60 min)
  position_monitor:    20 min  (runs every 10 min)
  backtest_collector:  26 hours (runs daily at 8 AM)
  morning_check:       26 hours (runs daily at 6 AM)

Cron setup for monitoring (runs every 30 min):
  */30 * * * * cd /Users/miqadmin/Documents/limitless && python3 heartbeat.py --check >> /tmp/heartbeat_monitor.log 2>&1
"""

import asyncio
import fcntl
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
PROJECT_ROOT = Path(__file__).resolve().parent
HEARTBEAT_FILE = PROJECT_ROOT / "heartbeats.json"
HEARTBEAT_LOCK = PROJECT_ROOT / ".heartbeats.lock"

# Service name → max age in minutes before considered stale
EXPECTED_SERVICES: Dict[str, int] = {
    "auto_scan": 90,             # Runs hourly, alert if >90 min stale
    "position_monitor": 20,      # Runs every 10 min, alert if >20 min stale
    "backtest_collector": 26 * 60,  # Runs daily, alert if >26 hours stale
    "morning_check": 26 * 60,       # Runs daily, alert if >26 hours stale
}


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


def check_heartbeats() -> List[Tuple[str, str, float]]:
    """Check all expected services for staleness.

    Returns list of (service_name, status, age_minutes) for stale/missing services.
    Status is one of: "stale", "missing", "never_seen".
    Empty list means all services are healthy.
    """
    now = datetime.now(ET)
    heartbeats = read_heartbeats()
    problems = []

    for service, max_age_min in EXPECTED_SERVICES.items():
        entry = heartbeats.get(service)

        if entry is None:
            problems.append((service, "never_seen", -1))
            continue

        ts_str = entry.get("timestamp", "")
        if not ts_str:
            problems.append((service, "missing", -1))
            continue

        try:
            last_beat = datetime.fromisoformat(ts_str)
            # Ensure timezone-aware comparison
            if last_beat.tzinfo is None:
                last_beat = last_beat.replace(tzinfo=ET)
            age = now - last_beat
            age_minutes = age.total_seconds() / 60

            if age_minutes > max_age_min:
                problems.append((service, "stale", round(age_minutes, 1)))
        except (ValueError, TypeError):
            problems.append((service, "parse_error", -1))

    return problems


async def monitor_heartbeats(quiet: bool = False):
    """Check heartbeats and send Discord alert if any service is stale.

    Designed to be called from cron every 30 minutes.
    Only alerts when problems are found to avoid notification fatigue.
    """
    problems = check_heartbeats()

    if not problems:
        if not quiet:
            print(f"  All {len(EXPECTED_SERVICES)} services healthy ✓")
        return

    # Build alert
    lines = []
    for service, status, age_min in problems:
        if status == "never_seen":
            lines.append(f"• **{service}**: never reported a heartbeat")
        elif status == "missing":
            lines.append(f"• **{service}**: heartbeat entry has no timestamp")
        elif status == "parse_error":
            lines.append(f"• **{service}**: timestamp parse error")
        else:
            max_age = EXPECTED_SERVICES.get(service, 0)
            if age_min >= 60:
                age_str = f"{age_min / 60:.1f} hours"
            else:
                age_str = f"{age_min:.0f} min"
            lines.append(f"• **{service}**: last seen {age_str} ago (max: {max_age} min)")

    description = "\n".join(lines)
    print(f"\n  ⚠ STALE SERVICES ({len(problems)}):")
    for line in lines:
        print(f"    {line}")

    # Send Discord alert
    try:
        from notifications import send_discord_alert
        await send_discord_alert(
            title=f"⚠ {len(problems)} Service(s) Stale — Cron Health Check",
            description=description,
            color=0xFF6600,
            context="heartbeat_monitor",
        )
        print("  Discord alert sent")
    except Exception as e:
        print(f"  Failed to send Discord alert: {e}")

    # Also write heartbeat for the monitor itself
    write_heartbeat("heartbeat_monitor")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Heartbeat Monitor — Cron job health check")
    parser.add_argument("--check", action="store_true", help="Check all services and alert on stale")
    parser.add_argument("--status", action="store_true", help="Print status of all services (no alert)")
    args = parser.parse_args()

    if args.check:
        asyncio.run(monitor_heartbeats())
    elif args.status:
        heartbeats = read_heartbeats()
        now = datetime.now(ET)
        print(f"\n  HEARTBEAT STATUS — {now.strftime('%I:%M %p ET')}")
        print(f"  {'─' * 50}")
        for service, max_age in EXPECTED_SERVICES.items():
            entry = heartbeats.get(service)
            if entry is None:
                print(f"  {service:<25s} NEVER SEEN")
                continue
            ts_str = entry.get("timestamp", "")
            try:
                last = datetime.fromisoformat(ts_str)
                if last.tzinfo is None:
                    last = last.replace(tzinfo=ET)
                age = (now - last).total_seconds() / 60
                healthy = "✓" if age <= max_age else "✗ STALE"
                if age >= 60:
                    age_str = f"{age / 60:.1f}h ago"
                else:
                    age_str = f"{age:.0f}m ago"
                print(f"  {service:<25s} {age_str:<12s} (max {max_age}m) {healthy}")
            except (ValueError, TypeError):
                print(f"  {service:<25s} PARSE ERROR")
        print(f"  {'─' * 50}")
    else:
        parser.print_help()
