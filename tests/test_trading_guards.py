#!/usr/bin/env python3
"""Tests for trading_guards.py — pure function tests, no mocking needed."""

from datetime import datetime
from zoneinfo import ZoneInfo


from trading_guards import (
    KILL_SWITCH_FILE,
    check_kill_switch,
    check_daily_trade_count,
    check_daily_exposure,
    check_circuit_breaker,
    check_intraday_drawdown,
    check_correlated_exposure,
    check_bot_window,
    check_upwind_shield,
    run_all_pre_trade_checks,
)

ET = ZoneInfo("America/New_York")
NOW_ISO = datetime.now(ET).isoformat()


# ── Kill switch ──

def test_kill_switch_off():
    """No PAUSE_TRADING file → allowed."""
    # Clean up just in case
    if KILL_SWITCH_FILE.exists():
        KILL_SWITCH_FILE.unlink()
    ok, reason = check_kill_switch()
    assert ok
    assert reason == "OK"


def test_kill_switch_on(tmp_path, monkeypatch):
    """PAUSE_TRADING file exists → blocked."""
    kill_file = tmp_path / "PAUSE_TRADING"
    kill_file.touch()
    monkeypatch.setattr("trading_guards.KILL_SWITCH_FILE", kill_file)
    ok, reason = check_kill_switch()
    assert not ok
    assert "Kill switch" in reason


# ── Daily trade count ──

def test_trade_count_under_limit():
    positions = [{"entry_time": NOW_ISO} for _ in range(3)]
    ok, reason = check_daily_trade_count(positions, today=datetime.now(ET).date())
    assert ok
    assert "3/8" in reason


def test_trade_count_at_limit():
    positions = [{"entry_time": NOW_ISO} for _ in range(8)]
    ok, reason = check_daily_trade_count(positions, today=datetime.now(ET).date())
    assert not ok
    assert "8/8" in reason


def test_trade_count_ignores_other_days():
    positions = [{"entry_time": "2020-01-01T12:00:00"} for _ in range(10)]
    ok, reason = check_daily_trade_count(positions, today=datetime.now(ET).date())
    assert ok
    assert "0/8" in reason


def test_trade_count_missing_entry_time():
    positions = [{"status": "open"}, {"entry_time": NOW_ISO}]
    ok, reason = check_daily_trade_count(positions, today=datetime.now(ET).date())
    assert ok
    assert "1/8" in reason


# ── Daily exposure ──

def test_exposure_under_cap():
    positions = [{"status": "open", "contracts": 5, "avg_price": 20, "entry_time": NOW_ISO}]
    ok, cost, reason = check_daily_exposure(positions, balance=100.0)
    assert ok
    assert cost == 1.0  # 5 * 20/100


def test_exposure_over_cap():
    positions = [{"status": "open", "contracts": 100, "avg_price": 50, "entry_time": NOW_ISO}]
    ok, cost, reason = check_daily_exposure(positions, balance=100.0)
    assert not ok  # $50 > 25% of $100 = $25


def test_exposure_zero_balance():
    ok, cost, reason = check_daily_exposure([], balance=0.0)
    assert not ok
    assert "Balance is $0" in reason


def test_exposure_ignores_closed():
    positions = [{"status": "closed", "contracts": 100, "avg_price": 50, "entry_time": NOW_ISO}]
    ok, cost, reason = check_daily_exposure(positions, balance=100.0)
    assert ok
    assert cost == 0.0


# ── Circuit breaker ──

def test_circuit_breaker_no_losses():
    positions = [{"status": "closed", "pnl_realized": 1.0, "entry_time": NOW_ISO}]
    ok, reason = check_circuit_breaker(positions)
    assert ok
    assert "0 consecutive" in reason


def test_circuit_breaker_3_losses():
    positions = [
        {"status": "closed", "pnl_realized": -0.5, "entry_time": NOW_ISO}
        for _ in range(3)
    ]
    ok, reason = check_circuit_breaker(positions)
    assert ok  # 3 < 4 threshold


def test_circuit_breaker_4_losses():
    positions = [
        {"status": "closed", "pnl_realized": -0.5, "entry_time": NOW_ISO}
        for _ in range(4)
    ]
    ok, reason = check_circuit_breaker(positions)
    assert not ok
    assert "4 consecutive" in reason


def test_circuit_breaker_win_breaks_streak():
    positions = [
        {"status": "closed", "pnl_realized": -1.0, "entry_time": f"2026-02-13T10:0{i}:00-05:00"}
        for i in range(3)
    ]
    positions.append({"status": "closed", "pnl_realized": 2.0, "entry_time": "2026-02-13T10:04:00-05:00"})
    positions.append({"status": "closed", "pnl_realized": -1.0, "entry_time": "2026-02-13T10:05:00-05:00"})
    # Most recent is 1 loss, then a win — streak is 1
    ok, reason = check_circuit_breaker(positions)
    assert ok


