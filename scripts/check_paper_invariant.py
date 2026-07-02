#!/usr/bin/env python3
"""Paper accounting invariant check — drift detector for the cash ledger.

Asserts the persisted paper balance equals what the position ledger implies:
    balance == initial + Σ realized_pnl(all) − Σ cost_basis(open)
Prints the result and exits non-zero if drift exceeds the threshold, so it can
gate a deploy or run from cron. Read-only — never writes. position_monitor runs
the same check every cycle and auto-corrects; this is the on-demand/CI version.

  python3 scripts/check_paper_invariant.py            # exit 1 if drifted >$1
  python3 scripts/check_paper_invariant.py --alert     # also send a Discord alert
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from position_store import load_positions  # noqa: E402
from paper_accounting import balance_drift, rebuild_balance  # noqa: E402

THRESHOLD_USD = 1.00


async def main() -> int:
    bal_file = config.PAPER_BALANCE_FILE
    loaded = 0.0
    if bal_file.exists():
        try:
            loaded = float(json.loads(bal_file.read_text()).get("balance", 0.0))
        except Exception as e:  # noqa: BLE001
            print(f"  could not read {bal_file.name}: {e}")
            return 2

    positions = load_positions(config.PAPER_POSITIONS_FILE)
    realized, open_cost, ledger, open_n = rebuild_balance(positions, config.PAPER_INITIAL_BALANCE)
    drift, _ = balance_drift(loaded, positions, config.PAPER_INITIAL_BALANCE)

    ok = abs(drift) <= THRESHOLD_USD
    print(f"  persisted balance : ${loaded:.2f}")
    print(f"  ledger balance    : ${ledger:.2f}  (initial ${float(config.PAPER_INITIAL_BALANCE):.0f} "
          f"+ realized ${realized:+.2f} − open_cost ${open_cost:.2f}, {open_n} open)")
    print(f"  drift             : ${drift:+.2f}  {'OK' if ok else 'DRIFT > $%.2f' % THRESHOLD_USD}")

    if not ok and "--alert" in sys.argv:
        try:
            from notifications import send_discord_alert
            await send_discord_alert(
                title="⚠ Paper balance drift (manual check)",
                description=f"Persisted ${loaded:.2f} vs ledger ${ledger:.2f} (drift ${drift:+.2f}).",
                color=0xFF6600, context="balance_invariant_cli",
            )
            print("  Discord alert sent")
        except Exception as e:  # noqa: BLE001
            print(f"  alert failed: {e}")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
