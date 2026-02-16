#!/usr/bin/env python3
"""
WEATHER EDGE v4.0 - Configuration Constants

Centralized configuration for all trading parameters, thresholds, and settings.
Single source of truth for city/station configs used by all modules.
"""

from dataclasses import dataclass, field
from pathlib import Path
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

# Default city if none specified
DEFAULT_CITY = "NYC"

# =============================================================================
# WATCHLIST — Cities to highlight in scans and Discord alerts
# =============================================================================
# When a watched city's best opportunity exceeds WATCHLIST_MIN_CONFIDENCE,
# the scanner sends an extra highlighted Discord embed and logs extra detail.
# Edit this set to track developing setups before they cross the 90+ gate.

WATCHLIST: set[str] = {"LAX"}
WATCHLIST_MIN_CONFIDENCE = 50  # Highlight if best opp confidence >= this


def get_station_config(city_code: str) -> StationConfig:
    """Get station configuration for a city code. Raises KeyError if not found."""
    city_upper = city_code.upper()
    if city_upper not in STATIONS:
        available = ", ".join(STATIONS.keys())
        raise KeyError(f"Unknown city code: {city_code}. Available: {available}")
    return STATIONS[city_upper]


# =============================================================================
# LEGACY CONSTANTS (for backward compatibility)
# These point to NYC by default. New code should use STATIONS dict.
# =============================================================================

_default_station = STATIONS[DEFAULT_CITY]

# NWS APIs (NYC defaults)
NWS_STATION_URL = _default_station.nws_station_url
NWS_OBSERVATION_URL = _default_station.nws_observation_url
NWS_HOURLY_FORECAST_URL = _default_station.nws_hourly_forecast_url
NWS_GRIDPOINT_FALLBACK = _default_station.nws_gridpoint

# MOS URLs (NYC defaults)
MOS_MAV_URL = _default_station.mos_mav_url
MOS_MET_URL = _default_station.mos_met_url

# Market identifier (NYC default)
NYC_HIGH_SERIES_TICKER = _default_station.series_ticker

# =============================================================================
# KALSHI API ENDPOINTS
# =============================================================================

KALSHI_LIVE_URL = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_DEMO_URL = "https://demo-api.kalshi.co/trade-api/v2"

# =============================================================================
# TRADING PARAMETERS (v2 — tighter risk controls)
# =============================================================================

# Maximum position size as percentage of Net Liquidation Value
MAX_POSITION_PCT = 0.10  # 10% per trade (conservative)

# Maximum daily exposure across all positions
MAX_DAILY_EXPOSURE = 0.25  # 25% of NLV

# Maximum correlated exposure (similar weather pattern cities)
MAX_CORRELATED_EXPOSURE = 0.15  # 15% across correlated cities

# Edge thresholds for trade recommendations
MIN_EDGE_THRESHOLD = 0.15  # 15% minimum edge after fees
MIN_KDE_PROBABILITY = 0.20  # 20% minimum model probability

# Confidence gate — ONLY trade at this level or above
MIN_CONFIDENCE_TO_TRADE = 90  # 90/100 confidence score required

# Maximum price to consider for entry (never buy YES above this)
# Above 50¢ on YES, risk/reward is worse than 1:1 on weather markets
MAX_ENTRY_PRICE_CENTS = 50

# ROI threshold for taking profit (sell half = freeroll)
FREEROLL_MULTIPLIER = 2.0  # Sell half when price doubles (100% ROI)

# Capital Efficiency threshold - sell when price exceeds this
# Above 90c, you risk 90 to make 10. Terrible risk/reward on weather.
CAPITAL_EFFICIENCY_THRESHOLD_CENTS = 90

# Mid-range profit take — sell 50% of remaining after freeroll
# At 65¢ on a $0 cost basis, you capture 76% of max payout on that tranche
# while letting the final 25% ride to 90¢+ or settlement.
MID_PROFIT_THRESHOLD_CENTS = 65
MID_PROFIT_SELL_FRACTION = 0.50  # Sell half of remaining (25% of original)

