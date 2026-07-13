"""Tests for take_approver.py guardrails (no network, no orders)."""
import subprocess
import sys
from datetime import datetime, timedelta, timezone

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
