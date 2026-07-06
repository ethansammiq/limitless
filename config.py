#!/usr/bin/env python3
"""
WEATHER EDGE — Configuration Constants

Station configs for the original 5 cities (peak_monitor / backtest_collector /
dashboard temp+price pollers) plus API client tuning. The full 40-ladder
settlement-station registry lives in ladders.json — this file is NOT it.
(The KDE trading-parameter surface died with the stack, 2026-07-06.)
"""

from dataclasses import dataclass, field
from typing import Dict, List

# =============================================================================
# CITY STATION CONFIGURATION
# =============================================================================

@dataclass
class StationConfig:
    """Configuration for a single weather station/city."""
    city_code: str              # Short code (NYC, CHI)
    city_name: str              # Full name for display
    station_id: str             # NWS station ID (KNYC, KMDW)
    series_ticker: str          # Kalshi market series (KXHIGHNY, KXHIGHCHI)
    lat: float                  # Latitude for Open-Meteo ensemble API
    lon: float                  # Longitude for Open-Meteo ensemble API
    nws_station_url: str        # NWS station metadata URL
    nws_observation_url: str    # NWS current observation URL
    nws_hourly_forecast_url: str  # NWS hourly forecast URL
    nws_gridpoint: str          # Gridpoint identifier for fallback
    mos_mav_url: str            # GFS MOS (MAV) URL
    mos_met_url: str            # NAM MOS (MET) URL
    timezone: str               # IANA timezone
    iem_station: str = ""         # IEM station ID (NYC, MDW — no K prefix)
    iem_network: str = ""         # IEM network (NY_ASOS, IL_ASOS)
    dsm_times_z: List[str] = field(default_factory=list)   # DSM release times (Zulu)
    six_hour_z: List[str] = field(default_factory=lambda: ["23:51", "05:51", "11:51", "17:51"])


def _nws_urls(station_id: str) -> tuple:
    """Generate NWS station and observation URLs from a station ID."""
    base = "https://api.weather.gov/stations"
    return (f"{base}/{station_id}", f"{base}/{station_id}/observations/latest")


def _mos_urls(station_id: str) -> tuple:
    """Generate MOS MAV/MET URLs from a station ID."""
    sid = station_id.lower()
    base = "https://tgftp.nws.noaa.gov/data/forecasts/mos"
    return (f"{base}/gfs/short/mav/{sid}.txt", f"{base}/nam/short/met/{sid}.txt")


