#!/usr/bin/env python3
"""
EDGE SCANNER v2.0 — Frontier AI Model Weather Arbitrage

UPGRADES over v1:
  1. AIFS Ensemble (51 members) — ECMWF's AI model, operational since Feb 2025
     10% more accurate than physics-based IFS for large-scale patterns
  2. KDE probability estimation — Gaussian kernel density, not crude histograms
  3. Model verification weighting — tracks which model is "hot" vs "cold"
  4. Real-time ASOS obs integration — detect when temp is running off-forecast
  5. DSM/6-hour bot protection — flags dangerous order windows
  6. HRRR-aware entry timing — blocks trades before 18Z HRRR convergence

Models pulled (FREE via Open-Meteo):
  - ECMWF AIFS 0.25° Ensemble (51 members) ← NEW FRONTIER AI MODEL
  - ECMWF IFS 0.25° Ensemble (51 members)
  - GFS Ensemble 0.25° (31 members)
  - ICON Seamless Ensemble (40 members)
  - GEM Global Ensemble (21 members)
  Total: ~194 ensemble members across 5 model families

Usage:
  python3 edge_scanner_v2.py              # Full scan
  python3 edge_scanner_v2.py --city NYC   # Single city
  python3 edge_scanner_v2.py --timing     # Show optimal entry windows
"""

from __future__ import annotations

import asyncio
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp
import numpy as np
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).resolve().parent / ".env")

from log_setup import get_logger

logger = get_logger(__name__)

# ─── Configuration ─────────────────────────────────────

ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"  # HRRR + NBM deterministic
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"

# All available ensemble models on Open-Meteo (free tier)
# API param name → suffix in response keys
ENSEMBLE_MODELS = [
    "ecmwf_ifs025",                # ECMWF IFS physics-based (51 members)
    "ecmwf_aifs025",               # ECMWF AIFS AI model (51 members) ← FRONTIER
    "gfs_seamless",                # GFS (31 members)
    "icon_seamless",               # ICON (40 members)
    "gem_global",                  # GEM Canada (21 members)
    "bom_access_global_ensemble",  # BOM ACCESS-GE Australia (18 members)
    "ukmo_global_ensemble_20km",   # UK Met MOGREPS-G (18 members)
]

# Map API model name → response key suffix (Open-Meteo renames them)
MODEL_KEY_SUFFIXES = {
    "ecmwf_ifs025": "ecmwf_ifs025_ensemble",
    "ecmwf_aifs025": "ecmwf_aifs025_ensemble",
    "gfs_seamless": "ncep_gefs_seamless",
    "icon_seamless": "icon_seamless_eps",
    "gem_global": "gem_global_ensemble",
    "bom_access_global_ensemble": "bom_access_global_ensemble",
    "ukmo_global_ensemble_20km": "ukmo_global_ensemble_20km",
}

# Model weights — defaults based on general verification performance
# Overridden by calibration.py when backtest data is available
_DEFAULT_MODEL_WEIGHTS = {
    "ecmwf_aifs025": 1.30,                # AI model — 10% better than IFS per ECMWF
    "ecmwf_ifs025": 1.15,                 # Gold standard physics model
    "gfs_seamless": 1.00,                 # Baseline
    "icon_seamless": 0.95,                # Slightly less skillful at US sites
    "gem_global": 0.85,                   # Lower resolution, less verification data
    "bom_access_global_ensemble": 0.80,   # 40km, 6-hourly — less US skill
    "ukmo_global_ensemble_20km": 0.85,    # 20km global, decent skill
}

# Load calibrated params if available (falls back to defaults gracefully)
_CALIBRATED_WEIGHTS = {}
_BANDWIDTH_FACTOR = 1.0
try:
    from calibration import get_calibrated_params
    _cal_w, _cal_bw = get_calibrated_params()
    if _cal_w:
        _CALIBRATED_WEIGHTS = _cal_w
        logger.info("Using calibrated model weights from backtest data")
    if _cal_bw and _cal_bw > 0:
        _BANDWIDTH_FACTOR = _cal_bw
        logger.info("Using calibrated bandwidth factor: %.3f", _cal_bw)
except ImportError:
    pass  # calibration.py not available
except Exception as e:
    logger.debug("Calibration load failed (using defaults): %s", e)

MODEL_WEIGHTS = _CALIBRATED_WEIGHTS if _CALIBRATED_WEIGHTS else _DEFAULT_MODEL_WEIGHTS

# Derive CITIES dict from canonical config.py STATIONS (single source of truth)
from config import STATIONS as _STATIONS

# Load model bias corrections (per-model, per-city adjustments)
# Correction is added to each member: if model runs +2F hot, correction = -2F.
_BIAS_CORRECTIONS: dict[tuple[str, str], float] = {}
try:
    from config import MODEL_BIAS_CORRECTION_ENABLED
    if MODEL_BIAS_CORRECTION_ENABLED:
        from model_bias import get_bias_correction, _load_records
        _bias_records = _load_records()
        if _bias_records:
            for _model in ENSEMBLE_MODELS:
                for _city in _STATIONS:
                    _corr = get_bias_correction(_model, _city, _bias_records)
                    if _corr != 0.0:
                        _BIAS_CORRECTIONS[(_model, _city)] = _corr
            if _BIAS_CORRECTIONS:
                logger.info("Loaded %d model bias corrections from backtest data", len(_BIAS_CORRECTIONS))
except ImportError:
    pass  # model_bias.py not available
except Exception as e:
    logger.debug("Bias correction load failed (using defaults): %s", e)

# Also load Previous Runs API corrections (from bias_collector.py output)
# These take precedence over backtest-derived corrections when available.
_PREV_RUNS_CORR_FILE = Path(__file__).resolve().parent / "model_bias_corrections.json"
try:
    if _PREV_RUNS_CORR_FILE.exists():
        import json as _json
        _prev_corr = _json.loads(_PREV_RUNS_CORR_FILE.read_text())
        _prev_count = 0
        for _key, _val in _prev_corr.items():
            _parts = _key.split("|")
            if len(_parts) == 2:
                _BIAS_CORRECTIONS[(_parts[0], _parts[1])] = _val
                _prev_count += 1
        if _prev_count:
            logger.info("Loaded %d Previous Runs API bias corrections", _prev_count)
except Exception as e:
    logger.debug("Previous Runs corrections load failed: %s", e)

CITIES = {
    code: {
        "name": s.city_name,
        "series": s.series_ticker,
        "lat": s.lat,
        "lon": s.lon,
        "nws_hourly": s.nws_hourly_forecast_url,
        "nws_obs": s.nws_observation_url,
        "tz": s.timezone,
        "dsm_times_z": s.dsm_times_z,
        "six_hour_z": s.six_hour_z,
    }
    for code, s in _STATIONS.items()
}


# ─── Data Structures ──────────────────────────────────

@dataclass
class ModelGroup:
    """Ensemble members from a single model."""
    name: str
    members: list[float] = field(default_factory=list)
    weight: float = 1.0
    mean: float = 0.0
    std: float = 0.0


@dataclass
class EnsembleV2:
    """Multi-model ensemble with per-model tracking."""
    models: list[ModelGroup] = field(default_factory=list)
    all_members: list[float] = field(default_factory=list)
    weighted_members: list[float] = field(default_factory=list)
    member_weights: list[float] = field(default_factory=list)  # Per-member KDE weights (parallel to weighted_members)
    total_count: int = 0
    # Stats from weighted distribution
    mean: float = 0.0
    median: float = 0.0
    std: float = 0.0
    min_val: float = 0.0
    max_val: float = 0.0
    p10: float = 0.0
    p25: float = 0.0
    p50: float = 0.0
    p75: float = 0.0
    p90: float = 0.0
    # KDE
    kde_bandwidth: float = 0.0
    is_bimodal: bool = False  # True when ensemble splits into 2 clusters


@dataclass
class HRRRNBMData:
    """Deterministic high-res forecasts: HRRR (3km) + NBM (2.5km, bias-corrected)."""
    hrrr_high: float = 0.0  # HRRR daily max from hourly temps
    nbm_high: float = 0.0   # NBM daily max from hourly temps


@dataclass
class NWSData:
    forecast_high: float = 0.0
    current_temp: float = 0.0
    current_wind: float = 0.0
    midnight_temp: float = 0.0
    afternoon_temp: float = 0.0
    peak_wind_gust: float = 0.0
    peak_precip_prob: int = 0
    peak_dewpoint: float = 0.0
    is_midnight_high: bool = False
    wind_penalty: float = 0.0
    wet_bulb_penalty: float = 0.0
    physics_high: float = 0.0
    temp_trend: str = ""  # "running_hot", "running_cold", "on_track"
    hourly_temps: list[tuple] = field(default_factory=list)


@dataclass
class OrderBookDepth:
    """Order book depth metrics for a single bracket."""
    ticker: str = ""
    bid_depth: int = 0        # Total contracts resting on bid side
    ask_depth: int = 0        # Total contracts resting on ask side
    bid_levels: int = 0       # Number of price levels with bids
    ask_levels: int = 0       # Number of price levels with asks
    spread: int = 0           # Best ask - best bid (cents)
    bid_wall: int = 0         # Largest single bid quantity
    ask_wall: int = 0         # Largest single ask quantity
    imbalance: float = 0.0    # (bid_depth - ask_depth) / (bid+ask), range -1 to +1
    grade: str = "?"          # A/B/C/D liquidity grade


@dataclass
class Opportunity:
    city: str
    bracket_title: str
    ticker: str
    low: float
    high: float
    yes_bid: int = 0
    yes_ask: int = 0
    volume: int = 0
    # Model (KDE-based)
    kde_prob: float = 0.0
    histogram_prob: float = 0.0
    weighted_prob: float = 0.0
    # Edge
    edge_raw: float = 0.0
    edge_after_fees: float = 0.0
    # Sizing
    kelly: float = 0.0
    suggested_contracts: int = 0
    side: str = "yes"
    confidence: str = "LOW"
    confidence_score: float = 0.0
    strategies: list[str] = field(default_factory=list)
    rationale: str = ""
    # Timing
    entry_window: str = ""
    bot_risk: str = ""
    # Hybrid Trade Score
    trade_score: float = 0.0
    trade_score_components: dict = field(default_factory=dict)


# ─── KDE Engine — imported from utils.stats ───────────

from utils.stats import kde_probability, _detect_bimodal, build_member_weights
from utils.stats import silverman_bandwidth as _silverman_bandwidth_base


