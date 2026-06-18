#!/usr/bin/env python3
"""
LLM ENSEMBLE CONFIDENCE — Multi-model weather reasoning via OpenRouter.

Adds a 6th confidence factor to the statistical model by querying 3 LLMs
in parallel for contextual weather pattern recognition. Results are
aggregated via median + majority vote.

Cost: ~$0.04-0.05 per opportunity (~$1/month at typical scan rates).

Requires OPENROUTER_API_KEY in .env. Disabled by default until validated.

Usage:
    from llm_confidence import LLMConfidenceModule
    module = LLMConfidenceModule()
    result = await module.get_consensus(context)
    # result = {"confidence": 75, "direction": "yes", "reasoning": "...", "model_results": [...]}
"""

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

import aiohttp
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from log_setup import get_logger
from config import (
    LLM_CONFIDENCE_ENABLED,
    LLM_CONFIDENCE_WEIGHT,
    LLM_TIMEOUT_SECONDS,
    LLM_MIN_MODELS_REQUIRED,
    LLM_MODELS,
)

logger = get_logger(__name__)

ET = ZoneInfo("America/New_York")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")

SYSTEM_PROMPT = """You are a quantitative weather forecasting analyst for prediction markets.
You analyze weather data and assess the probability of a temperature bracket being correct.

Given weather context (ensemble forecasts, NWS data, observations), respond with ONLY valid JSON:
{"confidence": <0-100>, "direction": "<yes|no>", "reasoning": "<1-2 sentences>"}

confidence: How certain you are that the target bracket will verify (0=no chance, 100=certain)
direction: Would you buy "yes" (bracket will verify) or "no" (bracket won't verify)?
reasoning: Brief explanation of your assessment

Be calibrated. 50 means coin flip. 70+ means you see a clear signal. 90+ means near-certain."""


@dataclass
class LLMResult:
    """Result from a single LLM model."""
    model: str
    confidence: int = 50
    direction: str = "abstain"
    reasoning: str = ""
    success: bool = False
    error: str = ""
    latency_ms: int = 0


@dataclass
class ConsensusResult:
    """Aggregated result from all LLM models."""
    confidence: int = 50
    direction: str = "abstain"
    reasoning: str = ""
    model_results: list = field(default_factory=list)
    n_models_responded: int = 0
    n_models_total: int = 0
    valid: bool = False


def _build_context_prompt(context: dict) -> str:
    """Build the user prompt with weather and market context."""
    city = context.get("city", "Unknown")
    bracket = context.get("bracket", "Unknown")
    ensemble_mean = context.get("ensemble_mean", 0)
    ensemble_std = context.get("ensemble_std", 0)
    ensemble_count = context.get("ensemble_count", 0)
    nws_high = context.get("nws_high", 0)
    physics_high = context.get("physics_high", 0)
    current_temp = context.get("current_temp", "N/A")
    kde_prob = context.get("kde_prob", 0)
    stat_confidence = context.get("stat_confidence", 0)
    market_price = context.get("market_price", 0)
    strategies = context.get("strategies", [])
    trend = context.get("trend", "unknown")

    return f"""Analyze this weather trading opportunity:

CITY: {city}
TARGET BRACKET: {bracket}°F (daily high temperature)
SETTLEMENT: Tomorrow morning ~7 AM ET

ENSEMBLE FORECAST ({ensemble_count} members):
  Mean: {ensemble_mean:.1f}°F ± {ensemble_std:.1f}°
  KDE Probability in bracket: {kde_prob:.1f}%

NWS FORECAST:
  Official high: {nws_high:.0f}°F
  Physics-adjusted: {physics_high:.1f}°F

CURRENT CONDITIONS:
  Temperature: {current_temp}°F
  Trend: {trend}

STATISTICAL MODEL:
  Confidence score: {stat_confidence:.0f}/100

MARKET:
  Current price: {market_price}¢

ACTIVE STRATEGIES: {', '.join(strategies) if strategies else 'None'}

Based on this data, what is your confidence that the actual high temperature will fall in the target bracket?"""


