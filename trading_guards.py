#!/usr/bin/env python3
"""
TRADING GUARDS — Safety checks for automated trading.

Pure functions, no side effects. Each check returns a CheckResult:
  - Standard checks: (allowed: bool, reason: str)
  - check_daily_exposure: (allowed: bool, exposure_dollars: float, reason: str)

Used by auto_trader.py before every trade execution.

Emergency stop: touch PAUSE_TRADING to halt all auto-trading.
"""

import re
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo

from config import (
    MAX_DAILY_EXPOSURE,
    MAX_CORRELATED_EXPOSURE,
    AUTO_MAX_TRADES_PER_DAY,
    AUTO_CIRCUIT_BREAKER_LOSSES,
    AUTO_INTRADAY_DRAWDOWN_PCT,
    BOT_WINDOW_BUFFER_MIN,
)

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
PROJECT_ROOT = Path(__file__).resolve().parent
KILL_SWITCH_FILE = PROJECT_ROOT / "PAUSE_TRADING"


def check_kill_switch() -> tuple[bool, str]:
    """Check if PAUSE_TRADING file exists. Touch this file to halt all auto-trading."""
    if KILL_SWITCH_FILE.exists():
        return False, f"Kill switch active: {KILL_SWITCH_FILE} exists. Remove to resume."
    return True, "OK"


def check_daily_trade_count(positions: list[dict], today: date | None = None) -> tuple[bool, str]:
    """Check if we've exceeded max trades per day."""
    today = today or datetime.now(ET).date()
    today_trades = 0
    for p in positions:
        entry = p.get("entry_time", "")
        if entry:
            try:
                entry_date = datetime.fromisoformat(entry).date()
                if entry_date == today:
                    today_trades += 1
            except (ValueError, TypeError):
                pass
    if today_trades >= AUTO_MAX_TRADES_PER_DAY:
        return False, f"Daily trade limit reached ({today_trades}/{AUTO_MAX_TRADES_PER_DAY})"
    return True, f"OK ({today_trades}/{AUTO_MAX_TRADES_PER_DAY} trades today)"


def check_daily_exposure(
    positions: list[dict], balance: float, max_pct: float | None = None,
) -> tuple[bool, float, str]:
    """Check if total exposure opened today exceeds daily cap.

    Returns (allowed, current_exposure_dollars, reason).
    Note: this returns a 3-tuple unlike other guards (2-tuple).
    run_all_pre_trade_checks() handles the unpacking difference.
    """
    max_pct = max_pct or MAX_DAILY_EXPOSURE
    today = datetime.now(ET).date()
    daily_cost = 0.0
    for p in positions:
        if p.get("status") not in ("open", "resting", "pending_sell"):
            continue
        entry = p.get("entry_time", "")
        if entry:
            try:
                if datetime.fromisoformat(entry).date() == today:
                    daily_cost += p.get("contracts", 0) * p.get("avg_price", 0) / 100
            except (ValueError, TypeError):
                pass
    max_daily = balance * max_pct
    if balance <= 0:
        return False, 0.0, "Balance is $0"
    if daily_cost >= max_daily:
        return False, daily_cost, f"Daily exposure ${daily_cost:.2f} >= cap ${max_daily:.2f}"
    return True, daily_cost, f"OK (${daily_cost:.2f} / ${max_daily:.2f} used)"


def check_circuit_breaker(positions: list[dict]) -> tuple[bool, str]:
    """Check for consecutive losses (circuit breaker). Examines today's closed positions."""
    today = datetime.now(ET).date()
    today_closed = []
    for p in positions:
        if p.get("status") not in ("closed", "settled"):
            continue
        entry = p.get("entry_time", "")
        if entry:
            try:
                if datetime.fromisoformat(entry).date() == today:
                    today_closed.append(p)
            except (ValueError, TypeError):
                pass

    consecutive_losses = 0
    for p in sorted(today_closed, key=lambda x: x.get("entry_time", ""), reverse=True):
        if p.get("pnl_realized", 0) < 0:
            consecutive_losses += 1
        else:
            break
    if consecutive_losses >= AUTO_CIRCUIT_BREAKER_LOSSES:
        return False, f"Circuit breaker: {consecutive_losses} consecutive losses today"
    return True, f"OK ({consecutive_losses} consecutive losses)"


