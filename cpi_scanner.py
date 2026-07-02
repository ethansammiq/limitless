#!/usr/bin/env python3
"""
CPI EDGE v1.0 — Consumer Price Index Prediction Market Scanner

Identifies mispriced CPI brackets on Kalshi by fusing multiple inflation
forecast sources into a synthetic ensemble, then applying KDE probability
estimation and confidence scoring — same architecture as Weather Edge.

Data Sources (FREE):
  - Cleveland Fed Inflation Nowcast (manual input, daily)     ← FRONTIER
  - TIPS Breakeven Inflation 5Y+10Y (FRED, daily)
  - UMich Consumer Inflation Expectations (FRED, monthly)
  - Gasoline + Brent Crude prices (FRED, daily/weekly)
  - BLS CPI historical prints (BLS API)
  - Kalshi CPI market brackets (Kalshi API)

Ensemble Method:
  Parametric bootstrap: each source generates N(estimate, historical_std)
  synthetic members → ~200 total → feed into same KDE engine as weather.

Usage:
  python3 cpi_scanner.py                    # Full scan (all CPI series)
  python3 cpi_scanner.py --series mom       # MoM only
  python3 cpi_scanner.py --no-prompt        # Skip manual inputs (FRED only)
  python3 cpi_scanner.py --schedule         # Show release schedule
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
import numpy as np
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from log_setup import get_logger

# Import KDE engine from weather scanner (model-agnostic, reuse directly)
from edge_scanner_v2 import kde_probability, silverman_bandwidth

# Import CPI config
from cpi_config import (
    CPI_SOURCES, CPI_MARKETS, BLS_SERIES,
    CPI_RELEASE_DATES_2026, get_next_cpi_release,
    CPI_MAX_POSITION_PCT, CPI_MIN_EDGE_THRESHOLD, CPI_MIN_KDE_PROBABILITY,
    CPI_MIN_CONFIDENCE_TO_TRADE, CPI_MAX_ENTRY_PRICE_CENTS,
    NOWCAST_DIVERGENCE_THRESHOLD_MOM, ENERGY_SURPRISE_THRESHOLD_PCT,
    ENERGY_LARGE_SURPRISE_PCT, CPI_SCAN_WINDOW_DAYS,
    SEASONAL_ANOMALY_MONTHS, CLEVELAND_FED_MONTHLY_MAE,
)

logger = get_logger(__name__)

FRED_BASE_URL = "https://api.stlouisfed.org/fred/series/observations"
BLS_BASE_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"

ET = ZoneInfo("America/New_York")


# ─── Dataclasses ──────────────────────────────────────

@dataclass
class CPISourceEstimate:
    """Single data source estimate (analogous to weather ModelGroup)."""
    name: str
    display_name: str = ""
    point_estimate: float = 0.0        # CPI MoM % (e.g., 0.30)
    uncertainty_std: float = 0.05
    synthetic_members: list[float] = field(default_factory=list)
    weight: float = 1.0
    last_updated: str = ""
    data_available: bool = False
    weather_analog: str = ""


@dataclass
class CPIEnsemble:
    """Multi-source CPI ensemble (analogous to weather EnsembleV2)."""
    sources: list[CPISourceEstimate] = field(default_factory=list)
    all_members: list[float] = field(default_factory=list)
    member_weights: list[float] = field(default_factory=list)
    total_count: int = 0
    # Stats (in % MoM)
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
    kde_bandwidth: float = 0.0
    sources_available: int = 0


@dataclass
class ConsensusData:
    """Wall Street consensus + energy indicators (analogous to NWSData)."""
    consensus_mom: float = 0.0
    consensus_yoy: float = 0.0
    consensus_core_mom: float = 0.0
    consensus_core_yoy: float = 0.0
    consensus_source: str = ""
    prior_month_actual_mom: float = 0.0
    prior_month_actual_yoy: float = 0.0
    # Energy
    gas_current: float = 0.0
    gas_prior: float = 0.0
    gas_change_pct: float = 0.0
    oil_current: float = 0.0
    oil_prior: float = 0.0
    oil_change_pct: float = 0.0
    energy_signal: str = "stable"      # "stable", "rising", "falling"
    # Derived
    consensus_vs_nowcast: str = ""     # "aligned", "nowcast_higher", "nowcast_lower"
    data_available: bool = False


@dataclass
class CPIOpportunity:
    """Trading opportunity on a CPI bracket (analogous to weather Opportunity)."""
    market_type: str = ""              # "mom", "yoy", "core_yoy"
    bracket_title: str = ""
    ticker: str = ""
    threshold: float = 0.0
    direction: str = ""                # "above" or "below"
    side: str = "yes"
    yes_bid: int = 0
    yes_ask: int = 0
    no_bid: int = 0
    no_ask: int = 0
    volume: int = 0
    # Model
    kde_prob: float = 0.0
    histogram_prob: float = 0.0
    # Edge
    edge_raw: float = 0.0
    edge_after_fees: float = 0.0
    # Sizing
    kelly: float = 0.0
    suggested_contracts: int = 0
    cost: float = 0.0
    max_payout: float = 0.0
    # Confidence
    confidence: str = "LOW"
    confidence_score: float = 0.0
    confidence_reasons: list[str] = field(default_factory=list)
    tradeable: bool = False
    strategies: list[str] = field(default_factory=list)
    rationale: str = ""
    # Timing
    days_to_release: int = 0
    release_date: str = ""


# ─── FRED API Fetcher ─────────────────────────────────

async def fetch_fred_series(session: aiohttp.ClientSession, series_id: str,
                            api_key: str, lookback_days: int = 90) -> tuple[float, str]:
    """Fetch latest observation from FRED API.

    Returns (value, observation_date) or (0.0, "") on failure.
    """
    end_date = date.today().isoformat()
    start_date = (date.today() - timedelta(days=lookback_days)).isoformat()

    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "observation_start": start_date,
        "observation_end": end_date,
        "sort_order": "desc",
        "limit": "5",
    }

    try:
        async with session.get(FRED_BASE_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                logger.warning("FRED %s returned status %d", series_id, resp.status)
                return 0.0, ""
            data = await resp.json()

        observations = data.get("observations", [])
        for obs in observations:
            val = obs.get("value", ".")
            if val != ".":  # FRED uses "." for missing
                return float(val), obs.get("date", "")

        logger.warning("FRED %s: no valid observations found", series_id)
        return 0.0, ""

    except Exception as e:
        logger.error("FRED %s fetch error: %s", series_id, e)
        return 0.0, ""


# ─── Source-Specific Fetchers ─────────────────────────

async def fetch_tips_breakevens(session: aiohttp.ClientSession, api_key: str) -> CPISourceEstimate:
    """Fetch 5Y and 10Y TIPS breakeven inflation from FRED.

    Converts annual breakeven to approximate monthly CPI MoM implication.
    """
    src_cfg = CPI_SOURCES["tips_breakeven"]
    est = CPISourceEstimate(
        name="tips_breakeven",
        display_name=src_cfg.display_name,
        weight=src_cfg.weight,
        uncertainty_std=src_cfg.historical_std,
        weather_analog=src_cfg.weather_analog,
    )

    t5y, t5y_date = await fetch_fred_series(session, "T5YIE", api_key)
    t10y, t10y_date = await fetch_fred_series(session, "T10YIE", api_key)

    if t5y > 0 and t10y > 0:
        # Average 5Y and 10Y breakevens, convert annual → monthly
        annual_breakeven = (t5y + t10y) / 2.0
        # Monthly CPI MoM ≈ annual / 12 (simplified; real relationship is non-linear)
        monthly_implied = annual_breakeven / 12.0
        est.point_estimate = round(monthly_implied, 4)
        est.last_updated = t5y_date
        est.data_available = True
        logger.info("TIPS breakeven: 5Y=%.2f%% 10Y=%.2f%% → MoM≈%.3f%%",
                     t5y, t10y, monthly_implied)
    elif t5y > 0:
        est.point_estimate = round(t5y / 12.0, 4)
        est.last_updated = t5y_date
        est.data_available = True
    elif t10y > 0:
        est.point_estimate = round(t10y / 12.0, 4)
        est.last_updated = t10y_date
        est.data_available = True

    return est


async def fetch_umich_expectations(session: aiohttp.ClientSession, api_key: str) -> CPISourceEstimate:
    """Fetch UMich 1-year inflation expectations from FRED (MICH series)."""
    src_cfg = CPI_SOURCES["umich"]
    est = CPISourceEstimate(
        name="umich",
        display_name=src_cfg.display_name,
        weight=src_cfg.weight,
        uncertainty_std=src_cfg.historical_std,
        weather_analog=src_cfg.weather_analog,
    )

    val, obs_date = await fetch_fred_series(session, "MICH", api_key, lookback_days=120)
    if val > 0:
        # UMich reports annual expectations → convert to monthly
        monthly_implied = val / 12.0
        est.point_estimate = round(monthly_implied, 4)
        est.last_updated = obs_date
        est.data_available = True
        logger.info("UMich expectations: %.1f%% annual → MoM≈%.3f%% (as of %s)",
                     val, monthly_implied, obs_date)

    return est


async def fetch_energy_indicators(session: aiohttp.ClientSession,
                                   api_key: str) -> dict:
    """Fetch gasoline and oil prices from FRED. Compute change vs prior month."""
    gas_current, gas_date = await fetch_fred_series(session, "GASREGW", api_key, lookback_days=30)
    oil_current, oil_date = await fetch_fred_series(session, "DCOILBRENTEU", api_key, lookback_days=14)

    # Get prior month values via date-range query (30-75 days ago)
    end_prior = (date.today() - timedelta(days=30)).isoformat()
    start_prior = (date.today() - timedelta(days=75)).isoformat()

    gas_prior_val = 0.0
    oil_prior_val = 0.0

    try:
        params = {
            "series_id": "GASREGW",
            "api_key": api_key,
            "file_type": "json",
            "observation_start": start_prior,
            "observation_end": end_prior,
            "sort_order": "desc",
            "limit": "5",
        }
        async with session.get(FRED_BASE_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json()
                for obs in data.get("observations", []):
                    if obs.get("value", ".") != ".":
                        gas_prior_val = float(obs["value"])
                        break
    except Exception:
        pass

    try:
        params = {
            "series_id": "DCOILBRENTEU",
            "api_key": api_key,
            "file_type": "json",
            "observation_start": start_prior,
            "observation_end": end_prior,
            "sort_order": "desc",
            "limit": "5",
        }
        async with session.get(FRED_BASE_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json()
                for obs in data.get("observations", []):
                    if obs.get("value", ".") != ".":
                        oil_prior_val = float(obs["value"])
                        break
    except Exception:
        pass

    gas_change = ((gas_current / gas_prior_val) - 1) * 100 if gas_prior_val > 0 else 0.0
    oil_change = ((oil_current / oil_prior_val) - 1) * 100 if oil_prior_val > 0 else 0.0

    if abs(gas_change) > ENERGY_LARGE_SURPRISE_PCT:
        signal = "rising_fast" if gas_change > 0 else "falling_fast"
    elif abs(gas_change) > ENERGY_SURPRISE_THRESHOLD_PCT:
        signal = "rising" if gas_change > 0 else "falling"
    else:
        signal = "stable"

    logger.info("Energy: Gas=$%.2f (Δ%.1f%%), Oil=$%.2f (Δ%.1f%%) → %s",
                gas_current, gas_change, oil_current, oil_change, signal)

    return {
        "gas_current": gas_current,
        "gas_prior": gas_prior_val,
        "gas_change_pct": round(gas_change, 2),
        "oil_current": oil_current,
        "oil_prior": oil_prior_val,
        "oil_change_pct": round(oil_change, 2),
        "energy_signal": signal,
    }


async def fetch_bls_cpi_history(session: aiohttp.ClientSession) -> list[dict]:
    """Fetch recent CPI history from BLS API (last 2 years).

    Returns list of {date, value, mom_change} sorted newest first.
    """
    payload = {
        "seriesid": [BLS_SERIES["cpi_all_sa"]],
        "startyear": str(date.today().year - 2),
        "endyear": str(date.today().year),
    }

    headers = {"Content-type": "application/json"}

    try:
        async with session.post(BLS_BASE_URL, json=payload, headers=headers,
                                timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                logger.warning("BLS API returned status %d", resp.status)
                return []
            data = await resp.json()

        results = data.get("Results", {}).get("series", [])
        if not results:
            return []

        raw_data = results[0].get("data", [])
        # BLS returns {year, period, periodName, value, ...}
        # Period M01 = January, M02 = February, etc.
        parsed = []
        for item in raw_data:
            period = item.get("period", "")
            if not period.startswith("M"):
                continue
            month = int(period[1:])
            year = int(item.get("year", 0))
            raw_val = item.get("value", "0")
            # BLS uses "-" for missing/unavailable data
            if raw_val in ("-", ".", "", None):
                continue
            try:
                value = float(raw_val)
            except (ValueError, TypeError):
                continue
            parsed.append({
                "year": year,
                "month": month,
                "period_name": item.get("periodName", ""),
                "index_value": value,
            })

        # Sort by date descending
        parsed.sort(key=lambda x: (x["year"], x["month"]), reverse=True)

        # Compute MoM changes
        for i in range(len(parsed) - 1):
            prior = parsed[i + 1]["index_value"]
            current = parsed[i]["index_value"]
            if prior > 0:
                parsed[i]["mom_change"] = round(((current / prior) - 1) * 100, 3)
            else:
                parsed[i]["mom_change"] = 0.0
        if parsed:
            parsed[-1]["mom_change"] = 0.0

        logger.info("BLS CPI: fetched %d months, latest index=%.1f",
                     len(parsed), parsed[0]["index_value"] if parsed else 0)
        return parsed

    except Exception as e:
        logger.error("BLS API error: %s", e)
        return []


# ─── Kalshi CPI Bracket Fetcher ───────────────────────

async def fetch_kalshi_cpi_brackets(session: aiohttp.ClientSession,
                                     series_ticker: str) -> list[dict]:
    """Fetch open Kalshi CPI brackets for a series. Returns raw market data."""
    try:
        params = {
            "status": "open",
            "series_ticker": series_ticker,
            "limit": "50",
        }
        headers = {"Accept": "application/json"}

        async with session.get(
            f"{KALSHI_BASE}/markets",
            params=params,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                logger.warning("Kalshi %s returned status %d", series_ticker, resp.status)
                return []
            data = await resp.json()

        markets = data.get("markets", [])
        logger.info("Kalshi %s: %d open markets", series_ticker, len(markets))
        return markets

    except Exception as e:
        logger.error("Kalshi %s fetch error: %s", series_ticker, e)
        return []


def parse_cpi_bracket(market: dict) -> tuple[float, str, str]:
    """Parse Kalshi CPI market into (threshold, direction, bracket_type).

    Kalshi CPI markets use threshold-based contracts:
      - Title: "CPI to increase 0.3% or more" → (0.3, "above", "mom")
      - Title: "Inflation rate above 2.5%"     → (2.5, "above", "yoy")

    Returns (threshold, direction, bracket_type).
    """
    title = market.get("title", "")
    subtitle = market.get("subtitle", "")
    ticker = market.get("ticker", "")
    floor_val = market.get("floor_strike")
    cap_val = market.get("cap_strike")

    # Try to extract threshold from strike values
    threshold = 0.0
    direction = "above"
    bracket_type = "unknown"

    # Determine type from series ticker
    series = market.get("series_ticker", "")
    if "KXCPICOREYOY" in series:
        bracket_type = "core_yoy"
    elif "KXCPIYOY" in series:
        bracket_type = "yoy"
    elif "KXCPI" in series:
        bracket_type = "mom"

    # Floor/cap strikes from Kalshi API are already in the natural unit:
    #   MoM: floor_strike=0.3 means "above 0.3%"
    #   YoY: floor_strike=2.7 means "above 2.7%"
    # No conversion needed — use as-is.
    if floor_val is not None:
        threshold = float(floor_val)
        direction = "above"
    elif cap_val is not None:
        threshold = float(cap_val)
        direction = "below"

    # Fallback: parse from title/subtitle
    if threshold == 0.0:
        # Try patterns like "0.3%", "2.5%", etc.
        pct_match = re.search(r'(\d+\.?\d*)\s*%', title)
        if pct_match:
            threshold = float(pct_match.group(1))

        # Determine direction
        title_lower = title.lower()
        if any(w in title_lower for w in ["above", "more", "higher", "increase", "rise"]):
            direction = "above"
        elif any(w in title_lower for w in ["below", "less", "lower", "decrease", "fall"]):
            direction = "below"

    return threshold, direction, bracket_type


# ─── BLS Historical Prior ─────────────────────────────

def build_bls_prior_source(bls_history: list[dict], target_month: int = 0) -> CPISourceEstimate:
    """Build a historical prior source from BLS CPI index data.

    Computes MoM % changes from the index history and uses them as an
    empirical prior. Optionally weights same-calendar-month observations
    more heavily (seasonal pattern).

    Args:
        bls_history: Output of fetch_bls_cpi_history() — list of dicts with
                     'index_value', 'mom_change', 'month', 'year' keys.
        target_month: Calendar month of the upcoming CPI report (1-12).
                      0 = use all months equally.

    Returns:
        CPISourceEstimate with historical MoM changes as synthetic_members.
    """
    from cpi_config import CPI_SOURCES

    cfg = CPI_SOURCES.get("bls_prior")
    if not cfg:
        return CPISourceEstimate(name="bls_prior", display_name="BLS Historical Prior")

    # Filter to entries with valid MoM changes (skip the oldest with 0.0)
    mom_changes = [
        entry["mom_change"] for entry in bls_history
        if "mom_change" in entry and entry.get("mom_change", 0.0) != 0.0
    ]

    if len(mom_changes) < 3:
        logger.info("BLS prior: insufficient history (%d entries)", len(mom_changes))
        return CPISourceEstimate(
            name="bls_prior", display_name="BLS Historical Prior",
            data_available=False,
        )

    # If target month specified, give 2x weight to same-month observations
    weighted_changes = []
    for entry in bls_history:
        if "mom_change" not in entry or entry.get("mom_change", 0.0) == 0.0:
            continue
        change = entry["mom_change"]
        if target_month > 0 and entry.get("month") == target_month:
            weighted_changes.extend([change, change])  # Double-weight same month
        elif target_month > 0 and abs(entry.get("month", 0) - target_month) <= 1:
            weighted_changes.extend([change, change])  # 2x for adjacent months too
        else:
            weighted_changes.append(change)

    if not weighted_changes:
        weighted_changes = mom_changes

    point_est = float(np.mean(weighted_changes))
    std_val = float(np.std(weighted_changes, ddof=1)) if len(weighted_changes) > 1 else cfg.historical_std

    logger.info("BLS prior: %d MoM changes → mean=%.3f%%, std=%.3f%%",
                len(weighted_changes), point_est, std_val)

    return CPISourceEstimate(
        name="bls_prior",
        display_name=cfg.display_name,
        point_estimate=point_est,
        uncertainty_std=std_val,
        weight=cfg.weight,
        data_available=True,
        last_updated=date.today().isoformat(),
        weather_analog=cfg.weather_analog,
        synthetic_members=[],  # Will be generated by build_cpi_ensemble()
    )


# ─── YoY Conversion from MoM Ensemble ─────────────────

def build_yoy_ensemble(mom_ensemble: CPIEnsemble,
                       bls_history: list[dict],
                       bls_series_id: str = "cpi_all_sa") -> CPIEnsemble:
    """Convert a MoM ensemble into a YoY ensemble using the CPI index.

    For each MoM member m:
        projected_index = latest_index * (1 + m/100)
        yoy_pct = (projected_index / index_12mo_ago - 1) * 100

    Args:
        mom_ensemble: The MoM CPIEnsemble (members are MoM % changes).
        bls_history: BLS CPI index history (newest first), from fetch_bls_cpi_history().
        bls_series_id: Which BLS series for distinguishing CPI-U vs Core.

    Returns:
        New CPIEnsemble with YoY-scale members, or empty ensemble if conversion fails.
    """
    if not mom_ensemble.all_members or len(bls_history) < 13:
        logger.warning("YoY conversion: insufficient data (members=%d, history=%d)",
                       len(mom_ensemble.all_members), len(bls_history))
        return CPIEnsemble(sources=mom_ensemble.sources)

    # Latest CPI index value (most recent month)
    latest_index = bls_history[0]["index_value"]

    # Index from 12 months ago
    # BLS history is sorted newest-first, so index 12 is ~12 months ago
    # But we need to match by calendar month for precision
    latest_year = bls_history[0]["year"]
    latest_month = bls_history[0]["month"]
    target_year = latest_year - 1
    target_month = latest_month

    index_12mo_ago = None
    for entry in bls_history:
        if entry["year"] == target_year and entry["month"] == target_month:
            index_12mo_ago = entry["index_value"]
            break

    if index_12mo_ago is None:
        # Fallback: use the 12th entry (approximate)
        if len(bls_history) >= 13:
            index_12mo_ago = bls_history[12]["index_value"]
        else:
            logger.warning("YoY conversion: cannot find index from 12 months ago")
            return CPIEnsemble(sources=mom_ensemble.sources)

    if index_12mo_ago <= 0:
        logger.warning("YoY conversion: invalid 12-month-ago index: %.1f", index_12mo_ago)
        return CPIEnsemble(sources=mom_ensemble.sources)

    logger.info("YoY conversion: latest_index=%.1f (%d-%02d), index_12mo_ago=%.1f (%d-%02d)",
                latest_index, latest_year, latest_month,
                index_12mo_ago, target_year, target_month)

    # Convert each MoM member to YoY
    yoy_members = []
    for m in mom_ensemble.all_members:
        projected_index = latest_index * (1 + m / 100.0)
        yoy_pct = (projected_index / index_12mo_ago - 1) * 100.0
        yoy_members.append(yoy_pct)

    # Convert weights (same order as members)
    yoy_weights = list(mom_ensemble.member_weights) if mom_ensemble.member_weights else []

    m = np.array(yoy_members)
    w = np.array(yoy_weights) if yoy_weights else np.ones(len(m)) / len(m)

    # Sort
    sort_idx = np.argsort(m)
    m_sorted = m[sort_idx]
    w_sorted = w[sort_idx]

    yoy_ensemble = CPIEnsemble(
        sources=mom_ensemble.sources,
        all_members=m_sorted.tolist(),
        member_weights=w_sorted.tolist(),
        total_count=len(yoy_members),
        mean=float(np.average(m, weights=w)),
        median=float(np.median(m)),
        std=float(np.std(m, ddof=1)) if len(m) > 1 else 0.0,
        min_val=float(m.min()),
        max_val=float(m.max()),
        p10=float(np.percentile(m, 10)),
        p25=float(np.percentile(m, 25)),
        p50=float(np.percentile(m, 50)),
        p75=float(np.percentile(m, 75)),
        p90=float(np.percentile(m, 90)),
        kde_bandwidth=silverman_bandwidth(m_sorted.tolist(), min_bandwidth=0.01),
        sources_available=mom_ensemble.sources_available,
    )

    logger.info("YoY ensemble: %d members, mean=%.3f%%, std=%.3f%%",
                yoy_ensemble.total_count, yoy_ensemble.mean, yoy_ensemble.std)
    return yoy_ensemble


async def fetch_core_bls_history(session: aiohttp.ClientSession) -> list[dict]:
    """Fetch Core CPI (less food & energy) history from BLS API.

    Same structure as fetch_bls_cpi_history() but uses Core CPI series.
    """
    payload = {
        "seriesid": [BLS_SERIES["core_cpi_sa"]],
        "startyear": str(date.today().year - 2),
        "endyear": str(date.today().year),
    }

    headers = {"Content-type": "application/json"}

    try:
        async with session.post(BLS_BASE_URL, json=payload, headers=headers,
                                timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                logger.warning("BLS Core CPI API returned status %d", resp.status)
                return []
            data = await resp.json()

        results = data.get("Results", {}).get("series", [])
        if not results:
            return []

        raw_data = results[0].get("data", [])
        parsed = []
        for item in raw_data:
            period = item.get("period", "")
            if not period.startswith("M"):
                continue
            month = int(period[1:])
            year = int(item.get("year", 0))
            raw_val = item.get("value", "0")
            if raw_val in ("-", ".", "", None):
                continue
            try:
                value = float(raw_val)
            except (ValueError, TypeError):
                continue
            parsed.append({
                "year": year,
                "month": month,
                "period_name": item.get("periodName", ""),
                "index_value": value,
            })

        parsed.sort(key=lambda x: (x["year"], x["month"]), reverse=True)

        # Compute MoM changes
        for i in range(len(parsed) - 1):
            prior = parsed[i + 1]["index_value"]
            current = parsed[i]["index_value"]
            if prior > 0:
                parsed[i]["mom_change"] = round(((current / prior) - 1) * 100, 3)
            else:
                parsed[i]["mom_change"] = 0.0
        if parsed:
            parsed[-1]["mom_change"] = 0.0

        logger.info("BLS Core CPI: fetched %d months, latest index=%.1f",
                     len(parsed), parsed[0]["index_value"] if parsed else 0)
        return parsed

    except Exception as e:
        logger.error("BLS Core CPI API error: %s", e)
        return []


# ─── Synthetic Ensemble Builder ───────────────────────

def build_cpi_ensemble(sources: list[CPISourceEstimate],
                       seed: int = None) -> CPIEnsemble:
    """Build synthetic ensemble from CPI data sources.

    For each available source:
      1. Generate N synthetic members from N(point_estimate, uncertainty_std)
      2. Assign per-member weight = source.weight
      3. Combine all into a single ensemble

    Uses deterministic seed for reproducibility.
    """
    rng = np.random.default_rng(seed=seed)

    all_members = []
    all_weights = []
    available_sources = []

    for src in sources:
        if not src.data_available or src.point_estimate == 0.0:
            continue

        available_sources.append(src)

        # Generate synthetic members
        n = len(src.synthetic_members) if src.synthetic_members else 51
        src_cfg = CPI_SOURCES.get(src.name)
        if src_cfg:
            n = src_cfg.synthetic_count

        members = rng.normal(
            loc=src.point_estimate,
            scale=max(0.005, src.uncertainty_std),  # Floor at 0.005% to prevent degenerate KDE
            size=n,
        ).tolist()

        src.synthetic_members = sorted(members)
        all_members.extend(members)
        all_weights.extend([src.weight] * n)

    if not all_members:
        return CPIEnsemble(sources=sources)

    m = np.array(all_members)
    w = np.array(all_weights)

    # Sort members and weights together
    sort_idx = np.argsort(m)
    m_sorted = m[sort_idx]
    w_sorted = w[sort_idx]

    # Compute stats
    ensemble = CPIEnsemble(
        sources=sources,
        all_members=m_sorted.tolist(),
        member_weights=w_sorted.tolist(),
        total_count=len(all_members),
        mean=float(np.average(m, weights=w)),
        median=float(np.median(m)),
        std=float(np.std(m, ddof=1)) if len(m) > 1 else 0.0,
        min_val=float(m.min()),
        max_val=float(m.max()),
        p10=float(np.percentile(m, 10)),
        p25=float(np.percentile(m, 25)),
        p50=float(np.percentile(m, 50)),
        p75=float(np.percentile(m, 75)),
        p90=float(np.percentile(m, 90)),
        kde_bandwidth=silverman_bandwidth(m_sorted.tolist(), min_bandwidth=0.005),
        sources_available=len(available_sources),
    )

    return ensemble


# ─── Confidence Scoring ───────────────────────────────

def compute_cpi_confidence(ensemble: CPIEnsemble, consensus: ConsensusData,
                            energy: dict, days_to_release: int,
                            data_month: int = 0) -> tuple[str, float, list[str]]:
    """Multi-factor CPI confidence scoring (100-point scale).

    Returns (label, score_0_to_100, reasons[]).
    """
    score = 40.0
    reasons = []

    # ── Factor 1: Source Agreement ── max +15
    available = [s for s in ensemble.sources if s.data_available]
    if len(available) >= 2:
        estimates = [s.point_estimate for s in available]
        spread = max(estimates) - min(estimates)
        if spread < 0.05:
            score += 15
            reasons.append(f"Sources agree within {spread:.3f}% (TIGHT)")
        elif spread < 0.10:
            score += 10
            reasons.append(f"Sources agree within {spread:.3f}% (good)")
        elif spread < 0.15:
            score += 5
            reasons.append(f"Source spread {spread:.3f}% (moderate)")
        elif spread < 0.25:
            reasons.append(f"Source spread {spread:.3f}% (wide)")
        else:
            score -= 10
            reasons.append(f"Source spread {spread:.3f}% (VERY WIDE ⚠)")
    else:
        score -= 15
        reasons.append(f"Only {len(available)} source(s) available (need ≥3) ⚠")

    # ── Factor 2: Nowcast Stability ── max +15
    cleveland = next((s for s in ensemble.sources if s.name == "cleveland_fed"), None)
    if cleveland and cleveland.data_available:
        # In Phase 1 we don't track revisions, so give partial credit
        score += 10
        reasons.append(f"Cleveland Fed nowcast available ({cleveland.point_estimate:.3f}%)")
    else:
        score -= 5
        reasons.append("Cleveland Fed nowcast MISSING ⚠")

    # ── Factor 3: Historical Accuracy ── max +10
    if data_month > 0:
        mae = CLEVELAND_FED_MONTHLY_MAE.get(data_month, 0.04)
        if mae <= 0.03:
            score += 10
            reasons.append(f"Month {data_month} historically easy (MAE={mae:.3f}%)")
        elif mae <= 0.04:
            score += 7
            reasons.append(f"Month {data_month} moderate difficulty (MAE={mae:.3f}%)")
        elif mae <= 0.05:
            score += 3
            reasons.append(f"Month {data_month} harder (MAE={mae:.3f}%)")
        else:
            score -= 5
            reasons.append(f"Month {data_month} is anomaly month (MAE={mae:.3f}%) ⚠")

        if data_month in SEASONAL_ANOMALY_MONTHS:
            score -= 3
            reasons.append("Seasonal adjustment anomaly month ⚠")

    # ── Factor 4: Lead Time ── max +10
    if days_to_release <= 1:
        score += 10
        reasons.append(f"T-{days_to_release} (release imminent — max certainty)")
    elif days_to_release <= 3:
        score += 7
        reasons.append(f"T-{days_to_release} (close)")
    elif days_to_release <= 5:
        score += 3
        reasons.append(f"T-{days_to_release} (moderate lead)")
    else:
        reasons.append(f"T-{days_to_release} (long lead)")

    # ── Factor 5: Energy Stability ── max +10
    gas_change = abs(energy.get("gas_change_pct", 0))
    if gas_change < 2:
        score += 10
        reasons.append(f"Energy stable (gas Δ{gas_change:.1f}%)")
    elif gas_change < 5:
        score += 5
        reasons.append(f"Energy moderate (gas Δ{gas_change:.1f}%)")
    elif gas_change < 10:
        reasons.append(f"Energy volatile (gas Δ{gas_change:.1f}%) ⚠")
    else:
        score -= 5
        reasons.append(f"Energy HIGHLY volatile (gas Δ{gas_change:.1f}%) ⚠")

    # ── Consensus alignment penalty ──
    if consensus.data_available and cleveland and cleveland.data_available:
        div = abs(cleveland.point_estimate - consensus.consensus_mom)
        if div > 0.15:
            score -= 5
            reasons.append(f"Nowcast↔Consensus diverge {div:.3f}% ⚠")
        elif div < 0.03:
            score += 3
            reasons.append(f"Nowcast↔Consensus aligned ({div:.3f}%)")

    # Clamp
    score = max(0, min(100, score))

    if score >= CPI_MIN_CONFIDENCE_TO_TRADE:
        return "ELITE", score, reasons
    elif score >= 75:
        return "HIGH", score, reasons
    elif score >= 55:
        return "MEDIUM", score, reasons
    else:
        return "LOW", score, reasons


# ─── Strategy Detection ───────────────────────────────

def detect_cpi_strategies(ensemble: CPIEnsemble, consensus: ConsensusData,
                           energy: dict, threshold: float,
                           direction: str) -> list[str]:
    """Detect which CPI alpha strategies are active."""
    strategies = []

    # Strategy A: Cleveland Fed Nowcast Divergence
    cleveland = next((s for s in ensemble.sources if s.name == "cleveland_fed"), None)
    if cleveland and cleveland.data_available and consensus.data_available:
        div = cleveland.point_estimate - consensus.consensus_mom
        if abs(div) > NOWCAST_DIVERGENCE_THRESHOLD_MOM:
            arrow = "↑" if div > 0 else "↓"
            strategies.append(
                f"A:NOWCAST_DIVERGE({arrow}{abs(div):.3f}% vs consensus)"
            )

    # Strategy B: Energy Surprise
    gas_change = energy.get("gas_change_pct", 0)
    if abs(gas_change) > ENERGY_SURPRISE_THRESHOLD_PCT:
        direction_str = "UP" if gas_change > 0 else "DOWN"
        strategies.append(f"B:ENERGY_SURPRISE(gas {direction_str} {abs(gas_change):.1f}%)")

    return strategies


# ─── Kelly Fraction ───────────────────────────────────

def half_kelly(prob: float, price_cents: int) -> float:
    """Half-Kelly fraction for binary contract. Same as weather."""
    if price_cents <= 0 or price_cents >= 100 or prob <= 0:
        return 0.0
    p = prob
    b = (100 - price_cents) / price_cents  # Odds ratio
    f = (b * p - (1 - p)) / b
    return max(0, f * 0.5)


# ─── Opportunity Analysis ─────────────────────────────

def analyze_cpi_opportunities(ensemble: CPIEnsemble, consensus: ConsensusData,
                               energy: dict, brackets: list[dict],
                               balance: float, days_to_release: int,
                               data_month: int = 0,
                               market_type: str = "mom") -> list[CPIOpportunity]:
    """Analyze CPI brackets using KDE probabilities.

    Returns list of CPIOpportunity sorted by (tradeable, edge).
    """
    if not ensemble.all_members or not brackets:
        return []

    release_date_obj, _, _ = get_next_cpi_release()
    opps = []

    for mkt in brackets:
        threshold, direction, bracket_type = parse_cpi_bracket(mkt)
        if threshold == 0.0:
            continue

        ticker = mkt.get("ticker", "")
        yes_bid = mkt.get("yes_bid", 0) or 0
        yes_ask = mkt.get("yes_ask", 0) or 0
        no_bid = mkt.get("no_bid", 0) or 0
        no_ask = mkt.get("no_ask", 0) or 0
        volume = mkt.get("volume", 0) or 0

        # Compute KDE probability of outcome crossing threshold
        if direction == "above":
            # P(CPI > threshold)
            kde_prob = 1.0 - kde_probability(
                ensemble.all_members, -999, threshold,
                bandwidth=ensemble.kde_bandwidth,
                weights=ensemble.member_weights,
                min_bandwidth=0.005,
            )
        else:
            # P(CPI < threshold)
            kde_prob = kde_probability(
                ensemble.all_members, -999, threshold,
                bandwidth=ensemble.kde_bandwidth,
                weights=ensemble.member_weights,
                min_bandwidth=0.005,
            )

        # Histogram probability for comparison
        if direction == "above":
            hist_count = sum(1 for m in ensemble.all_members if m >= threshold)
        else:
            hist_count = sum(1 for m in ensemble.all_members if m < threshold)
        histogram_prob = hist_count / len(ensemble.all_members) if ensemble.all_members else 0

        # Determine side and edge
        # YES side: we think the event happens (kde_prob > market price)
        # NO side: we think it doesn't happen (1 - kde_prob > no price)
        yes_prob = kde_prob
        no_prob = 1.0 - kde_prob

        yes_price = yes_ask if yes_ask > 0 else 100
        no_price = no_ask if no_ask > 0 else 100

        yes_edge = yes_prob - (yes_bid / 100.0) if yes_bid > 0 else 0
        no_edge = no_prob - (no_bid / 100.0) if no_bid > 0 else 0

        if yes_edge > no_edge:
            side = "yes"
            model_prob = yes_prob
            entry_price = min(yes_bid + 1, yes_ask) if yes_bid > 0 else yes_ask
            edge_raw = yes_edge
        else:
            side = "no"
            model_prob = no_prob
            entry_price = min(no_bid + 1, no_ask) if no_bid > 0 else no_ask
            edge_raw = no_edge

        # Gate 1: Min KDE probability
        if model_prob < CPI_MIN_KDE_PROBABILITY:
            continue

        # Gate 2: Min edge
        if edge_raw < CPI_MIN_EDGE_THRESHOLD:
            continue

        # Gate 3: Max entry price
        if side == "yes" and entry_price > CPI_MAX_ENTRY_PRICE_CENTS:
            continue

        # Kelly fraction
        kelly = half_kelly(model_prob, entry_price) if entry_price > 0 else 0.0

        # Position size
        max_cost = balance * CPI_MAX_POSITION_PCT
        contracts = min(
            int(max_cost / (entry_price / 100.0)) if entry_price > 0 else 0,
            100,  # Hard cap
        )
        cost = contracts * entry_price / 100.0
        max_payout = contracts * 1.0

        # Strategies
        strategies = detect_cpi_strategies(
            ensemble, consensus, energy, threshold, direction
        )

        # Confidence
        conf_label, conf_score, conf_reasons = compute_cpi_confidence(
            ensemble, consensus, energy, days_to_release, data_month
        )
        tradeable = conf_score >= CPI_MIN_CONFIDENCE_TO_TRADE

        # Rationale
        source_strs = []
        for s in ensemble.sources:
            if s.data_available:
                source_strs.append(f"{s.display_name}={s.point_estimate:.3f}%")
        rationale = " · ".join(source_strs) if source_strs else "Ensemble"

        opp = CPIOpportunity(
            market_type=market_type,
            bracket_title=mkt.get("title", ""),
            ticker=ticker,
            threshold=threshold,
            direction=direction,
            side=side,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=no_bid,
            no_ask=no_ask,
            volume=volume,
            kde_prob=round(model_prob * 100, 1),
            histogram_prob=round(histogram_prob * 100, 1),
            edge_raw=round(edge_raw * 100, 1),
            edge_after_fees=round(edge_raw * 100, 1),  # Maker = 0% fee on Kalshi
            kelly=round(kelly * 100, 1),
            suggested_contracts=contracts,
            cost=round(cost, 2),
            max_payout=round(max_payout, 2),
            confidence=conf_label,
            confidence_score=conf_score,
            confidence_reasons=conf_reasons,
            tradeable=tradeable,
            strategies=strategies,
            rationale=rationale,
            days_to_release=days_to_release,
            release_date=release_date_obj.isoformat(),
        )
        opps.append(opp)

    # Sort: tradeable first, then by edge descending
    opps.sort(key=lambda o: (-int(o.tradeable), -o.edge_raw))
    return opps


# ─── Display Functions ────────────────────────────────

def print_cpi_report(ensemble: CPIEnsemble, consensus: ConsensusData,
                      energy: dict, opps: list[CPIOpportunity],
                      market_config, days_to_release: int,
                      release_date: str, data_month_str: str):
    """Print formatted CPI scan report for one market series."""

    conf_label, conf_score, conf_reasons = compute_cpi_confidence(
        ensemble, consensus, energy, days_to_release,
    )

    print()
    print("=" * 72)
    print(f"  {market_config.display_name} — Release: {release_date}")
    print(f"  Confidence: {conf_label} ({conf_score:.0f}/100) | T-{days_to_release} days")
    print("=" * 72)

    # Ensemble stats
    print(f"\n  SYNTHETIC ENSEMBLE ({ensemble.total_count} members from {ensemble.sources_available} sources)")
    print(f"  ├─ Mean: {ensemble.mean:.3f}%  ±{ensemble.std:.3f}%  (Median: {ensemble.median:.3f}%)")
    print(f"  ├─ Range: {ensemble.min_val:.3f}% → {ensemble.max_val:.3f}%")
    print(f"  ├─ P10={ensemble.p10:.3f}  P25={ensemble.p25:.3f}  P50={ensemble.p50:.3f}  "
          f"P75={ensemble.p75:.3f}  P90={ensemble.p90:.3f}")
    print(f"  └─ KDE bandwidth: {ensemble.kde_bandwidth:.4f}% (Silverman)")

    # Source breakdown
    print("\n  SOURCE BREAKDOWN")
    print(f"  {'Source':<28} {'Estimate':>9} {'Std':>7} {'Wt':>6} {'Members':>8}")
    print(f"  {'─' * 28} {'─' * 9} {'─' * 7} {'─' * 6} {'─' * 8}")
    for src in ensemble.sources:
        status = "✓" if src.data_available else "✗"
        est = f"{src.point_estimate:.3f}%" if src.data_available else "N/A"
        print(f"  {status} {src.display_name:<26} {est:>9} {src.uncertainty_std:.3f}%"
              f" {src.weight:>5.2f}x {len(src.synthetic_members):>7}")

    # Consensus
    if consensus.data_available:
        print("\n  CONSENSUS FORECAST")
        print(f"  ├─ Consensus MoM: {consensus.consensus_mom:.2f}%")
        if consensus.consensus_yoy > 0:
            print(f"  ├─ Consensus YoY: {consensus.consensus_yoy:.1f}%")
        print(f"  └─ vs Ensemble: {consensus.consensus_mom - ensemble.mean:+.3f}%")

    # Energy
    print("\n  ENERGY INDICATORS")
    print(f"  ├─ Gasoline: ${energy.get('gas_current', 0):.2f}/gal "
          f"(Δ{energy.get('gas_change_pct', 0):+.1f}% vs prior month)")
    print(f"  ├─ Brent Oil: ${energy.get('oil_current', 0):.2f}/bbl "
          f"(Δ{energy.get('oil_change_pct', 0):+.1f}%)")
    print(f"  └─ Signal: {energy.get('energy_signal', 'unknown').upper()}")

    # Confidence
    print("\n  CONFIDENCE FACTORS")
    for r in conf_reasons:
        print(f"  ├─ {r}")
    gate_status = "✓ ABOVE GATE" if conf_score >= CPI_MIN_CONFIDENCE_TO_TRADE else "✗ Below gate — OBSERVE ONLY"
    print(f"  └─ {gate_status}")

    # Opportunities
    if opps:
        tradeable = [o for o in opps if o.tradeable]
        print(f"\n  OPPORTUNITIES ({len(opps)} found, {len(tradeable)} tradeable)")
        print(f"  {'─' * 64}")
        for i, opp in enumerate(opps, 1):
            star = "★" if opp.tradeable else "○"
            print(f"\n  {star} [{i}] {opp.side.upper()} {opp.direction} {opp.threshold}% "
                  f"@ {opp.yes_bid if opp.side == 'yes' else opp.no_bid}¢ — "
                  f"{'TRADEABLE' if opp.tradeable else 'observe only'}")
            print(f"      Ticker:     {opp.ticker}")
            print(f"      KDE Prob:   {opp.kde_prob:.1f}%  (Hist: {opp.histogram_prob:.1f}%)")
            print(f"      Edge:       +{opp.edge_raw:.1f}¢ raw → +{opp.edge_after_fees:.1f}¢ net")
            if opp.suggested_contracts > 0:
                print(f"      Kelly:      {opp.kelly:.1f}% → {opp.suggested_contracts} contracts")
            print(f"      Confidence: {opp.confidence} ({opp.confidence_score:.0f}/100)")
            if opp.strategies:
                print(f"      Strategies: {', '.join(opp.strategies)}")
            print(f"      Sources:    {opp.rationale}")
    else:
        print("\n  No opportunities found above minimum thresholds.")


def print_cpi_summary(all_opps: list[CPIOpportunity], balance: float,
                       release_date: str, days_to_release: int):
    """Print overall CPI scan summary."""
    tradeable = [o for o in all_opps if o.tradeable]

    print()
    print("=" * 72)
    print("  CPI SCAN SUMMARY v1.0")
    print("=" * 72)
    print(f"  Balance:       ${balance:.2f}")
    print(f"  Release:       {release_date} (T-{days_to_release})")
    print(f"  Opportunities: {len(all_opps)}")
    print(f"  Tradeable:     {len(tradeable)} (conf ≥ {CPI_MIN_CONFIDENCE_TO_TRADE})")
    print(f"  Min Edge: {CPI_MIN_EDGE_THRESHOLD * 100:.0f}%  |  "
          f"Min KDE: {CPI_MIN_KDE_PROBABILITY * 100:.0f}%  |  "
          f"Max Entry: {CPI_MAX_ENTRY_PRICE_CENTS}¢")

    if all_opps:
        print(f"\n  {'#':>3}   {'Type':<10} {'Side':<5} {'Threshold':<12} {'Price':>5} "
              f"{'KDE':>5} {'Edge':>8} {'Conf':>5}")
        print(f"  {'─' * 3} {'─' * 10} {'─' * 5} {'─' * 12} {'─' * 5} {'─' * 5} {'─' * 8} {'─' * 5}")
        for i, opp in enumerate(all_opps[:15], 1):
            star = "★" if opp.tradeable else " "
            price = opp.yes_bid if opp.side == "yes" else opp.no_bid
            print(f"  {i:>3} {star} {opp.market_type:<10} {opp.side.upper():<5} "
                  f"{opp.direction} {opp.threshold}%  {price:>4}¢ "
                  f"{opp.kde_prob:>4.0f}% {opp.edge_raw:>+7.1f}¢ {opp.confidence_score:>4.0f}")

    if not tradeable:
        print(f"\n  No opportunities meet the {CPI_MIN_CONFIDENCE_TO_TRADE}+ confidence gate.")
        if days_to_release > 3:
            print("  Check again at T-1 or T-2 for maximum confidence boost.")
    print()


# ─── CLI Input for Manual Sources ─────────────────────

def prompt_manual_inputs(skip: bool = False) -> tuple[float, float, float, float]:
    """Prompt user for Cleveland Fed nowcast and consensus values.

    Returns (nowcast_mom, nowcast_core_mom, consensus_mom, consensus_yoy).
    All return 0.0 if skipped.
    """
    if skip:
        return 0.0, 0.0, 0.0, 0.0

    print("\n  ─── Manual Data Input ───")
    print("  (Press Enter to skip any field — FRED-only mode)")
    print("  Cleveland Fed: https://www.clevelandfed.org/indicators-and-data/inflation-nowcasting")
    print("  Consensus:     https://www.investing.com/economic-calendar/cpi-733\n")

    def read_float(prompt: str) -> float:
        try:
            val = input(f"  {prompt}: ").strip()
            if not val:
                return 0.0
            return float(val)
        except (ValueError, EOFError):
            return 0.0

    nowcast_mom = read_float("Cleveland Fed CPI MoM nowcast % (e.g. 0.28)")
    nowcast_core = read_float("Cleveland Fed Core CPI MoM nowcast % (e.g. 0.22)")
    consensus_mom = read_float("Bloomberg/DJ Consensus CPI MoM % (e.g. 0.30)")
    consensus_yoy = read_float("Bloomberg/DJ Consensus CPI YoY % (e.g. 2.9)")

    return nowcast_mom, nowcast_core, consensus_mom, consensus_yoy


# ─── Main Scan Function ──────────────────────────────

async def scan(series_filter: str = None, no_prompt: bool = False):
    """Main CPI scan entry point.

    1. Determine next CPI release date
    2. Check if within scan window
    3. Fetch all data sources
    4. Build synthetic ensemble
    5. Fetch Kalshi brackets
    6. Analyze opportunities
    7. Print report
    """
    now = datetime.now(ET)
    today = date.today()
    release_date, days_to_release, data_month_str = get_next_cpi_release(today)

    print("#" * 72)
    print("  CPI EDGE v1.0 — Inflation Prediction Market Scanner")
    print(f"  {now.strftime('%Y-%m-%d %H:%M:%S')} ET")
    print(f"  Next CPI Release: {release_date.isoformat()} ({data_month_str} data)")
    print(f"  Days to Release: T-{days_to_release}")
    print("  Sources: Cleveland Fed + TIPS + UMich + Energy + BLS")
    print("  Method: Synthetic Ensemble → Gaussian KDE → Bracket Probabilities")
    print("#" * 72)

    # Gate: only scan within window
    if days_to_release > CPI_SCAN_WINDOW_DAYS:
        print(f"\n  ⏳ Outside scan window (T-{days_to_release} > T-{CPI_SCAN_WINDOW_DAYS}).")
        print(f"  Next scan window opens: {(release_date - timedelta(days=CPI_SCAN_WINDOW_DAYS)).isoformat()}")
        print("  Run with --force to override.\n")
        return

    # Get FRED API key
    fred_key = os.environ.get("FRED_API_KEY", "")
    if not fred_key:
        print("\n  ⚠ FRED_API_KEY not set in .env — FRED data sources will be unavailable.")
        print("  Register (free): https://fred.stlouisfed.org/docs/api/api_key.html\n")

    # Get manual inputs
    nowcast_mom, nowcast_core, consensus_mom, consensus_yoy = prompt_manual_inputs(skip=no_prompt)

    # Fetch balance
    try:
        from kalshi_client import fetch_balance_quick
        balance = await fetch_balance_quick()
    except Exception:
        balance = 100.0  # Fallback
    print(f"\n  Account Balance: ${balance:.2f}")

    # Fetch data sources in parallel
    async with aiohttp.ClientSession() as session:
        # Build source estimates
        sources = []

        # 1. Cleveland Fed Nowcast (manual)
        cleveland = CPISourceEstimate(
            name="cleveland_fed",
            display_name="Cleveland Fed Nowcast",
            weight=CPI_SOURCES["cleveland_fed"].weight,
            uncertainty_std=CPI_SOURCES["cleveland_fed"].historical_std,
            weather_analog="AIFS",
        )
        if nowcast_mom > 0:
            cleveland.point_estimate = nowcast_mom
            cleveland.data_available = True
            cleveland.last_updated = "manual"
        sources.append(cleveland)

        # 2-4. FRED sources (parallel)
        if fred_key:
            tips_task = fetch_tips_breakevens(session, fred_key)
            umich_task = fetch_umich_expectations(session, fred_key)
            energy_task = fetch_energy_indicators(session, fred_key)
            bls_task = fetch_bls_cpi_history(session)
            core_bls_task = fetch_core_bls_history(session)

            tips_est, umich_est, energy, bls_history, core_bls_history = await asyncio.gather(
                tips_task, umich_task, energy_task, bls_task, core_bls_task
            )
            sources.append(tips_est)
            sources.append(umich_est)
        else:
            energy = {"gas_current": 0, "gas_prior": 0, "gas_change_pct": 0,
                       "oil_current": 0, "oil_prior": 0, "oil_change_pct": 0,
                       "energy_signal": "unknown"}
            bls_history = []
            core_bls_history = []

        # NY Fed and SPF placeholders (Phase 2)
        nyfed = CPISourceEstimate(
            name="nyfed",
            display_name="NY Fed Consumer Survey",
            weight=CPI_SOURCES["nyfed"].weight,
            uncertainty_std=CPI_SOURCES["nyfed"].historical_std,
            weather_analog="ICON",
        )
        sources.append(nyfed)

        spf = CPISourceEstimate(
            name="spf",
            display_name="Philly Fed SPF",
            weight=CPI_SOURCES["spf"].weight,
            uncertainty_std=CPI_SOURCES["spf"].historical_std,
            weather_analog="GEM",
        )
        sources.append(spf)

        # BLS Historical Prior — anchors ensemble to base rates
        # data_month computed later (line ~1277), use release_date.month - 1
        target_month = release_date.month - 1 if release_date.month > 1 else 12
        bls_prior = build_bls_prior_source(bls_history, target_month=target_month)
        if bls_prior.data_available:
            sources.append(bls_prior)

        # Consensus
        consensus = ConsensusData()
        if consensus_mom > 0:
            consensus.consensus_mom = consensus_mom
            consensus.consensus_yoy = consensus_yoy
            consensus.consensus_source = "manual"
            consensus.data_available = True

        # Add energy to consensus
        consensus.gas_current = energy.get("gas_current", 0)
        consensus.gas_change_pct = energy.get("gas_change_pct", 0)
        consensus.oil_current = energy.get("oil_current", 0)
        consensus.oil_change_pct = energy.get("oil_change_pct", 0)
        consensus.energy_signal = energy.get("energy_signal", "unknown")

        # Prior month actual from BLS
        if bls_history and len(bls_history) >= 2:
            consensus.prior_month_actual_mom = bls_history[0].get("mom_change", 0)

        # Build synthetic ensemble
        # Use release date as seed for reproducibility
        seed = int(release_date.strftime("%Y%m%d"))
        ensemble = build_cpi_ensemble(sources, seed=seed)

        print(f"  Ensemble: {ensemble.total_count} synthetic members from "
              f"{ensemble.sources_available} sources")
        print(f"  Mean: {ensemble.mean:.3f}%  ±{ensemble.std:.3f}%")

        # Determine data month for seasonal analysis
        # The data month is the month BEFORE the release month
        data_month = release_date.month - 1 if release_date.month > 1 else 12

        # Scan each market series
        all_opps = []
        series_to_scan = ["mom", "yoy", "core_yoy"]
        if series_filter:
            series_to_scan = [s for s in series_to_scan if s == series_filter]

        for series_key in series_to_scan:
            mkt_cfg = CPI_MARKETS.get(series_key)
            if not mkt_cfg:
                continue

            print(f"\n  Scanning {mkt_cfg.display_name} ({mkt_cfg.series_ticker})...")
            brackets = await fetch_kalshi_cpi_brackets(session, mkt_cfg.series_ticker)

            if not brackets:
                print(f"    No open markets for {mkt_cfg.series_ticker}")
                continue

            # Determine which ensemble to use for this series
            if series_key == "mom":
                scan_ensemble = ensemble
            elif series_key == "yoy":
                # Convert MoM ensemble to YoY using CPI index
                scan_ensemble = build_yoy_ensemble(ensemble, bls_history)
                if not scan_ensemble.all_members:
                    print(f"    Found {len(brackets)} brackets (YoY conversion failed — need ≥13mo BLS history)")
                    continue
                print(f"    YoY ensemble: mean={scan_ensemble.mean:.2f}%, "
                      f"std={scan_ensemble.std:.3f}%, {scan_ensemble.total_count} members")
            elif series_key == "core_yoy":
                # Convert MoM ensemble to Core YoY using Core CPI index
                scan_ensemble = build_yoy_ensemble(ensemble, core_bls_history,
                                                    bls_series_id="core_cpi_sa")
                if not scan_ensemble.all_members:
                    print(f"    Found {len(brackets)} brackets (Core YoY conversion failed — need ≥13mo Core BLS history)")
                    continue
                print(f"    Core YoY ensemble: mean={scan_ensemble.mean:.2f}%, "
                      f"std={scan_ensemble.std:.3f}%, {scan_ensemble.total_count} members")
            else:
                continue

            opps = analyze_cpi_opportunities(
                scan_ensemble, consensus, energy, brackets,
                balance, days_to_release, data_month, series_key,
            )
            all_opps.extend(opps)

            print_cpi_report(
                scan_ensemble, consensus, energy, opps,
                mkt_cfg, days_to_release,
                release_date.isoformat(), data_month_str,
            )

    # Summary
    print_cpi_summary(all_opps, balance, release_date.isoformat(), days_to_release)

    return all_opps


# ─── CLI ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="CPI Edge Scanner v1.0")
    parser.add_argument("--series", choices=["mom", "yoy", "core_yoy"],
                        help="Scan specific series only")
    parser.add_argument("--no-prompt", action="store_true",
                        help="Skip manual input prompts (FRED-only mode)")
    parser.add_argument("--force", action="store_true",
                        help="Force scan even outside T-7 window")
    parser.add_argument("--schedule", action="store_true",
                        help="Show CPI release schedule and exit")
    args = parser.parse_args()

    if args.schedule:
        print("\n  2026 CPI Release Schedule (all at 8:30 AM ET)")
        print("  " + "─" * 40)
        today = date.today()
        for rd in CPI_RELEASE_DATES_2026:
            days = (rd - today).days
            marker = " ← NEXT" if days >= 0 and all(
                (r - today).days < 0 or r >= rd for r in CPI_RELEASE_DATES_2026
            ) else ""
            status = f"(T-{days}){marker}" if days >= 0 else "(past)"
            print(f"  {rd.isoformat()}  {status}")
        print()
        return

    if args.force:
        # Override scan window
        import cpi_config
        cpi_config.CPI_SCAN_WINDOW_DAYS = 365

    asyncio.run(scan(series_filter=args.series, no_prompt=args.no_prompt))


if __name__ == "__main__":
    main()
