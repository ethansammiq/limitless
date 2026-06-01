#!/usr/bin/env python3
"""
PROXY ARB ENGINE — Spatial Lead-Lag Front Propagation + 1-Minute ASOS Peak Detection

Two intraday edges not captured by the ensemble/KDE scanner:

━━ Edge 1: 1-Minute ASOS Peak Detection ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  The NWS /observations/latest endpoint refreshes hourly and is frequently cached.
  Kalshi's KXHIGH contracts settle on the NWS Daily Climate Report (CLI), which
  uses the ASOS-observed maximum — including brief spikes lasting only 1-2 minutes.
  This module fetches raw 1-minute ASOS records from the Iowa Environmental Mesonet
  (IEM) to find the true intraday maximum that will appear in the final CLI.

━━ Edge 2: Proxy Station Front Propagation ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Surrounding ASOS stations observe incoming air masses before the target station.
  Given wind speed, wind direction, and temperature at each proxy, we compute:
    1. Bearing from the proxy toward the target station (haversine + great-circle).
    2. Alignment: cos(angle between wind-toward-vector and proxy→target bearing).
    3. Effective propagation speed = wind_speed × alignment.
    4. ETA in minutes = distance_km / effective_speed_kmh.
    5. Forward predicted temperature: proxy temp adjusted for atmospheric modification
       over the intervening distance (exponential decay: τ = 50 km half-length).
  Combined with the 1-min ASOS peak, this gives a forward predicted daily high
  that can be compared against Kalshi's order book to identify mispriced brackets.

━━ Trade Signal Logic ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  A trade is placed only when ALL of:
    ① obs_peak already falls inside the target bracket (floor established)
      OR forward_predicted_temp projects into the bracket with ≥ PROXY_MIN_PROB
    ② Edge (model_prob − entry_price) ≥ MIN_EDGE_THRESHOLD (0.15)
    ③ No existing open/resting position for the ticker (checked via load_positions)
    ④ client_order_id not already registered in StateDB (idempotency guard)

━━ Data Sources ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  IEM 1-min ASOS:  https://mesonet.agron.iastate.edu/cgi-bin/request/asos1min.py
  NWS observation: https://api.weather.gov/stations/{STATION}/observations/latest
  Kalshi markets:  https://api.elections.kalshi.com/trade-api/v2/markets

Usage:
    async with aiohttp.ClientSession() as session:
        engine = ProxyArbEngine(
            city_code="NYC",
            kalshi_client=client,
            db=get_db(),
            session=session,
        )
        signal = await engine.evaluate_and_trade(nws_forecast_high=44.0)
"""

from __future__ import annotations

import asyncio
import csv
import io
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from math import asin, atan2, cos, degrees, exp, log, radians, sin, sqrt
from typing import Literal
from zoneinfo import ZoneInfo

import yarl

import aiohttp

from log_setup import get_logger
from notifications import send_discord_alert


async def _resolved(val):
    """Awaitable wrapper for a plain value — replaces deprecated asyncio.coroutine."""
    return val
from utils.state_db import StateDB, get_db

logger = get_logger(__name__)

# ─── Kalshi API ───────────────────────────────────────────────────────────────

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"

# ─── IEM ASOS endpoints ───────────────────────────────────────────────────────

IEM_ASOS_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
IEM_CURRENTS_URL = "https://mesonet.agron.iastate.edu/api/1/currents.json"


def _iem_url(station: str, day: "datetime.date") -> yarl.URL:
    """Build IEM ASOS request URL for a full UTC calendar day.

    Uses the standard ASOS endpoint (asos.py, report_type=3) rather than the
    1-minute feed (asos1min.py) because the 1-minute feed has no data for
    most ASOS stations — they only report via routine METAR (~hourly).

    aiohttp/yarl percent-encodes '[' and ']' in query param keys regardless
    of how params are passed, producing vars%5B%5D which IEM rejects.
    Building the query string manually and wrapping it in yarl.URL(...,
    encoded=True) preserves the literal bracket syntax IEM requires.

    Wind speed variable is `sknt` (knots); the parser converts to mph.
    """
    sts = f"{day.isoformat()}T00:00:00Z"
    ets = f"{day.isoformat()}T23:59:59Z"
    qs = (
        f"station={station}&sts={sts}&ets={ets}"
        f"&vars[]=tmpf&vars[]=drct&vars[]=sknt&direct=no&report_type=3"
    )
    return yarl.URL(f"{IEM_ASOS_URL}?{qs}", encoded=True)

# NWS observation fallback
NWS_OBS_URL = "https://api.weather.gov/stations/{station}/observations/latest"
NWS_HEADERS = {"User-Agent": "ProxyArbEngine/1.0", "Accept": "application/geo+json"}

# ─── Risk thresholds ──────────────────────────────────────────────────────────

MIN_EDGE_THRESHOLD = 0.15       # Minimum model_prob - entry_price/100 to trade
MAX_ENTRY_PRICE_CENTS = 50      # Never pay more than 50¢ on YES (1:1 risk/reward)
PROXY_MIN_ALIGNMENT = 0.30      # cos(72.5°) — wind must point within ~72° of target
PROXY_MAX_ETA_MIN = 240         # Ignore proxies whose front won't arrive for 4+ hours
PROXY_TEMP_DECAY_KM = 50.0      # e-folding distance for thermal modification (km)
ASOS_CACHE_TTL_SEC = 55         # Re-fetch 1-min ASOS data no faster than this

# ─── City series tickers (for Kalshi market fetch) ───────────────────────────

_SERIES_BY_CITY = {
    "NYC": "KXHIGHNY",
    "CHI": "KXHIGHCHI",
    "DEN": "KXHIGHDEN",
    "MIA": "KXHIGHMIA",
    "LAX": "KXHIGHLAX",
}

# ─── Proxy station registry ───────────────────────────────────────────────────

@dataclass(frozen=True)
class ProxyStationConfig:
    """Geographic and network metadata for a proxy ASOS station."""
    icao: str           # Full ICAO ID (KEWR)
    iem_id: str         # IEM station ID — typically ICAO minus the K (EWR), or 3-letter (NYC)
    iem_network: str    # IEM network (NY_ASOS, IL_ASOS, etc.)
    name: str           # Human-readable name for logs/Discord
    lat: float
    lon: float


