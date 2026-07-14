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


class TestAutoDecide:
    def test_auto_approval_substitutes_only_for_the_tap(self):
        # no reactors at all, yet the order clears every remaining guard
        verdict, reason = decide(_entry(), NOW, set(), APPROVERS, 18, TTL,
                                 auto_approved=True)
        assert verdict == "execute" and "auto" in reason

    def test_auto_never_beats_expiry_book_or_reprice(self):
        late = NOW + timedelta(minutes=TTL + 1)
        assert decide(_entry(), late, set(), APPROVERS, 18, TTL,
                      auto_approved=True)[0] == "expire"
        assert decide(_entry(), NOW, set(), APPROVERS, None, TTL,
                      auto_approved=True)[0] == "wait"
        assert decide(_entry(), NOW, set(), APPROVERS, 19, TTL,
                      auto_approved=True)[0] == "reprice"


class TestAutoAllowance:
    def _fired(self, n, price_c=18, count=23):
        return {f"e{i}": {"action": "buy", "side": "yes", "count": count,
                          "price_c": price_c, "auto_fired": True,
                          "resolved_ts": NOW.isoformat(timespec="seconds")}
                for i in range(n)}

    def test_first_fire_of_the_day_is_allowed(self):
        ok, reason = take_approver.auto_allowance(_entry(), {}, NOW)
        assert ok and "0 fire(s)" in reason

    def test_fires_per_day_cap(self, monkeypatch):
        monkeypatch.delenv("AUTO_TAKE_MAX_PER_DAY", raising=False)
        ok, reason = take_approver.auto_allowance(_entry(), self._fired(3), NOW)
        assert not ok and "3 auto-fires" in reason

    def test_daily_notional_cap(self, monkeypatch):
        monkeypatch.delenv("AUTO_TAKE_DAILY_CAP", raising=False)
        # two fires at $13.86 each = $27.72 spent; +$4.14 more breaches $30
        entries = self._fired(2, price_c=18, count=77)
        ok, reason = take_approver.auto_allowance(_entry(), entries, NOW)
        assert not ok and "$30.00 daily auto cap" in reason

    def test_yesterdays_fires_do_not_count(self):
        entries = self._fired(3)
        for e in entries.values():
            e["resolved_ts"] = (NOW - timedelta(days=1)).isoformat(
                timespec="seconds")
        ok, _ = take_approver.auto_allowance(_entry(), entries, NOW)
        assert ok

    def test_executing_marker_counts_as_spent(self, monkeypatch):
        # a crash mid-order is money out the door until fills reconcile
        monkeypatch.setenv("AUTO_TAKE_MAX_PER_DAY", "1")
        stuck = self._fired(1)
        stuck["e0"]["status"] = "executing"
        ok, _ = take_approver.auto_allowance(_entry(), stuck, NOW)
        assert not ok


class TestShadowRecord:
    def test_record_carries_the_would_verdict_and_caps(self):
        rec = take_approver.shadow_record(
            _entry(source="metar_sniper", auto_eligible=True), 18,
            "execute", "auto-approved (00Z class)", True, "0 fire(s)", NOW)
        assert rec["kind"] == "auto_shadow"
        assert rec["would"] == "execute" and rec["live_px"] == 18
        assert rec["caps_ok"] is True
        assert rec["cost_dollars"] == 4.14
        assert rec["ts"].startswith("2026-07-12")


class TestAutoMode:
    def test_default_is_shadow(self, monkeypatch):
        monkeypatch.delenv("AUTO_TAKE_00Z", raising=False)
        assert take_approver.auto_mode() == "shadow"
        monkeypatch.setenv("AUTO_TAKE_00Z", "true")   # anything but "on"
        assert take_approver.auto_mode() == "shadow"

    def test_on_flips_live(self, monkeypatch):
        monkeypatch.setenv("AUTO_TAKE_00Z", "on")
        assert take_approver.auto_mode() == "on"


class TestPrompt:
    def test_prompt_carries_ledger_tag_ticker_cost_and_expiry(self):
        text = format_prompt(_entry(), TTL)
        assert text.startswith("💰 REAL")
        assert "`T1` buy yes 23× @ 18¢" in text
        assert "worst-case $4.14" in text
        assert "<t:" in text and ":R>" in text

    def test_prompt_includes_the_drift_summary(self):
        assert "drift 88%" in format_prompt(_entry(), TTL)

    def test_auto_eligible_entry_declares_itself(self, monkeypatch):
        monkeypatch.delenv("AUTO_TAKE_00Z", raising=False)
        text = format_prompt(_entry(auto_eligible=True), TTL)
        assert "🤖 auto-eligible" in text and "shadow" in text
        monkeypatch.setenv("AUTO_TAKE_00Z", "on")
        assert "no tap needed" in format_prompt(_entry(auto_eligible=True), TTL)

    def test_plain_entries_carry_no_auto_line(self):
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