# ── Intraday drawdown ──

def test_intraday_drawdown_no_losses():
    """No closed positions today → allowed."""
    ok, reason = check_intraday_drawdown([], balance=100.0)
    assert ok
    assert "today PnL: $+0.00" in reason


def test_intraday_drawdown_small_loss():
    """Losses under 10% of balance → allowed."""
    positions = [
        {"status": "closed", "pnl_realized": -5.0, "entry_time": NOW_ISO},
    ]
    ok, reason = check_intraday_drawdown(positions, balance=100.0)
    assert ok  # $5 < 10% of $100 = $10


def test_intraday_drawdown_at_limit():
    """Losses at exactly 10% of balance → blocked."""
    positions = [
        {"status": "closed", "pnl_realized": -10.0, "entry_time": NOW_ISO},
    ]
    ok, reason = check_intraday_drawdown(positions, balance=100.0)
    assert not ok
    assert "Intraday drawdown" in reason


def test_intraday_drawdown_multiple_losses():
    """Cumulative losses across multiple positions → blocked."""
    positions = [
        {"status": "closed", "pnl_realized": -4.0, "entry_time": NOW_ISO},
        {"status": "closed", "pnl_realized": -3.0, "entry_time": NOW_ISO},
        {"status": "closed", "pnl_realized": -4.0, "entry_time": NOW_ISO},
    ]
    ok, reason = check_intraday_drawdown(positions, balance=100.0)
    assert not ok  # $11 > 10% of $100


def test_intraday_drawdown_wins_offset_losses():
    """Wins reduce net PnL → allowed if net is positive."""
    positions = [
        {"status": "closed", "pnl_realized": -8.0, "entry_time": NOW_ISO},
        {"status": "closed", "pnl_realized": 5.0, "entry_time": NOW_ISO},
    ]
    ok, reason = check_intraday_drawdown(positions, balance=100.0)
    assert ok  # net -$3 < $10


def test_intraday_drawdown_ignores_open_positions():
    """Only closed/settled positions count toward drawdown."""
    positions = [
        {"status": "open", "pnl_realized": -50.0, "entry_time": NOW_ISO},
    ]
    ok, reason = check_intraday_drawdown(positions, balance=100.0)
    assert ok  # open positions ignored


def test_intraday_drawdown_ignores_other_days():
    """Losses from other days don't count."""
    positions = [
        {"status": "closed", "pnl_realized": -50.0, "entry_time": "2020-01-01T12:00:00"},
    ]
    ok, reason = check_intraday_drawdown(positions, balance=100.0)
    assert ok


def test_intraday_drawdown_zero_balance():
    """Zero balance → blocked."""
    ok, reason = check_intraday_drawdown([], balance=0.0)
    assert not ok
    assert "Balance is $0" in reason


def test_intraday_drawdown_returns_2tuple():
    """check_intraday_drawdown should return (bool, str)."""
    result = check_intraday_drawdown([], balance=100.0)
    assert len(result) == 2
    assert isinstance(result[0], bool)
    assert isinstance(result[1], str)


# ── Correlated exposure ──

def test_correlated_exposure_under_cap():
    positions = [
        {"status": "open", "ticker": "KXHIGHNY-26FEB14-B36.5", "contracts": 5, "avg_price": 20}
    ]
    series_to_city = {"KXHIGHNY": "NYC"}
    ok, reason = check_correlated_exposure(
        positions, "NYC", new_cost=1.0, balance=100.0, series_to_city=series_to_city,
    )
    assert ok  # $1 + $1 = $2 < 15% of $100 = $15


def test_correlated_exposure_over_cap():
    positions = [
        {"status": "open", "ticker": "KXHIGHNY-26FEB14-B36.5", "contracts": 50, "avg_price": 25}
    ]
    series_to_city = {"KXHIGHNY": "NYC"}
    ok, reason = check_correlated_exposure(
        positions, "NYC", new_cost=3.0, balance=100.0, series_to_city=series_to_city,
    )
    assert not ok  # $12.50 + $3 = $15.50 > $15


def test_correlated_exposure_different_city():
    positions = [
        {"status": "open", "ticker": "KXHIGHNY-26FEB14-B36.5", "contracts": 50, "avg_price": 25}
    ]
    series_to_city = {"KXHIGHNY": "NYC", "KXHIGHDEN": "DEN"}
    ok, reason = check_correlated_exposure(
        positions, "DEN", new_cost=5.0, balance=100.0, series_to_city=series_to_city,
    )
    assert ok  # DEN has $0 exposure + $5 < $15


# ── Bot window ──

