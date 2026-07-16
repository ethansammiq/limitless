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

    def test_obs_killed_warned_or_walled_finding_never_gets_a_button(self):
        f = _finding(obs_kill="obs already 97.0° ⇒ settle ≥97° — bracket dead")
        assert take_queue.entry_from_finding(f, "cli_sniper", NOW) is None
        f = _finding(obs_warn="lone ob 97.0° — uncorroborated, verify")
        assert take_queue.entry_from_finding(f, "cli_sniper", NOW) is None
        # 2026-07-13: CHI T87 5000×1¢ got a button — a fade dispenser
        f = _finding(wall_ask=True)
        assert take_queue.entry_from_finding(f, "metar_sniper", NOW) is None


class TestAutoEligible:
    def _metar_finding(self, **over):
        return _finding(**{"ladder_kind": "high", "synoptic_anchor_utc": 0,
                           **over})

    def test_00z_metar_high_buy_winner_is_eligible(self):
        assert take_queue.is_auto_eligible(self._metar_finding(), "metar_sniper")
        e = take_queue.entry_from_finding(self._metar_finding(),
                                          "metar_sniper", NOW)
        assert e["auto_eligible"] is True

    def test_earlier_anchors_are_not(self):
        # 2026-07-13: the 18Z batch would have gone 1-for-5 vs the finals
        for anchor in (6, 12, 18):
            f = self._metar_finding(synoptic_anchor_utc=anchor)
            assert not take_queue.is_auto_eligible(f, "metar_sniper")

    def test_missing_anchor_low_ladder_and_other_sources_are_not(self):
        f = self._metar_finding()
        del f["synoptic_anchor_utc"]
        assert not take_queue.is_auto_eligible(f, "metar_sniper")
        assert not take_queue.is_auto_eligible(
            self._metar_finding(ladder_kind="low"), "metar_sniper")
        assert not take_queue.is_auto_eligible(
            self._metar_finding(kind="sell_dead"), "metar_sniper")
        assert not take_queue.is_auto_eligible(self._metar_finding(),
                                               "cli_sniper")

    def test_non_metar_entries_stage_as_not_eligible(self):
        e = take_queue.entry_from_finding(_finding(), "cli_sniper", NOW)
        assert e["auto_eligible"] is False


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
            "cli_sniper", NOW + timedelta(minutes=1))
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


class TestStageableClass:
    """Buttons only from ≥95% classes — the raw feed grades 52%, the
    selected book is the edge (scorecard 2026-07-14)."""

    def test_cli_floor_at_bottom_stages(self):
        f = _finding(drift_prob=0.98, ask=34)
        assert take_queue.stageable_class(f, "cli_sniper") is True

    def test_cli_floor_at_top_above_entry_cap_is_alert_only(self):
        # the 2026-07-14 NYC T90: 64¢ at drift .886 — 34% of bankroll
        f = _finding(drift_prob=0.886, ask=64)
        assert take_queue.stageable_class(f, "cli_sniper") is False
        assert take_queue.entry_from_finding(f, "cli_sniper", NOW) is None

    def test_cli_cheap_floor_at_top_stays_legal(self):
        f = _finding(drift_prob=0.886, ask=18)
        assert take_queue.stageable_class(f, "cli_sniper") is True

    def test_metar_00z_anchor_stages_and_midday_does_not(self):
        base = dict(_finding(), ladder_kind="high")
        assert take_queue.stageable_class(
            dict(base, synoptic_anchor_utc=0), "metar_sniper") is True
        # a full day of 2026-07-14 morning/afternoon buttons graded as traps
        assert take_queue.stageable_class(
            dict(base, synoptic_anchor_utc=18), "metar_sniper") is False
        assert take_queue.stageable_class(base, "metar_sniper") is False

    def test_sell_dead_always_stages(self):
        f = _finding(kind="sell_dead")
        assert take_queue.stageable_class(f, "metar_sniper") is True
        assert take_queue.stageable_class(f, "cli_sniper") is True


