#!/usr/bin/env python3
"""
CPI EDGE v1.0 — Configuration for CPI Prediction Market Trading

Centralized configuration for CPI data sources, ensemble weights,
Kalshi market series, BLS release schedule, and trading parameters.

Architecture mirrors weather config.py: single source of truth for
all CPI scanner modules.
"""

from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional


# =============================================================================
# CPI DATA SOURCE CONFIGURATION
# =============================================================================

@dataclass
class CPISourceConfig:
    """Configuration for a single CPI forecast source (analogous to weather ModelGroup)."""
    name: str                          # Internal identifier
    display_name: str                  # Human-readable name
    source_type: str                   # "nowcast", "market", "survey", "ensemble"
    weight: float                      # Skill weight (1.30 = best, 0.85 = weakest)
    historical_std: float              # Historical forecast error std (% MoM)
    synthetic_count: int               # Number of synthetic ensemble members to generate
    fred_series_id: Optional[str] = None   # FRED API series ID (if available)
    url: str = ""                      # Manual lookup URL
    update_frequency: str = ""         # "daily", "weekly", "monthly", "quarterly"
    weather_analog: str = ""           # Which weather model this maps to


# Source definitions — matching weather ensemble structure
# Total synthetic members: 51 + 51 + 31 + 40 + 40 = 213

CPI_SOURCES: Dict[str, CPISourceConfig] = {
    "cleveland_fed": CPISourceConfig(
        name="cleveland_fed",
        display_name="Cleveland Fed Nowcast",
        source_type="nowcast",
        weight=1.30,
        historical_std=0.04,   # ~0.04% MoM MAE historically
        synthetic_count=51,
        url="https://www.clevelandfed.org/indicators-and-data/inflation-nowcasting",
        update_frequency="daily",
        weather_analog="AIFS (frontier AI)",
    ),
    "tips_breakeven": CPISourceConfig(
        name="tips_breakeven",
        display_name="TIPS Breakeven (5Y+10Y)",
        source_type="market",
        weight=1.15,
        historical_std=0.06,   # Derived from breakeven volatility
        synthetic_count=51,
        fred_series_id="T5YIE",  # Also uses T10YIE
        update_frequency="daily",
        weather_analog="IFS (physics)",
    ),
    "umich": CPISourceConfig(
        name="umich",
        display_name="UMich Expectations",
        source_type="survey",
        weight=1.00,
        historical_std=0.20,   # Consumer surveys are noisy
        synthetic_count=31,
        fred_series_id="MICH",
        update_frequency="monthly",
        weather_analog="GFS (workhorse)",
    ),
    "nyfed": CPISourceConfig(
        name="nyfed",
        display_name="NY Fed Consumer Survey",
        source_type="survey",
        weight=0.95,
        historical_std=0.15,
        synthetic_count=40,
        url="https://www.newyorkfed.org/microeconomics/sce",
        update_frequency="monthly",
        weather_analog="ICON (alternative)",
    ),
    "spf": CPISourceConfig(
        name="spf",
        display_name="Philly Fed SPF",
        source_type="ensemble",
        weight=0.85,
        historical_std=0.10,   # Actual forecaster spread, not synthetic
        synthetic_count=40,    # Use raw forecaster count when available
        url="https://www.philadelphiafed.org/surveys-and-data/real-time-data-research/survey-of-professional-forecasters",
        update_frequency="quarterly",
        weather_analog="GEM (supporting)",
    ),
    "bls_prior": CPISourceConfig(
        name="bls_prior",
        display_name="BLS Historical Prior",
        source_type="prior",
        weight=0.60,
        historical_std=0.12,  # Std of actual MoM % changes over 24 months
        synthetic_count=25,
        update_frequency="monthly",
        weather_analog="Climatology (baseline)",
    ),
}


# =============================================================================
# FRED API SERIES IDS
# =============================================================================

FRED_SERIES = {
    # TIPS breakeven inflation rates (daily)
    "tips_5y": "T5YIE",
    "tips_10y": "T10YIE",
    "tips_5y5y_fwd": "T5YIFR",

    # Consumer inflation expectations (monthly)
    "umich_1y": "MICH",

    # Energy prices (for CPI energy component)
    "gasoline_weekly": "GASREGW",       # Regular gas $/gallon (weekly)
    "brent_crude_daily": "DCOILBRENTEU",  # Brent crude $/barrel (daily)

    # CPI actual prints (settlement data)
    "cpi_u_sa": "CPIAUCSL",            # CPI-U, seasonally adjusted (index level)
    "core_cpi_sa": "CPILFESL",         # Core CPI, less food & energy, SA

    # Fed funds rate (macro context)
    "fed_funds_upper": "DFEDTARU",
}

# BLS API series IDs (different from FRED)
BLS_SERIES = {
    "cpi_all_sa": "CUSR0000SA0",         # CPI-U All items, SA
    "core_cpi_sa": "CUSR0000SA0L1E",     # All items less food & energy, SA
    "cpi_all_nsa": "CUUR0000SA0",        # CPI-U All items, not SA
}


# =============================================================================
# KALSHI CPI MARKET SERIES
# =============================================================================

@dataclass
class CPIMarketConfig:
    """Configuration for a Kalshi CPI market series."""
    series_ticker: str          # Kalshi series identifier
    market_type: str            # "mom", "yoy", "core_yoy"
    display_name: str           # Human-readable
    bls_series: str             # Which BLS series settles this


