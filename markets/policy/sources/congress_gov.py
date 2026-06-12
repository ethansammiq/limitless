"""api.congress.gov adapter.

Auth: requires CONGRESS_GOV_API_KEY (free — register at api.data.gov).
Rate limit: 5,000 req/hour. Our scan cadence stays well under this.

Two surfaces:
  1. Low-level endpoint wrappers (fetch_bill, fetch_nomination, etc.) for
     direct use by custom scanners or tests.
  2. High-level fetch_fresh_doc(market) that maps a Kalshi market to a
     DocBundle, returning None if no fresh primary source matches.

The mapping is heuristic: we score recent bills/nominations against the market
title by token overlap and require a minimum similarity to avoid loose matches
driving bad trades. The scanner will skip anything below the threshold rather
than trade on a doubtful source.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from log_setup import get_logger

logger = get_logger(__name__)

API_BASE = "https://api.congress.gov/v3"

# 119th Congress: Jan 3, 2025 – Jan 3, 2027. Update when the next one convenes.
CURRENT_CONGRESS = 119

# Threshold for token-overlap similarity between a market title and a
# candidate bill/nomination title. Conservative to avoid false matches.
MIN_MATCH_SCORE = 0.35


@dataclass
class DocBundle:
    """Primary source bundle returned by a source adapter."""
    adapter: str
    doc_type: str                            # "bill" | "nomination" | "vote" | "committee_report"
    title: str
    doc_text: str                            # Full primary source text
    source_urls: list[str] = field(default_factory=list)
    last_updated: str = ""                   # ISO 8601
    metadata: dict = field(default_factory=dict)


class CongressGovAdapter:
    """Async client for api.congress.gov with retry + graceful degradation."""

    def __init__(self, api_key: Optional[str] = None, timeout_sec: float = 20.0):
        self.api_key = api_key or os.getenv("CONGRESS_GOV_API_KEY", "")
        self.timeout_sec = timeout_sec
        self._session: Optional[aiohttp.ClientSession] = None

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.timeout_sec),
            headers={"User-Agent": "Limitless-Policy-Scanner/1.0"},
        )
        if not self.api_key:
            logger.warning(
                "CONGRESS_GOV_API_KEY not set — adapter will return empty results "
                "(get a free key at https://api.data.gov/signup/)"
            )

    async def stop(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=1, max=10),
        retry=retry_if_exception_type((aiohttp.ClientError, asyncio.TimeoutError)),
        reraise=True,
    )
    async def _get(self, path: str, params: Optional[dict] = None) -> dict:
        if not self.api_key or not self._session:
            return {}
        q = {**(params or {}), "api_key": self.api_key, "format": "json"}
        url = f"{API_BASE}{path}"
        try:
            async with self._session.get(url, params=q) as resp:
                if resp.status == 200:
                    return await resp.json()
                body = (await resp.text())[:200]
                logger.warning("congress.gov %s %d: %s", path, resp.status, body)
                return {}
        except Exception as e:
            logger.debug("congress.gov request error: %s %s", path, e)
            raise

    # ── Low-level endpoints ──

    async def fetch_bill(self, congress: int, bill_type: str, bill_number: int) -> dict:
        result = await self._get(f"/bill/{congress}/{bill_type.lower()}/{bill_number}")
        return result.get("bill", {})

    async def fetch_bill_text(
        self, congress: int, bill_type: str, bill_number: int,
    ) -> str:
        """Fetch the latest formatted text for a bill (best effort)."""
        result = await self._get(f"/bill/{congress}/{bill_type.lower()}/{bill_number}/text")
        text_versions = result.get("textVersions", [])
        if not text_versions or not self._session:
            return ""
        latest = text_versions[0]
        for fmt in latest.get("formats", []):
            if fmt.get("type") == "Formatted Text":
                url = fmt.get("url", "")
                if not url:
                    continue
                try:
                    async with self._session.get(url) as resp:
                        if resp.status == 200:
                            # Cap very long bills at 400KB chars to stay
                            # well within Opus 4.7 1M context budget.
                            return (await resp.text())[:400_000]
                except Exception as e:
                    logger.warning("bill text fetch failed: %s", e)
        return ""

    async def fetch_nomination(self, congress: int, nomination_number: int) -> dict:
        result = await self._get(f"/nomination/{congress}/{nomination_number}")
        return result.get("nomination", {})

    async def fetch_recent_bills(
        self, congress: int = CURRENT_CONGRESS, limit: int = 50,
    ) -> list[dict]:
        """Bills sorted by most recent update."""
        result = await self._get(
            f"/bill/{congress}",
            params={"limit": limit, "sort": "updateDate+desc"},
        )
        return result.get("bills", [])

    async def fetch_recent_nominations(
        self, congress: int = CURRENT_CONGRESS, limit: int = 50,
    ) -> list[dict]:
        result = await self._get(
            f"/nomination/{congress}",
            params={"limit": limit, "sort": "updateDate+desc"},
        )
        return result.get("nominations", [])

    # ── High-level ticker resolver ──

    async def fetch_fresh_doc(
        self, market: dict, freshness_days: int = 7,
    ) -> Optional[DocBundle]:
        """Map a Kalshi market dict to a DocBundle via token-overlap matching.

        Strategy (v1, to be refined once we see real market data):
          1. Pull 50 most-recently-updated bills AND 50 most-recent nominations.
          2. Score each against the market title by token overlap.
          3. Pick the best match across both pools.
          4. If the best match is updated within `freshness_days`, return its bundle.
          5. Otherwise return None.
        """
        if not self.api_key:
            return None

        title = (market.get("title") or market.get("subtitle") or "").lower()
        if not title:
            return None

        cutoff = datetime.now(timezone.utc) - timedelta(days=freshness_days)

        bills, noms = await asyncio.gather(
            self.fetch_recent_bills(limit=50),
            self.fetch_recent_nominations(limit=50),
        )

        best: Optional[tuple[str, dict, float]] = None

        bill_match = _find_best_match(title, bills, "title")
        if bill_match:
            best = ("bill", *bill_match)

        nom_match = _find_best_match(title, noms, "description")
        if nom_match and (best is None or nom_match[1] > best[2]):
            best = ("nomination", *nom_match)

        if best is None:
            logger.debug("no match for market: %s", title[:80])
            return None

        doc_type, match_dict, score = best
        update_ts = match_dict.get("updateDate") or match_dict.get("receivedDate")
        update_dt = _parse_ts(update_ts)
        if update_dt is None or update_dt < cutoff:
            logger.debug(
                "match too stale (%s, score=%.2f) for: %s",
                update_ts, score, title[:80],
            )
            return None

        logger.info(
            "matched %s (score=%.2f, updated=%s) for: %s",
            doc_type, score, update_ts, title[:80],
        )

        if doc_type == "bill":
            return await _build_bill_bundle(self, match_dict)
        return await _build_nomination_bundle(match_dict)


# ── Matching + bundle helpers ──

_FILLER = frozenset({
    "the", "will", "be", "is", "by", "a", "an", "to", "of", "and", "or",
    "in", "on", "for", "at", "with", "as", "that", "it", "this", "s",
})


def _normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9\s]", " ", (s or "").lower())


def _score_match(market_title: str, candidate_title: str) -> float:
    mt = set(_normalize(market_title).split()) - _FILLER
    ct = set(_normalize(candidate_title).split()) - _FILLER
    mt.discard("")
    ct.discard("")
    if not mt or not ct:
        return 0.0
    return len(mt & ct) / len(mt)


def _find_best_match(
    market_title: str, candidates: list[dict], title_key: str,
) -> Optional[tuple[dict, float]]:
    best_dict = None
    best_score = 0.0
    for c in candidates:
        ct = c.get(title_key, "")
        if not ct:
            continue
        score = _score_match(market_title, ct)
        if score > best_score:
            best_score = score
            best_dict = c
    if best_score < MIN_MATCH_SCORE:
        return None
    return best_dict, best_score


def _parse_ts(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        dt = datetime.fromisoformat(ts)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


async def _build_bill_bundle(adapter: CongressGovAdapter, bill: dict) -> DocBundle:
    congress = bill.get("congress")
    btype = bill.get("type", "")
    bnum = bill.get("number")
    text = ""
    if congress and btype and bnum:
        try:
            text = await adapter.fetch_bill_text(congress, btype, int(bnum))
        except Exception as e:
            logger.warning("bill text fetch error for %s/%s-%s: %s", congress, btype, bnum, e)
    title = bill.get("title", "")
    return DocBundle(
        adapter="congress_gov",
        doc_type="bill",
        title=title,
        doc_text=text or title,  # Fall back to title if full text unavailable
        source_urls=[bill.get("url", "")],
        last_updated=bill.get("updateDate", ""),
        metadata={
            "congress": congress,
            "bill_type": btype,
            "bill_number": bnum,
            "latest_action": bill.get("latestAction", {}),
            "match_type": "token_overlap",
        },
    )


async def _build_nomination_bundle(nom: dict) -> DocBundle:
    description = nom.get("description", "")
    url = nom.get("url", "")
    # Nomination text is typically short (a paragraph). Use description as
    # the synthesis payload; metadata carries latest-action info.
    return DocBundle(
        adapter="congress_gov",
        doc_type="nomination",
        title=description,
        doc_text=description,
        source_urls=[url] if url else [],
        last_updated=nom.get("updateDate") or nom.get("receivedDate", ""),
        metadata={
            "congress": nom.get("congress"),
            "nomination_number": nom.get("number"),
            "latest_action": nom.get("latestAction", {}),
            "organization": nom.get("organization"),
            "match_type": "token_overlap",
        },
    )
