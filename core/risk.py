"""Risk constants and money math — the single source of truth.

Every dollar-shaped decision (worst-case collateral, size clamps, the
standing entry cap) lives here so the staging clamp, take.py's backstop,
and the approver's ledgers can never silently diverge (pre-2026-07: the
20¢ cap and the collateral math each existed in two files, the $50 cap
in four).

Imports stdlib only — cron daemons, scripts/take.py and the backtests all
sit above this module.
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