# Proxy stations grouped by primary city code.
# Ordered roughly upwind-priority-first (SW/W proxies first for mid-latitude fronts).
PROXY_STATIONS: dict[str, list[ProxyStationConfig]] = {
    "NYC": [
        ProxyStationConfig("KEWR", "EWR", "NJ_ASOS",  "Newark Liberty",        40.6895, -74.1745),
        ProxyStationConfig("KJFK", "JFK", "NY_ASOS",  "JFK International",     40.6413, -73.7781),
        ProxyStationConfig("KLGA", "LGA", "NY_ASOS",  "LaGuardia",             40.7769, -73.8740),
        ProxyStationConfig("KISP", "ISP", "NY_ASOS",  "Islip MacArthur",       40.7952, -73.1002),
        ProxyStationConfig("KPHL", "PHL", "PA_ASOS",  "Philadelphia Intl",     39.8719, -75.2411),
    ],
    "CHI": [
        ProxyStationConfig("KORD", "ORD", "IL_ASOS",  "O'Hare International",  41.9742, -87.9073),
        ProxyStationConfig("KGYY", "GYY", "IN_ASOS",  "Gary/Chicago Intl",     41.6163, -87.4128),
        ProxyStationConfig("KRFD", "RFD", "IL_ASOS",  "Rockford/Chicago",      42.1954, -89.0972),
        ProxyStationConfig("KSBN", "SBN", "IN_ASOS",  "South Bend Intl",       41.7087, -86.3173),
    ],
    "LAX": [
        ProxyStationConfig("KBUR", "BUR", "CA_ASOS",  "Hollywood Burbank",     34.2006, -118.3587),
        ProxyStationConfig("KSNA", "SNA", "CA_ASOS",  "John Wayne OC",         33.6757, -117.8682),
        ProxyStationConfig("KLGB", "LGB", "CA_ASOS",  "Long Beach",            33.8177, -118.1516),
        ProxyStationConfig("KONT", "ONT", "CA_ASOS",  "Ontario Intl",          34.0560, -117.6012),
    ],
    "DEN": [
        ProxyStationConfig("KAPA", "APA", "CO_ASOS",  "Centennial Airport",    39.5700, -104.8491),
        ProxyStationConfig("KCOS", "COS", "CO_ASOS",  "Colorado Springs",      38.8059, -104.7007),
        ProxyStationConfig("KBJC", "BJC", "CO_ASOS",  "Rocky Mountain Metro",  39.9088, -105.1165),
        ProxyStationConfig("KPUB", "PUB", "CO_ASOS",  "Pueblo Memorial",       38.2891, -104.4966),
    ],
    "MIA": [
        ProxyStationConfig("KFLL", "FLL", "FL_ASOS",  "Fort Lauderdale Intl",  26.0726, -80.1527),
        ProxyStationConfig("KOPF", "OPF", "FL_ASOS",  "Opa-locka Exec",        25.9070, -80.2783),
        ProxyStationConfig("KPBI", "PBI", "FL_ASOS",  "Palm Beach Intl",       26.6832, -80.0956),
        ProxyStationConfig("KTMB", "TMB", "FL_ASOS",  "Tamiami Executive",     25.6479, -80.4328),
    ],
}

# Target station metadata (IEM IDs and coordinates for each primary city)
_TARGET_META: dict[str, tuple[str, str, float, float]] = {
    # city_code: (icao, iem_id, lat, lon)
    "NYC": ("KNYC", "NYC", 40.7789, -73.9692),
    "CHI": ("KMDW", "MDW", 41.7868, -87.7522),
    "LAX": ("KLAX", "LAX", 33.9425, -118.4081),
    "DEN": ("KDEN", "DEN", 39.8561, -104.6737),
    "MIA": ("KMIA", "MIA", 25.7959, -80.2870),
}


# ─── Dataclasses ──────────────────────────────────────────────────────────────

@dataclass
class ProxyObservation:
    """Most recent valid observation from a single proxy station."""
    station_id: str
    temp_f: float | None        # Current temperature (°F); None if missing/stale
    wind_dir_deg: float         # Meteorological FROM direction (0–360)
    wind_speed_mph: float       # Wind speed (mph); 0 if calm or missing
    observed_at: datetime | None
    source: Literal["iem_1min", "nws_latest", "failed"]
    is_stale: bool = False      # True if observation is > 90 minutes old


@dataclass
class ASOSPeak:
    """Daily maximum temperature from 1-minute ASOS records."""
    station_id: str
    peak_temp_f: float | None      # None if no valid data for today
    current_temp_f: float | None   # Most recent 1-minute reading
    peak_time_utc: datetime | None
    record_count: int              # Number of 1-min records parsed for today
    source: Literal["iem_1min", "nws_fallback", "cache", "failed"]


@dataclass
class PropagationVector:
    """Wind-driven front propagation estimate from one proxy station."""
    proxy: ProxyStationConfig
    observation: ProxyObservation

    # Geometry
    distance_km: float             # Haversine distance proxy → target
    bearing_to_target_deg: float   # Initial bearing (proxy → target, 0–360)

    # Projection
    alignment: float               # cos(angle between wind-to-vector and bearing)
                                   # +1.0 = perfectly aimed at target; −1.0 = opposite
    effective_speed_mph: float     # wind_speed × alignment (negative = away from target)
    eta_minutes: float             # distance / effective_speed; inf if not propagating

    # Thermal
    proxy_temp_f: float | None
    thermal_impact_f: float        # Estimated ΔT at target from this proxy (sign matters)
                                   # Positive = warmer air incoming; negative = cold front
    is_converging: bool            # True if wind is pointing toward target


@dataclass
class ProxyArbSignal:
    """Fully computed arbitrage signal from the proxy engine."""
    city: str
    evaluated_at: datetime

    # Observation layer
    asos_peak: ASOSPeak
    propagation_vectors: list[PropagationVector]

    # Forward temperature estimate
    current_target_temp_f: float | None   # Most recent 1-min reading at target
    forward_predicted_high_f: float       # Best estimate of final daily high
    dominant_proxy: PropagationVector | None   # Highest-aligned upwind proxy

    # Trade decision
    target_bracket_lo: float | None
    target_bracket_hi: float | None
    target_ticker: str
    yes_bid: int
    yes_ask: int
    model_prob: float           # Estimated probability from forward predicted temp
    edge: float                 # model_prob − entry_price/100
    edge_passes: bool
    trade_placed: bool
    order_id: str               # Kalshi order_id if trade_placed, else ""
    client_order_id: str

    signal_reasons: list[str] = field(default_factory=list)


# ─── Spatial math (stdlib only, no scipy/geodesy dep) ─────────────────────────

