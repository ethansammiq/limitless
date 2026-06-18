"""Policy scanner end-to-end tests with mocked broker + adapter + LLM."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest


def _market(
    ticker: str,
    title: str,
    yes_bid: int = 42,
    yes_ask: int = 50,
    volume: int = 10_000,
    hours_to_settle: float = 72.0,
) -> dict:
    close_dt = datetime.now(timezone.utc) + timedelta(hours=hours_to_settle)
    return {
        "ticker": ticker,
        "title": title,
        "subtitle": title,
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "volume_24h": volume,
        "close_time": close_dt.isoformat().replace("+00:00", "Z"),
        "rules_primary": "Resolves YES if the specified event occurs.",
    }


def _mock_broker(markets: list, balance: float = 1000.0):
    b = AsyncMock()
    b.mode = "paper"
    b.get_markets = AsyncMock(return_value=markets)
    b.get_balance = AsyncMock(return_value=balance)
    return b


def _mock_adapter(bundle_by_ticker: dict):
    """Adapter that returns the given DocBundle for each ticker or None."""
    from markets.policy.sources.congress_gov import CongressGovAdapter

    class StubAdapter(CongressGovAdapter):
        def __init__(self):
            pass  # skip real init

        async def start(self):
            pass

        async def stop(self):
            pass

        async def fetch_fresh_doc(self, market, freshness_days=7):
            return bundle_by_ticker.get(market.get("ticker"))

    return StubAdapter()


class TestPrefilter:
    def test_rejects_non_policy_series(self):
        from markets.policy.scanner import scan

        async def go():
            broker = _mock_broker(markets=[_market("KXHIGHNY-26FEB11", "weather thing")])
            adapter = _mock_adapter({})
            opps, stats = await scan(broker=broker, adapter=adapter, bankroll=1000.0)
            assert len(opps) == 0
            assert stats.policy_series == 0  # weather ticker rejected

        asyncio.run(go())

    def test_rejects_high_volume(self):
        from markets.policy.scanner import scan

        async def go():
            # Volume > POLICY_SCAN_MAX_VOLUME (500K default)
            broker = _mock_broker(markets=[
                _market("KXCONFIRM-BIG", "Too popular market", volume=1_000_000),
            ])
            adapter = _mock_adapter({})
            opps, stats = await scan(broker=broker, adapter=adapter, bankroll=1000.0)
            assert stats.policy_series == 1
            assert stats.passed_prefilter == 0

        asyncio.run(go())

    def test_rejects_too_close_to_settlement(self):
        from markets.policy.scanner import scan

        async def go():
            # hours_to_settle < 48
            broker = _mock_broker(markets=[
                _market("KXCONFIRM-SOON", "Almost settled", hours_to_settle=12),
            ])
            adapter = _mock_adapter({})
            opps, stats = await scan(broker=broker, adapter=adapter, bankroll=1000.0)
            assert stats.passed_prefilter == 0

        asyncio.run(go())


class TestDivergenceGate:
    def _make_bundle(self):
        from markets.policy.sources.congress_gov import DocBundle
        return DocBundle(
            adapter="congress_gov",
            doc_type="bill",
            title="Test bill",
            doc_text="Full bill text here.",
            source_urls=["https://congress.gov/bill/X"],
            last_updated=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        )

    def test_high_divergence_produces_opportunity(self):
        from markets.policy.scanner import scan

        async def go():
            market = _market("KXCONFIRM-X", "test", yes_bid=30, yes_ask=35)
            broker = _mock_broker(markets=[market])
            adapter = _mock_adapter({"KXCONFIRM-X": self._make_bundle()})

            # Mock LLM returns 70% YES — market says 30% → 40pp divergence, HIGH conf
            async def fake_synth(**kwargs):
                from core.llm_synth import SynthResult
                return SynthResult(
                    probability=0.70, confidence_tier="HIGH",
                    supporting_facts=["Strong support"], opposing_facts=["Weak opposition"],
                    reasoning="Good signal.", success=True, model="claude-opus-4-7",
                )

            with patch("markets.policy.scanner.synthesize", side_effect=fake_synth):
                opps, stats = await scan(broker=broker, adapter=adapter, bankroll=1000.0)

            assert stats.tradeable == 1
            opp = opps[0]
            assert opp.side == "yes"
            assert opp.llm_prob == 0.70
            assert opp.divergence_pp > 10
            assert opp.suggested_contracts > 0
            assert opp.llm_confidence_tier == "HIGH"

        asyncio.run(go())

    def test_low_divergence_skipped(self):
        from markets.policy.scanner import scan

        async def go():
            market = _market("KXCONFIRM-Y", "test", yes_bid=48, yes_ask=52)
            broker = _mock_broker(markets=[market])
            adapter = _mock_adapter({"KXCONFIRM-Y": self._make_bundle()})

            async def fake_synth(**kwargs):
                from core.llm_synth import SynthResult
                # LLM 50% vs market 48% → only 2pp divergence
                return SynthResult(
                    probability=0.50, confidence_tier="HIGH", success=True,
                    model="claude-opus-4-7", reasoning="Coin flip.",
                )

            with patch("markets.policy.scanner.synthesize", side_effect=fake_synth):
                opps, stats = await scan(broker=broker, adapter=adapter, bankroll=1000.0)

            assert stats.tradeable == 0
            assert stats.skipped_low_divergence >= 1

        asyncio.run(go())

    def test_low_confidence_skipped(self):
        """Even with big divergence, LOW confidence below the POLICY_MIN_LLM_CONFIDENCE floor is skipped."""
        from markets.policy.scanner import scan
        import config

        async def go():
            market = _market("KXCONFIRM-Z", "test", yes_bid=30, yes_ask=35)
            broker = _mock_broker(markets=[market])
            adapter = _mock_adapter({"KXCONFIRM-Z": self._make_bundle()})

            async def fake_synth(**kwargs):
                from core.llm_synth import SynthResult
                return SynthResult(
                    probability=0.70, confidence_tier="LOW",  # below MEDIUM floor
                    success=True, model="claude-opus-4-7",
                    reasoning="Weak evidence.",
                )

            # Default POLICY_MIN_LLM_CONFIDENCE is MEDIUM — LOW should skip
            assert config.POLICY_MIN_LLM_CONFIDENCE in ("MEDIUM", "HIGH", "LOW")
            if config.POLICY_MIN_LLM_CONFIDENCE == "LOW":
                pytest.skip("Min confidence is LOW — test assumes MEDIUM+")

            with patch("markets.policy.scanner.synthesize", side_effect=fake_synth):
                opps, stats = await scan(broker=broker, adapter=adapter, bankroll=1000.0)

            assert stats.tradeable == 0
            assert stats.skipped_low_confidence >= 1

        asyncio.run(go())


class TestSideSelection:
    def _make_bundle(self):
        from markets.policy.sources.congress_gov import DocBundle
        return DocBundle(
            adapter="congress_gov", doc_type="bill", title="t",
            doc_text="d", last_updated=(datetime.now(timezone.utc)).isoformat(),
        )

    def test_picks_no_side_when_llm_prob_below_market(self):
        from markets.policy.scanner import scan

        async def go():
            # Market yes_ask=60 → no-implied = 40. LLM says 20% YES (=80% NO)
            # Edge on NO = 80% - 40% = 40pp, huge.
            market = _market("KXCONFIRM-NO", "test", yes_bid=55, yes_ask=60)
            broker = _mock_broker(markets=[market])
            adapter = _mock_adapter({"KXCONFIRM-NO": self._make_bundle()})

            async def fake_synth(**kwargs):
                from core.llm_synth import SynthResult
                return SynthResult(
                    probability=0.20, confidence_tier="HIGH",
                    success=True, model="claude-opus-4-7", reasoning="NO signal.",
                )

            with patch("markets.policy.scanner.synthesize", side_effect=fake_synth):
                opps, _ = await scan(broker=broker, adapter=adapter, bankroll=1000.0)

            assert len(opps) == 1
            assert opps[0].side == "no"

        asyncio.run(go())


class TestHelpers:
    def test_half_kelly_size(self):
        from markets.policy.scanner import _half_kelly_size
        # 10c edge at 30c entry on $1000 bankroll
        # half_kelly = 1000 * 0.10 * 0.5 = $50
        # cap = 1000 * 0.10 = $100
        # allocated = min($50, $100) = $50
        # contracts = int($50 / $0.30) = 166
        assert _half_kelly_size(10, 30, 1000.0) == 166

    def test_half_kelly_capped(self):
        from markets.policy.scanner import _half_kelly_size
        # Huge edge 50c at 10c → half_kelly = $250, cap = $100
        # allocated = $100 → contracts = int($100 / $0.10) = 1000
        assert _half_kelly_size(50, 10, 1000.0) == 1000

    def test_half_kelly_zero_edge(self):
        from markets.policy.scanner import _half_kelly_size
        assert _half_kelly_size(0, 30, 1000.0) == 0

    def test_confidence_score_scales(self):
        from markets.policy.scanner import _confidence_score
        assert _confidence_score("HIGH", divergence_pp=10) == 80.0
        high_wide = _confidence_score("HIGH", divergence_pp=20)
        assert 80 < high_wide <= 95  # extra points for wide divergence
        assert _confidence_score("LOW", divergence_pp=10) == 45.0
