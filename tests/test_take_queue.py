"""Tests for core/take_queue.py — staging, clamping, dedup, merge safety."""
from datetime import datetime, timedelta, timezone

import pytest

from core import take_queue

NOW = datetime(2026, 7, 12, 21, 32, 1, tzinfo=timezone.utc)
CMD = ".venv/bin/python scripts/take.py KXHIGHTMIN-26JUL12-T91 buy yes 23 18"


@pytest.fixture(autouse=True)
def _isolated_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(take_queue, "QUEUE_FILE", tmp_path / "take_queue.json")
    monkeypatch.setattr(take_queue, "QUEUE_LOCK", tmp_path / ".take_queue.lock")
    monkeypatch.delenv("TAKE_MAX_NOTIONAL", raising=False)
    monkeypatch.delenv("TAKE_APPROVE_TTL_MIN", raising=False)


def _finding(**over):
    base = {"kind": "buy_winner", "ticker": "KXHIGHTMIN-26JUL12-T91",
            "subtitle": "90° or below", "printed": 90, "ask": 18,
            "ask_depth": 23, "drift_prob": 0.875, "drift_ev_c": 67.5,
            "cmd": CMD}
    base.update(over)
    return base


class TestParseTakeCmd:
    def test_parses_the_snipers_emitted_shape(self):
        assert take_queue.parse_take_cmd(CMD) == {
            "ticker": "KXHIGHTMIN-26JUL12-T91", "action": "buy", "side": "yes",
            "count": 23, "price_c": 18}

    def test_parses_sell(self):
        cmd = ".venv/bin/python scripts/take.py T1 sell yes 40 9"
        assert take_queue.parse_take_cmd(cmd)["action"] == "sell"

    def test_rejects_trailing_flags(self):
        assert take_queue.parse_take_cmd(CMD + " --yes") is None

    def test_rejects_bad_action_price_count_and_garbage(self):
        assert take_queue.parse_take_cmd(
            ".venv/bin/python scripts/take.py T1 hold yes 5 10") is None
        assert take_queue.parse_take_cmd(
            ".venv/bin/python scripts/take.py T1 buy yes 5 100") is None
        assert take_queue.parse_take_cmd(
            ".venv/bin/python scripts/take.py T1 buy yes 0 10") is None
        assert take_queue.parse_take_cmd("rm -rf /") is None
        assert take_queue.parse_take_cmd("") is None


class TestClampCount:
    def test_buy_clamps_to_notional_cap(self):
        # the 2026-07-12 DAL alert: 60134 × 1¢ = $601 book depth, $50 cap
        assert take_queue.clamp_count("buy", "yes", 60134, 1, 50.0) == 5000

    def test_sell_clamps_on_complement_collateral(self):
        # selling YES at 22¢ collateralizes 78¢/contract → 64 fit in $50
        assert take_queue.clamp_count("sell", "yes", 100, 22, 50.0) == 64

    def test_small_orders_pass_through(self):
        assert take_queue.clamp_count("buy", "yes", 23, 18, 50.0) == 23

    def test_unaffordable_single_contract_is_zero(self):
        assert take_queue.clamp_count("buy", "yes", 10, 99, 0.5) == 0


class TestEntryFromFinding:
    def test_buy_winner_becomes_entry(self):
        e = take_queue.entry_from_finding(_finding(), "cli_sniper", NOW)
        assert e["ticker"] == "KXHIGHTMIN-26JUL12-T91"
        assert e["count"] == 23 and e["price_c"] == 18
        assert e["status"] == "pending"
        assert "drift 88%" in e["summary"]

    def test_count_clamped_at_staging(self):
        cmd = ".venv/bin/python scripts/take.py T1 buy yes 60134 1"
        e = take_queue.entry_from_finding(
            _finding(ticker="T1", cmd=cmd, ask=1, ask_depth=60134),
            "cli_sniper", NOW)
        assert e["count"] == 5000

    def test_suppressed_veto_and_cmdless_are_not_staged(self):
        assert take_queue.entry_from_finding(
            _finding(suppressed="low_floor_forecast"), "s", NOW) is None
        assert take_queue.entry_from_finding(
            _finding(kind="dsm_veto"), "s", NOW) is None
        f = _finding()
        del f["cmd"]
        assert take_queue.entry_from_finding(f, "s", NOW) is None

    def test_obs_killed_or_warned_finding_never_gets_a_button(self):
        f = _finding(obs_kill="obs already 97.0° ⇒ settle ≥97° — bracket dead")
        assert take_queue.entry_from_finding(f, "cli_sniper", NOW) is None
        f = _finding(obs_warn="lone ob 97.0° — uncorroborated, verify")
        assert take_queue.entry_from_finding(f, "cli_sniper", NOW) is None


class TestEnqueue:
    def test_enqueue_and_reload(self):
        assert take_queue.enqueue_findings([_finding()], "cli_sniper", NOW) == 1
        q = take_queue.load_queue()
        assert len(q["entries"]) == 1

    def test_active_ticker_not_double_staged(self):
        take_queue.enqueue_findings([_finding()], "cli_sniper", NOW)
        later = NOW + timedelta(minutes=2)
        assert take_queue.enqueue_findings([_finding()], "metar_sniper", later) == 0

    def test_resolved_ticker_can_stage_again(self):
        take_queue.enqueue_findings([_finding()], "cli_sniper", NOW)
        eid = next(iter(take_queue.load_queue()["entries"]))
        take_queue.update_entries({eid: {"status": "expired"}}, NOW)
        later = NOW + timedelta(hours=1)
        assert take_queue.enqueue_findings([_finding()], "cli_sniper", later) == 1

    def test_update_merge_survives_concurrent_enqueue(self):
        # approver snapshots, a sniper enqueues, approver persists mutations —
        # the sniper's new entry must survive the approver's write.
        take_queue.enqueue_findings([_finding()], "cli_sniper", NOW)
        eid = next(iter(take_queue.load_queue()["entries"]))
        take_queue.enqueue_findings(
            [_finding(ticker="T2",
                      cmd=".venv/bin/python scripts/take.py T2 buy yes 5 10")],
            "metar_sniper", NOW + timedelta(minutes=1))
        take_queue.update_entries({eid: {"status": "posted", "message_id": "m1"}})
        q = take_queue.load_queue()
        assert len(q["entries"]) == 2
        assert q["entries"][eid]["status"] == "posted"

    def test_terminal_entries_prune_after_48h(self):
        take_queue.enqueue_findings([_finding()], "cli_sniper", NOW)
        eid = next(iter(take_queue.load_queue()["entries"]))
        take_queue.update_entries({eid: {"status": "executed"}}, NOW)
        take_queue.update_entries({}, NOW)  # no-op mutation: no write path
        take_queue.enqueue_findings(
            [_finding(ticker="T3",
                      cmd=".venv/bin/python scripts/take.py T3 buy yes 5 10")],
            "cli_sniper", NOW + timedelta(hours=49))
        assert eid not in take_queue.load_queue()["entries"]


class TestExpiry:
    def test_fresh_entry_not_expired(self):
        e = {"ts": NOW.isoformat(timespec="seconds")}
        assert not take_queue.is_expired(e, NOW + timedelta(minutes=14), 15)

    def test_old_and_unparseable_expire(self):
        e = {"ts": NOW.isoformat(timespec="seconds")}
        assert take_queue.is_expired(e, NOW + timedelta(minutes=16), 15)
        assert take_queue.is_expired({}, NOW, 15)
        assert take_queue.is_expired({"ts": "not-a-time"}, NOW, 15)
