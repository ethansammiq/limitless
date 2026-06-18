"""
DUTCH BOOK DETECTOR — bracket-sum arbitrage on exhaustive Kalshi ladders.

A daily-high event's brackets partition the outcome space: exactly one
bracket settles YES. When the quoted prices across ALL legs sum past 100¢
(after fees), a riskless basket exists:

  NO basket:  sum(yes_bid) > 100 + total_fees
              Buy NO on every leg at (100 - yes_bid). Exactly one leg
              loses (the winning bracket); every other NO pays 100¢.
              Profit per set = sum(yes_bid) - 100 - total_fees.

  YES basket: sum(yes_ask) < 100 - total_fees
              Buy YES on every leg at yes_ask. Exactly one leg pays 100¢.
              Profit per set = 100 - sum(yes_ask) - total_fees.

Preconditions checked per event before any sum is trusted:
  - Exhaustive ladder: exactly one low tail and one high tail, with
    contiguous interior ranges (no gaps, no overlaps).
  - Every leg quoted two-sided (yes_bid >= 1 and 1 <= yes_ask <= 99).

Detection reuses the bracket payload run_scan already fetched — zero
extra API calls. Findings are surfaced as high-priority alerts ONLY;
they are never auto-executed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

from log_setup import get_logger

logger = get_logger(__name__)


def kalshi_taker_fee_cents(price_cents: int) -> int:
    """Per-contract taker fee in cents at execution price P, rounded UP.

    Baskets must cross the spread on every leg to fill simultaneously,
    so taker fees apply: ceil(0.07 * P * (100 - P) / 100).
    """
    return math.ceil(0.07 * price_cents * (100 - price_cents) / 100)


@dataclass
class DutchBookLeg:
    """One leg of a Dutch-book basket."""
    ticker: str
    title: str
    side: str               # "yes" or "no"
    price_cents: int        # execution price for this leg
    fee_cents: int          # taker fee at that price


@dataclass
class DutchBookArb:
    """A riskless bracket-sum arbitrage across one event's full ladder."""
    event_ticker: str
    side: str               # basket direction: "no" or "yes"
    legs: list[DutchBookLeg]
    sum_cents: int          # sum(yes_bid) for NO basket, sum(yes_ask) for YES
    total_fee_cents: int
    profit_cents: int       # guaranteed profit per set (one contract per leg)


def _event_key(mkt: dict) -> str:
    """Group key for one settlement event (a Dutch book only spans one event)."""
    event = mkt.get("event_ticker", "")
    if event:
        return event
    ticker = mkt.get("ticker", "")
    return ticker.rsplit("-", 1)[0] if "-" in ticker else ticker


def _leg_title(mkt: dict) -> str:
    return mkt.get("title", "") or mkt.get("subtitle", "")


def _ladder_legs(markets: list[dict]) -> list[dict] | None:
    """Validate that an event's markets form an exhaustive two-sided ladder.

    Returns the markets sorted by bracket position, or None when any
    precondition fails (missing/duplicate tail, gap or overlap,
    unparseable title, one-sided quote). Failing closed here is what
    prevents false-positive "arbs" on partial ladders.
    """
    # Deferred import: edge_scanner_v2 is heavy (numpy, calibration init)
    # and auto_scan already has it loaded.
    from edge_scanner_v2 import parse_bracket_range

    if len(markets) < 2:
        return None

    parsed: list[tuple[float, float, dict]] = []
    low_tails = 0
    high_tails = 0
    for mkt in markets:
        low, high, kind = parse_bracket_range(_leg_title(mkt))
        if kind == "unknown":
            return None

        yes_bid = int(mkt.get("yes_bid", 0) or 0)
        yes_ask = int(mkt.get("yes_ask", 0) or 0)
        if yes_bid < 1 or not 1 <= yes_ask <= 99:
            return None  # one-sided quote — the ladder sum is not executable

        if kind == "low_tail":
            low_tails += 1
            # "61 or below" covers integers <= 61 → half-open upper bound 62,
            # matching the [low, high+1) convention of "range" parses.
            cov_low, cov_high = -math.inf, high + 1
        elif kind == "high_tail":
            high_tails += 1
            cov_low, cov_high = low, math.inf
        else:
            cov_low, cov_high = low, high  # parse already returns [low, high+1)
        parsed.append((cov_low, cov_high, mkt))

    if low_tails != 1 or high_tails != 1:
        return None

    parsed.sort(key=lambda t: (t[0], t[1]))
    for (_, prev_high, _), (curr_low, _, _) in zip(parsed, parsed[1:]):
        if prev_high != curr_low:
            return None  # gap or overlap — not exhaustive
    return [mkt for _, _, mkt in parsed]


