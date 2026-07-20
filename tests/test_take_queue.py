"""Tests for core/take_queue.py — staging, clamping, dedup, merge safety."""
from datetime import datetime, timedelta, timezone

import pytest

from core import risk, take_queue

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

    def test_metar_buys_never_stage_at_any_anchor(self):
        # 2026-07-16: the 00Z class was falsified — it emits zero high-ladder
        # buys (the CLI floor beat it and the market already priced ~99¢),
        # and 18Z/12Z are the 1-for-5 forecast class.
        base = dict(_finding(), ladder_kind="high")
        for anchor in (0, 6, 12, 18, None):
            f = dict(base, synoptic_anchor_utc=anchor) if anchor is not None else base
            assert take_queue.stageable_class(f, "metar_sniper") is False
            assert take_queue.entry_from_finding(f, "metar_sniper", NOW) is None

    def test_metar_sell_dead_still_stages(self):
        # the riskless class is obs-certain from either sniper
        assert take_queue.stageable_class(
            _finding(kind="sell_dead"), "metar_sniper") is True

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


class TestDailyCap:
    """Portfolio-day cap: station-night budgets can stack across cities —
    the day budget can't."""

    def _f(self, ticker, count, price, **over):
        return _finding(ticker=ticker, ask=price,
                        cmd=f".venv/bin/python scripts/take.py {ticker} buy yes {count} {price}",
                        **over)

    def test_second_station_is_trimmed_into_the_day_budget(self, monkeypatch):
        # night budgets are fresh per station; the $5 day budget is not:
        # NY books $3.60, CHI gets the $1.40 remainder → 7×18¢
        monkeypatch.setenv("TAKE_DAILY_CAP_DOLLARS", "5")
        take_queue.enqueue_findings(
            [self._f("KXHIGHNY-26JUL14-T90", 20, 18)], "cli_sniper", NOW)
        take_queue.enqueue_findings(
            [self._f("KXHIGHCHI-26JUL14-T94", 20, 18)], "cli_sniper", NOW)
        chi = [e for e in take_queue.load_queue()["entries"].values()
               if e["ticker"].startswith("KXHIGHCHI")][0]
        assert chi["count"] == 7

    def test_exhausted_day_refuses_to_stage(self, monkeypatch):
        monkeypatch.setenv("TAKE_DAILY_CAP_DOLLARS", "4")
        take_queue.enqueue_findings(
            [self._f("KXHIGHNY-26JUL14-T90", 20, 18)], "cli_sniper", NOW)
        assert take_queue.enqueue_findings(
            [self._f("KXHIGHCHI-26JUL14-T94", 20, 99)], "cli_sniper", NOW) == 0

    def test_the_tighter_cap_binds(self, monkeypatch):
        # night $2 < daily $100 → 11×18¢, not 20
        monkeypatch.setenv("TAKE_NIGHT_CAP_DOLLARS", "2")
        monkeypatch.setenv("TAKE_DAILY_CAP_DOLLARS", "100")
        take_queue.enqueue_findings(
            [self._f("KXHIGHNY-26JUL14-T90", 20, 18)], "cli_sniper", NOW)
        e = next(iter(take_queue.load_queue()["entries"].values()))
        assert e["count"] == 11

    def test_released_budget_returns_to_the_day(self, monkeypatch):
        monkeypatch.setenv("TAKE_DAILY_CAP_DOLLARS", "4")
        take_queue.enqueue_findings(
            [self._f("KXHIGHNY-26JUL14-T90", 20, 18)], "cli_sniper", NOW)
        eid = next(iter(take_queue.load_queue()["entries"]))
        take_queue.update_entries({eid: {"status": "expired"}}, NOW)
        later = NOW + timedelta(hours=1)
        assert take_queue.enqueue_findings(
            [self._f("KXHIGHCHI-26JUL14-T94", 20, 18)], "cli_sniper", later) == 1
        chi = [e for e in take_queue.load_queue()["entries"].values()
               if e["ticker"].startswith("KXHIGHCHI")][0]
        assert chi["count"] == 20

    def test_yesterdays_spend_does_not_count(self, monkeypatch):
        monkeypatch.setenv("TAKE_DAILY_CAP_DOLLARS", "4")
        yesterday = NOW - timedelta(days=1)
        take_queue.enqueue_findings(
            [self._f("KXHIGHNY-26JUL13-T90", 20, 18)], "cli_sniper", yesterday)
        eid = next(iter(take_queue.load_queue()["entries"]))
        take_queue.update_entries({eid: {"status": "executed"}}, yesterday)
        assert take_queue.enqueue_findings(
            [self._f("KXHIGHCHI-26JUL14-T94", 20, 18)], "cli_sniper", NOW) == 1
        chi = [e for e in take_queue.load_queue()["entries"].values()
               if e["ticker"].startswith("KXHIGHCHI")][0]
        assert chi["count"] == 20

    def test_overspent_day_negative_budget_stages_nothing(self, monkeypatch):
        # env tightened mid-flight below what's already committed
        monkeypatch.setenv("TAKE_DAILY_CAP_DOLLARS", "100")
        take_queue.enqueue_findings(
            [self._f("KXHIGHNY-26JUL14-T90", 20, 18)], "cli_sniper", NOW)
        monkeypatch.setenv("TAKE_DAILY_CAP_DOLLARS", "2")
        assert take_queue.enqueue_findings(
            [self._f("KXHIGHCHI-26JUL14-T94", 20, 18)], "cli_sniper", NOW) == 0


