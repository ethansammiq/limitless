"""Risk constants and money math — the single source of truth.

Every dollar-shaped decision (worst-case collateral, size clamps, the
standing entry cap) lives here so the staging clamp, take.py's backstop,
and the approver's ledgers can never silently diverge (pre-2026-07: the
20¢ cap and the collateral math each existed in two files, the $50 cap
in four).

Imports stdlib + the ladders registry only — cron daemons, scripts/take.py
and the backtests all sit above this module.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

# The 2026-07-11 standing entry cap: 20¢ max suggested buy — doubles as
# the already-repriced filter (a book above it never alerts or stages).
MAX_ENTRY_ASK_C = 20

# Fixed caps — the pre-registered ceilings ("the caps stay", claude.md §4;
# capital step-up is EARNED at the Sept gate, never automatic). The
# bankroll-derived caps below can only TIGHTEN under these, never exceed
# them; every failure mode degrades here.
DEFAULT_MAX_NOTIONAL = 50.0   # $ worst-case collateral per order
DEFAULT_NIGHT_CAP = 25.0      # $ per station-night ≈ 15% of the 07-2026 bankroll

# Bankroll-relative fractions (of CASH balance, which excludes deployed
# collateral — caps shrink as money deploys intra-day, conservative by
# construction). Sizing, not winrate, is the ruin lever: on 2026-07-14 a
# fixed-dollar button offered 34% of the bankroll.
PER_ORDER_PCT = 0.30
NIGHT_CAP_PCT = 0.15

# live_watch.py (VPS cron */10) owns this snapshot and SKIPS writes on
# degraded reads — staleness, not a false $0, is its failure signature.
# On the Mac the copy is weeks stale, so dev-machine runs use the fixed
# caps. This module only ever reads it.
BANKROLL_SNAPSHOT = Path(__file__).resolve().parents[1] / "logs" / "live_account.json"
BANKROLL_MAX_AGE_MIN = 60     # tolerate a few missed */10 runs, no more


def bankroll_dollars(now_utc: datetime | None = None) -> float | None:
    """Cash balance from live_watch's snapshot; None when the file is
    missing, unparseable, non-positive, or staler than BANKROLL_MAX_AGE_MIN.
    Never raises, never fetches."""
    now_utc = now_utc or datetime.now(timezone.utc)
    try:
        snap = json.loads(BANKROLL_SNAPSHOT.read_text())
        balance = snap["balance"]
        updated = datetime.fromisoformat(snap["updated"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if isinstance(balance, bool) or not isinstance(balance, (int, float)):
        return None
    if balance <= 0 or updated.tzinfo is None:
        return None
    if (now_utc - updated).total_seconds() > BANKROLL_MAX_AGE_MIN * 60:
        return None
    return float(balance)


def _cap_detail(env_key: str, fixed: float, pct: float,
                now_utc: datetime | None = None) -> tuple[float, str]:
    """(dollars, log-ready provenance) for one cap.

    env parses → the env value (the documented human escape hatch — the
    only path that can exceed the fixed cap); no fresh bankroll → fixed;
    else min(fixed, pct·bankroll). A garbage env value falls through to
    the derivation, which is tighter-or-equal to the old fixed fallback."""
    env = os.getenv(env_key)
    if env is not None:
        try:
            return float(env), f"${float(env):.2f} ({env_key} env)"
        except ValueError:
            pass
    bank = bankroll_dollars(now_utc)
    if bank is None:
        return fixed, f"${fixed:.2f} fixed (no fresh bankroll snapshot)"
    derived = pct * bank
    if derived >= fixed:
        return fixed, (f"${fixed:.2f} fixed ceiling "
                       f"({pct:.0%} of ${bank:.2f} = ${derived:.2f})")
    return derived, f"${derived:.2f} = {pct:.0%} of ${bank:.2f} bankroll"


def max_notional_detail(now_utc: datetime | None = None) -> tuple[float, str]:
    return _cap_detail("TAKE_MAX_NOTIONAL", DEFAULT_MAX_NOTIONAL,
                       PER_ORDER_PCT, now_utc)


def max_notional_dollars(now_utc: datetime | None = None) -> float:
    return max_notional_detail(now_utc)[0]


def night_cap_detail(now_utc: datetime | None = None) -> tuple[float, str]:
    return _cap_detail("TAKE_NIGHT_CAP_DOLLARS", DEFAULT_NIGHT_CAP,
                       NIGHT_CAP_PCT, now_utc)


def night_cap_dollars(now_utc: datetime | None = None) -> float:
    return night_cap_detail(now_utc)[0]


def order_cost_dollars(action: str, side: str, count: int, price_c: int) -> float:
    """Worst-case collateral: buys cost price; sells of YES you hold cost 0
    but selling short / buying NO collateralizes the complement. Be
    conservative and cap on the larger leg."""
    leg = price_c if action == "buy" else 100 - price_c
    return count * leg / 100


def clamp_count(action: str, side: str, count: int, price_c: int,
                cap_dollars: float) -> int:
    """Largest count ≤ `count` whose worst-case collateral fits the cap.

    Same money math as take.py's validate() — the alert sizes to full book
    depth (60k×1¢ observed 2026-07-12), the staged order sizes to the cap.
    take.py re-validates as the final backstop.
    """
    per_contract = order_cost_dollars(action, side, 1, price_c)
    if per_contract <= 0:
        return 0
    return min(count, int(cap_dollars / per_contract))


_SERIES_TO_AWIPS: dict[str, str] | None = None


def _series_to_awips() -> dict[str, str]:
    """series → AWIPS station, memoized from the committed ladders registry.

    A failed load returns {} WITHOUT caching (the next call retries) — the
    caller falls back to the v1 key, which only ever tightens nothing."""
    global _SERIES_TO_AWIPS
    if _SERIES_TO_AWIPS is None:
        try:
            from ladders import load_ladders
            _SERIES_TO_AWIPS = {lad.series: lad.awips for lad in load_ladders()}
        except (OSError, ValueError, TypeError, KeyError):
            return {}
    return _SERIES_TO_AWIPS


def station_night_key(ticker: str) -> str:
    """KXHIGHNY-26JUL14-T90 and KXLOWTNYC-26JUL14-B70.5 → 'NYC:26JUL14' —
    the STATION-night, matching the scorecard's cluster-bootstrap unit
    (awips, summary_date). Series names are irregular (KXHIGHCHI/KXLOWTCHI
    → MDW, KXHIGHTDAL/KXLOWTDAL → DFW), so the mapping goes through the
    ladders registry, never string munging.

    Unknown series or malformed ticker → the v1 series-date key
    (ticker.rsplit('-', 1)[0]): today's behavior, never looser, and this
    function never raises inside staging."""
    parts = ticker.rsplit("-", 2)
    if len(parts) == 3:
        awips = _series_to_awips().get(parts[0])
        if awips:
            return f"{awips}:{parts[1]}"
    return ticker.rsplit("-", 1)[0]