def test_bot_window_safe_empty():
    ok, reason = check_bot_window("NYC", [], [])
    assert ok


def test_bot_window_returns_tuple():
    ok, reason = check_bot_window("NYC", ["03:00"], ["09:00"])
    assert isinstance(ok, bool)
    assert isinstance(reason, str)


# ── Upwind shield ──

class _FakeVector:
    """Minimal stand-in for proxy_arb_engine.PropagationVector."""

    def __init__(
        self,
        proxy_temp_f: float | None,
        thermal_impact_f: float | None = None,
        eta_minutes: float = 120.0,
        is_converging: bool = True,
        name: str = "PHL",
    ):
        self.proxy_temp_f = proxy_temp_f
        self.thermal_impact_f = thermal_impact_f
        self.eta_minutes = eta_minutes
        self.is_converging = is_converging
        self.proxy = type("Proxy", (), {"name": name})()


MORNING_ET = datetime(2026, 6, 12, 8, 0, tzinfo=ET)   # 8 AM NYC — diurnal minimum
PEAK_ET = datetime(2026, 6, 12, 15, 0, tzinfo=ET)     # 3 PM NYC — diurnal peak


def test_shield_no_vectors():
    ok, reason = check_upwind_shield("NYC", 70.0, 72.0, [], "yes", now=MORNING_ET)
    assert ok
    assert "no upwind vectors" in reason


def test_shield_morning_cold_proxy_passes():
    """Core fix: 8 AM proxy at 48°F is below a 70-72 daily-HIGH bracket because it's
    morning, not because a cold front is inbound. Small decayed impact → no veto."""
    vectors = [_FakeVector(proxy_temp_f=48.0, thermal_impact_f=-0.5)]
    ok, reason = check_upwind_shield("NYC", 70.0, 72.0, vectors, "yes", now=MORNING_ET)
    assert ok
    assert "clear" in reason


def test_shield_morning_strong_cold_front_blocked():
    """A genuinely contradicting cold front (large decayed impact) still vetoes at 8 AM."""
    vectors = [_FakeVector(proxy_temp_f=40.0, thermal_impact_f=-6.0)]
    ok, reason = check_upwind_shield("NYC", 70.0, 72.0, vectors, "yes", now=MORNING_ET)
    assert not ok
    assert "Cold front" in reason


def test_shield_warm_proxy_above_ceiling_blocked_any_hour():
    """Converging air already at/above the ceiling breaks a YES thesis even at 8 AM —
    temps only rise off the morning minimum."""
    vectors = [_FakeVector(proxy_temp_f=74.0, thermal_impact_f=0.2)]
    ok, reason = check_upwind_shield("NYC", 70.0, 72.0, vectors, "yes", now=MORNING_ET)
    assert not ok
    assert "ceiling" in reason


def test_shield_warm_impact_blocked():
    """Proxy below the ceiling but decayed warm impact would push high above it."""
    vectors = [_FakeVector(proxy_temp_f=60.0, thermal_impact_f=3.0)]
    ok, reason = check_upwind_shield("NYC", 70.0, 72.0, vectors, "yes", now=MORNING_ET)
    assert not ok
    assert "Warm front" in reason


def test_shield_near_peak_cold_proxy_blocked():
    """Near the diurnal peak, instantaneous proxy temps approximate the daily high —
    sub-floor air converging at 3 PM vetoes even with small decayed impact."""
    vectors = [_FakeVector(proxy_temp_f=65.0, thermal_impact_f=-0.4)]
    ok, reason = check_upwind_shield("NYC", 70.0, 72.0, vectors, "yes", now=PEAK_ET)
    assert not ok
    assert "Cold front" in reason


def test_shield_impact_threshold_boundary():
    """Threshold = half bracket width (clamped >= 1.0). Bracket 70-72 → 1.0°F."""
    at_threshold = [_FakeVector(proxy_temp_f=55.0, thermal_impact_f=-1.0)]
    ok, _ = check_upwind_shield("NYC", 70.0, 72.0, at_threshold, "yes", now=MORNING_ET)
    assert not ok

    under_threshold = [_FakeVector(proxy_temp_f=55.0, thermal_impact_f=-0.9)]
    ok, _ = check_upwind_shield("NYC", 70.0, 72.0, under_threshold, "yes", now=MORNING_ET)
    assert ok


def test_shield_non_converging_ignored():
    vectors = [_FakeVector(proxy_temp_f=40.0, thermal_impact_f=-6.0, is_converging=False)]
    ok, _ = check_upwind_shield("NYC", 70.0, 72.0, vectors, "yes", now=PEAK_ET)
    assert ok