class TestReissueGuardStaging:
    """2026-07-16 BOS: a silently re-issued CLI (min 51→69, no CORRECTED
    tag) had a falsified sell_dead staged on the live favorite."""

    def test_reissue_conflict_never_gets_a_button(self):
        f = _finding(reissue_conflict="reissued 162139: min 51→69")
        assert take_queue.entry_from_finding(f, "cli_sniper", NOW) is None

    def test_cli_finding_carries_its_premise(self):
        f = _finding(awips="BOS", stamp="162129",
                     summary_date="2026-07-16", ladder_kind="low",
                     printed=51, final=False)
        e = take_queue.entry_from_finding(f, "cli_sniper", NOW)
        assert e["premise"] == {"awips": "BOS", "stamp": "162129",
                                "summary_date": "2026-07-16", "printed": 51,
                                "ladder_kind": "low", "final": False}

    def test_finding_without_provenance_stages_premiseless(self):
        e = take_queue.entry_from_finding(_finding(kind="sell_dead"),
                                          "metar_sniper", NOW)
        assert e is not None and "premise" not in e


class TestSupersede:
    def _staged_id(self):
        take_queue.enqueue_findings([_finding()], "cli_sniper", NOW)
        return next(iter(take_queue.load_queue()["entries"]))

    def test_active_entry_superseded_and_returned(self):
        eid = self._staged_id()
        take_queue.update_entries({eid: {"status": "posted",
                                         "message_id": "m1"}}, NOW)
        dead = take_queue.supersede_entries([eid], "CLI reissued 162139", NOW)
        assert [e["id"] for e in dead] == [eid]
        assert dead[0]["message_id"] == "m1"
        e = take_queue.load_queue()["entries"][eid]
        assert e["status"] == "superseded"
        assert e["result"] == "CLI reissued 162139"

    def test_superseded_releases_the_night_budget(self):
        eid = self._staged_id()
        entries = take_queue.load_queue()["entries"]
        assert take_queue.night_spent_dollars(
            entries, "KXHIGHTMIN-26JUL12-T91") > 0
        take_queue.supersede_entries([eid], "reissue", NOW)
        entries = take_queue.load_queue()["entries"]
        assert take_queue.night_spent_dollars(
            entries, "KXHIGHTMIN-26JUL12-T91") == 0

    def test_terminal_and_unknown_ids_untouched(self):
        eid = self._staged_id()
        take_queue.update_entries({eid: {"status": "executed"}}, NOW)
        assert take_queue.supersede_entries([eid, "ghost"], "r", NOW) == []
        assert take_queue.load_queue()["entries"][eid]["status"] == "executed"


