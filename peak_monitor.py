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
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from config import (
    STATIONS,
    SETTLEMENT_HOUR_ET,
    PEAK_MIN_DECLINE_OBS,
    PEAK_MIN_DROP_F,
    PEAK_MIN_DECLINE_MINUTES,
    PEAK_EARLIEST_HOUR,
    PEAK_LATEST_HOUR,
    PEAK_POLL_INTERVAL_SEC,
    PEAK_TRADE_ENABLED,
)
from notifications import send_discord_alert, send_discord_embeds
from heartbeat import write_heartbeat
from log_setup import get_logger

logger = get_logger(__name__)

# ─── Configuration ─────────────────────────────────────

NWS_OBS_LIMIT = 30             # Fetch last 30 observations (covers ~24h)
IEM_BASE_URL = "https://mesonet.agron.iastate.edu"

# State file for tracking across cron invocations
PROJECT_ROOT = Path(__file__).resolve().parent
STATE_FILE = PROJECT_ROOT / "peak_state.json"


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
    alerted: bool = False                  # Already sent Discord alert

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

    now_local = datetime.now(local_tz)

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

async def fetch_bracket_prices(
    session: aiohttp.ClientSession,
    city_key: str,
) -> list[dict]:
    """Fetch current Kalshi bracket prices for a city."""
    from edge_scanner_v2 import fetch_kalshi_brackets
    try:
        return await fetch_kalshi_brackets(session, city_key)
    except Exception as e:
        logger.error("Kalshi fetch failed for %s: %s", city_key, e)
        return []


def find_bracket_price(brackets: list[dict], peak_temp: float) -> dict | None:
    """Find the bracket that contains the peak temperature."""
    from edge_scanner_v2 import parse_bracket_range
    rounded = round(peak_temp)
    for mkt in brackets:
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
                f"Execute: `python3 execute_trade.py {ticker} yes {entry} 10`\n"
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
                # Fetch bracket prices to find the matching market
                brackets = await fetch_bracket_prices(session, city_key)
                bracket_info = find_bracket_price(brackets, state.peak_temp)

                print(f"  🔒 {city_key}: PEAK CONFIRMED at {state.peak_temp:.1f}°F → {state.peak_bracket}")
                if bracket_info:
                    print(f"     Market: {bracket_info['yes_bid']}¢/{bracket_info['yes_ask']}¢ ({bracket_info['ticker']})")

                await send_peak_alert(state, bracket_info, tz, dry_run=dry_run)

                # ── Strategy G: Auto-execute peak trade ──
                if PEAK_TRADE_ENABLED and bracket_info:
                    try:
                        from peak_trader import execute_peak_trade
                        trade_result = await execute_peak_trade(
                            city_key=city_key,
                            peak_temp=state.peak_temp,
                            bracket_info=bracket_info,
                            dry_run=dry_run,
                        )
                        if trade_result["success"]:
                            print(f"  🔒⚡ {city_key}: PEAK TRADE {'[DRY RUN] ' if dry_run else ''}EXECUTED")
                        else:
                            print(f"  🔒   {city_key}: Peak trade skipped — {trade_result['reason']}")
                    except Exception as e:
                        logger.error("Peak trade failed for %s: %s", city_key, e)
                        print(f"  🔒❌ {city_key}: Peak trade error — {e}")

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
        asyncio.run(poll_once(args.city, args.quiet, args.dry_run))