# Trailing profit lock — after freeroll, protect gains
# Scaled by price zone (wider when cheap, tighter as price rises)
TRAILING_OFFSET_CENTS = 8  # Legacy default (used as fallback)
TRAILING_ZONES = [
    # (min_price, max_price, offset)
    # Tightened 2026-02-15: old offsets gave back too much (37c peak → 30c exit
    # on a 26c entry = captured only 4c of 11c gain). New offsets capture more.
    (0,  20, 10),   # Deep value — breathing room, but not excessive
    (20, 40,  7),   # Low-mid — where most weather trades live
    (40, 60,  6),   # Mid — thesis developing, moderate trail
    (60, 80,  5),   # High — protect gains actively
    (80, 100, 3),   # Near-certain — very tight, almost at efficiency exit
]

# Near-settlement override — hold for $1 if price > threshold and near settlement
SETTLEMENT_HOLD_THRESHOLD_CENTS = 80
SETTLEMENT_HOUR_ET = 7  # Markets settle ~7 AM ET
SETTLEMENT_WINDOW_HOURS = 2

# Stop-loss: thesis-based (primary) + ROI backstop (secondary)
# Primary: auto_trader re-scans and tags confidence; if conf < threshold, exit
THESIS_BREAK_CONFIDENCE = 40  # Exit if re-scan confidence drops below this
# Secondary: hard ROI backstop for edge cases where re-scan can't run
STOP_LOSS_ROI_PCT = -50  # Sell everything if ROI drops to -50%
STOP_LOSS_FLOOR_CENTS = 2  # Don't sell at 1-2¢ — too illiquid to exit

# =============================================================================
# EXIT STRATEGY UPGRADES — Institutional-grade enhancements
# =============================================================================

# ── Upgrade 1: Time-Decay Urgency Scaling ──
# Sigmoid-based decay tightens exits as settlement approaches.
# f(h) = 1 / (1 + exp(-steepness * (h - midpoint)))
# Each exit threshold *= lerp(min_factor, 1.0, f(h))
TIME_DECAY_ENABLED = True
TIME_DECAY_MIDPOINT_HOURS = 6.0       # f(6h) = 0.5
TIME_DECAY_STEEPNESS = 0.6            # Controls sigmoid slope
TIME_DECAY_TRAILING_MIN_FACTOR = 0.6  # Trailing offset → 60% at settlement
TIME_DECAY_FREEROLL_MIN_FACTOR = 0.75 # Freeroll multiplier → 75% at settlement
TIME_DECAY_MID_PROFIT_MIN_FACTOR = 0.80  # Mid-profit → 80% at settlement

# ── Upgrade 2: Observation-Aware Dynamic Trailing ──
# After time-decay, adjust trailing offset based on live obs vs bracket.
# Obs confirms thesis → widen (let it run). Obs diverges → tighten (protect).
OBS_TRAILING_ENABLED = True
OBS_TRAILING_WIDEN_FACTOR = 1.3       # +30% when obs confirms bracket
OBS_TRAILING_TIGHTEN_FACTOR = 0.6     # -40% when obs diverges
OBS_TRAILING_DIVERGENCE_F = 3.0       # °F outside bracket to trigger tightening

# ── Upgrade 3: Adaptive Freeroll Trigger ──
# Price-level-aware freeroll multiplier. Cheap entries get more room;
# expensive entries lock profit earlier.
ADAPTIVE_FREEROLL_ENABLED = True
ADAPTIVE_FREEROLL_TIERS = [
    # (min_entry, max_entry, multiplier)
    # Calibrated 2026-02-15: old 1.8x at 26c entry → 47c target was unreachable
    # (market peaked at 37c). New tiers produce reachable targets.
    (0,  10, 2.5),   # Very cheap (≤9c) → let it run, doubles easily
    (10, 20, 2.0),   # Cheap (10-19c) → standard double
    (20, 30, 1.5),   # Mid-price (20-29c) → 26c×1.5=39c (reachable near peak)
    (30, 40, 1.4),   # Upper-mid (30-39c) → lock in at +40%
    (40, 51, 1.3),   # Expensive (40-50c) → lock in fast at +30%
]

