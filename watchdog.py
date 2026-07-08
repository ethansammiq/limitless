#!/usr/bin/env python3
"""
Watchdog — Alert if cron jobs stop running, and self-heal missed daily jobs.

Checks heartbeats.json for stale services and sends Discord alerts.
If backtest_collector is stale and its cron window has already passed —
typically because the host slept/was down through the tick — the watchdog
re-spawns it via the venv python (pinned to the missed date), at most once
per service per day.

Runs every 15 minutes via cron. Anti-spam: alerts at most once per hour.

Cron setup:
  */15 * * * * /usr/bin/python3 /Users/miqadmin/Documents/limitless/watchdog.py >> /tmp/watchdog.log 2>&1
"""

import asyncio
import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from heartbeat import write_heartbeat
from notifications import send_discord_alert

ET = ZoneInfo("America/New_York")
PROJECT_ROOT = Path(__file__).resolve().parent
HEARTBEAT_FILE = PROJECT_ROOT / "heartbeats.json"
VENV_PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python3"
LOGS_DIR = PROJECT_ROOT / "logs"
CATCHUP_STATE_FILE = PROJECT_ROOT / "watchdog_catchup.json"

# Expected heartbeat intervals per service (hours).
# 2026-07-06: the KDE stack (auto_trader / position_monitor / auto_scan /
# morning_check / bias_collector) was deleted outright — KDE forecasting
# measured -EV in June; only the settlement-source jobs remain.
EXPECTED_INTERVALS = {
    "peak_monitor": 16,        # Runs every 10 min, 13-22 ET. Overnight gap ~14h. Allow 16h.
    "backtest_collector": 25,  # Runs daily at 8 AM. Allow 25h.
    "shadow_logger": 2,        # Runs every 30 min (beats even out of window). Allow 2h.
    "dead_bracket_sweeper": 2,  # Runs every 15 min (beats even when nothing found). Allow 2h.
    "live_watch": 2,            # Runs every 10 min (read-only live journal). Allow 2h.
    "weekly_digest": 192,       # Runs Sundays 18:00. Allow 8 days.
    "cli_sniper": 0.5,          # Runs every 2 min (beats even out of window). Allow 30 min.
    "sniper_scorecard": 192,    # Runs Sundays 17:45 (before the digest). Allow 8 days.
    "audit_coverage": 192,      # Runs Sundays 17:30. Allow 8 days.
    "wall_fingerprint": 192,    # Runs Sundays 17:15 (wall win rates). Allow 8 days.
    "export_public_stats": 2,   # Runs every 30 min (website snapshot). Allow 2h.
    "pre_window_briefing": 25,  # Runs daily 16:27 ET. Allow 25h.
}

# Daily cron jobs the watchdog can re-run when the host slept through the
# tick (Mac-era failure class; kept only for backtest_collector because the
# daily_data.jsonl settlement ground truth must not gap).
CATCHUP_JOBS: dict[str, tuple[str, float]] = {
    "backtest_collector": ("backtest_collector.py", 8.0),   # cron: 0 8 * * *
}

# Don't spam Discord — alert at most once per hour
ALERT_COOLDOWN_SECONDS = 3600


def _load_catchup_state() -> dict:
    """Per-day spawn guard: service -> date (YYYY-MM-DD) of last catch-up."""
    try:
        return json.loads(CATCHUP_STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _spawn_job(service: str, script: str, now: datetime, target_date: str | None = None) -> bool:
    """Fire-and-forget re-run of a missed daily job under the venv python.

    target_date pins the collector to the actually-missed date via --date, so a
    catch-up that drifts late in the day still targets the right day rather than
    'yesterday-relative-to-now'.
    """
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOGS_DIR / f"catchup_{service}_{now.strftime('%Y-%m-%d')}.log"
    cmd = [str(VENV_PYTHON), str(PROJECT_ROOT / script)]
    if target_date:
        cmd += ["--date", target_date]
    try:
        with open(log_path, "a") as log_file:
            subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                cwd=str(PROJECT_ROOT),
                start_new_session=True,
            )
        print(f"  Spawned catch-up for {service} → {log_path}")
        return True
    except OSError as e:
        print(f"  Failed to spawn catch-up for {service}: {e}")
        return False