class TestRunAutoIntegration:
    """run() wiring for the auto path — Discord and Kalshi stubbed out."""

    @pytest.fixture(autouse=True)
    def _rig(self, tmp_path, monkeypatch):
        from core import take_queue

        monkeypatch.setattr(take_queue, "QUEUE_FILE", tmp_path / "q.json")
        monkeypatch.setattr(take_queue, "QUEUE_LOCK", tmp_path / ".q.lock")
        monkeypatch.setattr(take_approver, "SHADOW_JOURNAL_DIR",
                            tmp_path / "shadow")
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
        monkeypatch.setenv("DISCORD_TAKE_CHANNEL_ID", "42")
        monkeypatch.setenv("DISCORD_TAKE_APPROVER_IDS", "111")
        monkeypatch.delenv("AUTO_TAKE_00Z", raising=False)

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

    def _stage_posted_auto_entry(self):
        finding = {"kind": "buy_winner", "ticker": "T1", "subtitle": "89-90°",
                   "printed": 90, "ladder_kind": "high",
                   "synoptic_anchor_utc": 0,
                   "cmd": ".venv/bin/python scripts/take.py T1 buy yes 23 18"}
        assert self.tq.enqueue_findings([finding], "metar_sniper") == 1
        eid = next(iter(self.tq.load_queue()["entries"]))
        self.tq.update_entries({eid: {"status": "posted", "message_id": "m1"}})
        return eid

    def _shadow_rows(self):
        files = sorted((self.tmp / "shadow").glob("*.jsonl"))
        import json as _json
        return [_json.loads(line) for f in files
                for line in f.read_text().splitlines()]

    def test_shadow_journals_once_and_leaves_the_button_live(self):
        import asyncio as _asyncio
        eid = self._stage_posted_auto_entry()
        _asyncio.run(take_approver.run(dry_run=False))
        rows = self._shadow_rows()
        assert len(rows) == 1
        assert rows[0]["would"] == "execute" and rows[0]["caps_ok"] is True
        e = self.tq.load_queue()["entries"][eid]
        assert e["status"] == "posted" and e.get("auto_shadow")
        # second tick: no duplicate row, entry still awaiting a tap
        _asyncio.run(take_approver.run(dry_run=False))
        assert len(self._shadow_rows()) == 1

    def test_auto_take_on_fires_without_a_tap(self, monkeypatch):
        import asyncio as _asyncio
        monkeypatch.setenv("AUTO_TAKE_00Z", "on")
        fired = []

        def _fake_take(entry):
            fired.append(entry["ticker"])
            return True, "FILLED 23/23 @ 18c"

        monkeypatch.setattr(take_approver, "run_take", _fake_take)
        eid = self._stage_posted_auto_entry()
        _asyncio.run(take_approver.run(dry_run=False))
        assert fired == ["T1"]
        e = self.tq.load_queue()["entries"][eid]
        assert e["status"] == "executed" and e["auto_fired"] is True
        assert any("🤖 AUTO-FIRED" in c for c in self.edits)
        assert not self._shadow_rows()  # live mode does not shadow

    def test_non_eligible_entry_never_auto_fires(self, monkeypatch):
        import asyncio as _asyncio
        monkeypatch.setenv("AUTO_TAKE_00Z", "on")
        monkeypatch.setattr(take_approver, "run_take",
                            lambda e: (_ for _ in ()).throw(
                                AssertionError("order fired without a tap")))
        finding = {"kind": "buy_winner", "ticker": "T2", "subtitle": "89-90°",
                   "printed": 90, "ladder_kind": "high",
                   "synoptic_anchor_utc": 18,   # PM anchor: warming risk
                   "cmd": ".venv/bin/python scripts/take.py T2 buy yes 23 18"}
        assert self.tq.enqueue_findings([finding], "metar_sniper") == 1
        eid = next(iter(self.tq.load_queue()["entries"]))
        self.tq.update_entries({eid: {"status": "posted", "message_id": "m2"}})
        _asyncio.run(take_approver.run(dry_run=False))
        assert self.tq.load_queue()["entries"][eid]["status"] == "posted"


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
