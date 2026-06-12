"""Policy market scanner.

Stages (in order):
  1. Fetch open markets from Kalshi via broker.get_markets()
  2. Pre-filter: policy series prefix, volume <= cap, hours_to_settle >= floor
  3. For each candidate, call Congress.gov adapter for a fresh primary source
  4. If a fresh doc is returned, call core.llm_synth for a calibrated probability
  5. Compute divergence from market-implied; gate on divergence + confidence tier
  6. Emit DocOpportunity instances; downstream CLI handles execution
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from log_setup import get_logger
from config import (
    POLICY_SCAN_MAX_VOLUME,
    POLICY_SCAN_MIN_HOURS_TO_SETTLE,
    POLICY_SCAN_FRESHNESS_DAYS,
    POLICY_DIVERGENCE_THRESHOLD_PP,
    POLICY_MIN_LLM_CONFIDENCE,
    POLICY_MAX_ENTRY_PRICE_CENTS,
)
from core.broker import BrokerInterface
from core.opportunity import DocOpportunity
from core.llm_synth import synthesize, SynthResult
from markets.policy.config import is_policy_market
from markets.policy.sources.congress_gov import CongressGovAdapter, DocBundle

logger = get_logger(__name__)
ET = ZoneInfo("America/New_York")

_CONFIDENCE_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}


@dataclass
class ScanStats:
    """Diagnostics for one scan pass — useful for logging and tuning."""
    total_markets: int = 0
    policy_series: int = 0
    passed_prefilter: int = 0
    with_fresh_doc: int = 0
    llm_calls: int = 0
    llm_failures: int = 0
    tradeable: int = 0
    near_misses: int = 0
    skipped_low_confidence: int = 0
    skipped_low_divergence: int = 0


async def scan(
    broker: BrokerInterface,
    adapter: Optional[CongressGovAdapter] = None,
    freshness_days: int = POLICY_SCAN_FRESHNESS_DAYS,
    bankroll: Optional[float] = None,
) -> tuple[list[DocOpportunity], ScanStats]:
    """Run one scan cycle.

    Caller owns broker lifecycle (start/stop). The Congress.gov adapter is
    created internally unless supplied.

    Returns:
        (opportunities_that_passed_all_gates, scan_statistics)
    """
    stats = ScanStats()
    now = datetime.now(ET)

    owns_adapter = adapter is None
    if owns_adapter:
        adapter = CongressGovAdapter()
        await adapter.start()

    try:
        markets = await broker.get_markets(status="open", limit=200)
        stats.total_markets = len(markets)
        logger.info("Fetched %d open markets from Kalshi", stats.total_markets)

        candidates = _prefilter_markets(markets, now, stats)
        stats.passed_prefilter = len(candidates)
        logger.info(
            "Prefilter: %d policy-series, %d passed (vol<=%d, hts>=%.0fh)",
            stats.policy_series, stats.passed_prefilter,
            POLICY_SCAN_MAX_VOLUME, POLICY_SCAN_MIN_HOURS_TO_SETTLE,
        )

        if bankroll is None:
            bankroll = await broker.get_balance()
        logger.info("Bankroll for sizing: $%.2f", bankroll)

        opportunities: list[DocOpportunity] = []
        for market, hts in candidates:
            bundle = await adapter.fetch_fresh_doc(market, freshness_days=freshness_days)
            if not bundle:
                continue
            stats.with_fresh_doc += 1

            synth = await _synthesize_for_market(market, bundle)
            stats.llm_calls += 1
            if not synth.success:
                stats.llm_failures += 1
                logger.warning("LLM synth failed for %s: %s", market.get("ticker"), synth.error)
                continue

            opp = _maybe_build_opportunity(market, bundle, synth, hts, bankroll, stats)
            if opp is not None:
                opportunities.append(opp)
                stats.tradeable += 1

        logger.info(
            "Scan: fresh_doc=%d llm_ok=%d llm_fail=%d tradeable=%d "
            "(low_conf=%d, low_div=%d)",
            stats.with_fresh_doc,
            stats.llm_calls - stats.llm_failures, stats.llm_failures,
            stats.tradeable,
            stats.skipped_low_confidence, stats.skipped_low_divergence,
        )
        return opportunities, stats

    finally:
        if owns_adapter:
            await adapter.stop()


# ── Pre-filter ──

def _prefilter_markets(
    markets: list[dict], now: datetime, stats: ScanStats,
) -> list[tuple[dict, float]]:
    out: list[tuple[dict, float]] = []
    for m in markets:
        ticker = m.get("ticker", "")
        if not is_policy_market(ticker):
            continue
        stats.policy_series += 1

        vol = _market_volume(m)
        if vol > POLICY_SCAN_MAX_VOLUME:
            continue

        hts = _hours_to_settle(m, now)
        if hts < POLICY_SCAN_MIN_HOURS_TO_SETTLE:
            continue

        # Early-reject if both sides are too expensive to enter
        yes_bid = int(m.get("yes_bid", 0) or 0)
        yes_ask = int(m.get("yes_ask", 100) or 100)
        no_equiv_price = 100 - yes_ask + 1
        if yes_bid > POLICY_MAX_ENTRY_PRICE_CENTS and no_equiv_price > POLICY_MAX_ENTRY_PRICE_CENTS:
            continue

        out.append((m, hts))
    return out


def _hours_to_settle(market: dict, now: datetime) -> float:
    ts = market.get("close_time") or market.get("expiration_time") or ""
    if not ts:
        return 0.0
    try:
        if ts.endswith("Z"):
            close_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        else:
            close_dt = datetime.fromisoformat(ts)
        if close_dt.tzinfo is None:
            close_dt = close_dt.replace(tzinfo=ET)
        return (close_dt - now).total_seconds() / 3600.0
    except (ValueError, TypeError):
        return 0.0


def _market_volume(market: dict) -> int:
    for key in ("volume_24h", "volume", "open_interest"):
        v = market.get(key)
        if isinstance(v, (int, float)) and v > 0:
            return int(v)
    return 0


# ── Synthesis + opportunity construction ──

async def _synthesize_for_market(market: dict, bundle: DocBundle) -> SynthResult:
    title = market.get("title", "") or market.get("subtitle", "")
    rules = (
        market.get("rules_primary")
        or market.get("rules")
        or market.get("settlement_source")
        or "See Kalshi market page for resolution criteria."
    )
    yes_bid = int(market.get("yes_bid", 0) or 0)
    implied = yes_bid / 100.0
    return await synthesize(
        market_question=title,
        resolution_criteria=rules,
        market_implied_prob=implied,
        doc_text=bundle.doc_text,
        doc_sources=bundle.source_urls,
    )


def _maybe_build_opportunity(
    market: dict,
    bundle: DocBundle,
    synth: SynthResult,
    hours_to_settle: float,
    bankroll: float,
    stats: ScanStats,
) -> Optional[DocOpportunity]:
    yes_bid = int(market.get("yes_bid", 0) or 0)
    yes_ask = int(market.get("yes_ask", 100) or 100)
    volume = _market_volume(market)

    yes_implied = yes_bid / 100.0 if yes_bid > 0 else 0.5
    no_implied = (100 - yes_ask) / 100.0 if yes_ask > 0 else 0.5

    # Pick the side with the larger positive edge
    yes_edge = synth.probability - yes_implied
    no_edge = (1.0 - synth.probability) - no_implied

    if yes_edge >= no_edge and yes_edge > 0:
        side = "yes"
        entry_price = min(yes_bid + 1, POLICY_MAX_ENTRY_PRICE_CENTS)
        edge_cents = int(round((synth.probability * 100) - entry_price))
    elif no_edge > 0:
        side = "no"
        entry_price = min((100 - yes_ask) + 1, POLICY_MAX_ENTRY_PRICE_CENTS)
        edge_cents = int(round(((1 - synth.probability) * 100) - entry_price))
    else:
        return None

    if edge_cents <= 0:
        return None

    # Divergence gate (in percentage points, one-sided)
    if side == "yes":
        divergence_pp = (synth.probability - yes_implied) * 100
    else:
        divergence_pp = ((1 - synth.probability) - no_implied) * 100

    if divergence_pp < POLICY_DIVERGENCE_THRESHOLD_PP:
        stats.skipped_low_divergence += 1
        stats.near_misses += 1
        return None

    # Confidence-tier gate
    min_rank = _CONFIDENCE_RANK.get(POLICY_MIN_LLM_CONFIDENCE, 1)
    if _CONFIDENCE_RANK.get(synth.confidence_tier, 0) < min_rank:
        stats.skipped_low_confidence += 1
        stats.near_misses += 1
        return None

    contracts = _half_kelly_size(edge_cents, entry_price, bankroll)
    if contracts <= 0:
        stats.near_misses += 1
        return None

    bracket = (market.get("subtitle") or market.get("title") or market.get("ticker", ""))[:80]
    reasoning_short = (synth.reasoning or "")[:200]
    rationale = (
        f"LLM {synth.probability:.1%} vs market {yes_implied:.1%} "
        f"(side={side}, div={divergence_pp:.1f}pp, tier={synth.confidence_tier}). "
        f"{reasoning_short}"
    )
    confidence_score = _confidence_score(synth.confidence_tier, divergence_pp)

    return DocOpportunity(
        ticker=market.get("ticker", ""),
        side=side,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        volume=volume,
        edge_after_fees=edge_cents / 100.0,
        confidence_score=confidence_score,
        suggested_contracts=contracts,
        bracket_title=bracket,
        rationale=rationale,
        market_question=market.get("title", ""),
        resolution_criteria=market.get("rules_primary", "") or market.get("rules", ""),
        market_implied_prob=yes_implied,
        hours_to_settlement=hours_to_settle,
        llm_prob=synth.probability,
        llm_confidence_tier=synth.confidence_tier,
        llm_reasoning=synth.reasoning,
        divergence_pp=divergence_pp,
        source_adapter=bundle.adapter,
        source_urls=bundle.source_urls,
        doc_last_updated=bundle.last_updated,
        supporting_facts=synth.supporting_facts,
        opposing_facts=synth.opposing_facts,
    )


def _half_kelly_size(edge_cents: int, price_cents: int, bankroll: float) -> int:
    """Half-Kelly contract sizing, capped at 10% of bankroll per trade.

    For Kalshi binary at entry price p (cents), winning pays 100c. Kelly:
      f* = (bp - q) / b, b = (100-p)/p, q = 1-p.
    We approximate with: allocated_dollars = bankroll * edge_fraction * 0.5,
    then clamp at 10% of bankroll. Contracts = allocated_dollars / price_dollars.
    """
    if edge_cents <= 0 or price_cents <= 0 or bankroll <= 0:
        return 0
    edge_fraction = edge_cents / 100.0
    price_dollars = price_cents / 100.0
    half_kelly_dollars = bankroll * edge_fraction * 0.5
    cap_dollars = bankroll * 0.10
    allocated = min(half_kelly_dollars, cap_dollars)
    return max(0, int(allocated / price_dollars))


def _confidence_score(tier: str, divergence_pp: float) -> float:
    """Map (tier, divergence) to a 0-100 confidence score.

    Tier sets the base; divergence adds up to 15 points for very wide gaps.
    This feeds trade_score and the Discord-alert header.
    """
    base = {"HIGH": 80, "MEDIUM": 65, "LOW": 45}.get(tier.upper(), 45)
    if divergence_pp > POLICY_DIVERGENCE_THRESHOLD_PP:
        extra_pp = divergence_pp - POLICY_DIVERGENCE_THRESHOLD_PP
        extra = min(15.0, extra_pp / 5.0 * 10.0)
    else:
        extra = 0.0
    return min(100.0, base + extra)
