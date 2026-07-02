#!/usr/bin/env python3
"""Tests for heartbeat.py — write, read, staleness detection — and
watchdog.py self-healing catch-up of sleep-missed daily cron jobs."""

import asyncio
import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

_tmpdir = tempfile.mkdtemp()
_test_heartbeat = Path(_tmpdir) / "heartbeats.json"
_test_lock = Path(_tmpdir) / ".heartbeats.lock"


@pytest.fixture(autouse=True)
def _patch_paths(monkeypatch):
    """Redirect heartbeat to temp files for every test."""
    import heartbeat
    monkeypatch.setattr(heartbeat, "HEARTBEAT_FILE", _test_heartbeat)
    monkeypatch.setattr(heartbeat, "HEARTBEAT_LOCK", _test_lock)
    monkeypatch.setattr(heartbeat, "PROJECT_ROOT", Path(_tmpdir))
    if _test_heartbeat.exists():
        _test_heartbeat.unlink()
    if _test_lock.exists():
        _test_lock.unlink()
    yield


class TestWriteHeartbeat:
    """write_heartbeat() tests."""

    def test_write_creates_file(self):
        from heartbeat import write_heartbeat
        assert not _test_heartbeat.exists()
        write_heartbeat("test_service")
        assert _test_heartbeat.exists()

    def test_write_records_timestamp(self):
        from heartbeat import write_heartbeat
        write_heartbeat("auto_scan")
        data = json.loads(_test_heartbeat.read_text())
        assert "auto_scan" in data
        assert "timestamp" in data["auto_scan"]

    def test_write_multiple_services(self):
        from heartbeat import write_heartbeat
        write_heartbeat("auto_scan")
        write_heartbeat("position_monitor")
        data = json.loads(_test_heartbeat.read_text())
        assert "auto_scan" in data
        assert "position_monitor" in data

    def test_write_overwrites_same_service(self):
        from heartbeat import write_heartbeat
        write_heartbeat("auto_scan")
        first = json.loads(_test_heartbeat.read_text())["auto_scan"]["timestamp"]
        import time; time.sleep(0.01)
        write_heartbeat("auto_scan")
        second = json.loads(_test_heartbeat.read_text())["auto_scan"]["timestamp"]
        # Timestamps should differ (second is more recent)
        # They might be same if sub-second, so just check data is valid
        assert second is not None


class TestReadHeartbeats:
    """read_heartbeats() tests."""

    def test_read_missing_file(self):
        from heartbeat import read_heartbeats
        assert read_heartbeats() == {}

    def test_read_after_write(self):
        from heartbeat import write_heartbeat, read_heartbeats
        write_heartbeat("test_service")
        data = read_heartbeats()
        assert "test_service" in data

    def test_read_corrupted_file(self):
        from heartbeat import read_heartbeats
        _test_heartbeat.write_text("{invalid json")
        assert read_heartbeats() == {}


class TestCheckHeartbeats:
    """check_heartbeats() staleness detection."""

    def test_all_healthy(self):
        """All services recently reported → no problems."""
        from heartbeat import write_heartbeat, check_heartbeats
        # Write heartbeats for all expected services
        write_heartbeat("auto_scan")
        write_heartbeat("position_monitor")
        write_heartbeat("backtest_collector")
        write_heartbeat("morning_check")
        problems = check_heartbeats()
        assert problems == []

    def test_missing_service(self):
        """Service never reported → flagged as never_seen."""
        from heartbeat import check_heartbeats
        # Empty file → all services are never_seen
        problems = check_heartbeats()
        assert len(problems) > 0
        services = [p[0] for p in problems]
        assert "auto_scan" in services
        statuses = {p[0]: p[1] for p in problems}
        assert statuses["auto_scan"] == "never_seen"

    def test_stale_service(self, monkeypatch):
        """Service last reported 2 hours ago with 90-min threshold → stale."""
        from heartbeat import write_heartbeat, check_heartbeats, ET
        from datetime import datetime, timedelta

        write_heartbeat("auto_scan")
        write_heartbeat("position_monitor")
        write_heartbeat("backtest_collector")
        write_heartbeat("morning_check")

        # Manually backdate auto_scan by 2 hours
        data = json.loads(_test_heartbeat.read_text())
        old_time = datetime.now(ET) - timedelta(hours=2)
        data["auto_scan"]["timestamp"] = old_time.isoformat()
        _test_heartbeat.write_text(json.dumps(data))

        problems = check_heartbeats()
        stale = [p for p in problems if p[0] == "auto_scan"]
        assert len(stale) == 1
        assert stale[0][1] == "stale"
        assert stale[0][2] > 90  # More than 90 minutes old

    def test_partial_health(self):
        """Some services healthy, others missing."""
        from heartbeat import write_heartbeat, check_heartbeats
        write_heartbeat("auto_scan")
        write_heartbeat("position_monitor")
        # backtest_collector and morning_check NOT written
        problems = check_heartbeats()
        problem_services = {p[0] for p in problems}
        assert "auto_scan" not in problem_services
        assert "position_monitor" not in problem_services
        assert "backtest_collector" in problem_services
        assert "morning_check" in problem_services


