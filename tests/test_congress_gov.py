"""CongressGovAdapter tests — matching + bundle construction (HTTP mocked)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone



class TestMatching:
    """Token-overlap scoring behavior."""

    def test_score_match_identical(self):
        from markets.policy.sources.congress_gov import _score_match
        assert _score_match("Senate confirms Smith", "Senate confirms Smith") == 1.0

    def test_score_match_partial(self):
        from markets.policy.sources.congress_gov import _score_match
        # "confirm smith" shared; filler words removed
        score = _score_match(
            "Will the Senate confirm Smith by May 15",
            "A bill to confirm Smith as director",
        )
        assert 0 < score < 1

    def test_score_match_empty(self):
        from markets.policy.sources.congress_gov import _score_match
        assert _score_match("", "anything") == 0.0
        assert _score_match("something", "") == 0.0

    def test_find_best_match_below_threshold_returns_none(self):
        from markets.policy.sources.congress_gov import _find_best_match
        # Market title has nothing in common with candidates
        candidates = [{"title": "Unrelated fruit import tariff bill"}]
        result = _find_best_match("Senate confirms Smith", candidates, "title")
        assert result is None

    def test_find_best_match_picks_highest(self):
        from markets.policy.sources.congress_gov import _find_best_match
        candidates = [
            {"title": "Unrelated fruit import tariff bill"},
            {"title": "A bill to confirm Sarah Smith as director of NIH"},
            {"title": "Another confirmation thing"},
        ]
        result = _find_best_match("Will Senate confirm Sarah Smith", candidates, "title")
        assert result is not None
        best_dict, score = result
        assert "Sarah Smith" in best_dict["title"]


class TestTimestampParsing:
    def test_parse_z_format(self):
        from markets.policy.sources.congress_gov import _parse_ts
        dt = _parse_ts("2026-04-17T12:34:56Z")
        assert dt is not None
        assert dt.tzinfo is not None

    def test_parse_offset_format(self):
        from markets.policy.sources.congress_gov import _parse_ts
        dt = _parse_ts("2026-04-17T12:34:56+00:00")
        assert dt is not None

    def test_parse_naive_adds_utc(self):
        from markets.policy.sources.congress_gov import _parse_ts
        dt = _parse_ts("2026-04-17T12:34:56")
        assert dt is not None
        assert dt.tzinfo is not None  # UTC inferred

    def test_parse_invalid(self):
        from markets.policy.sources.congress_gov import _parse_ts
        assert _parse_ts("not a date") is None
        assert _parse_ts("") is None
        assert _parse_ts(None) is None


class TestFreshDocLookup:
    """fetch_fresh_doc picks freshest relevant match."""

    def test_no_api_key_returns_none(self):
        from markets.policy.sources.congress_gov import CongressGovAdapter

        async def go():
            a = CongressGovAdapter(api_key="")
            result = await a.fetch_fresh_doc({"title": "anything"})
            assert result is None

        asyncio.run(go())

    def test_fresh_bill_match(self):
        from markets.policy.sources.congress_gov import CongressGovAdapter

        async def go():
            adapter = CongressGovAdapter(api_key="fake")
            recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat().replace("+00:00", "Z")
            bill = {
                "title": "A bill to confirm Sarah Smith as director",
                "congress": 119,
                "type": "HR",
                "number": 1234,
                "updateDate": recent,
                "url": "https://congress.gov/bill/x",
            }

            async def mock_bills(*args, **kwargs):
                return [bill]

            async def mock_noms(*args, **kwargs):
                return []

            async def mock_bill_text(*args, **kwargs):
                return "Full bill text here."

            adapter.fetch_recent_bills = mock_bills
            adapter.fetch_recent_nominations = mock_noms
            adapter.fetch_bill_text = mock_bill_text

            result = await adapter.fetch_fresh_doc(
                {"title": "Will Senate confirm Sarah Smith"}, freshness_days=7,
            )
            assert result is not None
            assert result.doc_type == "bill"
            assert result.doc_text == "Full bill text here."

        asyncio.run(go())

    def test_stale_match_rejected(self):
        from markets.policy.sources.congress_gov import CongressGovAdapter

        async def go():
            adapter = CongressGovAdapter(api_key="fake")
            stale = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat().replace("+00:00", "Z")
            bill = {
                "title": "A bill to confirm Sarah Smith as director",
                "updateDate": stale,
            }

            async def mock_bills(*args, **kwargs):
                return [bill]

            async def mock_noms(*args, **kwargs):
                return []

            adapter.fetch_recent_bills = mock_bills
            adapter.fetch_recent_nominations = mock_noms

            result = await adapter.fetch_fresh_doc(
                {"title": "Will Senate confirm Sarah Smith"}, freshness_days=7,
            )
            assert result is None  # Match exists but too stale

        asyncio.run(go())