class TestNightCap:
    """Per-station-night exposure cap: same-night brackets are one
    correlated bet — sizing, not winrate, is where ruin lives."""

    def _f(self, ticker, count, price, **over):
        return _finding(ticker=ticker, ask=price,
                        cmd=f".venv/bin/python scripts/take.py {ticker} buy yes {count} {price}",
                        **over)

    def test_second_bracket_same_night_gets_trimmed(self, monkeypatch):
        monkeypatch.setenv("TAKE_NIGHT_CAP_DOLLARS", "5")
        take_queue.enqueue_findings(
            [self._f("KXHIGHNY-26JUL14-T90", 20, 18)], "cli_sniper", NOW)
        take_queue.enqueue_findings(
            [self._f("KXHIGHNY-26JUL14-B89.5", 40, 10)], "cli_sniper", NOW)
        by_ticker = {e["ticker"]: e for e in
                     take_queue.load_queue()["entries"].values()}
        assert by_ticker["KXHIGHNY-26JUL14-T90"]["count"] == 20
        trimmed = by_ticker["KXHIGHNY-26JUL14-B89.5"]["count"]
        assert 1 <= trimmed < 40  # clamped into the remaining night budget

    def test_exhausted_night_refuses_to_stage(self, monkeypatch):
        monkeypatch.setenv("TAKE_NIGHT_CAP_DOLLARS", "4")
        take_queue.enqueue_findings(
            [self._f("KXHIGHNY-26JUL14-T90", 20, 18)], "cli_sniper", NOW)
        assert take_queue.enqueue_findings(
            [self._f("KXHIGHNY-26JUL14-B89.5", 40, 99)], "cli_sniper", NOW) == 0

    def test_other_station_night_unaffected(self, monkeypatch):
        monkeypatch.setenv("TAKE_NIGHT_CAP_DOLLARS", "5")
        take_queue.enqueue_findings(
            [self._f("KXHIGHNY-26JUL14-T90", 20, 18)], "cli_sniper", NOW)
        assert take_queue.enqueue_findings(
            [self._f("KXHIGHCHI-26JUL14-T94", 20, 18)], "cli_sniper", NOW) == 1
        chi = [e for e in take_queue.load_queue()["entries"].values()
               if e["ticker"].startswith("KXHIGHCHI")][0]
        assert chi["count"] == 20  # full size — separate budget

    def test_high_and_low_ladders_share_one_station_night_budget(self, monkeypatch):
        # Pre-2026-07-16 the v1 series-date key counted a city's high and
        # low ladders separately, so one station-night could absorb 2× the
        # cap (sell_dead stages on low ladders at complement collateral;
        # final CLI low buys ≤20¢ stage too).
        monkeypatch.setenv("TAKE_NIGHT_CAP_DOLLARS", "4")
        take_queue.enqueue_findings(
            [self._f("KXHIGHNY-26JUL14-T90", 20, 18)], "cli_sniper", NOW)
        assert take_queue.enqueue_findings(
            [self._f("KXLOWTNYC-26JUL14-B70.5", 20, 99)], "cli_sniper", NOW) == 0

    def test_low_ladder_entry_is_trimmed_into_the_shared_budget(self, monkeypatch):
        # $4 cap − $3.60 on the HIGH ladder leaves $0.40 → 2×18¢ on the LOW
        # (the v1 key would have granted the LOW its own fresh $4)
        monkeypatch.setenv("TAKE_NIGHT_CAP_DOLLARS", "4")
        take_queue.enqueue_findings(
            [self._f("KXHIGHNY-26JUL14-T90", 20, 18)], "cli_sniper", NOW)
        take_queue.enqueue_findings(
            [self._f("KXLOWTNYC-26JUL14-B70.5", 20, 18)], "cli_sniper", NOW)
        low = [e for e in take_queue.load_queue()["entries"].values()
               if e["ticker"].startswith("KXLOWTNYC")][0]
        assert low["count"] == 2

    def test_sell_dead_complement_consumes_the_shared_budget(self, monkeypatch):
        # selling a 5¢ dead bracket books the 95¢ complement per contract
        monkeypatch.setenv("TAKE_NIGHT_CAP_DOLLARS", "10")
        sell = _finding(
            kind="sell_dead", ticker="KXLOWTNYC-26JUL14-B70.5",
            cmd=".venv/bin/python scripts/take.py KXLOWTNYC-26JUL14-B70.5 sell yes 10 5")
        assert take_queue.enqueue_findings([sell], "cli_sniper", NOW) == 1
        # $9.50 of the $10 station-night is committed on the LOW ladder:
        # the HIGH buy is trimmed to the $0.50 remainder (2×18¢), not
        # granted a fresh budget
        take_queue.enqueue_findings(
            [self._f("KXHIGHNY-26JUL14-T90", 20, 18)], "cli_sniper", NOW)
        high = [e for e in take_queue.load_queue()["entries"].values()
                if e["ticker"].startswith("KXHIGHNY")][0]
        assert high["count"] == 2

    def test_same_city_other_night_is_a_fresh_budget(self, monkeypatch):
        monkeypatch.setenv("TAKE_NIGHT_CAP_DOLLARS", "4")
        take_queue.enqueue_findings(
            [self._f("KXHIGHNY-26JUL14-T90", 20, 18)], "cli_sniper", NOW)
        assert take_queue.enqueue_findings(
            [self._f("KXHIGHNY-26JUL15-T90", 20, 18)], "cli_sniper", NOW) == 1

    def test_repriced_entry_releases_its_budget(self, monkeypatch):
        monkeypatch.setenv("TAKE_NIGHT_CAP_DOLLARS", "5")
        take_queue.enqueue_findings(
            [self._f("KXHIGHNY-26JUL14-T90", 20, 18)], "cli_sniper", NOW)
        eid = next(iter(take_queue.load_queue()["entries"]))
        take_queue.update_entries({eid: {"status": "repriced"}}, NOW)
        later = NOW + timedelta(hours=1)
        assert take_queue.enqueue_findings(
            [self._f("KXHIGHNY-26JUL14-B89.5", 20, 18)], "cli_sniper", later) == 1
        b = [e for e in take_queue.load_queue()["entries"].values()
             if e["ticker"].endswith("B89.5")][0]
        assert b["count"] == 20  # full budget back