async def _query_model(
    session: aiohttp.ClientSession,
    model: str,
    prompt: str,
) -> LLMResult:
    """Query a single LLM model via OpenRouter."""
    result = LLMResult(model=model)
    start = asyncio.get_event_loop().time()

    try:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 200,
            "temperature": 0.3,  # Low temp for consistency
        }

        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://weather-edge.local",
            "X-Title": "Weather Edge Scanner",
        }

        timeout = aiohttp.ClientTimeout(total=LLM_TIMEOUT_SECONDS)
        async with session.post(OPENROUTER_URL, json=payload, headers=headers, timeout=timeout) as resp:
            if resp.status != 200:
                body = await resp.text()
                result.error = f"HTTP {resp.status}: {body[:200]}"
                logger.warning(f"LLM {model}: {result.error}")
                return result

            data = await resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")

            # Parse JSON response — robust extraction from markdown fences
            content = content.strip()
            # Try extracting JSON from code fences first
            fence_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', content, re.DOTALL)
            if fence_match:
                content = fence_match.group(1)
            elif content.startswith("```"):
                # Fallback: strip fences manually
                content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

            parsed = json.loads(content)
            result.confidence = max(0, min(100, int(parsed.get("confidence", 50))))
            result.direction = parsed.get("direction", "abstain").lower()
            result.reasoning = parsed.get("reasoning", "")[:200]
            result.success = True

    except json.JSONDecodeError as e:
        result.error = f"Invalid JSON: {e}"
        logger.warning(f"LLM {model}: could not parse response as JSON")
    except asyncio.TimeoutError:
        result.error = f"Timeout after {LLM_TIMEOUT_SECONDS}s"
        logger.warning(f"LLM {model}: timeout")
    except Exception as e:
        result.error = str(e)
        logger.warning(f"LLM {model}: {e}")

    result.latency_ms = int((asyncio.get_event_loop().time() - start) * 1000)
    return result


class LLMConfidenceModule:
    """
    Multi-model LLM confidence scoring via OpenRouter.

    Queries 3 models in parallel, aggregates via median confidence
    and majority vote on direction.
    """

    def __init__(self, models: list[str] = None):
        self.models = models or LLM_MODELS
        self.enabled = LLM_CONFIDENCE_ENABLED and bool(OPENROUTER_API_KEY)

        if LLM_CONFIDENCE_ENABLED and not OPENROUTER_API_KEY:
            logger.warning("LLM confidence enabled but OPENROUTER_API_KEY not set — disabled")

    async def get_consensus(self, context: dict) -> ConsensusResult:
        """
        Query all models and return aggregated consensus.

        Args:
            context: Dict with keys: city, bracket, ensemble_mean, ensemble_std,
                    ensemble_count, nws_high, physics_high, kde_prob,
                    stat_confidence, market_price, strategies, trend

        Returns:
            ConsensusResult with median confidence, majority direction, and per-model details.
        """
        result = ConsensusResult(n_models_total=len(self.models))

        if not self.enabled:
            result.reasoning = "LLM module disabled"
            return result

        prompt = _build_context_prompt(context)

        async with aiohttp.ClientSession() as session:
            tasks = [_query_model(session, model, prompt) for model in self.models]
            model_results = await asyncio.gather(*tasks)

        result.model_results = model_results
        successful = [r for r in model_results if r.success]
        result.n_models_responded = len(successful)

        if len(successful) < LLM_MIN_MODELS_REQUIRED:
            result.reasoning = f"Only {len(successful)}/{len(self.models)} models responded (need {LLM_MIN_MODELS_REQUIRED})"
            logger.warning(result.reasoning)
            return result

        # Aggregate: median confidence
        confidences = sorted([r.confidence for r in successful])
        result.confidence = confidences[len(confidences) // 2]  # Median

        # Aggregate: majority vote on direction
        yes_votes = sum(1 for r in successful if r.direction == "yes")
        no_votes = sum(1 for r in successful if r.direction == "no")
        if yes_votes > no_votes:
            result.direction = "yes"
        elif no_votes > yes_votes:
            result.direction = "no"
        else:
            result.direction = "abstain"

        # Combine reasoning
        reasons = [f"{r.model.split('/')[-1]}: {r.reasoning}" for r in successful if r.reasoning]
        result.reasoning = " | ".join(reasons[:3])

        result.valid = True

        latencies = [r.latency_ms for r in model_results]
        logger.info(
            f"LLM consensus: {result.confidence}/100 ({result.direction}) "
            f"from {len(successful)}/{len(self.models)} models "
            f"[{min(latencies)}-{max(latencies)}ms]"
        )

        return result

    @staticmethod
    def blend_scores(stat_score: float, llm_result: ConsensusResult) -> float:
        """
        Blend statistical confidence with LLM consensus.

        Returns the blended score, or the stat_score unchanged if LLM is invalid.
        """
        if not llm_result.valid:
            return stat_score

        llm_score = float(llm_result.confidence)
        blended = stat_score * (1 - LLM_CONFIDENCE_WEIGHT) + llm_score * LLM_CONFIDENCE_WEIGHT
        return round(blended, 1)
