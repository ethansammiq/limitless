"""Tests for take_approver.py guardrails (no network, no orders)."""
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

import take_approver
from take_approver import build_take_argv, decide, format_prompt, run_take

NOW = datetime(2026, 7, 12, 21, 32, 1, tzinfo=timezone.utc)
APPROVERS = {"111"}
TTL = 15


def _entry(**over):
    base = {"id": "cli_sniper:T1:x", "ts": NOW.isoformat(timespec="seconds"),
            "source": "cli_sniper", "kind": "buy_winner", "ticker": "T1",
            "action": "buy", "side": "yes", "count": 23, "price_c": 18,
            "summary": "90° or below · printed 90° · drift 88% EV +68¢",
            "status": "posted", "message_id": "m1"}
    base.update(over)
    return base


class TestDecide:
    def test_expired_beats_everything(self):
        late = NOW + timedelta(minutes=TTL + 1)
        assert decide(_entry(), late, {"111"}, APPROVERS, 18, TTL)[0] == "expire"

    def test_no_reaction_waits(self):
        assert decide(_entry(), NOW, set(), APPROVERS, 18, TTL)[0] == "wait"

    def test_non_allowlisted_reactor_never_fires(self):
        assert decide(_entry(), NOW, {"999", "bot"}, APPROVERS, 18, TTL)[0] == "wait"

    def test_unreadable_book_fails_closed(self):
        assert decide(_entry(), NOW, {"111"}, APPROVERS, None, TTL)[0] == "wait"

    def test_buy_repriced_above_staged_ask_never_fires(self):
        verdict, reason = decide(_entry(), NOW, {"111"}, APPROVERS, 19, TTL)
        assert verdict == "reprice" and "19¢" in reason

    def test_sell_repriced_below_staged_bid_never_fires(self):
        e = _entry(action="sell", price_c=9)
        assert decide(e, NOW, {"111"}, APPROVERS, 8, TTL)[0] == "reprice"

    def test_approved_at_or_under_staged_price_executes(self):
        assert decide(_entry(), NOW, {"111"}, APPROVERS, 18, TTL)[0] == "execute"
        assert decide(_entry(), NOW, {"111"}, APPROVERS, 15, TTL)[0] == "execute"

    def test_sell_at_or_above_staged_bid_executes(self):
        e = _entry(action="sell", price_c=9)
        assert decide(e, NOW, {"111"}, APPROVERS, 12, TTL)[0] == "execute"


class TestPrompt:
    def test_prompt_carries_ledger_tag_ticker_cost_and_expiry(self):
        text = format_prompt(_entry(), TTL)
        assert text.startswith("💰 REAL")
        assert "`T1` buy yes 23× @ 18¢" in text
        assert "worst-case $4.14" in text
        assert "<t:" in text and ":R>" in text

    def test_prompt_includes_the_drift_summary(self):
        assert "drift 88%" in format_prompt(_entry(), TTL)

    def test_prompt_carries_no_robot_banner(self):
        # the auto class was falsified 2026-07-16 — every button is a tap
        assert "🤖" not in format_prompt(_entry(), TTL)


class TestExecution:
    def test_argv_is_exec_form_ioc_and_confirmed(self):
        argv = build_take_argv(_entry())
        assert argv[0] == sys.executable
        assert argv[1].endswith("scripts/take.py")
        assert argv[2:] == ["T1", "buy", "yes", "23", "18", "--ioc", "--yes"]

    def test_run_take_reports_success_output_tail(self, monkeypatch):
        monkeypatch.setattr(take_approver.subprocess, "run", lambda *a, **k:
                            subprocess.CompletedProcess(a, 0, "FILLED 23/23", ""))
        ok, out = run_take(_entry())
        assert ok and "FILLED 23/23" in out

    def test_run_take_failure_and_timeout_never_raise(self, monkeypatch):
        monkeypatch.setattr(take_approver.subprocess, "run", lambda *a, **k:
                            subprocess.CompletedProcess(a, 2, "", "boom"))
        ok, out = run_take(_entry())
        assert not ok and "boom" in out

        def _timeout(*a, **k):
            raise subprocess.TimeoutExpired(cmd="take.py", timeout=90)
        monkeypatch.setattr(take_approver.subprocess, "run", _timeout)
        ok, out = run_take(_entry())
        assert not ok and "CHECK FILLS" in out