def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometers."""
    R = 6371.0
    φ1, φ2 = radians(lat1), radians(lat2)
    Δφ = radians(lat2 - lat1)
    Δλ = radians(lon2 - lon1)
    a = sin(Δφ / 2) ** 2 + cos(φ1) * cos(φ2) * sin(Δλ / 2) ** 2
    return R * 2 * asin(min(1.0, sqrt(a)))


def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing (degrees, 0–360) from point A to point B."""
    φ1, φ2 = radians(lat1), radians(lat2)
    Δλ = radians(lon2 - lon1)
    x = sin(Δλ) * cos(φ2)
    y = cos(φ1) * sin(φ2) - sin(φ1) * cos(φ2) * cos(Δλ)
    return (degrees(atan2(x, y)) + 360) % 360


def _angular_diff(a: float, b: float) -> float:
    """Smallest angular difference between two bearings (0–180°)."""
    diff = abs(a - b) % 360
    return min(diff, 360 - diff)


def _thermal_decay(delta_temp_f: float, distance_km: float) -> float:
    """
    Estimate residual thermal impact at the target station after air travels distance_km.

    Uses exponential decay with e-folding length PROXY_TEMP_DECAY_KM.
    Physical basis: surface friction, mixing, and boundary-layer modification
    reduce the temperature signal of an air mass over distance.
    A 50 km e-folding means at 50 km the signal is 37%, at 100 km it is 14%.
    """
    return delta_temp_f * exp(-distance_km / PROXY_TEMP_DECAY_KM)


# ─── IEM CSV parser ───────────────────────────────────────────────────────────

def _parse_iem_asos_csv(
    text: str,
    station_iem_id: str,
    day_utc: "datetime.date",
) -> tuple[float | None, float | None, float | None, float | None, int]:
    """Parse IEM ASOS CSV response (standard asos.py endpoint, report_type=3).

    Columns: station, valid, tmpf, dwpf, relh, drct, sknt, ...
    The `valid` field uses UTC without a timezone suffix ("2026-06-01 15:51").
    Wind speed `sknt` is in knots; returned current_wind_speed is in mph.

    Returns:
        peak_temp_f        : highest tmpf for the UTC calendar day
        current_temp_f     : most recent valid tmpf
        current_wind_dir   : most recent valid drct (degrees, meteorological FROM)
        current_wind_speed : most recent valid sknt converted to mph
        record_count       : number of valid rows parsed
    """
    _KNOTS_TO_MPH = 1.15078

    peak_temp: float | None = None
    current_temp: float | None = None
    current_dir: float | None = None
    current_spd: float | None = None
    count = 0

    try:
        reader = csv.DictReader(
            (line for line in io.StringIO(text) if not line.startswith("#")),
        )
        rows: list[dict] = []
        for row in reader:
            valid_str = (row.get("valid(UTC)") or row.get("valid") or "").strip()
            if not valid_str:
                continue
            try:
                # IEM ASOS format: "2026-06-01 15:51" (no timezone suffix — UTC)
                ts = datetime.strptime(valid_str, "%Y-%m-%d %H:%M").replace(
                    tzinfo=timezone.utc
                )
                if ts.date() != day_utc:
                    continue
            except (ValueError, TypeError):
                continue

            tmpf_str = (row.get("tmpf") or "M").strip()
            if tmpf_str not in ("M", "", "None"):
                try:
                    t = float(tmpf_str)
                    if -60 < t < 150:
                        count += 1
                        if peak_temp is None or t > peak_temp:
                            peak_temp = t
                        rows.append({
                            "ts": ts, "tmpf": t,
                            "drct": (row.get("drct") or "M").strip(),
                            "sknt": (row.get("sknt") or "M").strip(),
                        })
                except ValueError:
                    pass

        if rows:
            last = rows[-1]
            current_temp = last["tmpf"]
            if last["drct"] not in ("M", "", None):
                try:
                    current_dir = float(last["drct"]) % 360
                except ValueError:
                    pass
            if last["sknt"] not in ("M", "", None):
                try:
                    current_spd = float(last["sknt"]) * _KNOTS_TO_MPH
                except ValueError:
                    pass

    except Exception as exc:
        logger.warning("IEM ASOS CSV parse error for %s: %s", station_iem_id, exc)

    return peak_temp, current_temp, current_dir, current_spd, count


# ─── Bracket parsing (self-contained, no import from edge_scanner_v2) ─────────

_BRACKET_RE = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*(?:°F)?\s*[-–to]+\s*(-?\d+(?:\.\d+)?)\s*(?:°F)?",
    re.I,
)

def _parse_bracket(title: str) -> tuple[float, float] | None:
    """Extract (low, high) from a bracket title like '44-45°F' or '44.5 to 45.5°F'."""
    m = _BRACKET_RE.search(title)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        if lo < hi and -60 < lo < 150:
            return lo, hi
    return None


def _point_prob_in_bracket(predicted_temp: float, lo: float, hi: float,
                            sigma: float = 1.5) -> float:
    """
    Probability that the daily high falls in [lo, hi) given a normally-distributed
    prediction with mean=predicted_temp and std=sigma.

    sigma=1.5°F ≈ typical ASOS 1-minute observation uncertainty + propagation error.
    Uses the analytic Gaussian CDF rather than a numerical KDE.
    """
    from math import erf, sqrt as msqrt
    def _Φ(x: float) -> float:        # standard normal CDF
        return 0.5 * (1 + erf(x / msqrt(2)))
    p = _Φ((hi - predicted_temp) / sigma) - _Φ((lo - predicted_temp) / sigma)
    return max(0.0, min(1.0, p))


# ─── Main Engine ──────────────────────────────────────────────────────────────

