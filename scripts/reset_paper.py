#!/usr/bin/env python3
"""Reset paper-trading state.

Deletes:
  - positions_paper.json
  - .positions_paper.lock
  - paper_balance.json
  - paper_orders.json

Run:
  python3 scripts/reset_paper.py

Live state (positions.json, .positions.lock) is NEVER touched. The script
refuses to run unless PAPER_TRADING_MODE=true, so an accidental invocation
can't damage a real portfolio.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Resolve project root regardless of where the script is invoked from
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

from config import (
    PAPER_TRADING_MODE,
    PAPER_POSITIONS_FILE,
    PAPER_BALANCE_FILE,
    PAPER_ORDERS_FILE,
)


def reset(force: bool = False) -> int:
    if not PAPER_TRADING_MODE and not force:
        print(
            "PAPER_TRADING_MODE is not set to true. Refusing to run to prevent\n"
            "accidental damage to live state. Either set the env var or pass --force."
        )
        return 1

    paper_lock = PAPER_POSITIONS_FILE.parent / f".{PAPER_POSITIONS_FILE.stem}.lock"
    targets = [
        PAPER_POSITIONS_FILE,
        paper_lock,
        PAPER_BALANCE_FILE,
        PAPER_ORDERS_FILE,
    ]

    removed = 0
    for p in targets:
        if p.exists():
            p.unlink()
            print(f"removed {p.name}")
            removed += 1
        else:
            print(f"skip (not found) {p.name}")
    print(f"\npaper state reset ({removed} file(s) removed)")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reset paper-trading state (paper files only).")
    parser.add_argument("--force", action="store_true", help="Reset even if PAPER_TRADING_MODE is not true.")
    args = parser.parse_args()
    sys.exit(reset(force=args.force))
