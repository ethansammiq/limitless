#!/usr/bin/env python3
"""
Watchdog — Alert if cron jobs stop running.

Checks heartbeats.json for stale services and sends Discord alerts.
Runs every 15 minutes via cron. Anti-spam: alerts at most once per hour.

Cron setup:
  */15 * * * * /usr/bin/python3 /Users/miqadmin/Documents/limitless/watchdog.py >> /tmp/watchdog.log 2>&1
"""

import asyncio
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from notifications import send_discord_alert

ET = ZoneInfo("America/New_York")
PROJECT_ROOT = Path(__file__).resolve().parent
HEARTBEAT_FILE = PROJECT_ROOT / "heartbeats.json"

# Expected heartbeat intervals per service (hours)
EXPECTED_INTERVALS = {
    "auto_scan": 7,            # Runs 4x daily, max gap ~5h. Allow 7h.
    "auto_trader": 7,          # Replaces auto_scan in cron. Same schedule.
    "position_monitor": 0.25,  # Runs every 5 min. Allow 15 min.
    "peak_monitor": 16,        # Runs every 10 min, 13-22 ET. Overnight gap ~14h. Allow 16h.
    "backtest_collector": 25,  # Runs daily at 8 AM. Allow 25h.
    "morning_check": 25,       # Runs daily at 6:30 AM. Allow 25h.
}

# Don't spam Discord — alert at most once per hour
ALERT_COOLDOWN_SECONDS = 3600


async def check_heartbeats():
    """Check all services for staleness and alert if needed."""
    now = datetime.now(ET)

    if not HEARTBEAT_FILE.exists():
        await send_discord_alert(
            "WATCHDOG: No heartbeat file",
            "heartbeats.json not found. No cron jobs have reported yet.",
            color=0xFF0000,
        )
        return

    heartbeats = {}
    try:
        heartbeats = json.loads(HEARTBEAT_FILE.read_text())
    except (json.JSONDecodeError, Exception):
        await send_discord_alert(
            "WATCHDOG: Corrupt heartbeat file",
            "heartbeats.json could not be parsed.",
            color=0xFF0000,
        )
        return

    # Anti-spam: check cooldown
    last_alert_str = heartbeats.get("_watchdog_last_alert", "")
    if last_alert_str:
        try:
            last_alert_time = datetime.fromisoformat(last_alert_str)
            if (now - last_alert_time).total_seconds() < ALERT_COOLDOWN_SECONDS:
                return  # Already alerted within the hour
        except (ValueError, TypeError):
            pass  # Invalid timestamp, proceed with check

    stale = []
    for service, max_hours in EXPECTED_INTERVALS.items():
        entry = heartbeats.get(service)
        if not entry:
            stale.append(f"**{service}**: NEVER reported")
            continue

        try:
            last_ts = datetime.fromisoformat(entry["timestamp"])
            age_hours = (now - last_ts).total_seconds() / 3600
            if age_hours > max_hours:
                stale.append(f"**{service}**: last seen {age_hours:.1f}h ago (limit: {max_hours}h)")
        except (ValueError, KeyError):
            stale.append(f"**{service}**: invalid timestamp")

    if stale:
        await send_discord_alert(
            "WATCHDOG ALERT: Cron jobs stale",
            "**Stale services:**\n" + "\n".join(f"- {s}" for s in stale),
            color=0xFF0000,
        )
        # Update cooldown timestamp
        heartbeats["_watchdog_last_alert"] = now.isoformat()
        HEARTBEAT_FILE.write_text(json.dumps(heartbeats, indent=2))
    else:
        print(f"  [{now.strftime('%H:%M')}] All services healthy.")


if __name__ == "__main__":
    asyncio.run(check_heartbeats())
