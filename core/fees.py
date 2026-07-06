"""Kalshi fee math shared by the live stack.

Moved from dutch_book.py when the KDE stack was retired (2026-07-06).
This is the ceil-to-int variant — the one the live sweeper/scorecard have
always used. (edge_scanner_v2 carried a round-to-float twin; it died with
the KDE stack.)
"""
from __future__ import annotations

import math


def kalshi_taker_fee_cents(price_cents: int) -> int:
    """Per-contract taker fee in cents at execution price P, rounded UP:
    ceil(0.07 * P * (100 - P) / 100). Zero outside the 1-99 book."""
    if price_cents <= 0 or price_cents >= 100:
        return 0
    return math.ceil(0.07 * price_cents * (100 - price_cents) / 100)