def silverman_bandwidth(members: list[float], min_bandwidth: float = 0.3) -> float:
    """Silverman bandwidth with calibration factor applied (from backtest data)."""
    return _silverman_bandwidth_base(
        members, min_bandwidth=min_bandwidth, bandwidth_factor=_BANDWIDTH_FACTOR
    )


# ─── Model Weighting ──────────────────────────────────

def weight_ensemble_members(models: list[ModelGroup]) -> list[float]:
    """Backward-compatible helper: returns flat sorted member list."""
    all_members = []
    for mg in models:
        if not mg.members:
            continue
        all_members.extend(mg.members)
    return sorted(all_members)


# ─── Fetchers ──────────────────────────────────────────

async def fetch_ensemble_v2(session: aiohttp.ClientSession, city_key: str, target_date: str) -> EnsembleV2:
    """Fetch multi-model ensemble including AIFS AI model."""
    city = CITIES[city_key]
    result = EnsembleV2()

    params = {
        "latitude": city["lat"],
        "longitude": city["lon"],
        "models": ",".join(ENSEMBLE_MODELS),
        "daily": "temperature_2m_max",
        "temperature_unit": "fahrenheit",
        "start_date": target_date,
        "end_date": target_date,
    }

    try:
        async with session.get(ENSEMBLE_URL, params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status != 200:
                body = await resp.text()
                logger.warning("Open-Meteo returned %d for %s: %s", resp.status, city_key, body[:200])
                return result
            data = await resp.json()

        daily = data.get("daily", {})

        # Parse per-model members using key suffix matching
        # Keys look like: temperature_2m_max_ecmwf_aifs025_ensemble
        #                  temperature_2m_max_member01_ecmwf_aifs025_ensemble
        model_members = {name: [] for name in ENSEMBLE_MODELS}

        for key, values in daily.items():
            if not key.startswith("temperature_2m_max") or not isinstance(values, list):
                continue

            # Match key suffix to model
            matched_model = None
            for api_name, key_suffix in MODEL_KEY_SUFFIXES.items():
                if key.endswith(key_suffix):
                    matched_model = api_name
                    break

            if matched_model is None:
                continue  # Unknown model, skip

            for v in values:
                if v is not None:
                    model_members[matched_model].append(float(v))

        # Build per-model groups (with bias correction if available)
        all_temps = []
        for model_name in ENSEMBLE_MODELS:
            members = model_members.get(model_name, [])
            if members:
                # Apply bias correction: shift all members to remove systematic bias
                bias_corr = _BIAS_CORRECTIONS.get((model_name, city_key), 0.0)
                if bias_corr != 0.0:
                    members = [v + bias_corr for v in members]
                    logger.debug("  %s bias correction for %s: %+.2f°F (%d members)",
                                 model_name, city_key, bias_corr, len(members))

                mg = ModelGroup(
                    name=model_name,
                    members=sorted(members),
                    weight=MODEL_WEIGHTS.get(model_name, 1.0),
                )
                mg.mean = sum(members) / len(members)
                mg.std = math.sqrt(sum((v - mg.mean) ** 2 for v in members) / (len(members) - 1)) if len(members) > 1 else 0
                result.models.append(mg)
                all_temps.extend(members)

        if not all_temps:
            return result

        result.all_members = sorted(all_temps)
        result.total_count = len(all_temps)

        # Weighted ensemble — raw members + per-member weights for KDE
        result.weighted_members, result.member_weights = build_member_weights(result.models)

        # Compute weighted stats (numpy)
        if result.weighted_members:
            wm_arr = np.asarray(result.weighted_members, dtype=np.float64)
            w_arr = np.asarray(result.member_weights, dtype=np.float64)
            w_norm = w_arr / w_arr.sum()
            result.mean = float(np.average(wm_arr, weights=w_norm))
            # Weighted sample variance: V1/(V1^2 - V2) * sum(w*(x-mean)^2)
            v1 = w_norm.sum()  # = 1.0 after normalization
            v2 = (w_norm ** 2).sum()
            denom = v1 * v1 - v2
            if denom > 0:
                result.std = float(np.sqrt((w_norm * (wm_arr - result.mean) ** 2).sum() / denom))
            else:
                result.std = float(np.std(wm_arr, ddof=1))
            result.min_val = float(wm_arr[0])
            result.max_val = float(wm_arr[-1])
            result.p10 = float(np.percentile(wm_arr, 10))
            result.p25 = float(np.percentile(wm_arr, 25))
            result.median = result.p50 = float(np.percentile(wm_arr, 50))
            result.p75 = float(np.percentile(wm_arr, 75))
            result.p90 = float(np.percentile(wm_arr, 90))
            result.kde_bandwidth = silverman_bandwidth(result.weighted_members)
            result.is_bimodal = _detect_bimodal(np.sort(np.asarray(result.weighted_members)))

    except Exception as e:
        logger.error("Ensemble fetch failed for %s: %s", city_key, e)

    return result


async def fetch_hrrr_nbm(session: aiohttp.ClientSession, city_key: str, target_date: str) -> HRRRNBMData:
    """Fetch HRRR (3km) and NBM (2.5km, bias-corrected) deterministic daily max.

    Both are hourly-updated US models served via Open-Meteo forecast API.
    HRRR: best short-range model, 18h horizon.
    NBM: NOAA's post-processed blend of ~40 models, already bias-corrected.
    """
    city = CITIES[city_key]
    result = HRRRNBMData()

    params = {
        "latitude": city["lat"],
        "longitude": city["lon"],
        "models": "ncep_hrrr_conus,ncep_nbm_conus",
        "hourly": "temperature_2m",
        "temperature_unit": "fahrenheit",
        "start_date": target_date,
        "end_date": target_date,
    }

    try:
        async with session.get(FORECAST_URL, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                body = await resp.text()
                logger.debug("HRRR/NBM returned %d for %s: %s", resp.status, city_key, body[:200])
                return result
            data = await resp.json()

        hourly = data.get("hourly", {})

        # HRRR temps — key: temperature_2m_ncep_hrrr_conus (or temperature_2m if single model)
        hrrr_temps = hourly.get("temperature_2m_ncep_hrrr_conus") or []
        hrrr_valid = [t for t in hrrr_temps if t is not None]
        if hrrr_valid:
            result.hrrr_high = max(hrrr_valid)

        # NBM temps — key: temperature_2m_ncep_nbm_conus
        nbm_temps = hourly.get("temperature_2m_ncep_nbm_conus") or []
        nbm_valid = [t for t in nbm_temps if t is not None]
        if nbm_valid:
            result.nbm_high = max(nbm_valid)

        if result.hrrr_high or result.nbm_high:
            logger.debug("HRRR/NBM %s: HRRR=%.1f°F NBM=%.1f°F",
                         city_key, result.hrrr_high, result.nbm_high)

    except Exception as e:
        logger.debug("HRRR/NBM fetch failed for %s: %s", city_key, e)

    return result


async def fetch_nws(session: aiohttp.ClientSession, city_key: str, target_date) -> NWSData:
    """Fetch NWS hourly forecast and current obs with trend detection."""
    city = CITIES[city_key]
    tz = ZoneInfo(city["tz"])
    result = NWSData()
    headers = {"User-Agent": "EdgeScannerV2/2.0", "Accept": "application/geo+json"}

    # Current observation
    try:
        async with session.get(city["nws_obs"], headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json()
                props = data.get("properties", {})
                temp_c = props.get("temperature", {}).get("value")
                wind_kmh = props.get("windSpeed", {}).get("value")
                if temp_c is not None:
                    result.current_temp = round(temp_c * 1.8 + 32, 1)
                if wind_kmh is not None:
                    result.current_wind = round(wind_kmh * 0.621371, 1)  # km/h → mph
    except Exception as e:
        logger.warning("NWS obs failed: %s", e)

    # Hourly forecast
    try:
        async with session.get(city["nws_hourly"], headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return result
            data = await resp.json()

        periods = data.get("properties", {}).get("periods", [])
        tomorrow_temps = []
        midnight_temps = []
        afternoon_temps = []
        peak_wind = 0.0
        peak_gust_explicit = 0.0
        peak_precip = 0
        peak_dewpoint = 0.0

        for p in periods:
            try:
                t = datetime.fromisoformat(p["startTime"].replace("Z", "+00:00")).astimezone(tz)
                if t.date() != target_date:
                    continue

                temp_f = float(p.get("temperature", 0))
                wind_str = p.get("windSpeed", "0 mph")
                wind_match = re.search(r"(\d+)\s*(?:to\s*(\d+))?\s*mph", wind_str, re.I)
                wind_speed = float(wind_match.group(2) or wind_match.group(1)) if wind_match else 0.0

                # BUG FIX: Use explicit NWS gust data when available
                gust_str = p.get("windGust", {})
                if isinstance(gust_str, dict):
                    gust_val = gust_str.get("value")
                    gust_mph = float(gust_val) * 0.621371 if gust_val is not None else 0  # km/h → mph
                elif isinstance(gust_str, str):
                    gust_match = re.search(r"(\d+)", gust_str)
                    gust_mph = float(gust_match.group(1)) if gust_match else 0
                else:
                    gust_mph = 0
                # Fallback: estimate if no explicit gust
                if gust_mph == 0 and wind_speed > 10:
                    gust_mph = wind_speed * 1.5

                precip_val = p.get("probabilityOfPrecipitation", {}).get("value")
                precip = int(precip_val) if precip_val is not None else 0
                dew_val = p.get("dewpoint", {}).get("value")
                dew_f = (float(dew_val) * 1.8 + 32) if dew_val is not None else 0.0

                tomorrow_temps.append(temp_f)
                result.hourly_temps.append((t.hour, temp_f, wind_speed, precip))

                # BUG FIX: Use max() for midnight/afternoon, not first value
                if 0 <= t.hour <= 1:
                    midnight_temps.append(temp_f)
                if 14 <= t.hour <= 16:
                    afternoon_temps.append(temp_f)

                if gust_mph > peak_gust_explicit:
                    peak_gust_explicit = gust_mph
                if wind_speed > peak_wind:
                    peak_wind = wind_speed
                # BUG FIX: Track precip only during DAYTIME (8AM-8PM) for wet bulb
                if 8 <= t.hour <= 20 and precip > peak_precip:
                    peak_precip = precip
                if temp_f == max(tomorrow_temps):
                    peak_dewpoint = dew_f

            except (KeyError, ValueError):
                continue

        if tomorrow_temps:
            result.forecast_high = max(tomorrow_temps)
        if midnight_temps:
            result.midnight_temp = max(midnight_temps)  # BUG FIX: max() not first
        if afternoon_temps:
            result.afternoon_temp = max(afternoon_temps)  # BUG FIX: max() not first

        result.is_midnight_high = (
            result.midnight_temp > result.afternoon_temp
            if result.midnight_temp and result.afternoon_temp else False
        )

        result.peak_wind_gust = peak_gust_explicit if peak_gust_explicit > 0 else (peak_wind * 1.5 if peak_wind > 10 else peak_wind)
        result.peak_precip_prob = peak_precip
        result.peak_dewpoint = peak_dewpoint

        # Strategy B: Wind penalty (using explicit gusts now)
        if result.peak_wind_gust > 25:
            result.wind_penalty = 2.0
        elif result.peak_wind_gust > 15:
            result.wind_penalty = 1.0

        # Strategy D: Wet bulb (DAYTIME precip only — bug fix)
        if peak_precip >= 40:
            depression = result.forecast_high - peak_dewpoint
            if depression >= 5:
                factor = 0.40 if peak_precip >= 70 else 0.25
                result.wet_bulb_penalty = round(depression * factor, 1)

        # Cap stacked physics penalties: wind + wet bulb can over-correct together
        total_penalty = result.wind_penalty + result.wet_bulb_penalty
        MAX_COMBINED_PENALTY = 3.0  # Empirical cap — both effects share boundary layer physics
        if total_penalty > MAX_COMBINED_PENALTY:
            # Scale each penalty proportionally to preserve relative weights
            scale = MAX_COMBINED_PENALTY / total_penalty
            result.wind_penalty = round(result.wind_penalty * scale, 1)
            result.wet_bulb_penalty = round(result.wet_bulb_penalty * scale, 1)

        result.physics_high = result.forecast_high - result.wind_penalty - result.wet_bulb_penalty
        if result.is_midnight_high:
            result.physics_high = result.midnight_temp

        # Trend detection: is current temp running hot/cold vs forecast?
        if result.current_temp > 0 and result.forecast_high > 0:
            now_hour = datetime.now(tz).hour
            # Find what NWS expected at this hour
            expected = None
            for h, t, _, _ in result.hourly_temps:
                if h == now_hour:
                    expected = t
                    break
            if expected:
                diff = result.current_temp - expected
                if diff > 2:
                    result.temp_trend = "running_hot"
                elif diff < -2:
                    result.temp_trend = "running_cold"
                else:
                    result.temp_trend = "on_track"

    except Exception as e:
        logger.error("NWS fetch failed for %s: %s", city_key, e)

    return result


async def fetch_kalshi_brackets(session: aiohttp.ClientSession, city_key: str) -> list[dict]:
    """Fetch open Kalshi brackets."""
    city = CITIES[city_key]
    try:
        url = f"{KALSHI_BASE}/markets?series_ticker={city['series']}&status=open&limit=100"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
            return data.get("markets", [])
    except Exception as e:
        logger.error("Kalshi fetch failed for %s: %s", city_key, e)
        return []


async def fetch_orderbook_depth(
    session: aiohttp.ClientSession,
    tickers: list[str],
) -> dict[str, OrderBookDepth]:
    """Fetch order book depth for a list of tickers and compute liquidity metrics.

    Calls GET /markets/{ticker}/orderbook?depth=10 for each ticker in parallel.
    Returns dict keyed by ticker → OrderBookDepth.
    Non-critical: returns empty dict on total failure.
    """
    if not tickers:
        return {}

    depth_map: dict[str, OrderBookDepth] = {}

    async def _fetch_one(ticker: str) -> tuple[str, OrderBookDepth]:
        ob = OrderBookDepth(ticker=ticker)
        try:
            url = f"{KALSHI_BASE}/markets/{ticker}/orderbook?depth=10"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status != 200:
                    return ticker, ob
                data = await resp.json()

            book = data.get("orderbook", data)  # Sometimes nested, sometimes flat
            yes_bids = book.get("yes", [])  # [[price, qty], ...]
            no_bids = book.get("no", [])    # [[price, qty], ...]

            # Bid side (YES bids)
            if yes_bids:
                ob.bid_levels = len(yes_bids)
                ob.bid_depth = sum(entry[1] for entry in yes_bids)
                ob.bid_wall = max(entry[1] for entry in yes_bids)
                best_bid = max(entry[0] for entry in yes_bids)
            else:
                best_bid = 0

            # Ask side (derived from NO bids: yes_ask = 100 - no_bid)
            if no_bids:
                ob.ask_levels = len(no_bids)
                ob.ask_depth = sum(entry[1] for entry in no_bids)
                ob.ask_wall = max(entry[1] for entry in no_bids)
                best_no_bid = max(entry[0] for entry in no_bids)
                best_ask = 100 - best_no_bid
            else:
                best_ask = 100

            # Spread
            ob.spread = max(0, best_ask - best_bid) if best_bid > 0 else 99

            # Imbalance: positive = buyers stacking, negative = sellers stacking
            total = ob.bid_depth + ob.ask_depth
            if total > 0:
                ob.imbalance = round((ob.bid_depth - ob.ask_depth) / total, 2)

            # Liquidity grade
            if total > 500 and ob.spread <= 2 and ob.bid_levels >= 3 and ob.ask_levels >= 3:
                ob.grade = "A"
            elif total > 200 and ob.spread <= 3:
                ob.grade = "B"
            elif total > 50 and ob.spread <= 5:
                ob.grade = "C"
            else:
                ob.grade = "D"

        except Exception as e:
            logger.debug("Orderbook fetch failed for %s: %s", ticker, e)

        return ticker, ob

    # Fetch all in parallel (small batches with stagger for rate limiting)
    batch_size = 8
    for i in range(0, len(tickers), batch_size):
        batch = tickers[i:i + batch_size]
        results = await asyncio.gather(*[_fetch_one(t) for t in batch], return_exceptions=True)
        for r in results:
            if isinstance(r, tuple) and len(r) == 2:
                depth_map[r[0]] = r[1]
        if i + batch_size < len(tickers):
            await asyncio.sleep(0.2)  # Small stagger between batches

    return depth_map


# ─── Analysis ──────────────────────────────────────────

def get_bid(mkt: dict) -> int:
    """Get the best available bid price from a market dict."""
    return mkt.get("yes_bid", 0) or mkt.get("yes_price", 0)


def parse_bracket_range(title: str) -> tuple[float, float, str]:
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


def taker_fee_cents(price_cents: int) -> float:
    p = price_cents / 100
    return round(0.07 * p * (1 - p) * 100, 2)


def kelly_fraction(model_prob: float, market_price: float) -> float:
    if model_prob <= 0 or market_price <= 0 or market_price >= 1:
        return 0.0
    b = (1 / market_price) - 1
    f = (b * model_prob - (1 - model_prob)) / b
    return max(0, f * 0.5)  # Half-Kelly


def is_tomorrow_ticker(ticker: str, tomorrow_date) -> bool:
    months = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC']
    date_str = f"{tomorrow_date.year % 100:02d}{months[tomorrow_date.month - 1]}{tomorrow_date.day:02d}"
    return date_str in ticker


def compute_confidence_score(ensemble: EnsembleV2, nws: NWSData, bracket_low: float = 0,
                             bracket_high: float = 999, lead_hours: float = 18.0,
                             hrrr_nbm: HRRRNBMData = None) -> tuple[str, float, list[str]]:
    """
    Multi-factor confidence scoring — calibrated for 90+ threshold.

    Returns (label, score_0_to_100, reasons[])

    lead_hours: hours until settlement. Thresholds scale:
      - At 6h: tighter thresholds (more certainty expected)
      - At 18h: looser thresholds (wider spread is normal)

    Scoring:
      Base: 40 points
      Factor 1: Ensemble spread (σ)             → up to +15
      Factor 2: AIFS vs IFS agreement            → up to +15
      Factor 3: Multi-model bracket agreement     → up to +15
      Factor 4: NWS alignment with ensemble       → up to +10
      Factor 5: Real-time trend confirmation      → up to +5
      Factor 6: Lead-time bonus (short lead)      → up to +5

    Hard penalties (can push score negative):
      - Model spread > 4°F: -20
      - AIFS vs IFS diverge > 3°F: -15
      - NWS diverges > 3°F from ensemble: -10
      - Running hot/cold (observations off-forecast): -5
    """
    score = 40.0  # Conservative baseline
    reasons = []

    # Lead-time scaling factor: 1.0 at 18h, ~1.4 at 6h (tighter thresholds expected at short lead)
    # At short lead, models should be tighter, so we scale thresholds down to be stricter
    # Conversely, at long lead we relax thresholds
    lt_scale = max(0.7, min(1.4, lead_hours / 18.0)) if lead_hours > 0 else 1.0

    # ── Factor 1: Ensemble spread (σ) ── max +15
    # Thresholds scale with lead time: at 6h, expect σ < 0.5 for TIGHT; at 18h, σ < 0.8
    t_tight = 0.8 * lt_scale
    t_good = 1.2 * lt_scale
    t_mod = 1.8 * lt_scale
    t_wide = 2.5 * lt_scale

    if ensemble.std < t_tight:
        score += 15
        reasons.append(f"σ={ensemble.std:.1f}° (TIGHT)")
    elif ensemble.std < t_good:
        score += 12
        reasons.append(f"σ={ensemble.std:.1f}° (good)")
    elif ensemble.std < t_mod:
        score += 6
        reasons.append(f"σ={ensemble.std:.1f}° (moderate)")
    elif ensemble.std < t_wide:
        reasons.append(f"σ={ensemble.std:.1f}° (wide)")
    else:
        score -= 20
        reasons.append(f"σ={ensemble.std:.1f}° (VERY WIDE ⚠)")

    # ── Factor 2: AIFS vs IFS agreement ── max +15
    aifs_mean = None
    ifs_mean = None
    for mg in ensemble.models:
        if "aifs" in mg.name:
            aifs_mean = mg.mean
        if "ecmwf_ifs" in mg.name:
            ifs_mean = mg.mean
    if aifs_mean is not None and ifs_mean is not None:
        divergence = abs(aifs_mean - ifs_mean)
        if divergence < 0.5:
            score += 15
            reasons.append(f"AIFS↔IFS agree within {divergence:.1f}°F ✓")
        elif divergence < 1.0:
            score += 12
            reasons.append(f"AIFS↔IFS close ({divergence:.1f}°F)")
        elif divergence < 2.0:
            score += 5
            reasons.append(f"AIFS↔IFS diverge {divergence:.1f}°F")
        elif divergence < 3.0:
            score -= 5
            reasons.append(f"AIFS↔IFS disagree {divergence:.1f}°F ⚠")
        else:
            score -= 15
            reasons.append(f"AIFS↔IFS STRONGLY DISAGREE {divergence:.1f}°F ✗")

    # ── Factor 3: Multi-model bracket agreement ── max +15 (NEW)
    # How many model families place >25% of members in the target bracket?
    # Applies to all bracket types including tails (low=-999 or high=999)
    if ensemble.models:
        models_agree = 0
        total_models = 0
        for mg in ensemble.models:
            if not mg.members:
                continue
            total_models += 1
            in_bracket = sum(1 for t in mg.members if bracket_low <= t < bracket_high)
            if in_bracket / len(mg.members) >= 0.25:
                models_agree += 1
        if total_models > 0:
            agree_pct = models_agree / total_models
            if agree_pct >= 0.8:
                score += 15
                reasons.append(f"{models_agree}/{total_models} models agree on bracket (STRONG)")
            elif agree_pct >= 0.6:
                score += 10
                reasons.append(f"{models_agree}/{total_models} models agree on bracket")
            elif agree_pct >= 0.4:
                score += 5
                reasons.append(f"{models_agree}/{total_models} models agree on bracket (mixed)")
            else:
                score -= 5
                reasons.append(f"Only {models_agree}/{total_models} models agree (WEAK)")

    # ── Factor 4: NWS alignment ── max +10
    if nws.forecast_high > 0 and ensemble.mean > 0:
        nws_div = abs(nws.forecast_high - ensemble.mean)
        if nws_div < 0.5:
            score += 10
            reasons.append(f"NWS aligned with ensemble (Δ{nws_div:.1f}°F)")
        elif nws_div < 1.5:
            score += 5
            reasons.append(f"NWS close to ensemble (Δ{nws_div:.1f}°F)")
        elif nws_div < 3.0:
            reasons.append(f"NWS diverges from ensemble (Δ{nws_div:.1f}°F)")
        else:
            score -= 10
            reasons.append(f"NWS DIVERGES from ensemble (Δ{nws_div:.1f}°F ⚠)")
    else:
        # NWS data missing — neutral (don't penalize for NWS outage, which is
        # outside our control; other factors still validate the setup)
        if nws.forecast_high <= 0:
            reasons.append("NWS forecast unavailable — skipping alignment check")
        else:
            reasons.append("Ensemble mean unavailable — skipping alignment check")

    # ── Factor 5: Real-time trend ── max +5
    if nws.temp_trend == "on_track":
        score += 5
        reasons.append("Current temp ON TRACK ✓")
    elif nws.temp_trend == "running_hot":
        score -= 5
        reasons.append("Current temp RUNNING HOT ⚠")
    elif nws.temp_trend == "running_cold":
        score -= 5
        reasons.append("Current temp RUNNING COLD ⚠")
    else:
        reasons.append("No real-time trend data")

    # ── Factor 6: Lead-time bonus ── max +5
    # Short lead time = more certain forecast = bonus
    if lead_hours <= 8:
        score += 5
        reasons.append(f"Lead time {lead_hours:.0f}h (SHORT — high certainty)")
    elif lead_hours <= 12:
        score += 2
        reasons.append(f"Lead time {lead_hours:.0f}h (medium)")
    else:
        reasons.append(f"Lead time {lead_hours:.0f}h (long)")

    # ── Factor 7: HRRR/NBM agreement with ensemble ── max +5
    # HRRR is the best short-range model; NBM is NOAA's bias-corrected blend.
    # When both agree with ensemble, it's a strong validation signal.
    if hrrr_nbm and ensemble.mean > 0:
        _hrrr_checks = []
        if hrrr_nbm.hrrr_high > 0:
            hrrr_div = abs(hrrr_nbm.hrrr_high - ensemble.mean)
            _hrrr_checks.append(("HRRR", hrrr_div, hrrr_nbm.hrrr_high))
        if hrrr_nbm.nbm_high > 0:
            nbm_div = abs(hrrr_nbm.nbm_high - ensemble.mean)
            _hrrr_checks.append(("NBM", nbm_div, hrrr_nbm.nbm_high))
        if _hrrr_checks:
            avg_div = sum(d for _, d, _ in _hrrr_checks) / len(_hrrr_checks)
            labels = ", ".join(f"{n}={t:.1f}°F (Δ{d:.1f})" for n, d, t in _hrrr_checks)
            if avg_div < 1.0:
                score += 5
                reasons.append(f"HRRR/NBM aligned with ensemble ({labels}) ✓")
            elif avg_div < 2.0:
                score += 2
                reasons.append(f"HRRR/NBM close to ensemble ({labels})")
            elif avg_div > 3.0:
                score -= 5
                reasons.append(f"HRRR/NBM DIVERGE from ensemble ({labels}) ⚠")
            else:
                reasons.append(f"HRRR/NBM moderate divergence ({labels})")

    # Clamp
    score = max(0, min(100, score))

    # Classify
    if score >= 90:
        return "ELITE", score, reasons
    elif score >= 75:
        return "HIGH", score, reasons
    elif score >= 55:
        return "MEDIUM", score, reasons
    else:
        return "LOW", score, reasons


# ─── Risk Management Constants (imported from config.py) ──────────────────────

from config import (
    MAX_POSITION_PCT,
    MAX_DAILY_EXPOSURE,
    MAX_CORRELATED_EXPOSURE,
    MIN_EDGE_THRESHOLD,
    MIN_KDE_PROBABILITY,
    MIN_CONFIDENCE_TO_TRADE,
    MAX_ENTRY_PRICE_CENTS,
    FREEROLL_MULTIPLIER,
    CAPITAL_EFFICIENCY_THRESHOLD_CENTS,
    SETTLEMENT_HOUR_ET,
    LLM_CONFIDENCE_ENABLED,
    TRADE_SCORE_ENABLED,
    TRADE_SCORE_THRESHOLD,
)

# Alias for readability within this module
MAX_ENTRY_PRICE = MAX_ENTRY_PRICE_CENTS
EFFICIENCY_EXIT = CAPITAL_EFFICIENCY_THRESHOLD_CENTS

# LLM module — lazy import to avoid overhead when disabled
_llm_module = None

def _get_llm_module():
    """Lazy-load LLM module only when enabled."""
    global _llm_module
    if _llm_module is None:
        try:
            from llm_confidence import LLMConfidenceModule
            _llm_module = LLMConfidenceModule()
        except ImportError:
            _llm_module = False  # Sentinel: tried and failed
    return _llm_module if _llm_module is not False else None


def get_entry_timing(city_key: str) -> tuple[str, str]:
    """Determine optimal entry window and bot risk."""
    tz = ZoneInfo(CITIES[city_key]["tz"])
    now = datetime.now(tz)
    hour = now.hour

    # Check if near DSM/6-hour release
    city = CITIES[city_key]
    now_utc = datetime.now(ZoneInfo("UTC"))
    near_release = False
    for t_str in city.get("dsm_times_z", []) + city.get("six_hour_z", []):
        h, m = map(int, t_str.split(":"))
        release_min = h * 60 + m
        now_min = now_utc.hour * 60 + now_utc.minute
        if abs(release_min - now_min) < 15:
            near_release = True
            break

    if near_release:
        bot_risk = "HIGH — Near DSM/6-hour release. Pull exposed limit orders!"
    elif 22 <= hour or hour < 6:
        bot_risk = "LOW — Overnight, bots mostly idle"
    elif 10 <= hour <= 11:
        bot_risk = "MEDIUM — Market open, stale pricing"
    else:
        bot_risk = "LOW — Normal trading hours"

    # Optimal entry window (non-overlapping hour ranges, 0-23)
    if hour < 1 or hour >= 23:
        window = "MIDNIGHT WINDOW — Strategy A trigger zone. Check if midnight > afternoon"
    elif hour < 10:
        window = "PRE-MARKET (fresh 00Z models) — good for next-day positioning"
    elif hour <= 12:
        window = "MARKET OPEN — stale pricing, wide spreads. Good edge but uncertain models"
    elif hour < 15:
        window = "WAIT — 18Z HRRR hasn't posted yet. Models may shift"
    elif hour <= 17:
        window = "OPTIMAL — Post-HRRR convergence. Maximum information, minimum uncertainty"
    else:
        window = "LATE — Check for midnight high setup if cold front approaching"

    return window, bot_risk


def analyze_opportunities_v2(
    city_key: str,
    ensemble: EnsembleV2,
    nws: NWSData,
    brackets: list[dict],
    balance: float,
    existing_exposure: dict = None,
    hrrr_nbm: HRRRNBMData = None,
    depth_map: dict[str, OrderBookDepth] = None,
) -> list[Opportunity]:
    """Analyze brackets using KDE probabilities and weighted ensemble.

    existing_exposure: {city_key: dollar_exposure} of current open positions,
    used to enforce MAX_CORRELATED_EXPOSURE across same-city positions.
    """
    tz = ZoneInfo(CITIES[city_key]["tz"])
    now_local = datetime.now(tz)
    tomorrow = (now_local + timedelta(days=1)).date()
    entry_window, bot_risk = get_entry_timing(city_key)

    # Compute lead time to settlement (tomorrow at SETTLEMENT_HOUR_ET in ET)
    settlement_time = datetime.combine(tomorrow, datetime.min.time()).replace(
        hour=SETTLEMENT_HOUR_ET, tzinfo=ZoneInfo("America/New_York"))
    lead_hours = max(0, (settlement_time - datetime.now(ZoneInfo("America/New_York"))).total_seconds() / 3600)

    # ── HRRR pseudo-member injection ──
    # HRRR is deterministic (1 value), so inject as pseudo-members to give it
    # KDE presence without dominating 230+ real ensemble members.
    # Weight scales with lead time: HRRR is best < 8h out.
    _hrrr_members = list(ensemble.weighted_members)  # Copy
    _hrrr_weights = list(ensemble.member_weights)
    _hrrr_bw = ensemble.kde_bandwidth
    if hrrr_nbm and hrrr_nbm.hrrr_high > 0 and ensemble.weighted_members:
        hrrr_w = 2.0 if lead_hours < 8 else 1.5
        hrrr_n = 8  # 8 pseudo-members × weight → ~12-16 effective members
        for _ in range(hrrr_n):
            _hrrr_members.append(hrrr_nbm.hrrr_high)
            _hrrr_weights.append(hrrr_w)
        # Re-sort for KDE (member order matters for kernel placement)
        pairs = sorted(zip(_hrrr_members, _hrrr_weights))
        _hrrr_members = [p[0] for p in pairs]
        _hrrr_weights = [p[1] for p in pairs]
        # Recalculate bandwidth with augmented members
        _hrrr_bw = silverman_bandwidth(_hrrr_members)
        logger.debug("HRRR injection: +%d pseudo @ %.1f°F w=%.1f → %d total effective members",
                      hrrr_n, hrrr_nbm.hrrr_high, hrrr_w, len(_hrrr_members))

    opps = []
    for mkt in brackets:
        ticker = mkt.get("ticker", "")
        if not is_tomorrow_ticker(ticker, tomorrow):
            continue

        title = mkt.get("title", "") or mkt.get("subtitle", "")
        low, high, edge_type = parse_bracket_range(title)
        if edge_type == "unknown":
            continue

        yes_bid = get_bid(mkt)
        yes_ask = mkt.get("yes_ask", 0)
        volume = mkt.get("volume", 0)

        # KDE probability (augmented with HRRR pseudo-members if available)
        kde_prob = kde_probability(
            _hrrr_members, low, high,
            bandwidth=_hrrr_bw,
            weights=_hrrr_weights,
        ) if _hrrr_members else 0

        # Histogram probability (weighted, for comparison)
        if ensemble.weighted_members and ensemble.member_weights:
            w_total = sum(ensemble.member_weights)
            hist_prob = sum(w for t, w in zip(ensemble.weighted_members, ensemble.member_weights) if low <= t < high) / w_total if w_total > 0 else 0
        elif ensemble.weighted_members:
            hist_prob = sum(1 for t in ensemble.weighted_members if low <= t < high) / len(ensemble.weighted_members)
        else:
            hist_prob = 0

        # Use KDE as primary
        model_prob = kde_prob

        # Edge computation against actual entry prices (bid+1 for maker pegging)
        yes_entry = min(yes_bid + 1, MAX_ENTRY_PRICE) if yes_bid > 0 else 1
        no_entry = min(100 - yes_ask + 1, MAX_ENTRY_PRICE) if yes_ask < 100 else 1
        yes_entry_prob = yes_entry / 100
        no_entry_prob = no_entry / 100

        yes_edge = model_prob - yes_entry_prob
        no_edge = (1 - no_entry_prob) - model_prob  # NO edge: (1 - no_cost) - model_prob_yes = model_prob_no - no_cost

        # Maker orders (bid+1) pay 0% fee, so no fee deduction needed
        if yes_edge > 0.04:
            side = "yes"
            edge_raw = yes_edge
            edge_after = edge_raw  # Maker = 0% fee
        elif no_edge > 0.04:
            side = "no"
            edge_raw = no_edge
            edge_after = edge_raw  # Maker = 0% fee
        else:
            continue

        if edge_after <= 0.01:
            continue

        # ── Per-bracket confidence scoring (uses bracket bounds + lead time) ──
        confidence_label, confidence_score, _ = compute_confidence_score(
            ensemble, nws, bracket_low=low, bracket_high=high, lead_hours=lead_hours,
            hrrr_nbm=hrrr_nbm,
        )

        # ── Risk Management Gates ──
        price_cents = yes_entry if side == "yes" else no_entry

        # Gate 1: Minimum KDE probability
        if model_prob < MIN_KDE_PROBABILITY and side == "yes":
            continue  # Skip low-probability YES trades
        if (1 - model_prob) < MIN_KDE_PROBABILITY and side == "no":
            continue  # Skip low-probability NO trades

        # Gate 2: Minimum edge after fees
        if edge_after < MIN_EDGE_THRESHOLD:
            continue

        # Gate 3: Max entry price (never buy YES above 50¢)
        if side == "yes" and price_cents > MAX_ENTRY_PRICE:
            continue

        # Kelly & sizing
        if side == "yes":
            k = kelly_fraction(model_prob, yes_entry_prob)
        else:
            k = kelly_fraction(1 - model_prob, no_entry_prob)

        max_cost = balance * MAX_POSITION_PCT

        # Correlated exposure cap: limit total exposure per city
        if existing_exposure:
            city_exposure = existing_exposure.get(city_key, 0.0)
            correlated_max = balance * MAX_CORRELATED_EXPOSURE
            remaining_city_budget = max(0, correlated_max - city_exposure)
            max_cost = min(max_cost, remaining_city_budget)

        suggested = min(100, int(max_cost / (max(price_cents, 1) / 100))) if price_cents > 0 else 0

        # Strategy flags
        strategies = []
        if nws.is_midnight_high:
            strategies.append("A:MIDNIGHT_HIGH")
        if nws.wind_penalty > 0:
            strategies.append(f"B:WIND(-{nws.wind_penalty:.0f}°F, gusts {nws.peak_wind_gust:.0f}mph)")
        if nws.wet_bulb_penalty > 0:
            strategies.append(f"D:WET_BULB(-{nws.wet_bulb_penalty:.1f}°F)")
        if abs(nws.forecast_high - ensemble.mean) > 2:
            strategies.append(f"E:NWS_DIVERGE({nws.forecast_high:.0f}°F vs {ensemble.mean:.1f}°F)")
        if nws.temp_trend and nws.temp_trend != "on_track":
            strategies.append(f"TREND:{nws.temp_trend.upper()}")

        # Rationale
        parts = []
        if model_prob > 0.3 and yes_entry_prob < 0.10:
            parts.append("MASSIVE MISPRICING")
        if abs(kde_prob - hist_prob) > 0.02:
            parts.append(f"KDE={kde_prob*100:.0f}% vs Hist={hist_prob*100:.0f}%")

        # Per-model breakdown
        for mg in ensemble.models:
            mg_prob = sum(1 for t in mg.members if low <= t < high) / len(mg.members) if mg.members else 0
            if mg_prob > 0.25:
                label = mg.name.replace("_seamless", "").replace("025", "").replace("ecmwf_", "").upper()
                parts.append(f"{label}={mg_prob*100:.0f}%")

        if nws.temp_trend == "running_cold":
            parts.append("Obs BELOW forecast — supports lower brackets")
        elif nws.temp_trend == "running_hot":
            parts.append("Obs ABOVE forecast — supports higher brackets")

        # Mark tradeable based on confidence gate
        is_tradeable = confidence_score >= MIN_CONFIDENCE_TO_TRADE
        if is_tradeable:
            parts.insert(0, "★ MEETS 90+ CONFIDENCE GATE ★")

        opp = Opportunity(
            city=city_key,
            bracket_title=title,
            ticker=ticker,
            low=low, high=high,
            yes_bid=yes_bid, yes_ask=yes_ask, volume=volume,
            kde_prob=kde_prob,
            histogram_prob=hist_prob,
            weighted_prob=model_prob,
            edge_raw=edge_raw,
            edge_after_fees=edge_after,
            kelly=k,
            suggested_contracts=suggested,
            side=side,
            confidence=confidence_label,
            confidence_score=confidence_score,
            strategies=strategies,
            rationale=" · ".join(parts) if parts else "Ensemble edge",
            entry_window=entry_window,
            bot_risk=bot_risk,
        )

        # Compute hybrid trade score
        try:
            from trade_score import compute_trade_score as _compute_ts
            _opp_depth = depth_map.get(opp.ticker) if depth_map else None
            ts = _compute_ts(opp, lead_hours, depth=_opp_depth)
            opp.trade_score = round(ts.score, 4)
            opp.trade_score_components = {
                "confidence_signal": round(ts.confidence_signal, 4),
                "edge_signal": round(ts.edge_signal, 4),
                "urgency_signal": round(ts.urgency_signal, 4),
                "liquidity_penalty": round(ts.liquidity_penalty, 4),
                "w_confidence": round(ts.w_confidence, 4),
                "w_edge": round(ts.w_edge, 4),
                "w_urgency": round(ts.w_urgency, 4),
                "hours_to_settlement": round(lead_hours, 1),
                "tradeable": ts.tradeable,
            }
        except Exception as e:
            logger.warning("trade_score computation failed for %s: %s", opp.ticker, e)

        opps.append(opp)

    if TRADE_SCORE_ENABLED:
        opps.sort(key=lambda x: x.trade_score, reverse=True)
    else:
        opps.sort(key=lambda x: (x.confidence_score >= MIN_CONFIDENCE_TO_TRADE, x.edge_after_fees), reverse=True)
    return opps


# ─── Display ───────────────────────────────────────────

def shorten_bracket_title(title: str) -> str:
    """Strip verbose Kalshi title down to just the bracket range."""
    return re.sub(r"Will the \*?\*?high temp.*?be ", "", title).replace("?", "").split(" on ")[0].strip()


def print_model_breakdown(ensemble: EnsembleV2):
    """Print per-model analysis."""
    if not ensemble.models:
        return

    print(f"\n  MODEL BREAKDOWN")
    print(f"  {'Model':<20} {'Members':>7} {'Mean':>7} {'σ':>5} {'Weight':>7} {'Eff.Wt':>7}")
    print(f"  {'─'*20} {'─'*7} {'─'*7} {'─'*5} {'─'*7} {'─'*7}")

    for mg in ensemble.models:
        label = mg.name.replace("_seamless", "").replace("025", "")
        if "aifs" in mg.name:
            label = "★ " + label + " (AI)"
        eff = len(mg.members) * mg.weight
        print(f"  {label:<20} {len(mg.members):>7} {mg.mean:>6.1f}° {mg.std:>4.1f}° {mg.weight:>6.2f}x {eff:>6.0f}")

    # Agreement check
    means = [mg.mean for mg in ensemble.models if mg.members]
    if means:
        spread = max(means) - min(means)
        if spread < 1.5:
            print(f"  └─ ✓ Models AGREE (spread {spread:.1f}°F)")
        elif spread < 3.0:
            print(f"  └─ ~ Models DIVERGE moderately (spread {spread:.1f}°F)")
        else:
            print(f"  └─ ✗ Models DISAGREE strongly (spread {spread:.1f}°F)")


def _ticker_to_title(ticker: str, brackets: list[dict]) -> str:
    """Look up a bracket title from its ticker."""
    for m in brackets:
        if m.get("ticker") == ticker:
            return m.get("title", "") or m.get("subtitle", "") or ticker
    return ticker


def print_city_report_v2(
    city_key: str,
    ensemble: EnsembleV2,
    nws: NWSData,
    brackets: list[dict],
    opps: list[Opportunity],
    hrrr_nbm: HRRRNBMData = None,
    depth_map: dict[str, OrderBookDepth] = None,
):
    city = CITIES[city_key]
    tz = ZoneInfo(city["tz"])
    tomorrow = (datetime.now(tz) + timedelta(days=1)).date()
    entry_window, bot_risk = get_entry_timing(city_key)
    # Compute lead time for display report
    settlement_time = datetime.combine(tomorrow, datetime.min.time()).replace(
        hour=SETTLEMENT_HOUR_ET, tzinfo=ZoneInfo("America/New_York"))
    _lead_hours = max(0, (settlement_time - datetime.now(ZoneInfo("America/New_York"))).total_seconds() / 3600)
    conf_label, conf_score, conf_reasons = compute_confidence_score(ensemble, nws, lead_hours=_lead_hours, hrrr_nbm=hrrr_nbm)

    print(f"\n{'='*72}")
    print(f"  {city['name'].upper()} — {tomorrow.strftime('%A %B %d, %Y')}")
    print(f"  Confidence: {conf_label} ({conf_score:.0f}/100) | Window: {entry_window}")
    print(f"{'='*72}")

    # Ensemble summary
    if ensemble.total_count > 0:
        print(f"\n  WEIGHTED ENSEMBLE ({ensemble.total_count} raw → {len(ensemble.weighted_members)} weighted members)")
        print(f"  ├─ Mean: {ensemble.mean:.1f}°F  ±{ensemble.std:.1f}°  (Median: {ensemble.median:.1f}°F)")
        print(f"  ├─ Range: {ensemble.min_val:.0f}°F → {ensemble.max_val:.0f}°F")
        print(f"  ├─ P10={ensemble.p10:.0f}  P25={ensemble.p25:.0f}  P50={ensemble.p50:.0f}  P75={ensemble.p75:.0f}  P90={ensemble.p90:.0f}")
        bimodal_tag = " ⚠ BIMODAL — adaptive BW" if ensemble.is_bimodal else " (Silverman)"
        print(f"  └─ KDE bandwidth: {ensemble.kde_bandwidth:.2f}°F{bimodal_tag}")

    print_model_breakdown(ensemble)

    # NWS
    if nws.forecast_high > 0:
        print(f"\n  NWS POINT FORECAST")
        print(f"  ├─ Forecast High: {nws.forecast_high:.0f}°F")
        adj = []
        if nws.wind_penalty > 0:
            adj.append(f"wind -{nws.wind_penalty:.0f}°F [gusts {nws.peak_wind_gust:.0f}mph]")
        if nws.wet_bulb_penalty > 0:
            adj.append(f"wetbulb -{nws.wet_bulb_penalty:.1f}°F [daytime precip {nws.peak_precip_prob}%]")
        physics_suffix = f"  ({', '.join(adj)})" if adj else ""
        print(f"  ├─ Physics High:  {nws.physics_high:.1f}°F{physics_suffix}")
        if nws.current_temp > 0:
            print(f"  ├─ Current Temp: {nws.current_temp}°F  (Trend: {nws.temp_trend or 'N/A'})")
        midnight_suffix = f"  (12AM={nws.midnight_temp:.0f}°F vs 3PM={nws.afternoon_temp:.0f}°F)" if nws.midnight_temp else ""
        midnight_label = "YES ⚠" if nws.is_midnight_high else "No"
        print(f"  ├─ Midnight High: {midnight_label}{midnight_suffix}")
        if ensemble.total_count > 0:
            div = nws.forecast_high - ensemble.mean
            print(f"  └─ NWS vs Ensemble: {div:+.1f}°F {'⚠ DIVERGENT' if abs(div) > 2 else '✓ aligned'}")

    # HRRR / NBM deterministic models
    if hrrr_nbm and (hrrr_nbm.hrrr_high > 0 or hrrr_nbm.nbm_high > 0):
        print(f"\n  HIGH-RES DETERMINISTIC")
        if hrrr_nbm.hrrr_high > 0:
            hrrr_div = hrrr_nbm.hrrr_high - ensemble.mean if ensemble.mean > 0 else 0
            print(f"  ├─ HRRR (3km):  {hrrr_nbm.hrrr_high:.1f}°F  (Δ{hrrr_div:+.1f} vs ensemble)")
        if hrrr_nbm.nbm_high > 0:
            nbm_div = hrrr_nbm.nbm_high - ensemble.mean if ensemble.mean > 0 else 0
            nbm_nws_div = hrrr_nbm.nbm_high - nws.forecast_high if nws.forecast_high > 0 else 0
            print(f"  └─ NBM (2.5km): {hrrr_nbm.nbm_high:.1f}°F  (Δ{nbm_div:+.1f} vs ensemble, Δ{nbm_nws_div:+.1f} vs NWS)")

    # Confidence breakdown
    if conf_reasons:
        print(f"\n  CONFIDENCE FACTORS")
        for r in conf_reasons:
            print(f"  ├─ {r}")
        gate = "★ PASSES 90+ GATE — TRADEABLE" if conf_score >= MIN_CONFIDENCE_TO_TRADE else f"✗ Below {MIN_CONFIDENCE_TO_TRADE} gate — OBSERVE ONLY"
        print(f"  └─ {gate}")

    # Bot risk
    print(f"\n  ⚡ BOT RISK: {bot_risk}")

    # Order book intelligence
    if depth_map:
        tmrw_depths = [d for t, d in depth_map.items() if d.bid_depth + d.ask_depth > 0]
        if tmrw_depths:
            total_contracts = sum(d.bid_depth + d.ask_depth for d in tmrw_depths)
            most_liquid = max(tmrw_depths, key=lambda d: d.bid_depth + d.ask_depth)
            least_liquid = min(tmrw_depths, key=lambda d: d.bid_depth + d.ask_depth)
            ml_total = most_liquid.bid_depth + most_liquid.ask_depth
            ll_total = least_liquid.bid_depth + least_liquid.ask_depth
            ml_short = shorten_bracket_title(_ticker_to_title(most_liquid.ticker, brackets))
            ll_short = shorten_bracket_title(_ticker_to_title(least_liquid.ticker, brackets))

            print(f"\n  ORDER BOOK INTELLIGENCE")
            print(f"  ├─ Total market depth: {total_contracts:,} contracts across {len(tmrw_depths)} brackets")
            print(f"  ├─ Most liquid:  {ml_short} ({ml_total:,} contracts, spread {most_liquid.spread}¢, grade {most_liquid.grade})")
            print(f"  ├─ Least liquid: {ll_short} ({ll_total:,} contracts, spread {least_liquid.spread}¢, grade {least_liquid.grade})")

            # Find strongest imbalance signal
            imb_sorted = sorted(tmrw_depths, key=lambda d: abs(d.imbalance), reverse=True)
            top_imb = imb_sorted[0]
            if abs(top_imb.imbalance) > 0.2:
                imb_title = shorten_bracket_title(_ticker_to_title(top_imb.ticker, brackets))
                if top_imb.imbalance > 0:
                    print(f"  ├─ Imbalance signal: {imb_title} shows {top_imb.imbalance:+.2f} bid stacking → buyers loading YES")
                else:
                    print(f"  ├─ Imbalance signal: {imb_title} shows {top_imb.imbalance:+.2f} ask stacking → sellers loading NO")
            else:
                print(f"  ├─ Imbalance signal: balanced across all brackets (max |imb| = {abs(top_imb.imbalance):.2f})")

            # Fair value gaps: find brackets with very thin ask side
            thin_asks = [d for d in tmrw_depths if d.ask_depth < 50 and d.bid_depth > 100]
            thin_bids = [d for d in tmrw_depths if d.bid_depth < 50 and d.ask_depth > 100]
            if thin_asks:
                ta = thin_asks[0]
                ta_title = shorten_bracket_title(_ticker_to_title(ta.ticker, brackets))
                print(f"  └─ Fair value gap: {ta_title} thin ask side ({ta.ask_depth} contracts) — slippage risk on YES entry")
            elif thin_bids:
                tb = thin_bids[0]
                tb_title = shorten_bracket_title(_ticker_to_title(tb.ticker, brackets))
                print(f"  └─ Fair value gap: {tb_title} thin bid side ({tb.bid_depth} contracts) — slippage risk on exit")
            else:
                print(f"  └─ No significant fair value gaps detected")

    # Brackets with KDE probabilities
    if depth_map is None:
        depth_map = {}
    tomorrow_brackets = [m for m in brackets if is_tomorrow_ticker(m.get("ticker", ""), tomorrow)]
    if tomorrow_brackets:
        bracket_sum = sum(get_bid(m) for m in tomorrow_brackets)
        has_depth = bool(depth_map)
        print(f"\n  BRACKETS ({len(tomorrow_brackets)} markets, Σbid={bracket_sum}¢)")
        if has_depth:
            print(f"  {'Bracket':<16} {'Bid':>5} {'Ask':>5} {'KDE':>6} {'Hist':>5} {'Edge':>8} {'Vol':>8} {'BidDp':>6} {'AskDp':>6} {'Imb':>5} {'Liq':>3}")
            print(f"  {'─'*16} {'─'*5} {'─'*5} {'─'*6} {'─'*5} {'─'*8} {'─'*8} {'─'*6} {'─'*6} {'─'*5} {'─'*3}")
        else:
            print(f"  {'Bracket':<16} {'Bid':>5} {'Ask':>5} {'KDE':>6} {'Hist':>5} {'Edge':>8} {'Vol':>8}")
            print(f"  {'─'*16} {'─'*5} {'─'*5} {'─'*6} {'─'*5} {'─'*8} {'─'*8}")

        for mkt in sorted(tomorrow_brackets, key=lambda x: x.get("title", "")):
            title = mkt.get("title", "") or mkt.get("subtitle", "")
            bid = get_bid(mkt)
            ask = mkt.get("yes_ask", 0)
            vol = mkt.get("volume", 0)
            ticker = mkt.get("ticker", "")

            low, high, _ = parse_bracket_range(title)
            kde_p = kde_probability(ensemble.weighted_members, low, high, ensemble.kde_bandwidth, weights=ensemble.member_weights) * 100 if ensemble.weighted_members else 0
            if ensemble.weighted_members and ensemble.member_weights:
                w_total = sum(ensemble.member_weights)
                hist_p = (sum(w for t, w in zip(ensemble.weighted_members, ensemble.member_weights) if low <= t < high) / w_total * 100) if w_total > 0 else 0
            elif ensemble.weighted_members:
                hist_p = (sum(1 for t in ensemble.weighted_members if low <= t < high) / len(ensemble.weighted_members) * 100)
            else:
                hist_p = 0

            edge = kde_p - bid
            marker = " ⚡" if abs(edge) > 10 else " ●" if abs(edge) > 5 else ""

            # Shorten title for display
            short = shorten_bracket_title(title)
            if has_depth and ticker in depth_map:
                d = depth_map[ticker]
                imb_str = f"{d.imbalance:+.2f}" if d.bid_depth + d.ask_depth > 0 else "  —"
                print(f"  {short:<16} {bid:>4}¢ {ask:>4}¢ {kde_p:>5.1f}% {hist_p:>4.0f}% {edge:>+7.1f}¢{marker} {vol:>7,} {d.bid_depth:>6,} {d.ask_depth:>6,} {imb_str:>5} {d.grade:>3}")
            else:
                print(f"  {short:<16} {bid:>4}¢ {ask:>4}¢ {kde_p:>5.1f}% {hist_p:>4.0f}% {edge:>+7.1f}¢{marker} {vol:>7,}")

    # Opportunities
    if opps:
        print(f"\n  {'─'*68}")
        print(f"  OPPORTUNITIES ({len(opps)} found)")
        print(f"  {'─'*68}")

        for i, opp in enumerate(opps, 1):
            side_label = "YES" if opp.side == "yes" else "NO"
            price = opp.yes_bid if opp.side == "yes" else (100 - opp.yes_ask)
            short = shorten_bracket_title(opp.bracket_title)
            if TRADE_SCORE_ENABLED and opp.trade_score > 0:
                tradeable = opp.trade_score_components.get("tradeable", False)
            else:
                tradeable = opp.confidence_score >= MIN_CONFIDENCE_TO_TRADE
            gate_icon = "★" if tradeable else "○"

            print(f"\n  {gate_icon} [{i}] {side_label} {short} @ {price}¢ {'— TRADEABLE' if tradeable else '— observe only'}")
            print(f"      Ticker:     {opp.ticker}")
            print(f"      KDE Prob:   {opp.kde_prob*100:.1f}%  (Hist: {opp.histogram_prob*100:.1f}%)")
            print(f"      Edge:       {opp.edge_raw*100:+.1f}¢ raw → {opp.edge_after_fees*100:+.1f}¢ net")
            print(f"      Kelly:      {opp.kelly*100:.1f}% → {opp.suggested_contracts} contracts")
            print(f"      Confidence: {opp.confidence} ({opp.confidence_score:.0f}/100)")
            if opp.trade_score > 0:
                ts_label = "TRADEABLE" if tradeable else "observe"
                print(f"      TradeScore: {opp.trade_score:.3f} ({ts_label})")
            if opp.strategies:
                print(f"      Strategies: {', '.join(opp.strategies)}")
            if depth_map and opp.ticker in depth_map:
                d = depth_map[opp.ticker]
                dt = d.bid_depth + d.ask_depth
                if dt > 0:
                    print(f"      Depth:      Bid={d.bid_depth:,} Ask={d.ask_depth:,} (Imb: {d.imbalance:+.2f}, Grade: {d.grade})")
                    if d.bid_wall > 0 or d.ask_wall > 0:
                        print(f"      Walls:      Bid wall={d.bid_wall:,}  Ask wall={d.ask_wall:,}")
            print(f"      Rationale:  {opp.rationale}")
    else:
        print(f"\n  No opportunities above threshold.")


def print_summary_v2(all_opps: list[Opportunity], balance: float):
    print(f"\n{'='*72}")
    print(f"  SCAN SUMMARY v2.0 — RISK MANAGEMENT ACTIVE")
    print(f"{'='*72}")
    print(f"  Balance:       ${balance:.2f}")
    print(f"  Opportunities: {len(all_opps)}")
    if TRADE_SCORE_ENABLED:
        tradeable = [o for o in all_opps if o.trade_score_components.get("tradeable", False)]
        print(f"  Tradeable:     {len(tradeable)} (trade_score ≥ {TRADE_SCORE_THRESHOLD})")
    else:
        tradeable = [o for o in all_opps if o.confidence_score >= MIN_CONFIDENCE_TO_TRADE]
        print(f"  Tradeable:     {len(tradeable)} (conf ≥ {MIN_CONFIDENCE_TO_TRADE})")
    print(f"  Min Edge:      {MIN_EDGE_THRESHOLD*100:.0f}%  |  Min KDE: {MIN_KDE_PROBABILITY*100:.0f}%  |  Max Entry: {MAX_ENTRY_PRICE}¢")

    if not all_opps:
        print(f"\n  No opportunities above threshold. Check again at next model run.")
        return

    if TRADE_SCORE_ENABLED:
        ranked = sorted(all_opps, key=lambda x: x.trade_score, reverse=True)
    else:
        ranked = sorted(all_opps, key=lambda x: (x.confidence_score >= MIN_CONFIDENCE_TO_TRADE, x.edge_after_fees), reverse=True)

    print(f"\n  {'#':<3} {'':>1} {'City':<5} {'Side':<4} {'Bracket':<16} {'Price':>5} {'KDE':>5} {'Edge':>8} {'Conf':>5} {'TS':>5}")
    print(f"  {'─'*3} {'─':>1} {'─'*5} {'─'*4} {'─'*16} {'─'*5} {'─'*5} {'─'*8} {'─'*5} {'─'*5}")

    for i, opp in enumerate(ranked[:10], 1):
        price = opp.yes_bid if opp.side == "yes" else (100 - opp.yes_ask)
        short = shorten_bracket_title(opp.bracket_title)
        if TRADE_SCORE_ENABLED:
            gate = "★" if opp.trade_score_components.get("tradeable", False) else " "
        else:
            gate = "★" if opp.confidence_score >= MIN_CONFIDENCE_TO_TRADE else " "
        ts_str = f"{opp.trade_score:.2f}" if opp.trade_score > 0 else "  —"
        print(f"  {i:<3} {gate:>1} {opp.city:<5} {opp.side.upper():<4} {short:<16} {price:>4}¢ {opp.kde_prob*100:>4.0f}% {opp.edge_after_fees*100:>+7.1f}¢ {opp.confidence_score:>4.0f} {ts_str:>5}")

    if tradeable:
        if TRADE_SCORE_ENABLED:
            print(f"\n  ★ = TRADEABLE (trade_score ≥ {TRADE_SCORE_THRESHOLD})")
        else:
            print(f"\n  ★ = TRADEABLE (confidence ≥ {MIN_CONFIDENCE_TO_TRADE})")
    else:
        if TRADE_SCORE_ENABLED:
            print(f"\n  No opportunities meet the trade score threshold ({TRADE_SCORE_THRESHOLD}).")
        else:
            print(f"\n  No opportunities meet the {MIN_CONFIDENCE_TO_TRADE}+ confidence gate.")
        print(f"  This is NORMAL — high-confidence setups appear 2-5 times per week.")


def _save_snapshot(city_key: str, target_date, ensemble: EnsembleV2, nws: NWSData, brackets: list, opps: list):
    """Save ensemble + market snapshot for backtest calibration."""
    try:
        from backtest_collector import save_ensemble_snapshot
        snapshot = {
            "mean": ensemble.mean,
            "std": ensemble.std,
            "total_count": ensemble.total_count,
            "kde_bandwidth": ensemble.kde_bandwidth,
            "is_bimodal": ensemble.is_bimodal,
            "p10": ensemble.p10,
            "p25": ensemble.p25,
            "p50": ensemble.p50,
            "p75": ensemble.p75,
            "p90": ensemble.p90,
            "per_model_means": {m.name: round(m.mean, 2) for m in ensemble.models},
            "per_model_stds": {m.name: round(m.std, 2) for m in ensemble.models},
            "per_model_counts": {m.name: len(m.members) for m in ensemble.models},
            "nws_forecast_high": nws.forecast_high,
            "nws_physics_high": nws.physics_high,
            "nws_current_temp": nws.current_temp,
            "nws_wind_penalty": nws.wind_penalty,
            "nws_wet_bulb_penalty": nws.wet_bulb_penalty,
            "nws_temp_trend": nws.temp_trend,
            "bracket_prices": {
                m.get("ticker", ""): {"yes_bid": m.get("yes_bid", 0), "yes_ask": m.get("yes_ask", 0)}
                for m in brackets
            },
            "opportunities": [
                {
                    "ticker": o.ticker, "side": o.side,
                    "kde_prob": round(o.kde_prob, 4),
                    "confidence_score": round(o.confidence_score, 1),
                    "edge": round(o.edge_after_fees, 4),
                    "trade_score": round(o.trade_score, 4),
                    "trade_score_components": o.trade_score_components,
                }
                for o in opps
            ],
            "trade_score_threshold": TRADE_SCORE_THRESHOLD,
        }
        save_ensemble_snapshot(city_key, datetime.combine(target_date, datetime.min.time()), snapshot)
    except Exception as e:
        logger.debug("Snapshot save skipped: %s", e)


# ─── Main ──────────────────────────────────────────────

async def scan(city_filter: str = None, show_timing: bool = False):
    now = datetime.now()
    print(f"\n{'#'*72}")
    print(f"  EDGE SCANNER v2.0 — FRONTIER AI MODELS")
    print(f"  {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"  Models: AIFS(AI) + IFS + GFS + ICON + GEM = ~194 members")
    print(f"  Method: Gaussian KDE + Model Weighting + Bot Protection")
    print(f"{'#'*72}")

    if show_timing:
        for ck in CITIES:
            window, risk = get_entry_timing(ck)
            print(f"\n  {ck}: Window={window}")
            print(f"        Risk={risk}")
        return

    cities_to_scan = {city_filter.upper(): CITIES[city_filter.upper()]} if city_filter else CITIES

    # ── Startup validation ──
    import os
    startup_warnings = []
    key_id = os.environ.get("KALSHI_API_KEY_ID", "")
    key_path_str = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
    if not key_id:
        startup_warnings.append("KALSHI_API_KEY_ID not set in .env — trade execution will fail")
    if not key_path_str:
        startup_warnings.append("KALSHI_PRIVATE_KEY_PATH not set in .env — trade execution will fail")
    elif not Path(key_path_str).exists():
        startup_warnings.append(f"KALSHI_PRIVATE_KEY_PATH file not found: {key_path_str}")
    discord_url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not discord_url:
        startup_warnings.append("DISCORD_WEBHOOK_URL not set — alerts will fall back to file")

    if startup_warnings:
        print(f"\n  ⚠ STARTUP WARNINGS:")
        for sw in startup_warnings:
            print(f"    └─ {sw}")
            logger.warning("Startup: %s", sw)

    # Balance
    balance = 0.0
    try:
        from kalshi_client import fetch_balance_quick
        balance = await fetch_balance_quick()
        if balance > 0:
            print(f"\n  Account Balance: ${balance:.2f}")
    except Exception as e:
        logger.warning("Balance fetch: %s", e)

    # Compute existing exposure per city from open positions
    existing_exposure = {}
    try:
        from position_store import load_positions
        import re as _re
        open_pos = [p for p in load_positions() if p.get("status") in ("open", "resting", "pending_sell")]
        # Map series tickers back to city codes
        series_to_city = {cfg["series"]: code for code, cfg in CITIES.items()}
        for p in open_pos:
            ticker = p.get("ticker", "")
            match = _re.match(r'^([A-Z]+)', ticker)
            if match:
                city_code = series_to_city.get(match.group(1))
                if city_code:
                    cost = p.get("contracts", 0) * p.get("avg_price", 0) / 100
                    existing_exposure[city_code] = existing_exposure.get(city_code, 0) + cost
        if existing_exposure:
            print(f"  Existing exposure: {', '.join(f'{c}=${v:.2f}' for c, v in existing_exposure.items())}")
    except Exception as e:
        logger.debug("Could not load positions for exposure check: %s", e)

    tz = ZoneInfo("America/New_York")
    tomorrow = (datetime.now(tz) + timedelta(days=1)).date()
    target_date_str = tomorrow.isoformat()
    print(f"  Target: {tomorrow.strftime('%A %B %d, %Y')}")
    print(f"  Cities: {', '.join(cities_to_scan.keys())}")

    all_opps = []
    failed_cities = []

    async with aiohttp.ClientSession() as session:
        for city_key in cities_to_scan:
            print(f"\n  Scanning {city_key}...")

            try:
                ens_task = fetch_ensemble_v2(session, city_key, target_date_str)
                nws_task = fetch_nws(session, city_key, tomorrow)
                mkt_task = fetch_kalshi_brackets(session, city_key)
                hrrr_task = fetch_hrrr_nbm(session, city_key, target_date_str)

                results = await asyncio.gather(ens_task, nws_task, mkt_task, hrrr_task, return_exceptions=True)

                # Check for exceptions in any fetch — each result must be typed correctly
                fetch_labels = ["ensemble", "NWS", "brackets", "HRRR/NBM"]
                error_msgs = []
                for i, r in enumerate(results):
                    if isinstance(r, BaseException):
                        # HRRR/NBM failure is non-critical — log but don't block
                        if fetch_labels[i] == "HRRR/NBM":
                            logger.debug("HRRR/NBM fetch failed for %s (non-critical): %s", city_key, r)
                            results[i] = HRRRNBMData()  # Fallback to empty
                        else:
                            error_msgs.append(f"{fetch_labels[i]}: {type(r).__name__}: {r}")
                if error_msgs:
                    raise RuntimeError(f"Fetch errors: {'; '.join(error_msgs)}")

                ensemble, nws_data, brackets, hrrr_nbm = results

                # Defensive: verify results are correct types (not exceptions that slipped through)
                if isinstance(ensemble, BaseException) or isinstance(nws_data, BaseException) or isinstance(brackets, BaseException):
                    raise RuntimeError("Unexpected exception in fetch results after type check")
                if isinstance(hrrr_nbm, BaseException):
                    hrrr_nbm = HRRRNBMData()  # Graceful fallback

                # ── Data source health checks ──
                data_warnings = []
                if ensemble.total_count == 0:
                    data_warnings.append("ENSEMBLE: 0 members (Open-Meteo may be down)")
                elif ensemble.total_count < 100:
                    data_warnings.append(f"ENSEMBLE: only {ensemble.total_count} members (expected ~230)")
                if hrrr_nbm.hrrr_high <= 0 and hrrr_nbm.nbm_high <= 0:
                    data_warnings.append("HRRR/NBM: no data (non-critical, Open-Meteo forecast API may be down)")
                if nws_data.forecast_high <= 0:
                    data_warnings.append("NWS: no forecast high (NWS API may be down)")
                if nws_data.current_temp <= 0:
                    data_warnings.append("NWS: no current observation (trend detection disabled)")
                if not brackets:
                    data_warnings.append("KALSHI: no brackets returned (API may be down or no markets)")

                if data_warnings:
                    print(f"    ⚠ DATA WARNINGS for {city_key}:")
                    for w in data_warnings:
                        print(f"      └─ {w}")
                        logger.warning("Data health: %s — %s", city_key, w)

                # If critical data is missing, skip city entirely
                if ensemble.total_count == 0 or not brackets:
                    failed_cities.append(city_key)
                    reason = "no ensemble data" if ensemble.total_count == 0 else "no market brackets"
                    print(f"    ✗ {city_key} SKIPPED — {reason}")
                    continue

                print(f"    Ensemble: {ensemble.total_count} raw members → {len(ensemble.weighted_members)} weighted")
                bimodal_flag = " [BIMODAL]" if ensemble.is_bimodal else ""
                print(f"    Mean: {ensemble.mean:.1f}°F ±{ensemble.std:.1f}  KDE bw: {ensemble.kde_bandwidth:.2f}{bimodal_flag}")
                hrrr_str = f"HRRR: {hrrr_nbm.hrrr_high:.1f}°F" if hrrr_nbm.hrrr_high > 0 else "HRRR: N/A"
                nbm_str = f"NBM: {hrrr_nbm.nbm_high:.1f}°F" if hrrr_nbm.nbm_high > 0 else "NBM: N/A"
                print(f"    NWS: {nws_data.forecast_high:.0f}°F  Physics: {nws_data.physics_high:.1f}°F  {hrrr_str}  {nbm_str}")

                # ── Fetch order book depth for tomorrow's brackets ──
                tz_city = ZoneInfo(CITIES[city_key]["tz"])
                tomorrow_date = (datetime.now(tz_city) + timedelta(days=1)).date()
                tmrw_tickers = [
                    m.get("ticker", "") for m in brackets
                    if is_tomorrow_ticker(m.get("ticker", ""), tomorrow_date)
                ]
                depth_map: dict[str, OrderBookDepth] = {}
                if tmrw_tickers:
                    try:
                        depth_map = await fetch_orderbook_depth(session, tmrw_tickers)
                        grade_counts = {}
                        for d in depth_map.values():
                            grade_counts[d.grade] = grade_counts.get(d.grade, 0) + 1
                        grade_str = " ".join(f"{g}:{n}" for g, n in sorted(grade_counts.items()))
                        total_depth = sum(d.bid_depth + d.ask_depth for d in depth_map.values())
                        print(f"    Depth: {len(depth_map)} books fetched, {total_depth:,} contracts ({grade_str})")
                    except Exception as e:
                        logger.debug("Orderbook depth fetch failed for %s (non-critical): %s", city_key, e)

                opps = analyze_opportunities_v2(city_key, ensemble, nws_data, brackets, balance, existing_exposure, hrrr_nbm=hrrr_nbm, depth_map=depth_map)

                # ── LLM Confidence Blend (if enabled) ──
                llm_module = _get_llm_module()
                if llm_module and llm_module.enabled and opps:
                    for opp in opps:
                        try:
                            context = {
                                "city": city_key,
                                "bracket": f"{opp.low}-{opp.high}",
                                "ensemble_mean": ensemble.mean,
                                "ensemble_std": ensemble.std,
                                "ensemble_count": ensemble.total_count,
                                "nws_high": nws_data.forecast_high,
                                "physics_high": nws_data.physics_high,
                                "current_temp": nws_data.current_temp,
                                "kde_prob": opp.kde_prob * 100,
                                "stat_confidence": opp.confidence_score,
                                "market_price": opp.yes_bid if opp.side == "yes" else (100 - opp.yes_ask),
                                "strategies": opp.strategies,
                                "trend": nws_data.temp_trend or "unknown",
                            }
                            llm_result = await llm_module.get_consensus(context)
                            stat_score = opp.confidence_score
                            blended = llm_module.blend_scores(stat_score, llm_result)
                            if llm_result.valid:
                                print(f"    LLM: {llm_result.confidence}/100 ({llm_result.direction}) → blended {stat_score:.0f} → {blended:.0f}")
                                opp.confidence_score = blended
                                # Re-classify
                                if blended >= 90:
                                    opp.confidence = "ELITE"
                                elif blended >= 75:
                                    opp.confidence = "HIGH"
                                elif blended >= 55:
                                    opp.confidence = "MEDIUM"
                                else:
                                    opp.confidence = "LOW"
                        except Exception as e:
                            logger.warning("LLM blend failed for %s: %s", opp.ticker, e)

                all_opps.extend(opps)

                # Save ensemble snapshot for backtest calibration
                _save_snapshot(city_key, tomorrow, ensemble, nws_data, brackets, opps)

                print_city_report_v2(city_key, ensemble, nws_data, brackets, opps, hrrr_nbm=hrrr_nbm, depth_map=depth_map)

            except Exception as e:
                failed_cities.append(city_key)
                print(f"    ✗ {city_key} FAILED — {e}")
                print(f"    Skipping to next city...")
                continue

    if failed_cities:
        n_ok = len(cities_to_scan) - len(failed_cities)
        print(f"\n  ⚠ FAILED CITIES: {', '.join(failed_cities)} ({n_ok}/{len(cities_to_scan)} scanned)")
        if n_ok == 0:
            print(f"  ✗ ALL CITIES FAILED — check internet connection and API status")
            logger.error("ALL cities failed scan — possible API outage or network issue")
        elif len(failed_cities) >= len(cities_to_scan) // 2:
            print(f"  ⚠ MAJORITY FAILED — results may be unreliable, check data sources")
            logger.warning("Majority of cities failed: %s", failed_cities)

    print_summary_v2(all_opps, balance)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Edge Scanner v2.0 — Frontier AI Model Weather Arbitrage")
    parser.add_argument("--city", type=str, default=None, help="City code (NYC, CHI)")
    parser.add_argument("--timing", action="store_true", help="Show optimal entry windows only")
    args = parser.parse_args()
    asyncio.run(scan(args.city, args.timing))
