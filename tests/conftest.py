"""Shared test configuration — adds project root to sys.path."""

import sys
import os
from pathlib import Path

# Ensure project root is importable from all test files
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Force live mode during tests so position_store uses positions.json, not
# positions_paper.json — prevents PAPER_TRADING_MODE=true in .env from
# breaking test isolation for position-store tests.
os.environ.setdefault("PAPER_TRADING_MODE", "false")