# ── Upgrade 3b: Quick-Profit Take (pre-freeroll partial exit) ──
# When price rises significantly but hasn't reached freeroll target,
# sell a portion to bank some gains before a reversal.
# Added 2026-02-15 after LAX trade peaked at 37c (entry 26c, +42% ROI)
# but no mechanism existed to take profit below the 47c freeroll target.
QUICK_PROFIT_ENABLED = True
QUICK_PROFIT_ROI_PCT = 35           # Trigger at +35% ROI (26c → 35c)
QUICK_PROFIT_SELL_FRACTION = 0.30   # Sell 30% of position
QUICK_PROFIT_MIN_CONTRACTS = 3      # Need at least 3 contracts to trigger

# ── Upgrade 4: Momentum / Velocity Exit ──
# Track price changes between monitor cycles (~5 min).
# Large drops pre-freeroll trigger alert + temporary floor.
MOMENTUM_EXIT_ENABLED = True
MOMENTUM_DROP_ALERT_CENTS = 15   # Alert if price drops >15¢ in one cycle
MOMENTUM_DROP_TIGHTEN_CENTS = 10 # Set floor at (current_price + 10¢)

# ── Upgrade 5: Graduated Thesis Deterioration ──
# Three zones instead of binary hold/dump:
#   conf >= 70: hold | 40 <= conf < 70: trim 50% | conf < 40: full exit
THESIS_TRIM_ENABLED = True
THESIS_TRIM_CONFIDENCE_HIGH = 70   # Below this → trim (not full exit)
THESIS_TRIM_SELL_FRACTION = 0.50   # Sell 50% on trim

# ── Upgrade 6: Pending Sell Repricing ──
# Smart repricing of stale limit sells before the 30-min expiry catches them.
SELL_REPRICE_ENABLED = True
SELL_REPRICE_MIN_CYCLES = 3        # Wait 3 cycles (~15 min) before repricing
SELL_REPRICE_BID_DRIFT_CENTS = 3   # Reprice if bid drifted >3¢ from sell price
SELL_REPRICE_MAX_PER_ORDER = 2     # Max reprices before 30-min expiry handles it

# =============================================================================
# HYBRID TRADE SCORE (replaces simple confidence >= 90 gate)
# =============================================================================

# Feature flag: when False, falls back to old MIN_CONFIDENCE_TO_TRADE gate
TRADE_SCORE_ENABLED = True

# Threshold for trade_score to fire (0.0 to 1.0)
# Initial 0.55 from numerical walkthrough; calibrate after 30-60 days of data
TRADE_SCORE_THRESHOLD = 0.55

# Hard floors (never relaxed, even if trade_score is high)
TRADE_SCORE_CONFIDENCE_FLOOR = 70   # Absolute minimum confidence (0-100)
TRADE_SCORE_MIN_EDGE_CENTS = 10     # Minimum edge after fees (cents)

# Entry price penalty — expensive entries have worse asymmetry
# At 5c entry: max payout 19:1. At 26c: max payout 3.8:1.
# Penalize entries >20c to prefer cheap, high-asymmetry trades.
TRADE_SCORE_ENTRY_PRICE_PENALTY_START = 20   # No penalty below this
TRADE_SCORE_ENTRY_PRICE_PENALTY_RATE = 0.004 # Per cent above start (e.g. 26c → 6*0.004 = -0.024)

# Liquidity thresholds
TRADE_SCORE_LOW_VOLUME = 500        # Below this: heavy penalty (-0.15)
TRADE_SCORE_MED_VOLUME = 1000       # Below this: light penalty (-0.08)
TRADE_SCORE_WIDE_SPREAD = 5         # Above this spread: penalty (-0.05)