class ProxyArbEngine:
    """
    Async engine for spatial lead-lag + 1-minute ASOS arbitrage.

    Lifecycle:
        engine = ProxyArbEngine("NYC", kalshi_client, db, session)
        signal = await engine.evaluate_and_trade(nws_forecast_high=44.0)

    Can also be called as an async context manager:
        async with ProxyArbEngine("NYC", ...) as engine:
            signal = await engine.evaluate_and_trade(...)
    """

    def __init__(
        self,
        city_code: str,
        kalshi_client,              # KalshiClient — caller owns lifecycle (start/stop)
        db: StateDB | None = None,
        session: aiohttp.ClientSession | None = None,
        min_edge: float = MIN_EDGE_THRESHOLD,
        min_alignment: float = PROXY_MIN_ALIGNMENT,
        max_eta_min: float = PROXY_MAX_ETA_MIN,
        dry_run: bool = False,
    ) -> None:
        city_code = city_code.upper()
        if city_code not in _TARGET_META:
            raise ValueError(f"Unknown city_code '{city_code}'. Supported: {list(_TARGET_META)}")

        self.city_code = city_code
        self.client = kalshi_client
        self.db: StateDB = db or get_db()
        self._owned_session = session is None
        self.session: aiohttp.ClientSession = session or aiohttp.ClientSession(
            headers={"User-Agent": "ProxyArbEngine/1.0"},
            timeout=aiohttp.ClientTimeout(total=12),
        )
        self.min_edge = min_edge
        self.min_alignment = min_alignment
        self.max_eta_min = max_eta_min
        self.dry_run = dry_run

        icao, iem_id, lat, lon = _TARGET_META[city_code]
        self.target_icao = icao
        self.target_iem_id = iem_id
        self.target_lat = lat
        self.target_lon = lon

        self.proxy_stations: list[ProxyStationConfig] = PROXY_STATIONS.get(city_code, [])
        if not self.proxy_stations:
            logger.warning("No proxy stations configured for %s", city_code)

        # Augment each proxy with precomputed geometry
        self._proxy_geo: dict[str, tuple[float, float]] = {}  # iem_id → (dist_km, bearing)
        for p in self.proxy_stations:
            dist = _haversine_km(p.lat, p.lon, self.target_lat, self.target_lon)
            bearing = _bearing_deg(p.lat, p.lon, self.target_lat, self.target_lon)
            self._proxy_geo[p.iem_id] = (dist, bearing)

        # Simple in-process cache: station_iem_id → (ASOSPeak, fetched_at)
        self._asos_cache: dict[str, tuple[ASOSPeak, datetime]] = {}

    async def __aenter__(self) -> "ProxyArbEngine":
        return self

    async def __aexit__(self, *_) -> None:
        if self._owned_session and not self.session.closed:
            await self.session.close()

    # ── Sizing ────────────────────────────────────────────────────────────────

    def _half_kelly_size(self, model_prob: float, price_cents: int, balance: float) -> int:
        """Half-Kelly contract count, capped at MAX_POSITION_PCT of balance.

        Args:
            model_prob  : Estimated win probability (0–1) from the bracket scoring step.
            price_cents : Limit entry price in cents (e.g. 18 for 18¢).
            balance     : Current account balance in dollars.
        """
        from config import MAX_POSITION_PCT
        if model_prob <= 0 or price_cents <= 0 or balance <= 0:
            return 0
        price_frac = price_cents / 100.0
        b = (1.0 - price_frac) / price_frac     # net odds per dollar wagered
        kelly_f = (b * model_prob - (1.0 - model_prob)) / b
        half_f = kelly_f * 0.5
        if half_f <= 0:
            return 0
        allocated = min(balance * half_f, balance * MAX_POSITION_PCT)
        return max(1, int(allocated / price_frac))

    # ── 1-Minute ASOS ─────────────────────────────────────────────────────────

    async def fetch_1min_asos_temp(self, station_id: str) -> ASOSPeak:
        """
        Fetch today's 1-minute ASOS records for station_id (IEM format, e.g. 'NYC').

        Returns the absolute daily peak, current temperature, and wind data.
        Falls back to NWS /observations/latest on IEM failure.
        Results are cached for ASOS_CACHE_TTL_SEC seconds to avoid hammering IEM.

        The IEM 1-minute ASOS endpoint delivers data typically 2-5 minutes
        behind real time. For today's maximum, we request the full UTC calendar
        day from midnight to now.
        """
        now_utc = datetime.now(timezone.utc)

        # Cache hit?
        if station_id in self._asos_cache:
            cached_peak, cached_at = self._asos_cache[station_id]
            age_sec = (now_utc - cached_at).total_seconds()
            if age_sec < ASOS_CACHE_TTL_SEC:
                logger.debug("ASOS cache hit for %s (age %.0fs)", station_id, age_sec)
                return ASOSPeak(**{**vars(cached_peak), "source": "cache"})

        # Determine the IEM network for this station (needed by fallback, not 1-min)
        # For 1-min we only need the station ID.
        today_utc = now_utc.date()

        try:
            async with self.session.get(
                _iem_url(station_id, today_utc),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    raise aiohttp.ClientResponseError(
                        resp.request_info, resp.history, status=resp.status
                    )
                text = await resp.text()

            peak_f, current_f, _, _, count = _parse_iem_asos_csv(text, station_id, today_utc)
            if count == 0:
                raise ValueError("IEM returned 0 valid 1-minute records")

            peak_time = None  # Expensive to track; omit for now
            result = ASOSPeak(
                station_id=station_id,
                peak_temp_f=peak_f,
                current_temp_f=current_f,
                peak_time_utc=peak_time,
                record_count=count,
                source="iem_1min",
            )
            self._asos_cache[station_id] = (result, now_utc)
            logger.info(
                "ASOS 1-min %s: peak=%.1f°F current=%.1f°F (%d records)",
                station_id,
                peak_f or float("nan"),
                current_f or float("nan"),
                count,
            )
            return result

        except Exception as exc:
            logger.warning("IEM 1-min fetch failed for %s: %s — falling back to NWS", station_id, exc)
            return await self._nws_obs_fallback(station_id)

    async def _nws_obs_fallback(self, iem_id: str) -> ASOSPeak:
        """Fallback: fetch the latest NWS observation and treat it as an approximate peak."""
        # IEM IDs are typically 3-letter. Prepend K for NWS ICAO lookup.
        icao = f"K{iem_id}" if len(iem_id) == 3 and not iem_id.startswith("K") else iem_id

        # Special case: KNYC is correct; NYC → KNYC
        if iem_id == "NYC":
            icao = "KNYC"

        url = NWS_OBS_URL.format(station=icao)
        try:
            async with self.session.get(url, headers=NWS_HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    raise ValueError(f"NWS returned {resp.status}")
                data = await resp.json()

            props = data.get("properties", {})
            temp_block = props.get("temperature", {})
            qc = temp_block.get("qualityControl", "")
            val = temp_block.get("value")

            if val is None or qc not in ("C", "V", "S", "G"):
                raise ValueError(f"NWS observation has no valid temperature (QC={qc})")

            unit = temp_block.get("unitCode", "")
            temp_f = round(val * 1.8 + 32, 1) if "degC" in unit else round(float(val), 1)

            return ASOSPeak(
                station_id=iem_id,
                peak_temp_f=temp_f,
                current_temp_f=temp_f,
                peak_time_utc=None,
                record_count=1,
                source="nws_fallback",
            )

        except Exception as exc:
            logger.error("NWS fallback also failed for %s: %s", iem_id, exc)
            return ASOSPeak(
                station_id=iem_id,
                peak_temp_f=None,
                current_temp_f=None,
                peak_time_utc=None,
                record_count=0,
                source="failed",
            )

    # ── Proxy Observation Fetch ────────────────────────────────────────────────

    async def _fetch_proxy_obs(self, proxy: ProxyStationConfig) -> ProxyObservation:
        """
        Fetch the most recent wind + temperature observation for a proxy station.

        Requests the last 90 minutes of 1-minute ASOS data from IEM to get fresh
        wind direction and speed. Falls back to NWS /observations/latest on failure.
        A proxy observation older than 90 minutes is flagged as stale and excluded
        from propagation calculations.
        """
        now_utc = datetime.now(timezone.utc)
        today_utc = now_utc.date()

        try:
            async with self.session.get(
                _iem_url(proxy.iem_id, today_utc),
                timeout=aiohttp.ClientTimeout(total=12),
            ) as resp:
                if resp.status != 200:
                    raise aiohttp.ClientResponseError(
                        resp.request_info, resp.history, status=resp.status
                    )
                text = await resp.text()

            _, current_f, current_dir, current_spd, count = _parse_iem_asos_csv(
                text, proxy.iem_id, today_utc
            )
            if count == 0 or current_f is None:
                raise ValueError("No valid records in IEM response")

            obs = ProxyObservation(
                station_id=proxy.iem_id,
                temp_f=current_f,
                wind_dir_deg=current_dir if current_dir is not None else 0.0,
                wind_speed_mph=current_spd if current_spd is not None else 0.0,
                observed_at=now_utc,     # Approximate; IEM data is ~2-5 min behind
                source="iem_1min",
                is_stale=False,
            )
            logger.debug(
                "Proxy %s: %.1f°F wind %.0f° @ %.1f mph",
                proxy.iem_id, current_f or 0,
                current_dir or 0, current_spd or 0,
            )
            return obs

        except Exception as exc:
            logger.warning("IEM proxy fetch failed for %s: %s — trying NWS", proxy.iem_id, exc)

        # NWS fallback: get wind direction + speed from observation JSON
        icao = proxy.icao
        url = NWS_OBS_URL.format(station=icao)
        try:
            async with self.session.get(url, headers=NWS_HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    raise ValueError(f"NWS returned {resp.status}")
                data = await resp.json()

            props = data.get("properties", {})

            temp_block = props.get("temperature", {})
            temp_val = temp_block.get("value")
            temp_unit = temp_block.get("unitCode", "")
            temp_f: float | None = None
            if temp_val is not None and temp_block.get("qualityControl", "") in ("C", "V", "S", "G"):
                temp_f = round(temp_val * 1.8 + 32, 1) if "degC" in temp_unit else round(float(temp_val), 1)

            wind_dir_val = props.get("windDirection", {}).get("value")
            wind_spd_val = props.get("windSpeed", {}).get("value")    # km/h
            wind_dir = float(wind_dir_val) % 360 if wind_dir_val is not None else 0.0
            wind_spd_mph = float(wind_spd_val) * 0.621371 if wind_spd_val is not None else 0.0

            obs_time_str = props.get("timestamp", "")
            observed_at: datetime | None = None
            if obs_time_str:
                try:
                    observed_at = datetime.fromisoformat(obs_time_str.replace("Z", "+00:00"))
                except ValueError:
                    pass

            is_stale = (
                observed_at is not None
                and (now_utc - observed_at).total_seconds() > 90 * 60
            )

            return ProxyObservation(
                station_id=proxy.iem_id,
                temp_f=temp_f,
                wind_dir_deg=wind_dir,
                wind_speed_mph=wind_spd_mph,
                observed_at=observed_at,
                source="nws_latest",
                is_stale=is_stale,
            )

        except Exception as exc:
            logger.error("All proxy obs fetches failed for %s: %s", proxy.iem_id, exc)
            return ProxyObservation(
                station_id=proxy.iem_id,
                temp_f=None,
                wind_dir_deg=0.0,
                wind_speed_mph=0.0,
                observed_at=None,
                source="failed",
                is_stale=True,
            )

    # ── Propagation Vector Math ────────────────────────────────────────────────

    async def calculate_propagation_vector(self) -> list[PropagationVector]:
        """
        For each configured proxy station:
          1. Fetch current wind direction, speed, and temperature.
          2. Compute alignment: how directly the wind points toward the target.
          3. Compute effective propagation speed (wind_speed × alignment).
          4. Estimate ETA in minutes.
          5. Estimate thermal impact at target after atmospheric modification.

        Wind direction convention (meteorological):
          wind_dir = direction FROM which the wind blows.
          Wind is blowing TOWARD (wind_dir + 180°) % 360.
          A westerly wind (FROM 270°) blows TOWARD 90° (eastward).

        Returns all vectors (including non-converging ones) sorted by alignment desc,
        so callers can filter on is_converging or alignment >= threshold.
        """
        obs_tasks = [self._fetch_proxy_obs(p) for p in self.proxy_stations]
        observations: list[ProxyObservation] = await asyncio.gather(
            *obs_tasks, return_exceptions=False
        )

        vectors: list[PropagationVector] = []
        for proxy, obs in zip(self.proxy_stations, observations):
            if obs.source == "failed" or obs.is_stale:
                logger.debug("Skipping %s: stale or failed obs", proxy.iem_id)
                continue

            dist_km, bearing_to_target = self._proxy_geo[proxy.iem_id]

            # Wind is blowing TOWARD this compass direction
            wind_toward_deg = (obs.wind_dir_deg + 180.0) % 360.0

            # Angular difference between wind-toward and bearing-to-target
            angle_diff = _angular_diff(wind_toward_deg, bearing_to_target)
            alignment = cos(radians(angle_diff))

            # Effective speed component in the target direction (mph)
            effective_speed_mph = obs.wind_speed_mph * alignment

            # ETA: distance (km) / effective speed (km/h)
            if effective_speed_mph > 0.5:   # > 0.5 mph avoids divide-by-near-zero
                effective_speed_kmh = effective_speed_mph * 1.60934
                eta_min = (dist_km / effective_speed_kmh) * 60.0
            else:
                eta_min = float("inf")

            is_converging = alignment >= self.min_alignment and eta_min <= self.max_eta_min

            # Thermal impact: only meaningful when proxy is upwind and air will arrive
            target_peak_ref = None  # Caller fills this in evaluate_and_trade
            thermal_impact = 0.0
            if is_converging and obs.temp_f is not None:
                # We need the target's current temp to compute delta.
                # Placeholder filled by evaluate_and_trade after fetching target ASOS.
                # Here we compute the raw delta; caller applies decay.
                thermal_impact = obs.temp_f  # Raw proxy temp; delta computed in evaluate_and_trade

            vectors.append(PropagationVector(
                proxy=proxy,
                observation=obs,
                distance_km=round(dist_km, 1),
                bearing_to_target_deg=round(bearing_to_target, 1),
                alignment=round(alignment, 3),
                effective_speed_mph=round(effective_speed_mph, 1),
                eta_minutes=round(eta_min, 1) if eta_min != float("inf") else 9999.0,
                proxy_temp_f=obs.temp_f,
                thermal_impact_f=round(thermal_impact, 2),
                is_converging=is_converging,
            ))

        # Sort: converging first, then by alignment descending
        vectors.sort(key=lambda v: (-int(v.is_converging), -v.alignment))
        return vectors

    # ── Order Book + Bracket Fetch ────────────────────────────────────────────

    async def _fetch_brackets(self) -> list[dict]:
        series = _SERIES_BY_CITY.get(self.city_code, "")
        if not series:
            return []
        url = f"{KALSHI_BASE}/markets?series_ticker={series}&status=open&limit=100"
        try:
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                return data.get("markets", [])
        except Exception as exc:
            logger.error("Kalshi bracket fetch failed for %s: %s", self.city_code, exc)
            return []

    # ── Forward Temperature Estimate ──────────────────────────────────────────

    def _forward_predicted_high(
        self,
        asos_peak: ASOSPeak,
        vectors: list[PropagationVector],
        current_target_f: float | None,
        nws_forecast_high: float | None,
    ) -> float:
        """
        Synthesize the best estimate of the final daily high temperature.

        Priority hierarchy:
          1. Observed 1-min ASOS peak (hard floor — it already happened).
          2. Upwind proxy projection: proxy_temp decayed by distance, added to
             current target temp to estimate what target will read at proxy ETA.
          3. NWS forecast high (anchor; used when proxies are ambiguous).

        The estimate is conservative: we take the maximum of all upside projections
        but never go below the observed ASOS peak.
        """
        floor = asos_peak.peak_temp_f or current_target_f or 0.0

        projections: list[float] = [floor]

        if nws_forecast_high and nws_forecast_high > 0:
            projections.append(nws_forecast_high)

        if current_target_f is None:
            return max(projections)

        for v in vectors:
            if not v.is_converging:
                continue
            if v.proxy_temp_f is None:
                continue
            if v.eta_minutes > self.max_eta_min:
                continue

            raw_delta = v.proxy_temp_f - current_target_f
            decayed_delta = _thermal_decay(raw_delta, v.distance_km)

            projected = current_target_f + decayed_delta
            projections.append(projected)

        return max(projections)

    # ── Main Evaluation + Trade ───────────────────────────────────────────────

    async def evaluate_and_trade(
        self,
        nws_forecast_high: float | None = None,
        brackets: list[dict] | None = None,
    ) -> ProxyArbSignal | None:
        """
        Core arbitrage evaluation loop.

        Steps:
          1. Fetch 1-min ASOS peak for target station.
          2. Fetch propagation vectors from all proxy stations (concurrent).
          3. Compute forward predicted high.
          4. Score each open Kalshi bracket:
               model_prob = P(daily_high ∈ [lo, hi] | forward_predicted_high)
               edge       = model_prob − entry_price / 100
          5. Place idempotent limit order on the highest-edge bracket that
             passes the edge threshold, the position de-duplicate check,
             and the StateDB idempotency check.

        Args:
            nws_forecast_high : NWS point forecast high (°F) from the main scanner.
                                 Optional but improves forward_predicted accuracy.
            brackets          : Pre-fetched Kalshi market list (reuse from scanner).
                                 If None, fetched independently.

        Returns:
            ProxyArbSignal with full diagnostics, or None if city not configured.
        """
        evaluated_at = datetime.now(timezone.utc)
        reasons: list[str] = []

        # ── Concurrent data fetch ──────────────────────────────────────────────
        asos_task = self.fetch_1min_asos_temp(self.target_iem_id)
        vec_task = self.calculate_propagation_vector()
        bracket_task = self._fetch_brackets() if brackets is None else _resolved(brackets)

        asos_peak, vectors, live_brackets = await asyncio.gather(
            asos_task, vec_task, bracket_task, return_exceptions=False
        )

        brackets = live_brackets

        # ── ASOS peak analysis ─────────────────────────────────────────────────
        current_target_f = asos_peak.current_temp_f
        if asos_peak.source == "failed" or asos_peak.peak_temp_f is None:
            reasons.append("ASOS data unavailable — signal unreliable")
            logger.warning("ProxyArbEngine: no valid ASOS data for %s", self.city_code)
        else:
            reasons.append(
                f"1-min ASOS peak: {asos_peak.peak_temp_f:.1f}°F "
                f"(current: {current_target_f or 'N/A'}°F, "
                f"{asos_peak.record_count} records, source={asos_peak.source})"
            )

        # ── Resolve thermal_impact_f with actual current target temp ───────────
        # (calculate_propagation_vector doesn't know target temp; we resolve it here)
        for v in vectors:
            if v.is_converging and v.proxy_temp_f is not None and current_target_f is not None:
                raw_delta = v.proxy_temp_f - current_target_f
                object.__setattr__(v, "thermal_impact_f",
                                   round(_thermal_decay(raw_delta, v.distance_km), 2))
                # Note: PropagationVector is not frozen so we can assign directly

        # ── Propagation summary ────────────────────────────────────────────────
        converging = [v for v in vectors if v.is_converging]
        dominant = converging[0] if converging else None

        if dominant:
            reasons.append(
                f"Dominant proxy: {dominant.proxy.name} "
                f"({dominant.proxy_temp_f:.1f}°F, "
                f"wind FROM {dominant.observation.wind_dir_deg:.0f}° "
                f"@ {dominant.observation.wind_speed_mph:.0f} mph, "
                f"ETA {dominant.eta_minutes:.0f} min, "
                f"impact {dominant.thermal_impact_f:+.1f}°F, "
                f"alignment {dominant.alignment:.2f})"
            )
        else:
            reasons.append(f"No converging proxy stations (alignment < {self.min_alignment:.2f})")

        # ── Forward predicted high ─────────────────────────────────────────────
        forward_high = self._forward_predicted_high(
            asos_peak, vectors, current_target_f, nws_forecast_high
        )
        reasons.append(f"Forward predicted high: {forward_high:.1f}°F")
        if nws_forecast_high:
            reasons.append(f"NWS anchor: {nws_forecast_high:.1f}°F")

        # ── Bracket scoring ────────────────────────────────────────────────────
        # Tomorrow's date in city-local time (same logic as main scanner)
        from zoneinfo import ZoneInfo as _ZI
        from config import STATIONS as _STATIONS
        city_tz = _ZI(_STATIONS[self.city_code].timezone)
        tomorrow = (datetime.now(city_tz) + timedelta(days=1)).date()

        best_ticker = ""
        best_lo, best_hi = None, None
        best_bid = best_ask = 0
        best_model_prob = best_edge = 0.0
        best_entry_price = 0

        for mkt in brackets:
            ticker = mkt.get("ticker", "")
            # Filter to tomorrow's markets only (same ticker-date check as scanner)
            date_tag = tomorrow.strftime("%y%b%d").upper()[:7]
            if date_tag not in ticker.upper():
                continue

            title = mkt.get("title", "") or mkt.get("subtitle", "")
            parsed = _parse_bracket(title)
            if parsed is None:
                continue
            lo, hi = parsed

            yes_bid = mkt.get("yes_bid", 0) or mkt.get("yes_price", 0) or 0
            yes_ask = mkt.get("yes_ask", 0)

            if yes_bid <= 0:
                continue

            # Model probability from forward prediction
            model_prob = _point_prob_in_bracket(forward_high, lo, hi)

            # Entry price: bid+1 (maker strategy, 0% fee)
            entry_price = min(yes_bid + 1, MAX_ENTRY_PRICE_CENTS)

            edge = model_prob - (entry_price / 100.0)

            if edge > best_edge:
                best_edge = edge
                best_model_prob = model_prob
                best_lo, best_hi = lo, hi
                best_ticker = ticker
                best_bid = yes_bid
                best_ask = yes_ask
                best_entry_price = entry_price

        edge_passes = best_edge >= self.min_edge and best_ticker != ""

        if edge_passes:
            # Kelly sizing — fetch balance and compute dynamic contract count
            _balance = 0.0
            _contracts = 0
            try:
                _balance = await self.client.get_balance()
                _contracts = self._half_kelly_size(best_model_prob, best_entry_price, _balance)
            except Exception as _sz_err:
                logger.warning("Balance fetch for Kelly sizing failed: %s", _sz_err)
            if _contracts <= 0:
                edge_passes = False
                reasons.append(
                    f"SKIPPED — Kelly sizing returned 0 contracts "
                    f"(edge={best_edge:+.1%}, balance=${_balance:.2f})"
                )
            else:
                reasons.append(
                    f"Best bracket: {best_lo:.1f}–{best_hi:.1f}°F "
                    f"| model_prob={best_model_prob:.1%} "
                    f"| bid={best_bid}¢ ask={best_ask}¢ "
                    f"| entry={best_entry_price}¢ "
                    f"| edge={best_edge:+.1%} "
                    f"| Kelly size={_contracts}"
                )
        else:
            _contracts = 0
            reasons.append(
                f"No bracket meets edge threshold (best edge: {best_edge:+.1%}; "
                f"threshold: {self.min_edge:+.1%})"
            )

        # ── Position de-duplicate check ────────────────────────────────────────
        trade_placed = False
        placed_order_id = ""
        placed_cid = ""

        if edge_passes and not self.dry_run:
            from position_store import load_positions
            try:
                existing = [
                    p for p in load_positions()
                    if p.get("ticker") == best_ticker
                    and p.get("status") in ("resting", "open")
                ]
                if existing:
                    reasons.append(
                        f"SKIPPED — existing {existing[0]['status']} position on {best_ticker}"
                    )
                    edge_passes = False
            except Exception as exc:
                logger.warning("Position check failed: %s — proceeding cautiously", exc)

        # ── Idempotent order placement ─────────────────────────────────────────
        if edge_passes and not self.dry_run:
            placed_cid = str(uuid.uuid4())

            # StateDB idempotency guard (should never fire on first run,
            # but protects against tight retry loops)
            if self.db.is_duplicate(placed_cid):
                reasons.append(f"SKIPPED — client_order_id {placed_cid} already registered")
            else:
                self.db.register_order(
                    client_order_id=placed_cid,
                    ticker=best_ticker,
                    side="yes",
                    count=_contracts,
                    price=best_entry_price,
                    is_paper=False,
                )

                self.db.write_audit("PROXY_SIGNAL_TRADE_ATTEMPT", ticker=best_ticker, payload={
                    "city": self.city_code,
                    "forward_high": forward_high,
                    "asos_peak": asos_peak.peak_temp_f,
                    "dominant_proxy": dominant.proxy.iem_id if dominant else None,
                    "model_prob": round(best_model_prob, 4),
                    "edge": round(best_edge, 4),
                    "entry_price": best_entry_price,
                    "contracts": _contracts,
                    "client_order_id": placed_cid,
                })

                try:
                    result = await self.client.place_order(
                        ticker=best_ticker,
                        side="yes",
                        action="buy",
                        count=_contracts,
                        price=best_entry_price,
                        order_type="limit",
                        client_order_id=placed_cid,
                    )
                    if result:
                        order = result.get("order", result)
                        placed_order_id = order.get("order_id", "")
                        status = order.get("status", "unknown").upper()
                        rejected = {"REJECTED", "CANCELED", "CANCELLED", "FAILED", "ERROR"}

                        if placed_order_id and status not in rejected:
                            self.db.confirm_order(
                                placed_cid,
                                kalshi_order_id=placed_order_id,
                                status="resting" if status in ("RESTING", "PENDING") else "open",
                            )
                            self.db.write_audit("PROXY_ORDER_PLACED", ticker=best_ticker, payload={
                                "kalshi_order_id": placed_order_id,
                                "client_order_id": placed_cid,
                                "status": status,
                                "price": best_entry_price,
                                "contracts": _contracts,
                                "forward_high": round(forward_high, 2),
                                "asos_peak": asos_peak.peak_temp_f,
                                "edge": round(best_edge, 4),
                            })

                            from position_store import register_position
                            try:
                                register_position(
                                    best_ticker, "yes", best_entry_price, _contracts,
                                    placed_order_id, status,
                                    client_order_id=placed_cid,
                                )
                            except Exception as reg_err:
                                logger.error("register_position failed for proxy trade: %s", reg_err)
                                self.db.write_audit("ORPHANED_PROXY_ORDER", ticker=best_ticker, payload={
                                    "client_order_id": placed_cid,
                                    "kalshi_order_id": placed_order_id,
                                    "error": str(reg_err),
                                })

                            trade_placed = True
                            reasons.append(f"ORDER PLACED: {placed_order_id} ({status})")

                            await send_discord_alert(
                                title=f"🎯 PROXY ARB SIGNAL — {self.city_code}",
                                description=(
                                    f"**{best_ticker}**\n"
                                    f"Side: YES @ {best_entry_price}¢ × {_contracts} (limit, maker)\n"
                                    f"Forward high: **{forward_high:.1f}°F** "
                                    f"| ASOS peak: {asos_peak.peak_temp_f:.1f}°F\n"
                                    f"Model prob: {best_model_prob:.1%} "
                                    f"| Edge: {best_edge:+.1%}\n"
                                    + (f"Upwind proxy: {dominant.proxy.name} "
                                       f"({dominant.proxy_temp_f:.1f}°F, ETA {dominant.eta_minutes:.0f}min)"
                                       if dominant else "No upwind proxy")
                                ),
                                color=0x00CC44,
                                context="proxy_arb",
                            )
                        else:
                            self.db.reject_order(placed_cid)
                            reasons.append(f"Order rejected by Kalshi (status={status})")
                    else:
                        self.db.reject_order(placed_cid)
                        reasons.append("place_order returned empty result")

                except Exception as exc:
                    self.db.reject_order(placed_cid)
                    logger.error("Proxy arb order placement failed: %s", exc)
                    reasons.append(f"Order placement error: {exc}")

        elif edge_passes and self.dry_run:
            reasons.append(
                f"[DRY RUN] Would place YES {best_ticker} @ {best_entry_price}¢ "
                f"| edge {best_edge:+.1%}"
            )
            self.db.write_audit("PROXY_DRY_RUN_SIGNAL", ticker=best_ticker, payload={
                "city": self.city_code,
                "forward_high": forward_high,
                "asos_peak": asos_peak.peak_temp_f,
                "model_prob": round(best_model_prob, 4),
                "edge": round(best_edge, 4),
                "entry_price": best_entry_price,
            })
            await send_discord_alert(
                title=f"🔍 [DRY RUN] Proxy Signal — {self.city_code}",
                description=(
                    f"**{best_ticker}** | YES @ {best_entry_price}¢\n"
                    f"Forward high: {forward_high:.1f}°F | ASOS peak: {asos_peak.peak_temp_f or 'N/A'}°F\n"
                    f"Model prob: {best_model_prob:.1%} | Edge: {best_edge:+.1%}\n"
                    f"Dominant proxy: {dominant.proxy.name if dominant else 'None'}"
                ),
                color=0x888888,
                context="proxy_arb_dry_run",
            )

        # Log full signal to audit regardless of trade
        self.db.write_audit("PROXY_SCAN_COMPLETE", ticker=best_ticker or self.city_code, payload={
            "city": self.city_code,
            "asos_peak_f": asos_peak.peak_temp_f,
            "asos_source": asos_peak.source,
            "asos_records": asos_peak.record_count,
            "current_target_f": current_target_f,
            "forward_high_f": round(forward_high, 2),
            "nws_forecast_high": nws_forecast_high,
            "converging_proxies": [
                {"id": v.proxy.iem_id, "alignment": v.alignment,
                 "eta_min": v.eta_minutes, "impact_f": v.thermal_impact_f}
                for v in converging
            ],
            "best_edge": round(best_edge, 4),
            "edge_passes": edge_passes,
            "trade_placed": trade_placed,
        })

        for r in reasons:
            logger.info("  [%s] %s", self.city_code, r)

        return ProxyArbSignal(
            city=self.city_code,
            evaluated_at=evaluated_at,
            asos_peak=asos_peak,
            propagation_vectors=vectors,
            current_target_temp_f=current_target_f,
            forward_predicted_high_f=round(forward_high, 2),
            dominant_proxy=dominant,
            target_bracket_lo=best_lo,
            target_bracket_hi=best_hi,
            target_ticker=best_ticker,
            yes_bid=best_bid,
            yes_ask=best_ask,
            model_prob=round(best_model_prob, 4),
            edge=round(best_edge, 4),
            edge_passes=edge_passes,
            trade_placed=trade_placed,
            order_id=placed_order_id,
            client_order_id=placed_cid,
            signal_reasons=reasons,
        )


# ─── Convenience: scan all cities concurrently ────────────────────────────────

async def run_proxy_scan(
    kalshi_client,
    db: StateDB | None = None,
    city_codes: list[str] | None = None,
    nws_forecasts: dict[str, float] | None = None,
    dry_run: bool = False,
) -> dict[str, ProxyArbSignal]:
    """
    Run ProxyArbEngine.evaluate_and_trade() concurrently across all (or selected) cities.

    Args:
        kalshi_client   : Initialized KalshiClient (caller manages start/stop).
        db              : StateDB instance. If None, uses get_db().
        city_codes      : Subset of cities to scan. Defaults to all configured cities.
        nws_forecasts   : Dict of city_code → NWS forecast high °F (from main scanner).
        dry_run         : If True, compute signals but do not place orders.

    Returns:
        Dict of city_code → ProxyArbSignal for all cities that were scanned.
    """
    db = db or get_db()
    nws = nws_forecasts or {}
    cities = city_codes or list(PROXY_STATIONS.keys())

    async with aiohttp.ClientSession(
        headers={"User-Agent": "ProxyArbEngine/1.0"},
        timeout=aiohttp.ClientTimeout(total=20),
    ) as session:

        async def _scan_one(city: str) -> tuple[str, ProxyArbSignal | None]:
            try:
                engine = ProxyArbEngine(
                    city_code=city,
                    kalshi_client=kalshi_client,
                    db=db,
                    session=session,
                    dry_run=dry_run,
                )
                sig = await engine.evaluate_and_trade(nws_forecast_high=nws.get(city))
                return city, sig
            except Exception as exc:
                logger.error("Proxy scan failed for %s: %s", city, exc)
                return city, None

        results = await asyncio.gather(*[_scan_one(c) for c in cities])

    return {city: sig for city, sig in results if sig is not None}
