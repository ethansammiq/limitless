"""OpportunityBase — domain-agnostic contract for prediction-market opportunities.

Any object with the fields declared on OpportunityBase satisfies the contract,
so the existing weather Opportunity (in edge_scanner_v2.py) duck-types in
without modification. New scanners either subclass one of the concrete
dataclasses here or define their own class with the same surface.

The execution pipeline (auto_trader, execute_trade, position_monitor,
trading_guards) is type-hinted against OpportunityBase. That's the seam.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@runtime_checkable
class OpportunityBase(Protocol):
    """Duck-typed contract every tradeable opportunity must satisfy."""

    ticker: str              # Kalshi market ticker (e.g. "KXCONFIRM-SMITH-26MAY15")
    side: str                # "yes" or "no"
    yes_bid: int             # Current yes-side bid in cents (0-100)
    yes_ask: int             # Current yes-side ask in cents (0-100)
    volume: int              # Recent contracts traded
    edge_after_fees: float   # Edge as a fraction (0.15 == 15pp after fees)
    confidence_score: float  # 0-100, semantics vary by domain
    suggested_contracts: int # Scanner's sizing suggestion
    bracket_title: str       # Human-readable market label (for logs/alerts)
    rationale: str           # Free text explanation (for Discord + audit)


@dataclass
class DocOpportunity:
    """Opportunity produced by an LLM document-synthesis scanner.

    Satisfies OpportunityBase structurally. Adds scanner-specific metadata so
    calibration can trace the signal back to the primary source.
    """
    # ── OpportunityBase fields ──
    ticker: str
    side: str
    yes_bid: int
    yes_ask: int
    volume: int
    edge_after_fees: float
    confidence_score: float
    suggested_contracts: int
    bracket_title: str
    rationale: str

    # ── Market context ──
    market_question: str = ""
    resolution_criteria: str = ""
    market_implied_prob: float = 0.0  # yes_bid / 100
    hours_to_settlement: float = 0.0

    # ── Signal breakdown ──
    llm_prob: float = 0.0             # LLM's calibrated probability (0-1)
    llm_confidence_tier: str = "LOW"  # HIGH / MEDIUM / LOW
    llm_reasoning: str = ""
    divergence_pp: float = 0.0        # |llm_prob*100 - market_implied_pct| in pp

    # ── Source trail (for audit) ──
    source_adapter: str = ""          # e.g. "congress_gov"
    source_urls: list[str] = field(default_factory=list)
    doc_last_updated: str = ""        # ISO timestamp when the doc last changed
    supporting_facts: list[str] = field(default_factory=list)
    opposing_facts: list[str] = field(default_factory=list)

    # ── Scanner identity ──
    strategies: list[str] = field(default_factory=lambda: ["DOC_SYNTH"])
    scanner_version: str = "1.0"

    # ── Optional ensemble fields (for multi-LLM consensus — Phase 2) ──
    ensemble_votes: dict = field(default_factory=dict)  # model -> probability