CPI_MARKETS: Dict[str, CPIMarketConfig] = {
    "mom": CPIMarketConfig(
        series_ticker="KXCPI",
        market_type="mom",
        display_name="CPI Month-over-Month",
        bls_series="CUSR0000SA0",
    ),
    "yoy": CPIMarketConfig(
        series_ticker="KXCPIYOY",
        market_type="yoy",
        display_name="CPI Year-over-Year",
        bls_series="CUSR0000SA0",
    ),
    "core_yoy": CPIMarketConfig(
        series_ticker="KXCPICOREYOY",
        market_type="core_yoy",
        display_name="Core CPI Year-over-Year",
        bls_series="CUSR0000SA0L1E",
    ),
}


# =============================================================================
# 2026 CPI RELEASE SCHEDULE (from BLS)
# All releases at 8:30 AM ET
# =============================================================================

CPI_RELEASE_DATES_2026: List[date] = [
    date(2026, 1, 13),   # December 2025 data
    date(2026, 2, 13),   # January 2026 data  ← NEXT RELEASE
    date(2026, 3, 11),   # February 2026 data
    date(2026, 4, 10),   # March 2026 data
    date(2026, 5, 13),   # April 2026 data
    date(2026, 6, 10),   # May 2026 data
    date(2026, 7, 15),   # June 2026 data
    date(2026, 8, 12),   # July 2026 data
    date(2026, 9, 10),   # August 2026 data
    date(2026, 10, 14),  # September 2026 data
    date(2026, 11, 12),  # October 2026 data
    date(2026, 12, 10),  # November 2026 data
]

CPI_RELEASE_TIME_ET = "08:30"  # 8:30 AM Eastern


def get_next_cpi_release(as_of: date = None) -> tuple:
    """Get next CPI release date and days until release.

    Returns (release_date, days_until, data_month_str).
    """
    if as_of is None:
        as_of = date.today()

    data_months = [
        "Dec 2025", "Jan 2026", "Feb 2026", "Mar 2026",
        "Apr 2026", "May 2026", "Jun 2026", "Jul 2026",
        "Aug 2026", "Sep 2026", "Oct 2026", "Nov 2026",
    ]

    for i, release_date in enumerate(CPI_RELEASE_DATES_2026):
        if release_date >= as_of:
            days_until = (release_date - as_of).days
            return release_date, days_until, data_months[i]

    # Past all 2026 dates — return last one
    return CPI_RELEASE_DATES_2026[-1], 0, data_months[-1]


# =============================================================================
# CPI TRADING PARAMETERS
# =============================================================================

# Position sizing (slightly larger than weather — fewer opportunities)
CPI_MAX_POSITION_PCT = 0.15         # 15% per trade (vs 10% for weather)
CPI_MAX_DAILY_EXPOSURE = 0.30       # 30% across all CPI positions
CPI_MAX_CORRELATED_EXPOSURE = 0.20  # 20% across correlated CPI markets

# Edge thresholds
CPI_MIN_EDGE_THRESHOLD = 0.12       # 12% minimum edge (vs 15% for weather)
CPI_MIN_KDE_PROBABILITY = 0.15      # 15% minimum model probability (vs 20%)

# Confidence gate — trade at this level or above
CPI_MIN_CONFIDENCE_TO_TRADE = 85    # 85/100 (vs 90 for weather)

# Maximum entry price
CPI_MAX_ENTRY_PRICE_CENTS = 65      # 65¢ (vs 50¢ — CPI markets have higher implied probs)

# Same exit rules as weather (reuse position_monitor.py)
# Freeroll at 2x, efficiency exit at 90¢, trailing stop at -8¢


# =============================================================================
# CPI STRATEGY PARAMETERS
# =============================================================================

# Strategy A: Cleveland Fed Nowcast Divergence
NOWCAST_DIVERGENCE_THRESHOLD_MOM = 0.05  # 0.05% MoM divergence from consensus

# Strategy B: Energy Component Surprise
ENERGY_SURPRISE_THRESHOLD_PCT = 5.0      # 5% move in gas/oil since reference period
ENERGY_LARGE_SURPRISE_PCT = 10.0         # 10% = large surprise

# CPI energy weight in index (approximate)
CPI_ENERGY_WEIGHT = 0.07                 # ~7% of CPI basket


# =============================================================================
# SCAN SCHEDULING
# =============================================================================

# Only scan within this window before CPI release
CPI_SCAN_WINDOW_DAYS = 7                 # Start scanning T-7

# Optimal scan times (ET)
CPI_SCAN_TIMES = [
    "10:30",  # After Cleveland Fed daily update
    "16:30",  # After market close (breakeven data)
    "07:00",  # Release-day final check
]


# =============================================================================
# SEASONAL ADJUSTMENT REFERENCE
# =============================================================================

# Months where BLS seasonal adjustment is known to be tricky
# January: large seasonal factors (annual weight update)
# September: back-to-school, seasonal shift
SEASONAL_ANOMALY_MONTHS = [1, 9]

# Historical Cleveland Fed accuracy by month (MAE in % MoM)
# Source: Cleveland Fed own evaluation (approximate)
CLEVELAND_FED_MONTHLY_MAE = {
    1: 0.06,   # January — harder (seasonal adjustment)
    2: 0.04,
    3: 0.04,
    4: 0.03,
    5: 0.03,
    6: 0.04,
    7: 0.03,
    8: 0.03,
    9: 0.05,   # September — harder
    10: 0.04,
    11: 0.03,
    12: 0.04,
}
