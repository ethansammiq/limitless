"""
STALE PRICE DETECTOR — Ensemble shift tracking between scans.

Compares the current scan's ensemble mean per city against the previous
scan.  If the ensemble has shifted by ≥ STALE_PRICE_MIN_SHIFT_F, each
bracket's warranted repricing is estimated from the normal-approximated
ensemble (mean/std of both scans).  A bracket whose bid fell short of that
warranted move by ≥ STALE_PRICE_MIN_GAP_CENTS in the expected direction is
a stale-price opportunity.  Direction is per bracket: on a warmer shift,
brackets above the moving mean should get more expensive and brackets
below it cheaper (and vice versa for cooler shifts).

State persistence:
  Saves {city: {mean, bracket_bids}} after each scan to a JSON file.
  Next scan loads the previous state and computes deltas.

Usage:
  Called from auto_scan.py after each city scan completes.
  Returns a list of StaleAlert objects for Discord notification.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import NormalDist
from zoneinfo import ZoneInfo

from config import (
    STALE_PRICE_ENABLED,
    STALE_PRICE_MIN_SHIFT_F,
    STALE_PRICE_MIN_GAP_CENTS,
    STALE_PRICE_STATE_FILE,
)
from log_setup import get_logger

logger = get_logger(__name__)

ET = ZoneInfo("America/New_York")
PROJECT_ROOT = Path(__file__).resolve().parent
STATE_PATH = PROJECT_ROOT / STALE_PRICE_STATE_FILE

# Used when a snapshot carries no usable ensemble spread (e.g. legacy state
# files where std defaulted to 0) — a typical daily-high ensemble σ.
_FALLBACK_STD_F = 2.0


@dataclass
class StaleAlert:
    """A single stale-price detection."""
    city: str
    direction: str              # "warmer" or "cooler"
    mean_shift_f: float         # How far the ensemble moved (°F)
    prev_mean: float
    curr_mean: float
    bracket_title: str          # The bracket that should have repriced
    ticker: str
    expected_bid_change: int    # Signed model-implied bid change (¢); negative = should have gotten cheaper
    actual_bid: int             # Current market bid
    prev_bid: int               # Previous scan's bid


@dataclass
class ScanSnapshot:
    """Per-city snapshot from one scan."""
    mean: float
    std: float
    timestamp: str              # ISO timestamp
    bracket_bids: dict          # {ticker: {"bid": int, "title": str}}

    def to_dict(self) -> dict:
        return {
            "mean": self.mean,
            "std": self.std,
            "timestamp": self.timestamp,
            "bracket_bids": self.bracket_bids,
        }

    @classmethod
    def from_dict(cls, d: dict) -> ScanSnapshot:
        return cls(
            mean=d.get("mean", 0),
            std=d.get("std", 0),
            timestamp=d.get("timestamp", ""),
            bracket_bids=d.get("bracket_bids", {}),
        )


def load_previous_state() -> dict[str, ScanSnapshot]:
    """Load previous scan snapshots from disk."""
    if not STATE_PATH.exists():
        return {}
    try:
        with open(STATE_PATH) as f:
            data = json.load(f)
        return {k: ScanSnapshot.from_dict(v) for k, v in data.items()}
    except (json.JSONDecodeError, KeyError) as e:
        logger.warning("Stale price state corrupt, resetting: %s", e)
        return {}


def save_current_state(states: dict[str, ScanSnapshot]) -> None:
    """Save current scan state for next comparison."""
    data = {k: v.to_dict() for k, v in states.items()}
    tmp = STATE_PATH.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    tmp.rename(STATE_PATH)


def build_snapshot(
    city_key: str,
    ensemble_mean: float,
    ensemble_std: float,
    brackets: list[dict],
) -> ScanSnapshot:
    """Build a snapshot from current scan data.

    Parameters
    ----------
    brackets : list[dict]
        Raw Kalshi market dicts with 'ticker', 'title', 'yes_bid'.
    """
    bracket_bids = {}
    for mkt in brackets:
        ticker = mkt.get("ticker", "")
        if not ticker:
            continue
        bracket_bids[ticker] = {
            "bid": mkt.get("yes_bid", 0),
            "title": mkt.get("title", "") or mkt.get("subtitle", ""),
        }
    return ScanSnapshot(
        mean=ensemble_mean,
        std=ensemble_std,
        timestamp=datetime.now(ET).isoformat(),
        bracket_bids=bracket_bids,
    )


def _bracket_probability(low: float, high: float, mean: float, std: float) -> float:
    """P(daily high lands in the bracket) under a normal ensemble approximation.

    parse_bracket_range encodes tails with ±999 sentinels; the CDF saturates
    to 0/1 there, so no special-casing is needed.
    """
    dist = NormalDist(mean, std)
    return max(0.0, dist.cdf(high) - dist.cdf(low))


def _expected_repricing_cents(
    low: float,
    high: float,
    previous: ScanSnapshot,
    current: ScanSnapshot,
) -> int:
    """Signed model-implied bid change (¢) for one bracket between two scans.

    Positive → the bracket should have gotten more expensive, negative →
    cheaper.  The sign falls out of the bracket's position relative to the
    moving ensemble mean, so a below-mean bracket on a warmer shift correctly
    expects a drop.
    """
    prev_std = previous.std if previous.std > 0 else _FALLBACK_STD_F
    curr_std = current.std if current.std > 0 else _FALLBACK_STD_F
    p_prev = _bracket_probability(low, high, previous.mean, prev_std)
    p_curr = _bracket_probability(low, high, current.mean, curr_std)
    return round((p_curr - p_prev) * 100)


def detect_stale_prices(
    city_key: str,
    current: ScanSnapshot,
    previous: ScanSnapshot | None,
) -> list[StaleAlert]:
    """Compare current vs previous scan to find stale-priced brackets.

    Returns list of StaleAlert for brackets where:
    1. Ensemble mean shifted by >= STALE_PRICE_MIN_SHIFT_F
    2. The shift warranted a repricing of this bracket by
       >= STALE_PRICE_MIN_GAP_CENTS (model-implied, position-aware)
    3. The bid fell short of that warranted move by
       >= STALE_PRICE_MIN_GAP_CENTS in the expected direction
    """
    if not STALE_PRICE_ENABLED:
        return []
    if previous is None:
        return []
    if current.mean == 0 or previous.mean == 0:
        return []

    mean_shift = current.mean - previous.mean
    abs_shift = abs(mean_shift)

    if abs_shift < STALE_PRICE_MIN_SHIFT_F:
        return []

    # Deferred import: edge_scanner_v2 is heavy (numpy, calibration init) and
    # this module is otherwise a light leaf; auto_scan already has it loaded.
    from edge_scanner_v2 import parse_bracket_range

    direction = "warmer" if mean_shift > 0 else "cooler"
    alerts = []

    # For each bracket in current scan, check if its bid moved appropriately
    for ticker, curr_data in current.bracket_bids.items():
        prev_data = previous.bracket_bids.get(ticker)
        if not prev_data:
            continue

        curr_bid = curr_data.get("bid", 0)
        prev_bid = prev_data.get("bid", 0)

        if curr_bid == 0 or prev_bid == 0:
            continue
        # Only flag brackets priced mid-range (15-85¢) where there's actual
        # trading opportunity
        if not (15 <= curr_bid <= 85):
            continue

        title = curr_data.get("title") or prev_data.get("title") or ""
        low, high, kind = parse_bracket_range(title)
        if kind == "unknown":
            continue

        expected_change = _expected_repricing_cents(low, high, previous, current)
        if abs(expected_change) < STALE_PRICE_MIN_GAP_CENTS:
            # The shift didn't warrant a meaningful repricing of THIS bracket
            # (it sits far from both means) — an unmoved bid is not stale.
            continue

        bid_change = curr_bid - prev_bid

        # Stale = bid fell short of the warranted move by >= MIN_GAP in the
        # expected direction.  A bracket that repriced commensurately (or
        # overshot, e.g. a below-mean bracket collapsing on a warmer shift)
        # is healthy regardless of shift size.
        if expected_change > 0:
            is_stale = bid_change <= expected_change - STALE_PRICE_MIN_GAP_CENTS
        else:
            is_stale = bid_change >= expected_change + STALE_PRICE_MIN_GAP_CENTS

        if not is_stale:
            continue

        alerts.append(StaleAlert(
            city=city_key,
            direction=direction,
            mean_shift_f=round(mean_shift, 1),
            prev_mean=round(previous.mean, 1),
            curr_mean=round(current.mean, 1),
            bracket_title=title or ticker,
            ticker=ticker,
            expected_bid_change=expected_change,
            actual_bid=curr_bid,
            prev_bid=prev_bid,
        ))

    return alerts


def format_stale_alerts(alerts: list[StaleAlert]) -> str:
    """Format stale alerts into a human-readable Discord message."""
    if not alerts:
        return ""
    from edge_scanner_v2 import shorten_bracket_title  # deferred: heavy module
    lines = [f"**📊 STALE PRICE ALERT — {len(alerts)} bracket(s)**\n"]
    for a in alerts[:5]:  # Cap at 5 per message
        shift_icon = "🔴" if a.direction == "warmer" else "🔵"
        short = shorten_bracket_title(a.bracket_title)
        lines.append(
            f"{shift_icon} **{a.city}** {short}: ensemble shifted {a.mean_shift_f:+.1f}°F "
            f"({a.prev_mean:.1f}→{a.curr_mean:.1f}) but bid only moved "
            f"{a.actual_bid - a.prev_bid:+d}¢ ({a.prev_bid}→{a.actual_bid}¢)\n"
            f"   Ticker: `{a.ticker}`"
        )
    return "\n".join(lines)