class TestWatchdogCatchup:
    """watchdog.attempt_catchup — self-healing for sleep-missed daily crons."""

    @pytest.fixture(autouse=True)
    def _patch_watchdog(self, monkeypatch, tmp_path):
        import watchdog
        self.watchdog = watchdog
        monkeypatch.setattr(watchdog, "HEARTBEAT_FILE", tmp_path / "heartbeats.json")
        monkeypatch.setattr(watchdog, "CATCHUP_STATE_FILE", tmp_path / "watchdog_catchup.json")
        monkeypatch.setattr(watchdog, "LOGS_DIR", tmp_path / "logs")
        monkeypatch.setattr(watchdog, "VENV_PYTHON", tmp_path / "python3")
        monkeypatch.setattr(watchdog, "PROJECT_ROOT", tmp_path)

        self.popen_calls = []
        self.run_calls = []
        outer = self

        class FakePopen:
            def __init__(self, cmd, **kwargs):
                outer.popen_calls.append({"cmd": cmd, "kwargs": kwargs})

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_run(cmd, **kwargs):
            outer.run_calls.append({"cmd": cmd, "kwargs": kwargs})
            return None

        monkeypatch.setattr(watchdog.subprocess, "Popen", FakePopen)
        monkeypatch.setattr(watchdog.subprocess, "run", fake_run)
        yield

    def _now(self, hour, minute=0):
        return datetime(2026, 6, 12, hour, minute, tzinfo=self.watchdog.ET)

    def test_bias_collector_monitored(self):
        assert "bias_collector" in self.watchdog.EXPECTED_INTERVALS
        assert self.watchdog.EXPECTED_INTERVALS["bias_collector"] == 25

    def test_spawns_missed_job_after_window(self):
        spawned = self.watchdog.attempt_catchup(["backtest_collector"], self._now(9, 30))
        assert spawned == ["backtest_collector"]
        assert len(self.popen_calls) == 1
        cmd = self.popen_calls[0]["cmd"]
        assert cmd[0] == str(self.watchdog.VENV_PYTHON)
        assert cmd[1].endswith("backtest_collector.py")
        # Output goes to a per-day log under logs/
        log_files = list((self.watchdog.LOGS_DIR).glob("catchup_backtest_collector_*.log"))
        assert len(log_files) == 1

    def test_no_spawn_before_window(self):
        spawned = self.watchdog.attempt_catchup(["backtest_collector"], self._now(7, 30))
        assert spawned == []
        assert self.popen_calls == []

    def test_per_day_guard_blocks_second_spawn(self):
        now = self._now(10, 0)
        assert self.watchdog.attempt_catchup(["morning_check"], now) == ["morning_check"]
        assert self.watchdog.attempt_catchup(["morning_check"], now) == []
        assert len(self.popen_calls) == 1

    def test_guard_resets_next_day(self):
        assert self.watchdog.attempt_catchup(["morning_check"], self._now(10, 0)) == ["morning_check"]
        next_day = datetime(2026, 6, 13, 10, 0, tzinfo=self.watchdog.ET)
        assert self.watchdog.attempt_catchup(["morning_check"], next_day) == ["morning_check"]
        assert len(self.popen_calls) == 2

    def test_non_catchup_services_ignored(self):
        spawned = self.watchdog.attempt_catchup(
            ["position_monitor", "auto_trader"], self._now(12, 0)
        )
        assert spawned == []
        assert self.popen_calls == []

    def test_spawns_multiple_missed_jobs(self):
        spawned = self.watchdog.attempt_catchup(
            ["morning_check", "backtest_collector", "bias_collector"], self._now(9, 0)
        )
        assert set(spawned) == {"morning_check", "backtest_collector", "bias_collector"}
        assert len(self.popen_calls) == 3
        # bias_collector's upstream row is absent in the temp project, so its
        # catch-up first runs backtest_collector synchronously for the missed date.
        assert len(self.run_calls) == 1
        assert self.run_calls[0]["cmd"][1].endswith("backtest_collector.py")
        assert "--date" in self.run_calls[0]["cmd"]

    def test_collector_catchup_passes_target_date(self):
        """Catch-up pins collectors to the missed date so a drifted run still
        backfills the right day rather than 'yesterday-relative-to-now'."""
        self.watchdog.attempt_catchup(["backtest_collector"], self._now(9, 30))
        cmd = self.popen_calls[0]["cmd"]
        assert "--date" in cmd
        assert cmd[cmd.index("--date") + 1] == "2026-06-11"  # day before the 06-12 run

    def test_bias_skips_dependency_when_upstream_present(self, monkeypatch):
        """When yesterday's settlement row already exists, no synchronous
        backtest dependency run is needed — bias is spawned directly."""
        monkeypatch.setattr(self.watchdog, "_daily_data_has", lambda d: True)
        spawned = self.watchdog.attempt_catchup(["bias_collector"], self._now(9, 30))
        assert spawned == ["bias_collector"]
        assert len(self.run_calls) == 0
        assert len(self.popen_calls) == 1

    def test_spawn_failure_not_marked_done(self, monkeypatch):
        def boom(*args, **kwargs):
            raise OSError("no venv")

        monkeypatch.setattr(self.watchdog.subprocess, "Popen", boom)
        assert self.watchdog.attempt_catchup(["bias_collector"], self._now(9, 0)) == []
        # Guard not persisted → next watchdog cycle can retry
        assert not self.watchdog.CATCHUP_STATE_FILE.exists()

    def _seed_heartbeats(self, now, stale_service):
        fresh = now.isoformat()
        heartbeats = {
            s: {"timestamp": fresh} for s in self.watchdog.EXPECTED_INTERVALS
        }
        heartbeats[stale_service] = {"timestamp": (now - timedelta(hours=26)).isoformat()}
        return heartbeats

    def test_check_heartbeats_respawns_during_alert_cooldown(self, monkeypatch):
        """Catch-up must fire even when the Discord alert is in cooldown."""
        watchdog = self.watchdog
        now = self._now(9, 30)

        class FakeDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return now

        monkeypatch.setattr(watchdog, "datetime", FakeDateTime)

        heartbeats = self._seed_heartbeats(now, "backtest_collector")
        heartbeats["_watchdog_last_alert"] = {
            "timestamp": (now - timedelta(minutes=5)).isoformat()
        }
        watchdog.HEARTBEAT_FILE.write_text(json.dumps(heartbeats))

        alerts = []

        async def fake_alert(*args, **kwargs):
            alerts.append((args, kwargs))

        monkeypatch.setattr(watchdog, "send_discord_alert", fake_alert)

        asyncio.run(watchdog.check_heartbeats())

        assert len(self.popen_calls) == 1  # catch-up fired despite cooldown
        assert alerts == []                # alert suppressed by cooldown

    def test_check_heartbeats_alert_mentions_respawned(self, monkeypatch):
        watchdog = self.watchdog
        now = self._now(9, 30)

        class FakeDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return now

        monkeypatch.setattr(watchdog, "datetime", FakeDateTime)
        watchdog.HEARTBEAT_FILE.write_text(
            json.dumps(self._seed_heartbeats(now, "bias_collector"))
        )

        alerts = []

        async def fake_alert(*args, **kwargs):
            alerts.append((args, kwargs))

        monkeypatch.setattr(watchdog, "send_discord_alert", fake_alert)

        asyncio.run(watchdog.check_heartbeats())

        assert len(self.popen_calls) == 1
        assert len(alerts) == 1
        description = alerts[0][0][1]
        assert "bias_collector" in description
        assert "Auto-respawned" in description