class TestClaimForExecution:
    def _posted_id(self):
        take_queue.enqueue_findings([_finding()], "cli_sniper", NOW)
        eid = next(iter(take_queue.load_queue()["entries"]))
        take_queue.update_entries({eid: {"status": "posted",
                                         "message_id": "m1"}}, NOW)
        return eid

    def test_posted_entry_claims_once(self):
        eid = self._posted_id()
        assert take_queue.claim_for_execution(eid, NOW) is True
        e = take_queue.load_queue()["entries"][eid]
        assert e["status"] == "executing"
        # at-most-once: a second claimer must lose
        assert take_queue.claim_for_execution(eid, NOW) is False

    def test_pending_and_unknown_never_claim(self):
        take_queue.enqueue_findings([_finding()], "cli_sniper", NOW)
        eid = next(iter(take_queue.load_queue()["entries"]))
        assert take_queue.claim_for_execution(eid, NOW) is False
        assert take_queue.claim_for_execution("ghost", NOW) is False

    def test_supersede_beats_the_claim(self):
        # The race the atomic claim closes: a reissue supersede lands after
        # the approver's snapshot — the fire must be refused.
        eid = self._posted_id()
        take_queue.supersede_entries([eid], "CLI reissued", NOW)
        assert take_queue.claim_for_execution(eid, NOW) is False
        assert take_queue.load_queue()["entries"][eid]["status"] == "superseded"


class TestAttentionFloor:
    """2026-07-19: five buttons, all expired untapped, three worth <$9.
    A button that isn't worth an interrupt trains the next one to be
    ignored — so sub-floor findings alert and journal, but never post."""

    def test_production_default_is_25(self, monkeypatch):
        monkeypatch.delenv("TAKE_MIN_PAYOFF_DOLLARS", raising=False)
        assert risk.min_payoff_dollars() == 25.0

    def test_payoff_math_buy_is_upside_sell_is_the_credit(self):
        assert risk.max_payoff_dollars("buy", "yes", 16, 46) == pytest.approx(8.64)
        assert risk.max_payoff_dollars("sell", "yes", 19, 21) == pytest.approx(3.99)

    def test_the_81_cent_button_never_posts(self, monkeypatch):
        # KXHIGHTOKC-26JUL19-B92.5: 1 contract @19c, best case 81c.
        monkeypatch.setenv("TAKE_MIN_PAYOFF_DOLLARS", "25")
        cmd = ".venv/bin/python scripts/take.py T1 buy yes 1 19"
        assert take_queue.entry_from_finding(
            _finding(ticker="T1", cmd=cmd, ask=19, ask_depth=1),
            "cli_sniper", NOW) is None

    def test_a_worthwhile_button_still_posts(self, monkeypatch):
        monkeypatch.setenv("TAKE_MIN_PAYOFF_DOLLARS", "25")
        cmd = ".venv/bin/python scripts/take.py T1 buy yes 60 20"
        e = take_queue.entry_from_finding(
            _finding(ticker="T1", cmd=cmd, ask=20, ask_depth=60),
            "cli_sniper", NOW)
        assert e is not None and e["count"] == 60          # $48 best case

    def test_floor_applies_to_riskless_sells_too(self, monkeypatch):
        # Free money is still an interrupt: the 2026-07-16 SFO dead-bid
        # sell was 19@21c = $3.99 and expired untapped like the rest.
        monkeypatch.setenv("TAKE_MIN_PAYOFF_DOLLARS", "25")
        cmd = ".venv/bin/python scripts/take.py T1 sell yes 19 21"
        assert take_queue.entry_from_finding(
            _finding(kind="sell_dead", ticker="T1", cmd=cmd, ask=21, ask_depth=19),
            "cli_sniper", NOW) is None

    def test_floor_uses_the_clamped_count_not_the_asked_one(self, monkeypatch):
        # Book offers 5000 @1c ($49.50 raw) but the night cap clamps to 20
        # ($19.80) -> below floor. Order matters: clamp first, then gate.
        monkeypatch.setenv("TAKE_MIN_PAYOFF_DOLLARS", "25")
        monkeypatch.setenv("TAKE_MAX_NOTIONAL", "0.20")
        cmd = ".venv/bin/python scripts/take.py T1 buy yes 5000 1"
        assert take_queue.entry_from_finding(
            _finding(ticker="T1", cmd=cmd, ask=1, ask_depth=5000),
            "cli_sniper", NOW) is None

    def test_garbage_env_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setenv("TAKE_MIN_PAYOFF_DOLLARS", "twenty-five")
        assert risk.min_payoff_dollars() == 25.0
