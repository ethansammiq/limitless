#!/usr/bin/env python3
"""
PEAK MONITOR — Strategy F: Post-Peak Lock-In

Polls IEM ASOS (primary) and NWS (fallback) observations for all 5 stations, tracks the running daily max,
and detects when the daily high temperature has peaked.  Once peak is
confirmed (temperature declining for N consecutive observations over a
minimum time window), the settlement bracket is known with ~95%+ certainty
hours before the Kalshi market closes.

Detection algorithm:
  1. Fetch recent observations (hourly for most ASOS, 5-min for majors)
  2. Track running daily max per city
  3. Peak is "confirmed" when:
     a) At least PEAK_MIN_DECLINE_OBS consecutive observations are below
        the running max
     b) The most recent obs is at least PEAK_MIN_DROP_F degrees below the max
     c) At least PEAK_MIN_DECLINE_MINUTES have elapsed since the max was set
     d) Current time is past PEAK_EARLIEST_HOUR local (don't false-trigger
        on morning warming pauses)
  4. Fire Discord alert with settlement bracket and market price

Usage:
  python3 peak_monitor.py                 # Poll once, alert if peak confirmed
  python3 peak_monitor.py --watch         # Continuous polling every 5 min
  python3 peak_monitor.py --city LAX      # Single city
  python3 peak_monitor.py --dry-run       # Print alerts, don't send

Cron setup (poll every 10 min from 1 PM to 10 PM ET):
  */10 13-22 * * *  cd /path/to/limitless && python3 peak_monitor.py --quiet >> /tmp/peak_monitor.log 2>&1
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from config import (
    STATIONS,
    PEAK_MIN_DECLINE_OBS,
    PEAK_MIN_DROP_F,
    PEAK_MIN_DECLINE_MINUTES,
    PEAK_EARLIEST_HOUR,
    PEAK_LATEST_HOUR,
    PEAK_POLL_INTERVAL_SEC,
)
from core.obs import climate_day_start
from market_timeseries import extract_target_date_from_ticker
from notifications import send_discord_embeds
from heartbeat import write_heartbeat
from log_setup import get_logger

logger = get_logger(__name__)

# ─── Configuration ─────────────────────────────────────

NWS_OBS_LIMIT = 30             # Fetch last 30 observations (covers ~24h)
IEM_BASE_URL = "https://mesonet.agron.iastate.edu"

# State file for tracking across cron invocations
PROJECT_ROOT = Path(__file__).resolve().parent
STATE_FILE = PROJECT_ROOT / "peak_state.json"

ET_TZ = ZoneInfo("America/New_York")


# Max bracket-price fetch attempts after a peak is confirmed before we give up
# and alert with "market may be closed" (the fetch can't distinguish an API
# blip from a genuinely closed market, so we retry a few polls first).
PEAK_BRACKET_FETCH_MAX_ATTEMPTS = 3

# Mirror of the crontab schedule (`*/10 13-22 * * *`, ET host clock). Used
# only to detect the day's FINAL cron tick so the process can linger past it
# for cities whose local-time monitoring window outlives the ET-based cron
# (LAX: 12:00-22:00 PT runs until 01:00 ET, ~2h past the last tick).
# If the crontab is ever extended to cover that window (e.g.
# `*/10 0,13-23 * * *`), update or remove this so two processes never poll
# concurrently.
CRON_TICK_INTERVAL_MIN = 10
CRON_LAST_HOUR_ET = 22


# ─── Data Structures ──────────────────────────────────

@dataclass
class Observation:
    """A single temperature observation."""
    temp_f: float
    timestamp: datetime
    station: str

    def __repr__(self) -> str:
        local = self.timestamp.strftime("%I:%M %p")
        return f"{self.station} {local}: {self.temp_f:.1f}°F"


@dataclass
class CityPeakState:
    """Tracks peak detection state for one city."""
    city_key: str
    date: str                              # ISO date being tracked (local)
    running_max: float = -999.0
    max_time: datetime | None = None       # When the max was observed
    observations: list[Observation] = field(default_factory=list)
    peak_confirmed: bool = False
    peak_temp: float = 0.0
    peak_bracket: str = ""                 # e.g. "73-74"
    alerted: bool = False                  # Day complete: alert sent AND trade handoff resolved
    alert_sent: bool = False               # Discord alert dispatched (guards re-alert on retries)
    trade_attempts: int = 0                # Strategy G execution attempts today
    bracket_fetch_failures: int = 0        # Failed bracket-price fetches post-confirmation

    def to_dict(self) -> dict:
        return {
            "city_key": self.city_key,
            "date": self.date,
            "running_max": self.running_max,
            "max_time": self.max_time.isoformat() if self.max_time else None,
            "peak_confirmed": self.peak_confirmed,
            "peak_temp": self.peak_temp,
            "peak_bracket": self.peak_bracket,
            "alerted": self.alerted,
            "alert_sent": self.alert_sent,
            "trade_attempts": self.trade_attempts,
            "bracket_fetch_failures": self.bracket_fetch_failures,
        }

    @classmethod
    def from_dict(cls, d: dict) -> CityPeakState:
        state = cls(
            city_key=d["city_key"],
            date=d["date"],
            running_max=d.get("running_max", -999.0),
            peak_confirmed=d.get("peak_confirmed", False),
            peak_temp=d.get("peak_temp", 0.0),
            peak_bracket=d.get("peak_bracket", ""),
            alerted=d.get("alerted", False),
            alert_sent=d.get("alert_sent", False),
            trade_attempts=d.get("trade_attempts", 0),
            bracket_fetch_failures=d.get("bracket_fetch_failures", 0),
        )
        if d.get("max_time"):
            state.max_time = datetime.fromisoformat(d["max_time"])
        return state


# ─── State Persistence ─────────────────────────────────

def load_state() -> dict[str, CityPeakState]:
    """Load peak tracking state from disk."""
    if not STATE_FILE.exists():
        return {}
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        return {k: CityPeakState.from_dict(v) for k, v in data.items()}
    except (json.JSONDecodeError, KeyError) as e:
        logger.warning("Corrupt state file, resetting: %s", e)
        return {}


def save_state(states: dict[str, CityPeakState]) -> None:
    """Persist peak tracking state to disk."""
    data = {k: v.to_dict() for k, v in states.items()}
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    tmp.rename(STATE_FILE)


# ─── IEM Observation Fetching (primary — free, fast) ──

async def fetch_iem_observations(
    session: aiohttp.ClientSession,
    city_key: str,
) -> list[Observation]:
    """Fetch today's hourly obs from Iowa Environmental Mesonet ASOS API.

    IEM provides free ASOS/METAR data in CSV format with ~1-minute latency.
    This is faster and more reliable than NWS for peak detection.
    """
    station_cfg = STATIONS.get(city_key)
    if not station_cfg or not station_cfg.iem_station:
        return []

    tz = ZoneInfo(station_cfg.timezone)
    today_local = datetime.now(tz).date()
    tomorrow_local = today_local + timedelta(days=1)

    url = (
        f"{IEM_BASE_URL}/cgi-bin/request/asos.py"
        f"?station={station_cfg.iem_station}"
        f"&data=tmpf"
        f"&year1={today_local.year}&month1={today_local.month}&day1={today_local.day}"
        f"&year2={tomorrow_local.year}&month2={tomorrow_local.month}&day2={tomorrow_local.day}"
        f"&tz=America%2FNew_York"  # All timestamps in ET for consistent parsing
        f"&format=onlycomma&latlon=no&elev=no&missing=M&trace=T"
        f"&direct=no&report_type=3"
    )

    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                logger.warning("IEM %s returned %d", city_key, resp.status)
                return []
            text = await resp.text()
    except Exception as e:
        logger.warning("IEM fetch failed for %s: %s", city_key, e)
        return []

    observations = []
    et_tz = ZoneInfo("America/New_York")
    for line in text.strip().split("\n")[1:]:  # Skip header
        parts = line.split(",")
        if len(parts) < 3:
            continue
        try:
            # IEM returns timestamps in the requested tz (ET)
            ts_str = parts[1].strip()
            temp_str = parts[2].strip()
            if temp_str == "M":
                continue
            temp_f = float(temp_str)
            ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M").replace(tzinfo=et_tz)

            # Filter to today in LOCAL timezone
            ts_local = ts.astimezone(tz)
            if ts_local.date() != today_local:
                continue

            observations.append(Observation(
                temp_f=round(temp_f, 1),
                timestamp=ts,
                station=station_cfg.station_id,
            ))
        except (ValueError, IndexError):
            continue

    observations.sort(key=lambda o: o.timestamp)
    return observations


# ─── NWS Observation Fetching (fallback) ─────────────

async def fetch_today_observations(
    session: aiohttp.ClientSession,
    city_key: str,
) -> list[Observation]:
    """Fetch recent NWS observations and filter to today (local time)."""
    station_cfg = STATIONS.get(city_key)
    if not station_cfg:
        return []

    tz = ZoneInfo(station_cfg.timezone)
    today_local = datetime.now(tz).date()

    # Use the observations list endpoint (not /latest) to get history
    base_url = station_cfg.nws_observation_url.replace("/observations/latest", "/observations")
    url = f"{base_url}?limit={NWS_OBS_LIMIT}"
    headers = {"User-Agent": "PeakMonitor/1.0", "Accept": "application/geo+json"}

    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                logger.warning("NWS obs %s returned %d", city_key, resp.status)
                return []
            data = await resp.json()
    except Exception as e:
        logger.error("NWS obs fetch failed for %s: %s", city_key, e)
        return []

    observations = []
    for feature in data.get("features", []):
        props = feature.get("properties", {})
        temp_c = props.get("temperature", {}).get("value")
        ts_str = props.get("timestamp")

        if temp_c is None or ts_str is None:
            continue

        try:
            ts = datetime.fromisoformat(ts_str)
        except ValueError:
            continue

        # Filter to today in local timezone
        ts_local = ts.astimezone(tz)
        if ts_local.date() != today_local:
            continue

        temp_f = round(temp_c * 1.8 + 32, 1)
        observations.append(Observation(
            temp_f=temp_f,
            timestamp=ts,
            station=station_cfg.station_id,
        ))

    # Sort chronologically (oldest first)
    observations.sort(key=lambda o: o.timestamp)
    return observations


# ─── Peak Detection Engine ─────────────────────────────

def detect_peak(
    observations: list[Observation],
    state: CityPeakState,
    local_tz: ZoneInfo,
) -> CityPeakState:
    """
    Analyze observations and update peak detection state.

    Returns the updated state (mutated in place for convenience).
    """
    if not observations:
        return state

    # Anchor "now" to the latest observation, not the wall clock, so the
    # earliest-hour gate is a pure function of the data: deterministic in tests
    # regardless of run time, and correct in production (a peak whose data ends
    # before noon is the morning-warming case the gate exists to reject).
    now_local = max(o.timestamp for o in observations).astimezone(local_tz)

    # Update running max from all observations
    for obs in observations:
        if obs.temp_f > state.running_max:
            state.running_max = obs.temp_f
            state.max_time = obs.timestamp

    # Already confirmed — nothing more to do
    if state.peak_confirmed:
        return state

    # Don't check before earliest hour (morning warming pauses aren't peaks)
    if now_local.hour < PEAK_EARLIEST_HOUR:
        return state

    # Need a valid max_time
    if state.max_time is None:
        return state

    # Get observations AFTER the max was set
    post_max_obs = [
        o for o in observations
        if o.timestamp > state.max_time
    ]

    if len(post_max_obs) < PEAK_MIN_DECLINE_OBS:
        return state

    # Check: are the last N observations ALL below the running max?
    recent = post_max_obs[-PEAK_MIN_DECLINE_OBS:]
    all_declining = all(o.temp_f < state.running_max for o in recent)
    if not all_declining:
        return state

    # Check: minimum temperature drop
    latest_temp = recent[-1].temp_f
    drop = state.running_max - latest_temp
    if drop < PEAK_MIN_DROP_F:
        return state

    # Check: minimum elapsed time since max
    elapsed = recent[-1].timestamp - state.max_time
    if elapsed < timedelta(minutes=PEAK_MIN_DECLINE_MINUTES):
        return state

    # PEAK CONFIRMED
    state.peak_confirmed = True
    state.peak_temp = state.running_max

    # Determine settlement bracket (Kalshi rounds: temp X settles in bracket containing X)
    # NWS rounds to nearest integer; Kalshi brackets are typically 2°F wide
    rounded_temp = round(state.peak_temp)
    # Brackets are even-aligned: 30-31, 32-33, 34-35, etc.
    # A temp of 73°F falls in bracket "72 to 73" (which covers 72.00-73.99)
    # Actually: bracket "73-74" covers 73 ≤ temp < 75 based on parse_bracket_range
    # Settlement: temp is rounded to nearest integer, bracket containing that integer wins
    bracket_low = rounded_temp if rounded_temp % 2 == 0 else rounded_temp - 1
    state.peak_bracket = f"{bracket_low}-{bracket_low + 1}"

    logger.info(
        "🔒 PEAK CONFIRMED: %s = %.1f°F at %s → bracket %s",
        state.city_key, state.peak_temp,
        state.max_time.astimezone(local_tz).strftime("%I:%M %p"),
        state.peak_bracket,
    )

    return state


# ─── Bracket Price Lookup ──────────────────────────────
# (fetch/parse helpers moved in from edge_scanner_v2 when the KDE stack was
# retired, 2026-07-06 — peak_monitor was their last live consumer.)

_KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"


async def _fetch_kalshi_brackets(session: aiohttp.ClientSession, city_key: str) -> list[dict]:
    """Open Kalshi brackets for a city; [] on any failure."""
    from kalshi_client import normalize_market

    series = STATIONS[city_key].series_ticker
    try:
        url = f"{_KALSHI_BASE}/markets?series_ticker={series}&status=open&limit=100"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
            return [normalize_market(m) for m in data.get("markets", [])]
    except Exception as e:  # noqa: BLE001 — one city must not kill the poll
        logger.error("Kalshi fetch failed for %s: %s", city_key, e)
        return []


def parse_bracket_range(title: str) -> tuple[float, float, str]:
    """Loose market-title parser → (low, high, kind)."""
    clean = title.replace("°F", "").replace("°", "").replace("*", "").strip()
    if re.search(r"below|under|or less|<", clean, re.I):
        nums = re.findall(r"([\d.]+)", clean)
        if nums:
            return (-999, float(nums[0]), "low_tail")
    if re.search(r"above|or more|or higher|>", clean, re.I):
        nums = re.findall(r"([\d.]+)", clean)
        if nums:
            return (float(nums[0]), 999, "high_tail")
    match = re.search(r"([\d.]+)\s*(?:to|-)\s*([\d.]+)", clean)
    if match:
        low, high = float(match.group(1)), float(match.group(2))
        return (low, high + 1, "range")
    return (0, 0, "unknown")


async def fetch_bracket_prices(
    session: aiohttp.ClientSession,
    city_key: str,
) -> list[dict] | None:
    """Fetch current Kalshi bracket prices for a city.

    Returns None when no market data came back (exception OR empty list —
    _fetch_kalshi_brackets swallows HTTP/network errors into [], so an empty
    result is indistinguishable from an API blip and the caller should retry).
    """
    try:
        brackets = await _fetch_kalshi_brackets(session, city_key)
    except Exception as e:
        logger.error("Kalshi fetch failed for %s: %s", city_key, e)
        return None
    return brackets if brackets else None


def find_bracket_price(brackets: list[dict], peak_temp: float, target_date: str) -> dict | None:
    """Find the bracket for the peak temperature on the given climate day.

    `target_date` (ISO) must match the event date embedded in the ticker —
    today's and tomorrow's events are open simultaneously, so matching by
    strike alone can recommend tomorrow's market for today's peak (live
    failure 2026-07-06). Unparseable ticker dates are skipped: fail closed.
    """
    rounded = round(peak_temp)
    for mkt in brackets:
        if extract_target_date_from_ticker(mkt.get("ticker", "")) != target_date:
            continue
        title = mkt.get("title", "")
        low, high, btype = parse_bracket_range(title)
        if btype == "unknown":
            continue
        if low <= rounded < high:
            return {
                "ticker": mkt.get("ticker", ""),
                "title": title,
                "yes_bid": mkt.get("yes_bid", 0),
                "yes_ask": mkt.get("yes_ask", 0),
                "volume": mkt.get("volume", 0),
                "low": low,
                "high": high,
            }
    return None


# ─── Discord Alerts ────────────────────────────────────

async def send_peak_alert(
    state: CityPeakState,
    bracket_info: dict | None,
    local_tz: ZoneInfo,
    dry_run: bool = False,
) -> None:
    """Send a Discord alert for a confirmed peak."""
    station_cfg = STATIONS[state.city_key]
    peak_time_local = state.max_time.astimezone(local_tz).strftime("%I:%M %p %Z")

    if bracket_info:
        bid = bracket_info["yes_bid"]
        ask = bracket_info["yes_ask"]
        ticker = bracket_info["ticker"]
        title_str = bracket_info["title"]
        volume = bracket_info["volume"]

        # Calculate implied edge: true prob ~95% vs market price
        edge_cents = 95 - bid if bid > 0 else 0

        desc = (
            f"**🌡️ Daily high = {state.peak_temp:.1f}°F** (peaked at {peak_time_local})\n"
            f"**Settlement bracket: {title_str}**\n\n"
            f"Market: {bid}¢ bid / {ask}¢ ask (vol: {volume})\n"
            f"True prob: ~95%+ | Edge: +{edge_cents}¢\n"
            f"Ticker: `{ticker}`\n\n"
        )

        if edge_cents >= 10 and bid < 90:
            entry = bid + 1
            entry = min(entry, 50)
            desc += (
                f"**💰 TRADEABLE — {edge_cents}¢ edge**\n"
                f"Execute: `.venv/bin/python scripts/take.py {ticker} buy yes 10 {entry}`\n"
            )
        elif bid >= 90:
            desc += "⚠️ Market already priced at 90¢+ — edge is thin.\n"
        else:
            desc += "⚠️ Edge below 10¢ threshold — observe only.\n"
    else:
        desc = (
            f"**🌡️ Daily high = {state.peak_temp:.1f}°F** (peaked at {peak_time_local})\n"
            f"**Settlement bracket: ~{state.peak_bracket}°F**\n\n"
            f"⚠️ Could not find matching Kalshi bracket — market may be closed.\n"
        )

    embeds = [{
        "title": f"🔒 PEAK CONFIRMED — {station_cfg.city_name}",
        "description": desc,
        "color": 0x00FF00 if bracket_info and (95 - bracket_info.get("yes_bid", 0)) >= 10 else 0xFFAA00,
    }]

    await send_discord_embeds(embeds, dry_run=dry_run, context="peak_monitor")


# ─── Main Loop ─────────────────────────────────────────

async def poll_once(
    city_filter: str | None = None,
    quiet: bool = False,
    dry_run: bool = False,
) -> dict[str, CityPeakState]:
    """Run one polling cycle across all cities."""
    states = load_state()
    cities = {city_filter.upper(): STATIONS[city_filter.upper()]} if city_filter else STATIONS

    async with aiohttp.ClientSession() as session:
        for city_key, station_cfg in cities.items():
            tz = ZoneInfo(station_cfg.timezone)
            today_str = datetime.now(tz).strftime("%Y-%m-%d")
            now_local = datetime.now(tz)

            # Skip if outside monitoring window
            if now_local.hour < PEAK_EARLIEST_HOUR or now_local.hour >= PEAK_LATEST_HOUR:
                if not quiet:
                    print(f"  {city_key}: outside monitoring window ({PEAK_EARLIEST_HOUR}:00-{PEAK_LATEST_HOUR}:00 local)")
                continue

            # Initialize or reset state for new day
            if city_key not in states or states[city_key].date != today_str:
                states[city_key] = CityPeakState(city_key=city_key, date=today_str)

            state = states[city_key]

            # Skip if already alerted today
            if state.alerted:
                if not quiet:
                    print(f"  {city_key}: already alerted (peak={state.peak_temp:.1f}°F)")
                continue

            # Fetch observations: IEM (faster) → NWS (fallback)
            observations = await fetch_iem_observations(session, city_key)
            obs_source = "IEM"
            if not observations:
                observations = await fetch_today_observations(session, city_key)
                obs_source = "NWS"
            if not observations:
                if not quiet:
                    print(f"  {city_key}: no observations available")
                continue

            state.observations = observations  # store for debugging

            # Run peak detection
            detect_peak(observations, state, tz)

            if state.peak_confirmed and not state.alerted:
                # Fetch bracket prices to find the matching market. None means
                # no market data came back (API blip or closed market) — retry
                # on the next poll a few times before giving up, so a transient
                # failure at the confirmation moment doesn't forfeit the day.
                brackets = await fetch_bracket_prices(session, city_key)
                if brackets is None:
                    state.bracket_fetch_failures += 1
                    if state.bracket_fetch_failures < PEAK_BRACKET_FETCH_MAX_ATTEMPTS:
                        logger.warning(
                            "%s: no bracket data after peak confirmation (attempt %d/%d) — retrying next poll",
                            city_key, state.bracket_fetch_failures, PEAK_BRACKET_FETCH_MAX_ATTEMPTS,
                        )
                        if not quiet:
                            print(f"  {city_key}: bracket fetch failed — retrying next poll")
                        continue
                    # Persistent — proceed without bracket info: alert
                    # "market may be closed" below and finish the day.
                    brackets = []

                # Anchor the event date to the climate day of the peak itself,
                # not the wall-clock date at alert time.
                target_date = climate_day_start(tz, state.max_time).date().isoformat()
                bracket_info = find_bracket_price(brackets, state.peak_temp, target_date)

                print(f"  🔒 {city_key}: PEAK CONFIRMED at {state.peak_temp:.1f}°F → {state.peak_bracket}")
                if bracket_info:
                    print(f"     Market: {bracket_info['yes_bid']}¢/{bracket_info['yes_ask']}¢ ({bracket_info['ticker']})")

                if not state.alert_sent:
                    await send_peak_alert(state, bracket_info, tz, dry_run=dry_run)
                    state.alert_sent = True

                # (Strategy G auto-execution removed 2026-07-06 with the KDE
                # stack — peak detection is observability + alert only now.)
                state.alerted = True
            else:
                latest = observations[-1]
                decline_count = sum(
                    1 for o in observations
                    if state.max_time and o.timestamp > state.max_time and o.temp_f < state.running_max
                )
                if not quiet:
                    drop = state.running_max - latest.temp_f if state.running_max > -999 else 0
                    print(
                        f"  {city_key}: max={state.running_max:.1f}°F, "
                        f"now={latest.temp_f:.1f}°F (Δ{-drop:+.1f}°F), "
                        f"{decline_count}/{PEAK_MIN_DECLINE_OBS} declining obs "
                        f"[{obs_source}, {len(observations)} obs]"
                    )

    save_state(states)
    write_heartbeat("peak_monitor")
    return states


def _is_last_cron_tick(now_et: datetime) -> bool:
    """True when the ET-based cron schedule has no further tick today.

    The crontab fires every CRON_TICK_INTERVAL_MIN minutes through
    CRON_LAST_HOUR_ET (ET). On the tick whose successor would fall outside
    that window (i.e. the 22:50 ET run), this process is the day's last
    chance to poll — so it lingers for cities whose local window is open.
    """
    next_tick = now_et + timedelta(minutes=CRON_TICK_INTERVAL_MIN)
    return now_et.hour >= CRON_LAST_HOUR_ET and (
        next_tick.hour > CRON_LAST_HOUR_ET or next_tick.date() != now_et.date()
    )


def _cities_awaiting_peak(states: dict[str, CityPeakState]) -> list[str]:
    """Cities whose local monitoring window is still open and whose peak
    handoff hasn't completed today."""
    pending = []
    for city_key, station_cfg in STATIONS.items():
        tz = ZoneInfo(station_cfg.timezone)
        now_local = datetime.now(tz)
        if not (PEAK_EARLIEST_HOUR <= now_local.hour < PEAK_LATEST_HOUR):
            continue
        state = states.get(city_key)
        if (
            state is not None
            and state.date == now_local.strftime("%Y-%m-%d")
            and state.alerted
        ):
            continue
        pending.append(city_key)
    return pending