# Station configurations for all 5 supported cities
STATIONS: Dict[str, StationConfig] = {
    "NYC": StationConfig(
        city_code="NYC",
        city_name="New York (Central Park)",
        station_id="KNYC",
        series_ticker="KXHIGHNY",
        lat=40.78, lon=-73.97,
        nws_station_url=_nws_urls("KNYC")[0],
        nws_observation_url=_nws_urls("KNYC")[1],
        nws_hourly_forecast_url="https://api.weather.gov/gridpoints/OKX/33,37/forecast/hourly",
        nws_gridpoint="OKX/33,37",
        mos_mav_url=_mos_urls("KNYC")[0],
        mos_met_url=_mos_urls("KNYC")[1],
        iem_station="NYC", iem_network="NY_ASOS",
        timezone="America/New_York",
        dsm_times_z=["20:21", "21:21", "05:17"],
    ),
    "CHI": StationConfig(
        city_code="CHI",
        city_name="Chicago (Midway)",
        station_id="KMDW",
        series_ticker="KXHIGHCHI",
        lat=41.79, lon=-87.75,
        nws_station_url=_nws_urls("KMDW")[0],
        nws_observation_url=_nws_urls("KMDW")[1],
        nws_hourly_forecast_url="https://api.weather.gov/gridpoints/LOT/75,72/forecast/hourly",
        nws_gridpoint="LOT/75,72",
        mos_mav_url=_mos_urls("KMDW")[0],
        mos_met_url=_mos_urls("KMDW")[1],
        iem_station="MDW", iem_network="IL_ASOS",
        timezone="America/Chicago",
        dsm_times_z=["21:00", "22:00", "06:00"],
    ),
    "DEN": StationConfig(
        city_code="DEN",
        city_name="Denver (DIA)",
        station_id="KDEN",
        series_ticker="KXHIGHDEN",
        lat=39.86, lon=-104.67,
        nws_station_url=_nws_urls("KDEN")[0],
        nws_observation_url=_nws_urls("KDEN")[1],
        nws_hourly_forecast_url="https://api.weather.gov/gridpoints/BOU/63,62/forecast/hourly",
        nws_gridpoint="BOU/63,62",
        mos_mav_url=_mos_urls("KDEN")[0],
        mos_met_url=_mos_urls("KDEN")[1],
        iem_station="DEN", iem_network="CO_ASOS",
        timezone="America/Denver",
        dsm_times_z=["22:00", "23:00", "07:00"],
    ),
    "MIA": StationConfig(
        city_code="MIA",
        city_name="Miami (MIA Airport)",
        station_id="KMIA",
        series_ticker="KXHIGHMIA",
        lat=25.79, lon=-80.29,
        nws_station_url=_nws_urls("KMIA")[0],
        nws_observation_url=_nws_urls("KMIA")[1],
        nws_hourly_forecast_url="https://api.weather.gov/gridpoints/MFL/76,50/forecast/hourly",
        nws_gridpoint="MFL/76,50",
        mos_mav_url=_mos_urls("KMIA")[0],
        mos_met_url=_mos_urls("KMIA")[1],
        iem_station="MIA", iem_network="FL_ASOS",
        timezone="America/New_York",
        dsm_times_z=["20:30", "21:30", "05:30"],
    ),
    "LAX": StationConfig(
        city_code="LAX",
        city_name="Los Angeles (LAX)",
        station_id="KLAX",
        series_ticker="KXHIGHLAX",
        lat=33.94, lon=-118.41,
        nws_station_url=_nws_urls("KLAX")[0],
        nws_observation_url=_nws_urls("KLAX")[1],
        nws_hourly_forecast_url="https://api.weather.gov/gridpoints/LOX/150,44/forecast/hourly",
        nws_gridpoint="LOX/150,44",
        mos_mav_url=_mos_urls("KLAX")[0],
        mos_met_url=_mos_urls("KLAX")[1],
        iem_station="LAX", iem_network="CA_ASOS",
        timezone="America/Los_Angeles",
        dsm_times_z=["23:00", "00:00", "08:00"],
    ),
}

# =============================================================================
# SETTLEMENT
# =============================================================================

SETTLEMENT_HOUR_ET = 7  # Markets settle ~7 AM ET

# =============================================================================
# KALSHI API ENDPOINTS
# =============================================================================

KALSHI_LIVE_URL = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_DEMO_URL = "https://demo-api.kalshi.co/trade-api/v2"

# =============================================================================
# PEAK MONITOR (post-peak lock-in detection)
# =============================================================================

PEAK_MIN_DECLINE_OBS = 3         # Consecutive declining obs to confirm peak
PEAK_MIN_DROP_F = 1.5            # Min °F below running max to confirm
PEAK_MIN_DECLINE_MINUTES = 45    # Min minutes since max was set
PEAK_EARLIEST_HOUR = 12          # Don't confirm before noon local
PEAK_LATEST_HOUR = 22            # Stop monitoring after 10 PM local
PEAK_POLL_INTERVAL_SEC = 300     # Seconds between polls in --watch mode

# =============================================================================
# API RATE LIMITING & RETRY (kalshi_client)
# =============================================================================

API_MIN_REQUEST_INTERVAL = 0.1  # 10 requests/sec max

API_RETRY_ATTEMPTS = 3
API_RETRY_MIN_WAIT_SEC = 1
API_RETRY_MAX_WAIT_SEC = 10
API_RETRY_MULTIPLIER = 2  # Exponential backoff multiplier

HTTP_TIMEOUT_TOTAL_SEC = 10
HTTP_TIMEOUT_CONNECT_SEC = 2

CONNECTION_POOL_LIMIT = 10
DNS_CACHE_TTL_SEC = 300
KEEPALIVE_TIMEOUT_SEC = 120

# Orderbook depth for price queries
ORDERBOOK_DEPTH = 10
