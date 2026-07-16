"""Tests for core/risk.py — the single source of money math and risk caps."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from core import risk

NOW = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)


class TestCostModel:
    def test_buy_yes_costs_price(self):
        assert risk.order_cost_dollars("buy", "yes", 40, 16) == 6.40

    def test_sell_collateralizes_complement(self):
        # selling YES at 22c: worst case is the 78c complement per contract
        assert risk.order_cost_dollars("sell", "yes", 20, 22) == 15.60


class TestClampCount:
    def test_buy_clamps_to_notional_cap(self):
        # the 2026-07-12 DAL alert: 60134 × 1¢ = $601 book depth, $50 cap
        assert risk.clamp_count("buy", "yes", 60134, 1, 50.0) == 5000

    def test_sell_clamps_on_complement_collateral(self):
        # selling YES at 22¢ collateralizes 78¢/contract → 64 fit in $50
        assert risk.clamp_count("sell", "yes", 100, 22, 50.0) == 64

    def test_small_orders_pass_through(self):
        assert risk.clamp_count("buy", "yes", 23, 18, 50.0) == 23

    def test_unaffordable_single_contract_is_zero(self):
        assert risk.clamp_count("buy", "yes", 10, 99, 0.5) == 0


class TestStationNightKey:
    """One cap bucket per station-night — high and low ladders included.
    Series names are irregular, so the registry does the mapping."""

    def test_high_and_low_ladders_share_the_station_night(self):
        assert (risk.station_night_key("KXHIGHNY-26JUL14-T90")
                == risk.station_night_key("KXLOWTNYC-26JUL14-B70.5")
                == "NYC:26JUL14")

    def test_asymmetric_series_pairs_resolve_via_the_registry(self):
        for high, low, awips in (("KXHIGHCHI", "KXLOWTCHI", "MDW"),
                                 ("KXHIGHTDAL", "KXLOWTDAL", "DFW"),
                                 ("KXHIGHTMIN", "KXLOWTMIN", "MSP")):
            assert (risk.station_night_key(f"{high}-26JUL14-T90")
                    == risk.station_night_key(f"{low}-26JUL14-B70.5")
                    == f"{awips}:26JUL14")

    def test_different_nights_stay_separate(self):
        assert (risk.station_night_key("KXHIGHNY-26JUL14-T90")
                != risk.station_night_key("KXHIGHNY-26JUL15-T90"))

    def test_unknown_series_falls_back_to_the_v1_key(self):
        assert risk.station_night_key("KXFOO-26JUL14-T90") == "KXFOO-26JUL14"

    def test_malformed_ticker_never_raises(self):
        assert risk.station_night_key("T1") == "T1"
        assert risk.station_night_key("") == ""


class TestOneSourceOfTruth:
    """The constants exist exactly once; consumers alias, never redefine."""

    def test_entry_cap_is_the_standing_20c_rule(self):
        assert risk.MAX_ENTRY_ASK_C == 20

    def test_take_queue_and_metar_sniper_share_the_entry_cap(self):
        import metar_sniper
        from core import take_queue

        assert take_queue.MAX_STAGE_ASK_C is risk.MAX_ENTRY_ASK_C
        assert metar_sniper.MAX_BUY_ASK_C is risk.MAX_ENTRY_ASK_C

    def test_snipers_share_the_wall_ask_signature(self):
        import cli_sniper
        import metar_sniper
        from core import walls

        assert walls.WALL_ASK_DEPTH == 10_000
        assert cli_sniper.WALL_ASK_DEPTH is walls.WALL_ASK_DEPTH
        assert metar_sniper.WALL_ASK_DEPTH is walls.WALL_ASK_DEPTH

    def test_scorecard_sources_the_fixed_notional_cap(self):
        from backtest import sniper_scorecard

        assert risk.DEFAULT_MAX_NOTIONAL == 50.0
        # scorecard stays env-or-fixed (reproducible grading regardless of
        # the live balance at rerun time) but the fixed number comes from here
        assert sniper_scorecard.max_notional_dollars() == risk.DEFAULT_MAX_NOTIONAL


def _write_snapshot(balance=150.0, age_min=5, **extra):
    snap = {"updated": (NOW - timedelta(minutes=age_min)).isoformat(),
            "balance": balance, **extra}
    risk.BANKROLL_SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    risk.BANKROLL_SNAPSHOT.write_text(json.dumps(snap))


class TestBankrollDollars:
    """live_watch's snapshot is the only balance source; anything short of
    a fresh, positive, tz-aware read is None — the caller degrades to the
    fixed caps."""

    def test_fresh_snapshot_reads(self):
        _write_snapshot(balance=175.37)
        assert risk.bankroll_dollars(NOW) == 175.37

    def test_missing_file_is_none(self):
        assert risk.bankroll_dollars(NOW) is None

    def test_stale_snapshot_is_none(self):
        # live_watch skips writes on degraded reads, so staleness IS the
        # failure signature (2026-07-05: a false $0 once reached the curve)
        _write_snapshot(age_min=risk.BANKROLL_MAX_AGE_MIN + 1)
        assert risk.bankroll_dollars(NOW) is None

    @pytest.mark.parametrize("balance", [None, 0, -5.0, "175.37", True])
    def test_invalid_balance_is_none(self, balance):
        _write_snapshot(balance=balance)
        assert risk.bankroll_dollars(NOW) is None

    def test_garbage_json_and_naive_timestamp_are_none(self):
        risk.BANKROLL_SNAPSHOT.write_text("not json {")
        assert risk.bankroll_dollars(NOW) is None
        risk.BANKROLL_SNAPSHOT.write_text(json.dumps(
            {"updated": "2026-07-16T11:55:00", "balance": 150.0}))  # no tz
        assert risk.bankroll_dollars(NOW) is None

    def test_never_raises_never_fetches(self):
        risk.BANKROLL_SNAPSHOT.write_text(json.dumps({"balance": 150.0}))
        assert risk.bankroll_dollars(NOW) is None  # no "updated" key


class TestBankrollDerivedCaps:
    """min(fixed, pct·bankroll): derivation only ever TIGHTENS — 'the caps
    stay' (claude.md §4); growth past the fixed caps is a human decision."""

    def test_low_bankroll_tightens_the_caps(self):
        _write_snapshot(balance=150.0)
        assert risk.night_cap_dollars(NOW) == pytest.approx(22.50)   # 15%
        assert risk.max_notional_dollars(NOW) == pytest.approx(45.0)  # 30%

    def test_high_bankroll_never_exceeds_the_fixed_caps(self):
        _write_snapshot(balance=1000.0)
        assert risk.night_cap_dollars(NOW) == risk.DEFAULT_NIGHT_CAP
        assert risk.max_notional_dollars(NOW) == risk.DEFAULT_MAX_NOTIONAL

    def test_tiny_bankroll_derives_tiny_caps(self):
        _write_snapshot(balance=10.0)
        assert risk.night_cap_dollars(NOW) == pytest.approx(1.50)

    def test_no_snapshot_degrades_to_fixed(self):
        assert risk.night_cap_dollars(NOW) == risk.DEFAULT_NIGHT_CAP
        assert risk.max_notional_dollars(NOW) == risk.DEFAULT_MAX_NOTIONAL

    def test_env_override_wins_and_may_exceed_fixed(self, monkeypatch):
        # the documented human escape hatch (take.py:54)
        _write_snapshot(balance=150.0)
        monkeypatch.setenv("TAKE_NIGHT_CAP_DOLLARS", "100")
        assert risk.night_cap_dollars(NOW) == 100.0

    def test_garbage_env_falls_through_to_derivation(self, monkeypatch):
        _write_snapshot(balance=150.0)
        monkeypatch.setenv("TAKE_NIGHT_CAP_DOLLARS", "abc")
        assert risk.night_cap_dollars(NOW) == pytest.approx(22.50)

    def test_provenance_names_the_regime(self, monkeypatch):
        _write_snapshot(balance=150.0)
        assert "15% of $150.00 bankroll" in risk.night_cap_detail(NOW)[1]
        risk.BANKROLL_SNAPSHOT.unlink()
        assert "fixed" in risk.night_cap_detail(NOW)[1]
        monkeypatch.setenv("TAKE_NIGHT_CAP_DOLLARS", "5")
        assert "env" in risk.night_cap_detail(NOW)[1]
