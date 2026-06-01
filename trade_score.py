"""
Hybrid Trade Score — multi-dimensional decision engine for weather trading.

Replaces the simple confidence >= 90 gate with a weighted combination of:
  1. Confidence signal  (model agreement quality)
  2. Edge signal        (mispricing magnitude, sigmoid soft-cap)
  3. Urgency signal     (time-to-settlement pressure)
  4. Liquidity penalty  (volume + spread risk)

Weights blend continuously via sigmoid as settlement approaches:
  Early (far from settlement): confidence dominates
  Late  (near settlement):     edge + urgency dominate

Hard floors are NEVER relaxed: confidence >= 70, edge >= 10¢, KDE >= 20%.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from config import (
    MIN_CONFIDENCE_TO_TRADE,
    MIN_KDE_PROBABILITY,
    TRADE_SCORE_CONFIDENCE_FLOOR,
    TRADE_SCORE_ENABLED,
    TRADE_SCORE_ENTRY_PRICE_PENALTY_START,
    TRADE_SCORE_ENTRY_PRICE_PENALTY_RATE,
    TRADE_SCORE_LOW_VOLUME,
    TRADE_SCORE_MED_VOLUME,
    TRADE_SCORE_MIN_EDGE_CENTS,
    TRADE_SCORE_THRESHOLD,
    TRADE_SCORE_WIDE_SPREAD,
)


@dataclass
class TradeScore:
    """Result of hybrid trade score computation."""

    score: float                    # 0.0–1.0 composite score
    tradeable: bool                 # score >= threshold AND all floors pass

    # Component breakdown (for logging / calibration)
    confidence_signal: float        # 0.0–1.0
    edge_signal: float              # 0.0–1.0
    urgency_signal: float           # 0.0–1.0
    liquidity_penalty: float        # 0.0–~0.20
    entry_price_penalty: float      # 0.0–~0.12 (expensive entry discount)

    # Weights used (transparency)
    w_confidence: float
    w_edge: float
    w_urgency: float

    # Context
    hours_to_settlement: float
    reasons: list[str] = field(default_factory=list)
    floor_failures: list[str] = field(default_factory=list)


# ── Signal Functions ────────────────────────────────────────────────────────


def _confidence_signal(confidence_score: float) -> float:
    """Linear map: 70 → 0.0, 100 → 1.0, clamped."""
    return max(0.0, min(1.0, (confidence_score - 70.0) / 30.0))


def _edge_signal(edge_cents: float) -> float:
    """Sigmoid soft-cap: 0¢→0, 15¢→0.46, 30¢→0.76, 50¢→0.96."""
    if edge_cents <= 0:
        return 0.0
    return 2.0 / (1.0 + math.exp(-edge_cents / 15.0)) - 1.0


def _urgency_signal(hours_to_settlement: float) -> float:
    """Inverse sigmoid: high urgency near settlement, low far out.

    1h → 0.97, 4h → 0.88, 8h → 0.50, 14h → 0.05.
    """
    return 1.0 / (1.0 + math.exp(0.5 * (hours_to_settlement - 8.0)))


def _compute_weights(hours_to_settlement: float) -> tuple[float, float, float]:
    """Continuous sigmoid weight blending.

    Returns (w_confidence, w_edge, w_urgency).
    Invariant: w1 + w2 + w3 == 1.0 for all inputs.

    Early (far from settlement): confidence=0.60, edge=0.25, urgency=0.15
    Late  (near settlement):     confidence=0.25, edge=0.35, urgency=0.40
    """
    hours_elapsed = max(0.0, 24.0 - hours_to_settlement)
    # Sigmoid centered at 16h elapsed (= 8h remaining), steepness 0.3
    alpha = 1.0 / (1.0 + math.exp(-0.3 * (hours_elapsed - 16.0)))

    w1 = 0.60 - 0.35 * alpha   # confidence
    w2 = 0.25 + 0.10 * alpha   # edge
    w3 = 0.15 + 0.25 * alpha   # urgency
    return w1, w2, w3


def _liquidity_penalty(
    volume: int,
    spread: int,
    bid_depth: int = 0,
    ask_depth: int = 0,
    depth_grade: str = "",
    imbalance: float = 0.0,
    trade_side: str = "yes",
) -> float:
    """Penalize illiquid brackets that are hard to exit.

    Enhanced with order book depth data when available.
    """
    penalty = 0.0

    # Volume penalty
    if volume < TRADE_SCORE_LOW_VOLUME:
        penalty += 0.15
    elif volume < TRADE_SCORE_MED_VOLUME:
        penalty += 0.08

    # Spread penalty
    if spread > TRADE_SCORE_WIDE_SPREAD:
        penalty += 0.05
    elif spread > 3:
        penalty += 0.02

    # ── Depth-based penalties (additive, when data available) ──
    if bid_depth > 0 or ask_depth > 0:
        # Can't exit easily if bid side is thin
        if bid_depth < 50:
            penalty += 0.05

        # Hard to buy YES if ask side is thin
        if ask_depth < 50 and trade_side == "yes":
            penalty += 0.03

        # Grade D = illiquid overall
        if depth_grade == "D":
            penalty += 0.08

        # Imbalance opposing our direction (sellers stacking when we want YES)
        if trade_side == "yes" and imbalance < -0.3:
            penalty += 0.03
        elif trade_side == "no" and imbalance > 0.3:
            penalty += 0.03

    return penalty


def _entry_price_penalty(yes_bid: int) -> float:
    """Penalize expensive entries that have poor risk/reward asymmetry.

    At 5c: 0 penalty (19:1 payout ratio).
    At 26c: 0.024 penalty (3.8:1 payout).
    At 50c: 0.12 penalty (1:1 payout).
    """
    if yes_bid <= TRADE_SCORE_ENTRY_PRICE_PENALTY_START:
        return 0.0
    excess = yes_bid - TRADE_SCORE_ENTRY_PRICE_PENALTY_START
    return excess * TRADE_SCORE_ENTRY_PRICE_PENALTY_RATE


# ── Main Scoring Function ──────────────────────────────────────────────────


def compute_trade_score(
    opp,
    hours_to_settlement: float,
    threshold: float | None = None,
    depth: object | None = None,
) -> TradeScore:
    """Compute hybrid trade score for an opportunity.

    Parameters
    ----------
    opp : Opportunity-like object (duck-typed)
        Must have: confidence_score, edge_after_fees, volume,
        yes_bid, yes_ask, kde_prob.
    hours_to_settlement : float
        Hours until market settlement (~7 AM ET).
    threshold : float, optional
        Override for TRADE_SCORE_THRESHOLD. Defaults to config value.
    depth : OrderBookDepth-like, optional
        If provided, used for enhanced liquidity penalty. Must have:
        bid_depth, ask_depth, grade, imbalance.

    Returns
    -------
    TradeScore with score, tradeable flag, and full component breakdown.
    """
    thresh = threshold if threshold is not None else TRADE_SCORE_THRESHOLD
    edge_cents = opp.edge_after_fees * 100.0

    # ── Hard floors ──
    floor_failures: list[str] = []
    if opp.confidence_score < TRADE_SCORE_CONFIDENCE_FLOOR:
        floor_failures.append(
            f"confidence {opp.confidence_score:.0f} < floor {TRADE_SCORE_CONFIDENCE_FLOOR}"
        )
    if edge_cents < TRADE_SCORE_MIN_EDGE_CENTS:
        floor_failures.append(
            f"edge {edge_cents:.1f}¢ < floor {TRADE_SCORE_MIN_EDGE_CENTS}¢"
        )
    if opp.kde_prob < MIN_KDE_PROBABILITY:
        floor_failures.append(
            f"kde_prob {opp.kde_prob:.1%} < floor {MIN_KDE_PROBABILITY:.0%}"
        )

    # ── Signals ──
    conf_sig = _confidence_signal(opp.confidence_score)
    edge_sig = _edge_signal(edge_cents)
    urg_sig = _urgency_signal(hours_to_settlement)

    # ── Weights (continuous sigmoid blend) ──
    w1, w2, w3 = _compute_weights(hours_to_settlement)

    # ── Liquidity ──
    spread = opp.yes_ask - opp.yes_bid
    _depth_kwargs = {}
    if depth is not None:
        _depth_kwargs = {
            "bid_depth": getattr(depth, "bid_depth", 0),
            "ask_depth": getattr(depth, "ask_depth", 0),
            "depth_grade": getattr(depth, "grade", ""),
            "imbalance": getattr(depth, "imbalance", 0.0),
            "trade_side": getattr(opp, "side", "yes"),
        }
    liq_pen = _liquidity_penalty(opp.volume, spread, **_depth_kwargs)

    # ── Entry price ──
    entry_pen = _entry_price_penalty(opp.yes_bid)

    # ── Composite score ──
    raw_score = w1 * conf_sig + w2 * edge_sig + w3 * urg_sig
    score = max(0.0, raw_score - liq_pen - entry_pen)

    tradeable = score >= thresh and len(floor_failures) == 0

    # ── Reasons (human-readable) ──
    reasons = [
        f"conf={opp.confidence_score:.0f} → sig={conf_sig:.3f} (w={w1:.2f})",
        f"edge={edge_cents:.1f}¢ → sig={edge_sig:.3f} (w={w2:.2f})",
        f"urgency={hours_to_settlement:.1f}h → sig={urg_sig:.3f} (w={w3:.2f})",
        f"liquidity_penalty={liq_pen:.3f} (vol={opp.volume}, spread={spread})"
        + (f" [depth: bid={getattr(depth, 'bid_depth', 0)}+ask={getattr(depth, 'ask_depth', 0)}, grade={getattr(depth, 'grade', '?')}]" if depth else ""),
        f"entry_price_penalty={entry_pen:.3f} (bid={opp.yes_bid}¢)",
    ]
    if tradeable:
        reasons.append(f"SCORE={score:.3f} >= {thresh} → TRADEABLE")
    else:
        reasons.append(f"SCORE={score:.3f} < {thresh}")

    return TradeScore(
        score=score,
        tradeable=tradeable,
        confidence_signal=conf_sig,
        edge_signal=edge_sig,
        urgency_signal=urg_sig,
        liquidity_penalty=liq_pen,
        entry_price_penalty=entry_pen,
        w_confidence=w1,
        w_edge=w2,
        w_urgency=w3,
        hours_to_settlement=hours_to_settlement,
        reasons=reasons,
        floor_failures=floor_failures,
    )


def should_trade(opp, hours_to_settlement: float) -> bool:
    """Convenience: returns True if opportunity should be traded.

    Respects TRADE_SCORE_ENABLED feature flag.
    If disabled, falls back to old confidence >= 90 gate.
    """
    if not TRADE_SCORE_ENABLED:
        return opp.confidence_score >= MIN_CONFIDENCE_TO_TRADE
    ts = compute_trade_score(opp, hours_to_settlement)
    return ts.tradeable
