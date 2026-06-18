#!/usr/bin/env python3
"""Tests for notifications.py — backoff schedule, fast-fail, fallback replay."""

import asyncio
import json
from datetime import datetime, timedelta

import aiohttp
import pytest

import notifications
from notifications import (
    ET,
    FALLBACK_MAX_AGE_HOURS,
    MAX_RETRIES,
    REPLAY_MARKER,
    _backoff_delay,
    _claim_fallback_records,
    _mark_replayed,
    _post_with_retry,
    send_discord_embeds,
)


@pytest.fixture(autouse=True)
def _patch_fallback(monkeypatch, tmp_path):
    """Redirect the fallback file to a temp dir for every test."""
    monkeypatch.setattr(notifications, "FALLBACK_FILE", tmp_path / "alerts_fallback.jsonl")


@pytest.fixture
def sleeps(monkeypatch):
    """Replace asyncio.sleep with a recorder so backoff tests run instantly."""
    recorded = []

    async def fake_sleep(seconds):
        recorded.append(seconds)

    monkeypatch.setattr(notifications.asyncio, "sleep", fake_sleep)
    return recorded


class FakeResponse:
    def __init__(self, status=204, body="", json_data=None):
        self.status = status
        self._body = body
        self._json = json_data if json_data is not None else {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def text(self):
        return self._body

    async def json(self):
        return self._json


class FakeSession:
    """Scripted session: each script item is a FakeResponse or an Exception.

    Once the script is exhausted, every post succeeds with 204.
    """

    def __init__(self, script=()):
        self.script = list(script)
        self.posts = []

    def post(self, url, json=None, timeout=None):
        self.posts.append({"url": url, "payload": json})
        result = self.script.pop(0) if self.script else FakeResponse()
        if isinstance(result, Exception):
            raise result
        return result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _write_fallback_record(embeds, age_hours=0.0, context="test"):
    ts = datetime.now(ET) - timedelta(hours=age_hours)
    record = {"timestamp": ts.isoformat(), "context": context, "embeds": embeds}
    with open(notifications.FALLBACK_FILE, "a") as f:
        f.write(json.dumps(record) + "\n")


class TestBackoffSchedule:
    """Exponential backoff for connection-class errors."""

    def test_delays_grow_exponentially_to_cap(self):
        assert _backoff_delay(0) == 2
        assert _backoff_delay(1) == 8
        assert _backoff_delay(2) == 32
        assert _backoff_delay(3) == 60
        assert _backoff_delay(10) == 60

    def test_total_backoff_about_two_minutes(self):
        total = sum(_backoff_delay(i) for i in range(MAX_RETRIES - 1))
        assert 90 <= total <= 150

    def test_connection_errors_use_full_schedule(self, sleeps):
        session = FakeSession([aiohttp.ClientError("nodename nor servname")] * MAX_RETRIES)
        ok = asyncio.run(_post_with_retry(session, "http://x", {"embeds": []}))
        assert ok is False
        assert len(session.posts) == MAX_RETRIES
        assert sleeps == [2, 8, 32, 60]

    def test_connection_error_then_success(self, sleeps):
        session = FakeSession(
            [aiohttp.ClientError("dns"), aiohttp.ClientError("dns"), FakeResponse(204)]
        )
        ok = asyncio.run(_post_with_retry(session, "http://x", {}))
        assert ok is True
        assert len(session.posts) == 3
        assert sleeps == [2, 8]

    def test_4xx_fails_fast_without_retry(self, sleeps):
        session = FakeSession([FakeResponse(400, body="bad payload")])
        ok = asyncio.run(_post_with_retry(session, "http://x", {}))
        assert ok is False
        assert len(session.posts) == 1
        assert sleeps == []

    def test_429_respects_retry_after_and_retries(self, sleeps):
        session = FakeSession(
            [FakeResponse(429, json_data={"retry_after": 0.5}), FakeResponse(204)]
        )
        ok = asyncio.run(_post_with_retry(session, "http://x", {}))
        assert ok is True
        assert sleeps == [0.5]

    def test_5xx_retries_with_backoff(self, sleeps):
        session = FakeSession([FakeResponse(500, body="oops"), FakeResponse(204)])
        ok = asyncio.run(_post_with_retry(session, "http://x", {}))
        assert ok is True
        assert sleeps == [2]


class TestFallbackClaim:
    """_claim_fallback_records — at-most-once consumption with age cap."""

    def test_claim_returns_records_and_consumes_file(self):
        _write_fallback_record([{"title": "A", "description": "d"}])
        records = _claim_fallback_records()
        assert len(records) == 1
        assert records[0]["embeds"][0]["title"] == "A"
        assert not notifications.FALLBACK_FILE.exists()
        assert not notifications.FALLBACK_FILE.with_suffix(".replaying").exists()
        # Replay-once: second claim finds nothing
        assert _claim_fallback_records() == []

    def test_claim_drops_expired_records(self):
        _write_fallback_record([{"title": "old"}], age_hours=FALLBACK_MAX_AGE_HOURS + 1)
        _write_fallback_record([{"title": "fresh"}], age_hours=1)
        records = _claim_fallback_records()
        assert len(records) == 1
        assert records[0]["embeds"][0]["title"] == "fresh"

    def test_claim_drops_corrupt_lines(self):
        notifications.FALLBACK_FILE.write_text("{not json\n")
        _write_fallback_record([{"title": "good"}])
        records = _claim_fallback_records()
        assert len(records) == 1
        assert records[0]["embeds"][0]["title"] == "good"

    def test_claim_missing_file_returns_empty(self):
        assert _claim_fallback_records() == []


class TestMarkReplayed:
    def test_prefixes_title_once(self):
        marked = _mark_replayed([{"title": "Alert"}, {"title": f"{REPLAY_MARKER} Already"}])
        assert marked[0]["title"] == f"{REPLAY_MARKER} Alert"
        assert marked[1]["title"] == f"{REPLAY_MARKER} Already"

    def test_does_not_mutate_original(self):
        original = [{"title": "Alert"}]
        _mark_replayed(original)
        assert original[0]["title"] == "Alert"


class TestSendWithReplay:
    """send_discord_embeds end-to-end with replay (session fully mocked)."""

    @pytest.fixture(autouse=True)
    def _webhook(self, monkeypatch):
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.test/webhook")

    def test_successful_send_replays_pending_fallback(self, monkeypatch, sleeps):
        _write_fallback_record([{"title": "Stranded", "description": "x"}])
        session = FakeSession()
        monkeypatch.setattr(notifications.aiohttp, "ClientSession", lambda: session)

        asyncio.run(send_discord_embeds([{"title": "Live", "description": "y"}]))

        assert len(session.posts) == 2
        live_titles = [e["title"] for e in session.posts[0]["payload"]["embeds"]]
        replay_titles = [e["title"] for e in session.posts[1]["payload"]["embeds"]]
        assert live_titles == ["Live"]
        assert replay_titles == [f"{REPLAY_MARKER} Stranded"]
        assert not notifications.FALLBACK_FILE.exists()

    def test_failed_send_does_not_replay(self, monkeypatch, sleeps):
        _write_fallback_record([{"title": "Stranded"}])
        session = FakeSession([aiohttp.ClientError("down")] * MAX_RETRIES)
        monkeypatch.setattr(notifications.aiohttp, "ClientSession", lambda: session)

        asyncio.run(send_discord_embeds([{"title": "Live", "description": "y"}]))

        # Only the live send was attempted; no replay posts
        assert len(session.posts) == MAX_RETRIES
        content = notifications.FALLBACK_FILE.read_text()
        assert "Stranded" in content  # original record untouched
        assert "Live" in content      # failed live embed appended

    def test_replay_failure_requeues_embeds(self, monkeypatch, sleeps):
        _write_fallback_record([{"title": "Stranded"}])
        # Live post succeeds, then all replay attempts fail
        session = FakeSession([FakeResponse(204)] + [aiohttp.ClientError("down")] * MAX_RETRIES)
        monkeypatch.setattr(notifications.aiohttp, "ClientSession", lambda: session)

        asyncio.run(send_discord_embeds([{"title": "Live", "description": "y"}]))

        lines = notifications.FALLBACK_FILE.read_text().splitlines()
        records = [json.loads(line) for line in lines]
        titles = [e["title"] for r in records for e in r["embeds"]]
        assert titles == [f"{REPLAY_MARKER} Stranded"]
        assert records[0]["context"] == "replay_failed"

    def test_no_webhook_saves_to_fallback(self, monkeypatch):
        monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
        monkeypatch.delenv("DISCORD_WEBHOOK", raising=False)
        asyncio.run(send_discord_embeds([{"title": "X"}]))
        assert notifications.FALLBACK_FILE.exists()
        assert "X" in notifications.FALLBACK_FILE.read_text()
