"""Opportunity protocol + DocOpportunity tests."""

from __future__ import annotations


class TestOpportunityProtocol:
    """OpportunityBase is a Protocol — any compatible class satisfies it."""

    def test_docopportunity_matches_protocol(self):
        from core.opportunity import OpportunityBase, DocOpportunity
        opp = DocOpportunity(
            ticker="KXCONFIRM-TEST", side="yes", yes_bid=42, yes_ask=50,
            volume=10_000, edge_after_fees=0.15, confidence_score=80.0,
            suggested_contracts=10, bracket_title="Test Market",
            rationale="Test rationale",
        )
        assert isinstance(opp, OpportunityBase)

    def test_docopportunity_defaults(self):
        from core.opportunity import DocOpportunity
        opp = DocOpportunity(
            ticker="X", side="yes", yes_bid=0, yes_ask=0, volume=0,
            edge_after_fees=0.0, confidence_score=0.0, suggested_contracts=0,
            bracket_title="", rationale="",
        )
        assert opp.strategies == ["DOC_SYNTH"]
        assert opp.llm_confidence_tier == "LOW"
        assert opp.supporting_facts == []
        assert opp.opposing_facts == []
        assert opp.ensemble_votes == {}
        assert opp.scanner_version == "1.0"

    def test_existing_weather_opportunity_duck_types(self):
        """The existing Opportunity from edge_scanner_v2 must duck-type to
        OpportunityBase without any code changes."""
        from core.opportunity import OpportunityBase
        # Stub class mimicking the weather Opportunity's relevant fields
        class Stub:
            ticker = "KXHIGHNY-26FEB11-B36.5"
            side = "yes"
            yes_bid = 25
            yes_ask = 30
            volume = 5000
            edge_after_fees = 0.12
            confidence_score = 85.0
            suggested_contracts = 4
            bracket_title = "NY: 36°F"
            rationale = "Weather ensemble agrees"
        assert isinstance(Stub(), OpportunityBase)