class TestConfig:
    def test_unconfigured_returns_none(self, monkeypatch):
        for k in ("DISCORD_BOT_TOKEN", "DISCORD_TAKE_CHANNEL_ID",
                  "DISCORD_TAKE_APPROVER_IDS"):
            monkeypatch.delenv(k, raising=False)
        assert take_approver._config() is None

    def test_full_config_parses_approver_set(self, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
        monkeypatch.setenv("DISCORD_TAKE_CHANNEL_ID", "42")
        monkeypatch.setenv("DISCORD_TAKE_APPROVER_IDS", "111, 222")
        cfg = take_approver._config()
        assert cfg["approvers"] == {"111", "222"}


class TestRunIntegration:
    """run() wiring — Discord and Kalshi stubbed out."""

    @pytest.fixture(autouse=True)
    def _rig(self, tmp_path, monkeypatch):
        from core import take_queue

        monkeypatch.setattr(take_queue, "QUEUE_FILE", tmp_path / "q.json")
        monkeypatch.setattr(take_queue, "QUEUE_LOCK", tmp_path / ".q.lock")
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
        monkeypatch.setenv("DISCORD_TAKE_CHANNEL_ID", "42")
        monkeypatch.setenv("DISCORD_TAKE_APPROVER_IDS", "111")

        async def _no_reactors(session, cfg, mid):
            return set()

        async def _edit(session, cfg, mid, content):
            self.edits.append(content)

        async def _live_px(client, entry):
            return 18

        import kalshi_client

        class _StubKalshi:
            def __init__(self, **kw): ...
            async def start(self): ...
            async def stop(self): ...

        monkeypatch.setattr(kalshi_client, "KalshiClient", _StubKalshi)
        monkeypatch.setattr(take_approver, "get_reactors", _no_reactors)
        monkeypatch.setattr(take_approver, "edit_message", _edit)
        monkeypatch.setattr(take_approver, "fetch_live_px", _live_px)
        self.edits = []
        self.tmp = tmp_path
        self.tq = take_queue

    def _stage_posted_entry(self):
        finding = {"kind": "buy_winner", "ticker": "T1", "subtitle": "89-90°",
                   "printed": 90, "ladder_kind": "high", "ask": 18,
                   "cmd": ".venv/bin/python scripts/take.py T1 buy yes 23 18"}
        assert self.tq.enqueue_findings([finding], "cli_sniper") == 1
        eid = next(iter(self.tq.load_queue()["entries"]))
        self.tq.update_entries({eid: {"status": "posted", "message_id": "m1"}})
        return eid

    def test_tapped_fire_past_the_daily_cap_resolves_capped(self, monkeypatch):
        # the human tap does NOT bypass the portfolio-day cap
        import asyncio as _asyncio

        async def _tapped(session, cfg, mid):
            return {"111"}

        monkeypatch.setattr(take_approver, "get_reactors", _tapped)
        monkeypatch.setattr(take_approver, "run_take",
                            lambda e: (_ for _ in ()).throw(
                                AssertionError("order fired past the daily cap")))
        eid = self._stage_posted_entry()
        # cap tightened AFTER staging (env change mid-flight) — staging
        # couldn't trim for it, so the fire-time backstop must refuse
        monkeypatch.setenv("TAKE_DAILY_CAP_DOLLARS", "1")  # cost is $4.14
        _asyncio.run(take_approver.run(dry_run=False))
        e = self.tq.load_queue()["entries"][eid]
        assert e["status"] == "capped"
        assert any("🧢 NOT EXECUTED" in c for c in self.edits)

class TestPostPromptMentions:
    """The button must @mention approvers — Discord mobile only pushes on
    mentions, and an unseen button is an expired button (2026-07-14)."""

    def test_post_mentions_every_approver_and_allowlists_them(self, monkeypatch):
        import asyncio

        import take_approver as ta

        calls = []

        async def fake_discord(session, method, path, token, json_body=None):
            calls.append({"method": method, "path": path, "body": json_body})
            return {"id": "m1"}

        monkeypatch.setattr(ta, "_discord", fake_discord)
        cfg = {"token": "t", "channel": "c", "approvers": {"222", "111"}}
        mid = asyncio.run(ta.post_prompt(None, cfg, _entry(), TTL))

        assert mid == "m1"
        body = calls[0]["body"]
        assert body["content"].startswith("<@111> <@222>")
        assert "TAKE?" in body["content"]
        assert body["allowed_mentions"] == {"parse": [], "users": ["111", "222"]}


class TestPostNewEntries:
    """Stage-time posting (2026-07-14: the DC race repriced inside the
    0-60s cron-boundary wait between staging and posting)."""

    @pytest.fixture(autouse=True)
    def _rig(self, tmp_path, monkeypatch):
        from core import take_queue

        monkeypatch.setattr(take_queue, "QUEUE_FILE", tmp_path / "q.json")
        monkeypatch.setattr(take_queue, "QUEUE_LOCK", tmp_path / ".q.lock")
        monkeypatch.setattr(take_approver, "PROJECT_ROOT", tmp_path)
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
        monkeypatch.setenv("DISCORD_TAKE_CHANNEL_ID", "42")
        monkeypatch.setenv("DISCORD_TAKE_APPROVER_IDS", "111")
        self.posted = []

        async def _post(session, cfg, entry, ttl):
            self.posted.append(entry["ticker"])
            return f"m{len(self.posted)}"

        monkeypatch.setattr(take_approver, "post_prompt", _post)
        self.tq = take_queue

    def _stage(self):
        finding = {"kind": "buy_winner", "ticker": "T1", "subtitle": "89-90°",
                   "printed": 90, "ladder_kind": "high", "ask": 18,
                   "cmd": ".venv/bin/python scripts/take.py T1 buy yes 23 18"}
        assert self.tq.enqueue_findings([finding], "cli_sniper") == 1

    def test_posts_pending_entry_immediately(self):
        import asyncio as _asyncio

        self._stage()
        assert _asyncio.run(take_approver.post_new_entries()) == 1
        entry = next(iter(self.tq.load_queue()["entries"].values()))
        assert entry["status"] == "posted"
        assert entry["message_id"] == "m1"
        assert self.posted == ["T1"]

    def test_skips_while_approver_holds_the_lock(self):
        import asyncio as _asyncio

        self._stage()
        held = take_approver.try_run_lock()
        assert held is not None
        try:
            assert _asyncio.run(take_approver.post_new_entries()) == 0
        finally:
            held.close()
        entry = next(iter(self.tq.load_queue()["entries"].values()))
        assert entry["status"] == "pending"  # the cron instance will post it

    def test_unconfigured_is_a_noop(self, monkeypatch):
        import asyncio as _asyncio

        monkeypatch.delenv("DISCORD_BOT_TOKEN")
        self._stage()
        assert _asyncio.run(take_approver.post_new_entries()) == 0


class TestRunActivitySignal:
    """run() tells main() whether to keep fast-polling this minute."""

    @pytest.fixture(autouse=True)
    def _rig(self, tmp_path, monkeypatch):
        from core import take_queue

        monkeypatch.setattr(take_queue, "QUEUE_FILE", tmp_path / "q.json")
        monkeypatch.setattr(take_queue, "QUEUE_LOCK", tmp_path / ".q.lock")
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
        monkeypatch.setenv("DISCORD_TAKE_CHANNEL_ID", "42")
        monkeypatch.setenv("DISCORD_TAKE_APPROVER_IDS", "111")

        async def _no_reactors(session, cfg, mid):
            return set()

        monkeypatch.setattr(take_approver, "get_reactors", _no_reactors)
        self.tq = take_queue

    def test_empty_queue_reports_idle(self):
        import asyncio as _asyncio

        assert _asyncio.run(take_approver.run(dry_run=False)) is False

    def test_untapped_posted_entry_keeps_polling(self):
        import asyncio as _asyncio

        finding = {"kind": "buy_winner", "ticker": "T1", "subtitle": "89-90°",
                   "printed": 90, "ladder_kind": "high", "ask": 18,
                   "cmd": ".venv/bin/python scripts/take.py T1 buy yes 23 18"}
        self.tq.enqueue_findings([finding], "cli_sniper")
        eid = next(iter(self.tq.load_queue()["entries"]))
        self.tq.update_entries({eid: {"status": "posted", "message_id": "m1"}})
        assert _asyncio.run(take_approver.run(dry_run=False)) is True

    def test_expiring_last_entry_reports_idle(self, monkeypatch):
        import asyncio as _asyncio

        async def _no_edit(session, cfg, mid, content):
            return {}

        monkeypatch.setattr(take_approver, "edit_message", _no_edit)
        old = _entry(ts="2020-01-01T00:00:00+00:00")
        self.tq.update_entries({old["id"]: old})
        # update_entries only merges known ids — write directly instead
        import json as _json
        self.tq.QUEUE_FILE.write_text(_json.dumps({"entries": {old["id"]: old}}))
        assert _asyncio.run(take_approver.run(dry_run=False)) is False


class TestPremiseVerdict:
    """Fire-time reissue guard: the staged CLI premise is re-checked against
    the archive just before take.py (2026-07-16 BOS: min 51→69)."""

    NOW = datetime(2026, 7, 16, 21, 50, tzinfo=timezone.utc)
    ENTRY = {"ticker": "KXLOWTBOS-26JUL16-T68", "kind": "sell_dead",
             "premise": {"awips": "BOS", "stamp": "162129",
                         "summary_date": "2026-07-16", "printed": 51,
                         "ladder_kind": "low", "final": False}}

    def test_moved_verdict_passes_through(self, monkeypatch):
        import cli_sniper
        monkeypatch.setattr(cli_sniper, "check_premise",
                            lambda entry, now: ("moved", "min 51→69"))
        assert take_approver.premise_verdict(dict(self.ENTRY), self.NOW) == (
            "moved", "min 51→69")

    def test_check_failure_fails_open(self, monkeypatch):
        import cli_sniper

        def boom(entry, now):
            raise OSError("HTTP Error 429")
        monkeypatch.setattr(cli_sniper, "check_premise", boom)
        verdict, _ = take_approver.premise_verdict(dict(self.ENTRY), self.NOW)
        assert verdict == "unchecked"