def check_dutch_book(
    brackets: list[dict],
    fee_fn: Callable[[int], int] = kalshi_taker_fee_cents,
) -> list[DutchBookArb]:
    """Scan raw Kalshi market dicts for bracket-sum Dutch books.

    brackets may span multiple events (today's and tomorrow's markets come
    back in one /markets call); legs are grouped per event before the
    ladder preconditions and basket sums are evaluated.
    """
    arbs: list[DutchBookArb] = []
    if not brackets:
        return arbs

    events: dict[str, list[dict]] = {}
    for mkt in brackets:
        events.setdefault(_event_key(mkt), []).append(mkt)

    for event_ticker, markets in events.items():
        ladder = _ladder_legs(markets)
        if ladder is None:
            continue

        # ── NO basket: buy NO every leg at (100 - yes_bid) ──
        sum_bid = 0
        no_fees = 0
        no_legs: list[DutchBookLeg] = []
        for mkt in ladder:
            yes_bid = int(mkt.get("yes_bid", 0) or 0)
            no_price = 100 - yes_bid
            fee = fee_fn(no_price)
            sum_bid += yes_bid
            no_fees += fee
            no_legs.append(DutchBookLeg(
                ticker=mkt.get("ticker", ""),
                title=_leg_title(mkt),
                side="no",
                price_cents=no_price,
                fee_cents=fee,
            ))
        no_profit = sum_bid - 100 - no_fees
        if no_profit > 0:
            arbs.append(DutchBookArb(
                event_ticker=event_ticker,
                side="no",
                legs=no_legs,
                sum_cents=sum_bid,
                total_fee_cents=no_fees,
                profit_cents=no_profit,
            ))

        # ── YES basket: buy YES every leg at yes_ask ──
        sum_ask = 0
        yes_fees = 0
        yes_legs: list[DutchBookLeg] = []
        for mkt in ladder:
            yes_ask = int(mkt.get("yes_ask", 0) or 0)
            fee = fee_fn(yes_ask)
            sum_ask += yes_ask
            yes_fees += fee
            yes_legs.append(DutchBookLeg(
                ticker=mkt.get("ticker", ""),
                title=_leg_title(mkt),
                side="yes",
                price_cents=yes_ask,
                fee_cents=fee,
            ))
        yes_profit = 100 - sum_ask - yes_fees
        if yes_profit > 0:
            arbs.append(DutchBookArb(
                event_ticker=event_ticker,
                side="yes",
                legs=yes_legs,
                sum_cents=sum_ask,
                total_fee_cents=yes_fees,
                profit_cents=yes_profit,
            ))

    return arbs


def format_dutch_book_alerts(arbs: list[DutchBookArb]) -> str:
    """Human-readable Discord text: exact legs, prices, fees, profit."""
    if not arbs:
        return ""
    lines = []
    for arb in arbs:
        basket = "BUY NO every leg" if arb.side == "no" else "BUY YES every leg"
        sum_label = "Σ yes_bid" if arb.side == "no" else "Σ yes_ask"
        lines.append(
            f"**{arb.event_ticker}** — {basket} "
            f"({sum_label}={arb.sum_cents}¢, fees={arb.total_fee_cents}¢) → "
            f"**+{arb.profit_cents}¢ per set, riskless**"
        )
        for leg in arb.legs:
            lines.append(
                f"  `{leg.ticker}` {leg.side.upper()} @ {leg.price_cents}¢ "
                f"(fee {leg.fee_cents}¢)"
            )
        lines.append("")
    lines.append("⚠️ Alert only — NOT auto-executed. Verify quotes before placing legs.")
    return "\n".join(lines)
