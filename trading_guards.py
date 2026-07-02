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
    STATIONS,
)

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
PROJECT_ROOT = Path(__file__).resolve().parent
KILL_SWITCH_FILE = PROJECT_ROOT / "PAUSE_TRADING"

# ── Upwind shield tuning ──
SHIELD_MAX_ETA_MIN = 240         # only fronts arriving within 4 h matter
SHIELD_PEAK_HOUR_LOCAL = 15      # typical diurnal max (~3 PM local)
SHIELD_PEAK_WINDOW_HOURS = 4     # instantaneous temps ≈ daily high within ±4 h of peak
SHIELD_IMPACT_FLOOR_F = 1.0      # min decayed ΔT (°F) that can veto on its own
SHIELD_IMPACT_CAP_F = 4.0        # cap so wide tail brackets still get shielded


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


def _shield_impact_threshold_f(bracket_low: float, bracket_high: float) -> float:
    """Decayed thermal impact (°F) needed to displace a mid-bracket high past a bound.

    Half the bracket width, clamped to [SHIELD_IMPACT_FLOOR_F, SHIELD_IMPACT_CAP_F]
    so noise can't veto narrow brackets and tail brackets aren't unshieldable.
    """
    half_width = (bracket_high - bracket_low) / 2.0
    return min(max(half_width, SHIELD_IMPACT_FLOOR_F), SHIELD_IMPACT_CAP_F)


def _near_diurnal_peak(city_key: str, now: datetime | None = None) -> bool:
    """True when target-local time is within SHIELD_PEAK_WINDOW_HOURS of the typical daily max."""
    try:
        tz = ZoneInfo(STATIONS[city_key].timezone)
    except KeyError:  # unknown city or tz lookup failure — fall back to ET
        tz = ET
    local = (now or datetime.now(UTC)).astimezone(tz)
    return abs(local.hour - SHIELD_PEAK_HOUR_LOCAL) <= SHIELD_PEAK_WINDOW_HOURS


def check_upwind_shield(
    city_key: str,
    bracket_low: float,
    bracket_high: float,
    vectors: list,
    trade_side: str = "yes",
    *,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Block trades when an incoming upwind front genuinely contradicts the daily-high thesis.

    Iterates PropagationVector objects from proxy_arb_engine (converging, ETA <=
    SHIELD_MAX_ETA_MIN, with temperature data). Two veto channels:

    1. Decayed thermal impact (any hour): thermal_impact_f is the proxy-target
       delta after exponential distance decay, resolved by the engine against the
       target's CURRENT temperature — hour-of-day neutral. A signal strong enough
       to displace a mid-bracket high past a bound vetoes.
    2. Instantaneous proxy temperature (peak-gated): raw proxy temps only
       approximate the daily high near the diurnal peak. A 6-10 AM observation
       sits near the diurnal MINIMUM, so comparing it to a daily-HIGH bracket
       floor vetoed every legitimate morning YES entry; the cold-side and
       inside-bracket checks now apply only within SHIELD_PEAK_WINDOW_HOURS of
       the typical peak. The warm-side check (proxy already at/above ceiling)
       stays unconditional: temps only rise off the morning minimum, so
       converging air at the ceiling breaks a YES thesis at any hour.

    Vectors whose thermal_impact_f equals the raw proxy temp (engine placeholder
    when target ASOS was unavailable) skip the impact channel.
    """
    if not vectors:
        return True, "OK (no upwind vectors)"

    near_peak = _near_diurnal_peak(city_key, now)
    impact_threshold = _shield_impact_threshold_f(bracket_low, bracket_high)

    for v in vectors:
        if not getattr(v, "is_converging", False):
            continue
        eta = getattr(v, "eta_minutes", float("inf"))
        if eta > SHIELD_MAX_ETA_MIN:
            continue
        proxy_temp = getattr(v, "proxy_temp_f", None)
        if proxy_temp is None:
            continue
        proxy_name = (
            getattr(v.proxy, "name", "proxy") if hasattr(v, "proxy") else "proxy"
        )
        impact = getattr(v, "thermal_impact_f", None)
        if impact is not None and impact == round(proxy_temp, 2):
            impact = None  # engine placeholder: target ASOS temp was unavailable

        if trade_side == "yes":
            if proxy_temp >= bracket_high:
                return (
                    False,
                    f"SHIELD: Warm front at {proxy_name} ({proxy_temp:.1f}°F) "
                    f"ETA {eta:.0f} min will breach ceiling ({bracket_high}°F)",
                )
            if impact is not None and impact >= impact_threshold:
                return (
                    False,
                    f"SHIELD: Warm front at {proxy_name} (decayed impact {impact:+.1f}°F) "
                    f"ETA {eta:.0f} min would push high above ceiling ({bracket_high}°F)",
                )
            if impact is not None and impact <= -impact_threshold:
                return (
                    False,
                    f"SHIELD: Cold front at {proxy_name} (decayed impact {impact:+.1f}°F) "
                    f"ETA {eta:.0f} min would drag high below floor ({bracket_low}°F)",
                )
            if near_peak and proxy_temp < bracket_low:
                return (
                    False,
                    f"SHIELD: Cold front at {proxy_name} ({proxy_temp:.1f}°F) "
                    f"ETA {eta:.0f} min near peak will breach floor ({bracket_low}°F)",
                )
        elif trade_side == "no":
            if near_peak and bracket_low <= proxy_temp < bracket_high:
                return (
                    False,
                    f"SHIELD: Proxy temp at {proxy_name} ({proxy_temp:.1f}°F) "
                    f"ETA {eta:.0f} min near peak is inside bracket ({bracket_low}–{bracket_high}°F)",
                )

    return True, "OK (upwind shield clear)"


def run_all_pre_trade_checks(
    positions: list[dict], balance: float, city_key: str,
    new_cost: float, dsm_times_z: list[str], six_hour_z: list[str],
    series_to_city: dict[str, str] | None = None, dry_run: bool = False,
    *,
    proxy_vectors: list | None = None,
    bracket_bounds: tuple[float, float] | None = None,
    trade_side: str = "yes",
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

    if proxy_vectors is not None and bracket_bounds is not None:
        ok, reason = check_upwind_shield(
            city_key, bracket_bounds[0], bracket_bounds[1], proxy_vectors, trade_side,
        )
        results.append(f"{'PASS' if ok else 'FAIL'}: {reason}")
        if not ok:
            all_ok = False

    return all_ok, results