def test_shield_long_eta_ignored():
    vectors = [_FakeVector(proxy_temp_f=40.0, thermal_impact_f=-6.0, eta_minutes=999.0)]
    ok, _ = check_upwind_shield("NYC", 70.0, 72.0, vectors, "yes", now=PEAK_ET)
    assert ok


def test_shield_placeholder_impact_ignored():
    """When target ASOS is unavailable the engine leaves thermal_impact_f == raw proxy
    temp. That must not be read as a +48°F warm impact."""
    vectors = [_FakeVector(proxy_temp_f=48.0, thermal_impact_f=48.0)]
    ok, _ = check_upwind_shield("NYC", 70.0, 72.0, vectors, "yes", now=MORNING_ET)
    assert ok


def test_shield_missing_impact_attribute():
    """Vectors without a usable impact fall back to peak-gated instantaneous checks only."""
    vectors = [_FakeVector(proxy_temp_f=48.0, thermal_impact_f=None)]
    ok, _ = check_upwind_shield("NYC", 70.0, 72.0, vectors, "yes", now=MORNING_ET)
    assert ok
    ok, _ = check_upwind_shield("NYC", 70.0, 72.0, vectors, "yes", now=PEAK_ET)
    assert not ok


def test_shield_no_side_inside_bracket_near_peak_blocked():
    vectors = [_FakeVector(proxy_temp_f=71.0, thermal_impact_f=0.3)]
    ok, reason = check_upwind_shield("NYC", 70.0, 72.0, vectors, "no", now=PEAK_ET)
    assert not ok
    assert "inside bracket" in reason


def test_shield_no_side_inside_bracket_morning_passes():
    """A morning reading inside the daily-high bracket says nothing about where the
    HIGH lands — NO trades aren't vetoed on instantaneous temps outside the peak window."""
    vectors = [_FakeVector(proxy_temp_f=71.0, thermal_impact_f=0.3)]
    ok, _ = check_upwind_shield("NYC", 70.0, 72.0, vectors, "no", now=MORNING_ET)
    assert ok


def test_shield_uses_city_local_time():
    """3 PM ET is 1 PM in Denver — still inside Denver's peak window; 8 AM ET (6 AM MT)
    is not."""
    vectors = [_FakeVector(proxy_temp_f=65.0, thermal_impact_f=-0.4, name="COS")]
    ok, _ = check_upwind_shield("DEN", 70.0, 72.0, vectors, "yes", now=PEAK_ET)
    assert not ok
    ok, _ = check_upwind_shield("DEN", 70.0, 72.0, vectors, "yes", now=MORNING_ET)
    assert ok


# ── run_all_pre_trade_checks ──

def test_dry_run_bypasses_checks():
    ok, reasons = run_all_pre_trade_checks(
        positions=[], balance=0.0, city_key="NYC", new_cost=100.0,
        dsm_times_z=[], six_hour_z=[], dry_run=True,
    )
    assert ok
    assert "DRY RUN" in reasons[0]


def test_all_checks_pass():
    ok, reasons = run_all_pre_trade_checks(
        positions=[], balance=100.0, city_key="NYC", new_cost=1.0,
        dsm_times_z=["03:00"], six_hour_z=["09:00"],
        series_to_city={"KXHIGHNY": "NYC"},
    )
    # May or may not pass depending on current time vs bot window
    assert isinstance(ok, bool)
    assert len(reasons) == 7  # 7 checks total


def test_run_all_includes_upwind_shield():
    ok, reasons = run_all_pre_trade_checks(
        positions=[], balance=100.0, city_key="NYC", new_cost=1.0,
        dsm_times_z=["03:00"], six_hour_z=["09:00"],
        series_to_city={"KXHIGHNY": "NYC"},
        proxy_vectors=[_FakeVector(proxy_temp_f=48.0, thermal_impact_f=-0.5)],
        bracket_bounds=(70.0, 72.0),
        trade_side="yes",
    )
    assert isinstance(ok, bool)
    assert len(reasons) == 8  # 7 standard checks + upwind shield


# ── Type hint consistency ──

def test_check_kill_switch_returns_2tuple():
    """check_kill_switch should return (bool, str)."""
    result = check_kill_switch()
    assert len(result) == 2
    assert isinstance(result[0], bool)
    assert isinstance(result[1], str)


def test_check_daily_exposure_returns_3tuple():
    """check_daily_exposure returns (bool, float, str) — documented 3-tuple."""
    result = check_daily_exposure([], balance=100.0)
    assert len(result) == 3
    assert isinstance(result[0], bool)
    assert isinstance(result[1], float)
    assert isinstance(result[2], str)


def test_check_circuit_breaker_returns_2tuple():
    """check_circuit_breaker should return (bool, str)."""
    result = check_circuit_breaker([])
    assert len(result) == 2
    assert isinstance(result[0], bool)
    assert isinstance(result[1], str)
