"""Fee math for the live stack (ported from test_dutch_book/test_proxy_arb
when the KDE stack was retired, 2026-07-06)."""
import math

import pytest

from core.fees import kalshi_taker_fee_cents


@pytest.mark.parametrize("p,expected", [
    (1, 1), (11, 1), (30, 2), (50, 2), (60, 2), (89, 1), (99, 1),
])
def test_known_values(p, expected):
    assert kalshi_taker_fee_cents(p) == expected


def test_matches_ceil_formula_everywhere():
    for p in range(1, 100):
        assert kalshi_taker_fee_cents(p) == math.ceil(0.07 * p * (100 - p) / 100)


def test_symmetry():
    for p in range(1, 100):
        assert kalshi_taker_fee_cents(p) == kalshi_taker_fee_cents(100 - p)


def test_out_of_range_clamps_to_zero():
    assert kalshi_taker_fee_cents(0) == 0
    assert kalshi_taker_fee_cents(100) == 0
    assert kalshi_taker_fee_cents(-5) == 0
    assert kalshi_taker_fee_cents(150) == 0