# =============================================================================
# SMART PEGGING (Order Execution)
# =============================================================================

# Maximum spread to cross (if spread > this, peg Bid+1 instead of hitting Ask)
MAX_SPREAD_TO_CROSS_CENTS = 5

# When pegging, add this to the bid
PEG_OFFSET_CENTS = 1

# Minimum acceptable bid (don't place orders if bid is 0)
MIN_BID_CENTS = 1

# =============================================================================
# WEATHER STRATEGY PARAMETERS
# =============================================================================

# Strategy A: Midnight High detection hours
MIDNIGHT_HOUR_START = 0   # 12:00 AM
MIDNIGHT_HOUR_END = 1     # 1:00 AM
AFTERNOON_HOUR_START = 14 # 2:00 PM
AFTERNOON_HOUR_END = 16   # 4:00 PM

# Strategy B: Wind Mixing Penalty thresholds
WIND_PENALTY_LIGHT_THRESHOLD_MPH = 15   # Gusts > 15mph = -1.0F penalty
WIND_PENALTY_HEAVY_THRESHOLD_MPH = 25   # Gusts > 25mph = -2.0F penalty
WIND_PENALTY_LIGHT_DEGREES = 1.0
WIND_PENALTY_HEAVY_DEGREES = 2.0

# Gust estimation multiplier (when gusts not provided)
WIND_GUST_MULTIPLIER = 1.5
WIND_GUST_THRESHOLD_MPH = 10  # Only apply multiplier above this speed

# Strategy C: Rounding Arbitrage (implicit in temp_to_bracket)

# Strategy D: Wet Bulb / Evaporative Cooling
WET_BULB_PRECIP_THRESHOLD_PCT = 40      # Minimum precip probability to trigger
WET_BULB_DEPRESSION_MIN_F = 5           # Minimum temp-dewpoint spread to consider
WET_BULB_FACTOR_LIGHT = 0.25            # Cooling factor when precip 40-70%
WET_BULB_FACTOR_HEAVY = 0.40            # Cooling factor when precip >= 70%
WET_BULB_HEAVY_PRECIP_THRESHOLD = 70    # Precip % threshold for heavy factor

# Strategy E: MOS Consensus (Model vs Official)
MOS_DIVERGENCE_THRESHOLD_F = 2.0  # If NWS > MOS consensus by this much, fade NWS

# Strategy F: Post-Peak Lock-In (peak_monitor.py)
PEAK_MIN_DECLINE_OBS = 3         # Consecutive declining obs to confirm peak
PEAK_MIN_DROP_F = 1.5            # Min °F below running max to confirm
PEAK_MIN_DECLINE_MINUTES = 45    # Min minutes since max was set
PEAK_EARLIEST_HOUR = 12          # Don't confirm before noon local
PEAK_LATEST_HOUR = 22            # Stop monitoring after 10 PM local
PEAK_POLL_INTERVAL_SEC = 300     # Seconds between polls in --watch mode

# Confidence levels for strategies
CONFIDENCE_MIDNIGHT_HIGH = 0.80  # 80% confidence for midnight high
CONFIDENCE_WIND_PENALTY = 0.70   # 70% confidence for wind penalty
CONFIDENCE_WET_BULB = 0.75       # 75% confidence for wet bulb
CONFIDENCE_MOS_FADE = 0.85       # 85% confidence when fading NWS vs MOS

# =============================================================================
# API RATE LIMITING & RETRY
# =============================================================================

# Minimum seconds between API requests
API_MIN_REQUEST_INTERVAL = 0.1  # 10 requests/sec max

# Retry configuration
API_RETRY_ATTEMPTS = 3
API_RETRY_MIN_WAIT_SEC = 1
API_RETRY_MAX_WAIT_SEC = 10
API_RETRY_MULTIPLIER = 2  # Exponential backoff multiplier

