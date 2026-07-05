"""Kalshi temperature-bracket semantics, shared by every ladder consumer.

Bracket bounds parse from the market SUBTITLE ("98° or below", "99° to
100°", "107° or above") — strike-field semantics differ between B- and
T-tickers, subtitles don't. Deadness is judged against a certain settle
bound from core.obs (highs: CLI max can only be >= the bound; lows: CLI
min can only be <= it).

Extracted from dead_bracket_sweeper 2026-07 when the CLI sniper became a
second consumer.
"""
from __future__ import annotations

import re

_SUB_BELOW = re.compile(r"^(-?\d+)° or below$")
_SUB_RANGE = re.compile(r"^(-?\d+)° to (-?\d+)°$")
_SUB_ABOVE = re.compile(r"^(-?\d+)° or above$")


def parse_subtitle(subtitle: str | None) -> tuple[float | None, float | None] | None:
    """Inclusive (lo, hi) bounds from a Kalshi bracket subtitle; None ends open."""
    if not subtitle:
        return None
    sub = subtitle.strip()
    if m := _SUB_BELOW.match(sub):
        return None, float(m.group(1))
    if m := _SUB_RANGE.match(sub):
        return float(m.group(1)), float(m.group(2))
    if m := _SUB_ABOVE.match(sub):
        return float(m.group(1)), None
    return None


def is_dead(kind: str, lo: float | None, hi: float | None, certain: int) -> bool:
    """Can this bracket no longer win, given the certain settle bound?"""
    if kind == "high":
        return hi is not None and hi < certain
    return lo is not None and lo > certain


def contains(lo: float | None, hi: float | None, value: float) -> bool:
    """Does the bracket's inclusive range contain the value?"""
    return (lo is None or lo <= value) and (hi is None or value <= hi)