def attempt_catchup(stale_services: list[str], now: datetime) -> list[str]:
    """Re-spawn sleep-missed daily jobs, at most once per service per day.

    Only fires when a job's heartbeat is stale AND its daily cron window has
    already passed — i.e. cron can no longer run it today on its own.
    Returns the services that were spawned.
    """
    state = _load_catchup_state()
    today = now.strftime("%Y-%m-%d")
    missed_date = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    hour_now = now.hour + now.minute / 60
    spawned: list[str] = []

    for service, (script, window_start) in CATCHUP_JOBS.items():
        if service not in stale_services:
            continue
        if hour_now <= window_start:
            continue  # cron may still fire today
        if state.get(service) == today:
            continue  # per-day guard: already retriggered

        # Pin the collector to the actually-missed date so a drifted catch-up
        # still backfills the right day.
        if _spawn_job(service, script, now, missed_date):
            state[service] = today
            spawned.append(service)

    if spawned:
        try:
            CATCHUP_STATE_FILE.write_text(json.dumps(state, indent=2))
        except OSError as e:
            print(f"  Failed to persist catch-up state: {e}")
    return spawned


def _in_alert_cooldown(heartbeats: dict, now: datetime) -> bool:
    """True if a stale-services alert was sent within the cooldown window."""
    entry = heartbeats.get("_watchdog_last_alert", "")
    # write_heartbeat stores {"timestamp": ...}; older versions stored a bare string
    ts_str = entry.get("timestamp", "") if isinstance(entry, dict) else entry
    if not ts_str:
        return False
    try:
        last_alert = datetime.fromisoformat(ts_str)
    except (ValueError, TypeError):
        return False
    return (now - last_alert).total_seconds() < ALERT_COOLDOWN_SECONDS


async def check_heartbeats():
    """Check services for staleness, self-heal missed daily jobs, alert."""
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

    stale = []
    stale_services = []
    for service, max_hours in EXPECTED_INTERVALS.items():
        entry = heartbeats.get(service)
        if not entry:
            stale.append(f"**{service}**: NEVER reported")
            stale_services.append(service)
            continue

        try:
            last_ts = datetime.fromisoformat(entry["timestamp"])
            age_hours = (now - last_ts).total_seconds() / 3600
            if age_hours > max_hours:
                stale.append(f"**{service}**: last seen {age_hours:.1f}h ago (limit: {max_hours}h)")
                stale_services.append(service)
        except (ValueError, KeyError):
            stale.append(f"**{service}**: invalid timestamp")

    # Self-heal sleep-missed daily crons. Runs even during the alert
    # cooldown — it has its own once-per-day guard.
    respawned = attempt_catchup(stale_services, now)
    if respawned:
        print(f"  [{now.strftime('%H:%M')}] Respawned missed daily jobs: {', '.join(respawned)}")

    if not stale:
        print(f"  [{now.strftime('%H:%M')}] All services healthy.")
        return

    if _in_alert_cooldown(heartbeats, now):
        return  # Already alerted within the hour

    description = "**Stale services:**\n" + "\n".join(f"- {s}" for s in stale)
    if respawned:
        description += "\n\n**Auto-respawned:** " + ", ".join(respawned)
    await send_discord_alert(
        "WATCHDOG ALERT: Cron jobs stale",
        description,
        color=0xFF0000,
    )
    # Locked, atomic cooldown write — the old direct write_text here could
    # clobber a heartbeat written concurrently by another cron job.
    write_heartbeat("_watchdog_last_alert")


if __name__ == "__main__":
    asyncio.run(check_heartbeats())
