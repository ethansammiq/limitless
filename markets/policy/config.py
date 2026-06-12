"""Policy scanner configuration: Kalshi series whitelist + small utilities.

Thresholds (max volume, freshness, divergence, entry-price cap) live in the
top-level config.py so they're env-overridable alongside the weather params.
"""

from __future__ import annotations

# Approximate series-ticker prefixes for policy markets on Kalshi.
# Kalshi occasionally adds new series — maintainers should check live listings
# monthly. Unrecognized prefixes are silently skipped (safe default: pass).
POLICY_SERIES_PREFIXES: tuple[str, ...] = (
    "KXCONFIRM",     # Senate confirmations (nominees, cabinet, judges)
    "KXSENCONFIRM",
    "KXBILL",        # Bill passage
    "KXVOTE",        # Roll-call votes
    "KXNOM",         # Nominations
    "KXHOUSE",       # House-specific markets
    "KXSHUTDOWN",    # Government shutdown
    "KXFEDREG",      # Federal Register rules
    "KXRULE",        # Rulemaking
    "KXSPENDING",    # Appropriations
    "KXDEBTCEIL",    # Debt ceiling
    "KXTARIFF",      # Tariff decisions
    "KXAPPROVE",     # Approval-rating markets (weekly)
    "KXSIGNS",       # "Will the President sign X?"
    "KXPASS",        # "Will X pass?"
)


def is_policy_market(ticker: str) -> bool:
    """Return True if the ticker's series prefix matches a policy market."""
    if not ticker:
        return False
    head = ticker.split("-", 1)[0].upper()
    return any(head.startswith(p) for p in POLICY_SERIES_PREFIXES)
