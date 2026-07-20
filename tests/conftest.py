"""Shared test configuration — adds project root to sys.path."""

import sys
import os
from pathlib import Path

import pytest

# Ensure project root is importable from all test files
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Force live mode during tests so position_store uses positions.json, not
# positions_paper.json — prevents PAPER_TRADING_MODE=true in .env from
# breaking test isolation for position-store tests.
os.environ.setdefault("PAPER_TRADING_MODE", "false")


@pytest.fixture(autouse=True)
def _isolated_risk_caps(tmp_path, monkeypatch):
    """Every test runs on the fixed-cap path by default: no real balance
    snapshot, no ambient cap overrides from .env. Tests that exercise the
    bankroll derivation write their own snapshot at this path."""
    from core import risk

    monkeypatch.setattr(risk, "BANKROLL_SNAPSHOT",
                        tmp_path / "live_account.json")
    monkeypatch.delenv("TAKE_MAX_NOTIONAL", raising=False)
    monkeypatch.delenv("TAKE_NIGHT_CAP_DOLLARS", raising=False)
    # The attention floor is OFF by default in tests: it gates on reward,
    # so leaving it live would silently un-stage every fixture built
    # before 2026-07-19. Floor behaviour has its own tests, which set the
    # env explicitly (and pin the production default).
    monkeypatch.setenv("TAKE_MIN_PAYOFF_DOLLARS", "0")