# HTTP timeouts
HTTP_TIMEOUT_TOTAL_SEC = 10
HTTP_TIMEOUT_CONNECT_SEC = 2
NWS_TIMEOUT_TOTAL_SEC = 15
NWS_TIMEOUT_CONNECT_SEC = 5

# Connection pool settings
CONNECTION_POOL_LIMIT = 10
DNS_CACHE_TTL_SEC = 300
KEEPALIVE_TIMEOUT_SEC = 120

# =============================================================================
# FORECAST SETTINGS
# =============================================================================

# Number of hourly forecast periods to fetch
FORECAST_HOURS_AHEAD = 48

# Number of recent fills to fetch for position analysis
FILLS_FETCH_LIMIT = 200

# Orderbook depth for price queries
ORDERBOOK_DEPTH = 10

# =============================================================================
# FILE PATHS
# =============================================================================

TRADES_LOG_FILE = Path("sniper_trades.jsonl")

# =============================================================================
# LOGGING
# =============================================================================

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
LOG_LEVEL = "INFO"

# =============================================================================
# MIDNIGHT SCANNER CONFIGURATION
# =============================================================================

# Scan schedule (24-hour format, ET timezone)
# 23:00 = 11:00 PM, 23:30 = 11:30 PM, 23:55 = 11:55 PM, 00:05 = 12:05 AM
SCAN_TIMES_ET = ["23:00", "23:30", "23:55", "00:05"]

# Minimum edge to trigger a Discord alert
SCANNER_ALERT_EDGE_THRESHOLD = 0.40  # 40%

# Recommendations that trigger alerts
SCANNER_ALERT_RECOMMENDATIONS = ["BUY", "FADE_NWS"]

# Rate limiting for alerts (minutes between alerts for same ticker)
SCANNER_ALERT_COOLDOWN_MINUTES = 30

# =============================================================================
# DISCORD NOTIFICATIONS
# =============================================================================

# Set DISCORD_WEBHOOK_URL in your .env file:
# DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/YOUR_WEBHOOK_ID/YOUR_WEBHOOK_TOKEN
#
# To create a webhook:
# 1. Go to your Discord server settings
# 2. Click "Integrations" -> "Webhooks"
# 3. Click "New Webhook", name it "NYC Sniper", copy the URL

# =============================================================================
# LLM ENSEMBLE CONFIDENCE (OpenRouter)
# =============================================================================

# Set OPENROUTER_API_KEY in .env to enable
LLM_CONFIDENCE_ENABLED = False  # Disabled by default; enable after validation
LLM_CONFIDENCE_WEIGHT = 0.15   # 15% of final blended score (statistical = 85%)
LLM_TIMEOUT_SECONDS = 10       # Per-model timeout
LLM_MIN_MODELS_REQUIRED = 2    # Minimum models needed for valid consensus

# Models queried in parallel via OpenRouter
LLM_MODELS = [
    "anthropic/claude-sonnet-4",
    "openai/gpt-4o",
    "deepseek/deepseek-chat",
]


# =============================================================================
# CPI TRADING PARAMETERS (mirrors cpi_config.py — cross-reference)
# =============================================================================
# These are duplicated here so modules importing from config.py can access
# CPI params without importing cpi_config. Canonical values in cpi_config.py.

# =============================================================================
# POSITION MONITOR PARAMETERS
# =============================================================================

# How long to wait for a limit sell order to fill before cancelling and re-evaluating
PENDING_SELL_EXPIRY_MINUTES = 30

# How long to wait for a limit buy order to fill before cancelling as stale.
# A resting buy order that hasn't filled in 2 hours means the market moved away
# from our price. Cancel it to free up the exposure budget for fresh opportunities.
RESTING_ORDER_TIMEOUT_MINUTES = 120

# Minutes before a DSM/6-hour observation release to pull resting buy orders
# The DSM Bot and 6-Hour Bot will reprice the market instantly after release
BOT_WINDOW_BUFFER_MIN = 15

# =============================================================================
# AUTO TRADER SAFETY PARAMETERS
# =============================================================================

