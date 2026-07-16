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

# The 2026-07-11 standing entry cap: 20¢ max suggested buy — doubles as
# the already-repriced filter (a book above it never alerts or stages).
MAX_ENTRY_ASK_C = 20

# Fixed caps — the pre-registered ceilings ("the caps stay", claude.md §4;
# capital step-up is EARNED at the Sept gate, never automatic).
DEFAULT_MAX_NOTIONAL = 50.0   # $ worst-case collateral per order
DEFAULT_NIGHT_CAP = 25.0      # $ per station-night ≈ 15% of the 07-2026 bankroll


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