async def run_single_poll(
    city_filter: str | None = None,
    quiet: bool = False,
    dry_run: bool = False,
) -> dict[str, CityPeakState]:
    """One cron-driven poll; on the day's final cron tick, keep polling
    in-process while any city's LOCAL monitoring window is still open.

    The cron schedule is ET-based (*/10 13-22) but the per-city window is
    local time: LAX's 12:00-22:00 PT window runs until 01:00 ET, ~2h past
    the last cron tick, so without lingering its late peaks are never seen.
    """
    states = await poll_once(city_filter, quiet, dry_run)

    # Manual --city runs shouldn't hang a terminal for hours.
    if city_filter is not None or not _is_last_cron_tick(datetime.now(ET_TZ)):
        return states

    while True:
        pending = _cities_awaiting_peak(states)
        if not pending:
            break
        if not quiet:
            print(f"── Final cron tick: lingering for {', '.join(pending)} (local window still open) ──")
        await asyncio.sleep(PEAK_POLL_INTERVAL_SEC)
        try:
            states = await poll_once(city_filter, quiet, dry_run)
        except Exception as e:
            logger.error("Linger poll failed: %s", e)
    return states


async def watch(
    city_filter: str | None = None,
    quiet: bool = False,
    dry_run: bool = False,
) -> None:
    """Continuous monitoring loop."""
    print(f"Peak Monitor — watching every {PEAK_POLL_INTERVAL_SEC}s (Ctrl+C to stop)")
    print(f"Detection: {PEAK_MIN_DECLINE_OBS} declining obs, ≥{PEAK_MIN_DROP_F}°F drop, "
          f"≥{PEAK_MIN_DECLINE_MINUTES}min elapsed, after {PEAK_EARLIEST_HOUR}:00 local\n")

    while True:
        now_et = datetime.now(ZoneInfo("America/New_York"))
        print(f"── Poll at {now_et.strftime('%I:%M %p ET')} ──")

        try:
            await poll_once(city_filter, quiet, dry_run)
        except Exception as e:
            logger.error("Poll cycle failed: %s", e)
            print(f"  ERROR: {e}")

        print()
        await asyncio.sleep(PEAK_POLL_INTERVAL_SEC)


# ─── CLI ───────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Peak Monitor — Strategy F: Post-Peak Lock-In")
    parser.add_argument("--city", type=str, default=None, help="City code (NYC, CHI, DEN, MIA, LAX)")
    parser.add_argument("--watch", action="store_true", help="Continuous polling mode")
    parser.add_argument("--once", action="store_true", help="Single check (for cron)")
    parser.add_argument("--quiet", action="store_true", help="Only print when peak is confirmed")
    parser.add_argument("--dry-run", action="store_true", help="Print alerts, don't send to Discord")
    args = parser.parse_args()

    if args.watch:
        asyncio.run(watch(args.city, args.quiet, args.dry_run))
    else:
        asyncio.run(run_single_poll(args.city, args.quiet, args.dry_run))