# Maximum trades per day (hard cap for auto-trader)
AUTO_MAX_TRADES_PER_DAY = 8

# Circuit breaker: halt after N consecutive losses in one day
AUTO_CIRCUIT_BREAKER_LOSSES = 4

# Daily loss limit as % of NLV (halt trading if exceeded)
AUTO_DAILY_LOSS_LIMIT_PCT = 0.15

# Intraday drawdown circuit breaker — halt if cumulative realized losses
# today exceed this % of starting balance. More responsive than consecutive-loss
# breaker because it catches correlated losses across multiple cities.
AUTO_INTRADAY_DRAWDOWN_PCT = 0.10  # 10% of NLV = hard stop for the day

# ── Model Bias Corrections ──
# Apply per-model, per-city bias corrections before KDE probability computation.
# Requires >= MIN_RECORDS_FOR_BIAS (5) days of backtest data per model+city.
# When enabled, each model member gets shifted by -mean_bias (e.g., if ICON runs
# +2.0°F hot in CHI, subtract 2.0 from all ICON-CHI members before KDE).
MODEL_BIAS_CORRECTION_ENABLED = True

# Re-entry after trailing stop exit
# If scanner still shows high confidence and the market dipped (trailing stop
# triggered by thin-book volatility, not thesis break), re-enter.
REENTRY_MIN_CONFIDENCE = 90       # Must still be 90+ to re-enter
REENTRY_COOLDOWN_MINUTES = 30     # Wait at least 30min after trailing stop exit
REENTRY_MAX_PER_TICKER_PER_DAY = 1  # Max 1 re-entry per ticker per day


# =============================================================================
# STRATEGY G: PEAK → TRADE PIPELINE (same-day settlement plays)
# =============================================================================

# Feature flag: when False, peak_monitor only alerts (no auto-execution)
PEAK_TRADE_ENABLED = True

# Minimum edge (in cents) to trigger a peak trade
# Peak trades have ~95% true probability, so edge = 95 - market_bid
PEAK_TRADE_MIN_EDGE_CENTS = 10

# Maximum price to pay for a settlement bracket (don't buy at 90¢+ — thin edge)
PEAK_TRADE_MAX_PRICE_CENTS = 85

# Minimum hours to settlement — don't trade if market closes in < N hours
# (too little time for limit order to fill)
PEAK_TRADE_MIN_HOURS_TO_SETTLE = 1.0

# True probability assigned to a confirmed peak (conservative estimate)
# In practice, post-peak lock-in is >98%, but we use 95 for safety
PEAK_TRADE_TRUE_PROB_CENTS = 95

# Maximum contracts per peak trade (separate from scanner trades)
PEAK_TRADE_MAX_CONTRACTS = 20

# =============================================================================
# STALE PRICE DETECTOR (scan delta tracking)
# =============================================================================

# Feature flag
STALE_PRICE_ENABLED = True

# Minimum shift in ensemble mean between scans to flag (°F)
STALE_PRICE_MIN_SHIFT_F = 1.5

# Minimum price gap: if market hasn't repriced by this many cents, alert
STALE_PRICE_MIN_GAP_CENTS = 8

# State file for previous scan data
STALE_PRICE_STATE_FILE = "stale_price_state.json"


CPI_MAX_POSITION_PCT = 0.15         # 15% per trade (vs 10% for weather)
CPI_MAX_DAILY_EXPOSURE = 0.30       # 30% across all CPI positions
CPI_MAX_CORRELATED_EXPOSURE = 0.20  # 20% across correlated CPI markets
CPI_MIN_EDGE_THRESHOLD = 0.12       # 12% minimum edge (vs 15% for weather)
CPI_MIN_KDE_PROBABILITY = 0.15      # 15% minimum model probability (vs 20%)
CPI_MIN_CONFIDENCE_TO_TRADE = 85    # 85/100 (vs 90 for weather)
CPI_MAX_ENTRY_PRICE_CENTS = 65      # 65 cents (vs 50 for weather)
