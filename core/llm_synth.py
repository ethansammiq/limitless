"""LLM document synthesizer — primary source text -> calibrated probability.

Uses Claude Opus 4.7 via the Anthropic SDK. The primary source document is
dropped verbatim into the user message (1M-context model) and a structured
JSON response is forced via tool-use so callers always get a parseable
SynthResult.

Phase 2 will add an ensemble wrapper (this module + GPT-4o + DeepSeek via
OpenRouter with majority voting). For now this single-model path is the
baseline.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from log_setup import get_logger
from config import (
    LLM_SYNTH_MODEL,
    LLM_SYNTH_MAX_TOKENS,
    LLM_SYNTH_TIMEOUT_SEC,
)

logger = get_logger(__name__)

__all__ = ["SynthResult", "synthesize"]


@dataclass
class SynthResult:
    """Structured output from an LLM document-synthesis call."""

    probability: float = 0.5                    # 0-1, YES probability
    confidence_tier: str = "LOW"                # HIGH / MEDIUM / LOW
    supporting_facts: list[str] = field(default_factory=list)
    opposing_facts: list[str] = field(default_factory=list)
    reasoning: str = ""
    model: str = ""
    success: bool = False
    error: str = ""
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


SYSTEM_PROMPT = """You are a calibrated forecaster for prediction markets.

You are given (1) a market question with resolution criteria, (2) the current market-implied probability, and (3) one or more primary source documents. Your task is to produce a calibrated probability that the market resolves YES.

Calibration guidance:
- Use HIGH only when documents contain decisive evidence (e.g., a signed law, a settled vote, an unambiguous ruling).
- MEDIUM when evidence is material but material uncertainty remains.
- LOW when evidence is weak or indirect; report your best estimate near the base rate.
- 50% is a valid answer when evidence is genuinely balanced. Do not force a directional view.
- Cite specific facts from the documents. Do not rely on prior general knowledge unless a fact is trivially verifiable.
- Ignore the market-implied probability when forming your own estimate. It is provided only as context. Your task is to produce an INDEPENDENT forecast.

Respond by invoking the submit_forecast tool exactly once."""


_FORECAST_TOOL = {
    "name": "submit_forecast",
    "description": "Submit a calibrated probability forecast for the market question.",
    "input_schema": {
        "type": "object",
        "properties": {
            "probability": {
                "type": "number", "minimum": 0.0, "maximum": 1.0,
                "description": "Probability the market resolves YES, as a decimal (0.0-1.0).",
            },
            "confidence_tier": {
                "type": "string", "enum": ["HIGH", "MEDIUM", "LOW"],
                "description": "Your confidence in the probability estimate.",
            },
            "supporting_facts": {
                "type": "array", "items": {"type": "string"},
                "description": "Top 3-5 facts from the document supporting YES, each a concrete quote or paraphrase.",
            },
            "opposing_facts": {
                "type": "array", "items": {"type": "string"},
                "description": "Top 3-5 facts from the document supporting NO.",
            },
            "reasoning": {
                "type": "string",
                "description": "One-paragraph rationale tying the facts to the probability.",
            },
        },
        "required": ["probability", "confidence_tier", "reasoning"],
    },
}


async def synthesize(
    market_question: str,
    resolution_criteria: str,
    market_implied_prob: float,
    doc_text: str,
    doc_sources: Optional[list[str]] = None,
    model: Optional[str] = None,
) -> SynthResult:
    """Produce a calibrated probability for a market given primary source text.

    Args:
        market_question: The Kalshi market question, verbatim.
        resolution_criteria: The market's resolution rules.
        market_implied_prob: Current market-implied probability (yes_bid / 100).
        doc_text: Full primary source text. Can be up to ~900K tokens for Opus 4.7 1M.
        doc_sources: URLs or identifiers for the primary source (audit trail).
        model: Override for LLM_SYNTH_MODEL env / config.

    Returns:
        SynthResult. On failure, success=False and error is populated;
        probability defaults to 0.5 so callers can gracefully skip.
    """
    start = asyncio.get_event_loop().time()
    result = SynthResult(model=model or LLM_SYNTH_MODEL)

    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        result.error = "anthropic SDK not installed — run `pip install anthropic`"
        logger.error(result.error)
        return result

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        result.error = "ANTHROPIC_API_KEY not set"
        logger.error(result.error)
        return result

    doc_sources = doc_sources or []
    sources_block = (
        "\n\nPRIMARY SOURCE URLS:\n" + "\n".join(f"  - {u}" for u in doc_sources)
        if doc_sources else ""
    )

    user_prompt = f"""MARKET QUESTION:
{market_question}

RESOLUTION CRITERIA:
{resolution_criteria}

MARKET-IMPLIED PROBABILITY:
{market_implied_prob:.1%}
{sources_block}

PRIMARY SOURCE DOCUMENT:
--- BEGIN DOCUMENT ---
{doc_text}
--- END DOCUMENT ---

Read the document carefully. Produce an independent forecast by calling submit_forecast.
Prefer facts grounded in the document. If the document does not directly address the question, keep confidence LOW and explain what additional evidence would change your view."""

    client = AsyncAnthropic(api_key=api_key)

    try:
        msg = await asyncio.wait_for(
            client.messages.create(
                model=result.model,
                max_tokens=LLM_SYNTH_MAX_TOKENS,
                system=[{
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }],
                tools=[_FORECAST_TOOL],
                tool_choice={"type": "tool", "name": "submit_forecast"},
                messages=[{"role": "user", "content": user_prompt}],
            ),
            timeout=LLM_SYNTH_TIMEOUT_SEC,
        )

        for block in msg.content:
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", "") == "submit_forecast":
                inp = block.input or {}
                p = float(inp.get("probability", 0.5))
                result.probability = max(0.0, min(1.0, p))
                tier = str(inp.get("confidence_tier", "LOW")).upper()
                result.confidence_tier = tier if tier in ("HIGH", "MEDIUM", "LOW") else "LOW"
                result.supporting_facts = list(inp.get("supporting_facts", []) or [])
                result.opposing_facts = list(inp.get("opposing_facts", []) or [])
                result.reasoning = str(inp.get("reasoning", ""))
                result.success = True
                break

        if not result.success:
            result.error = "LLM response contained no submit_forecast tool_use block"
            logger.warning(result.error)

        usage = getattr(msg, "usage", None)
        if usage:
            result.input_tokens = getattr(usage, "input_tokens", 0)
            result.output_tokens = getattr(usage, "output_tokens", 0)

    except asyncio.TimeoutError:
        result.error = f"LLM timeout after {LLM_SYNTH_TIMEOUT_SEC}s"
        logger.warning(result.error)
    except Exception as e:
        result.error = f"LLM call failed: {type(e).__name__}: {e}"
        logger.error(result.error)

    result.latency_ms = int((asyncio.get_event_loop().time() - start) * 1000)
    return result