def check_intraday_drawdown(positions: list[dict], balance: float) -> tuple[bool, str]:
    """Check if cumulative realized losses today exceed the intraday drawdown limit.

    Unlike check_circuit_breaker (which only catches consecutive losses),
    this catches correlated losses across multiple cities in the same day.
    E.g., if NYC, CHI, and DEN all lose on the same cold front, total losses
    may exceed the limit even if interspersed with small wins.
    """
    today = datetime.now(ET).date()
    today_pnl = 0.0
    for p in positions:
        if p.get("status") not in ("closed", "settled"):
            continue
        entry = p.get("entry_time", "")
        if entry:
            try:
                if datetime.fromisoformat(entry).date() == today:
                    today_pnl += p.get("pnl_realized", 0)
            except (ValueError, TypeError):
                pass

    if balance <= 0:
        return False, "Balance is $0"

    max_loss = balance * AUTO_INTRADAY_DRAWDOWN_PCT
    if today_pnl < 0 and abs(today_pnl) >= max_loss:
        return False, f"Intraday drawdown: ${today_pnl:.2f} loss >= ${max_loss:.2f} limit ({AUTO_INTRADAY_DRAWDOWN_PCT:.0%} of NLV)"
    return True, f"OK (today PnL: ${today_pnl:+.2f}, limit: -${max_loss:.2f})"


def check_correlated_exposure(
    positions: list[dict], city_key: str, new_cost: float,
    balance: float, series_to_city: dict[str, str] | None = None,
    max_pct: float | None = None,
) -> tuple[bool, str]:
    """Check correlated exposure for a specific city."""
    max_pct = max_pct or MAX_CORRELATED_EXPOSURE
    series_to_city = series_to_city or {}
    city_exposure = 0.0
    for p in positions:
        if p.get("status") not in ("open", "resting", "pending_sell"):
            continue
        ticker = p.get("ticker", "")
        match = re.match(r"^([A-Z]+)", ticker)
        if match:
            p_city = series_to_city.get(match.group(1))
            if p_city == city_key:
                city_exposure += p.get("contracts", 0) * p.get("avg_price", 0) / 100
    max_corr = balance * max_pct
    if city_exposure + new_cost > max_corr:
        return False, f"{city_key} exposure ${city_exposure + new_cost:.2f} > cap ${max_corr:.2f}"
    return True, f"OK ({city_key}: ${city_exposure:.2f} + ${new_cost:.2f} < ${max_corr:.2f})"


def check_bot_window(
    city_key: str, dsm_times_z: list[str], six_hour_z: list[str],
) -> tuple[bool, str]:
    """Check if we're within BOT_WINDOW_BUFFER_MIN minutes of a DSM/6-hour release."""
    now_utc = datetime.now(UTC)
    now_min = now_utc.hour * 60 + now_utc.minute
    for t_str in dsm_times_z + six_hour_z:
        parts = t_str.split(":")
        if len(parts) != 2:
            continue
        h, m = int(parts[0]), int(parts[1])
        release_min = h * 60 + m
        diff = abs(release_min - now_min)
        diff = min(diff, 1440 - diff)  # wrap around midnight
        if diff < BOT_WINDOW_BUFFER_MIN:
            return False, f"{city_key}: Within {BOT_WINDOW_BUFFER_MIN}min of release at {t_str}Z"
    return True, "OK"


def run_all_pre_trade_checks(
    positions: list[dict], balance: float, city_key: str,
    new_cost: float, dsm_times_z: list[str], six_hour_z: list[str],
    series_to_city: dict[str, str] | None = None, dry_run: bool = False,
) -> tuple[bool, list[str]]:
    """Run all safety checks. Returns (all_passed, list_of_reasons)."""
    if dry_run:
        return True, ["DRY RUN — checks bypassed"]

    results: list[str] = []
    all_ok = True

    for check_fn, args in [
        (check_kill_switch, ()),
        (check_daily_trade_count, (positions,)),
        (check_circuit_breaker, (positions,)),
        (check_intraday_drawdown, (positions, balance)),
    ]:
        ok, reason = check_fn(*args)
        results.append(f"{'PASS' if ok else 'FAIL'}: {reason}")
        if not ok:
            all_ok = False

    # check_daily_exposure returns 3-tuple: (ok, exposure_dollars, reason)
    ok, _, reason = check_daily_exposure(positions, balance)
    results.append(f"{'PASS' if ok else 'FAIL'}: {reason}")
    if not ok:
        all_ok = False

    ok, reason = check_correlated_exposure(
        positions, city_key, new_cost, balance, series_to_city=series_to_city,
    )
    results.append(f"{'PASS' if ok else 'FAIL'}: {reason}")
    if not ok:
        all_ok = False

    ok, reason = check_bot_window(city_key, dsm_times_z, six_hour_z)
    results.append(f"{'PASS' if ok else 'FAIL'}: {reason}")
    if not ok:
        all_ok = False

    return all_ok, results
