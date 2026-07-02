#!/usr/bin/env python3
"""Reconcile the paper account: settle stale positions + rebuild the balance.

Two bugs this repairs (see session 2026-06-25):
  1. The paper system has NO settlement crediting — winners are never paid out
     and settled markets never close, so past-dated positions pile up as "open".
  2. paper_balance.json is corrupted: core/broker.py is its only writer and only
     moves balance on fills (8 ever, ~+$59 net), yet it reads $3,258 — legacy
     drift from a since-deleted crediting path plus concurrent-write races.

This tool, run from the project root:
  - Backs up positions_paper.json + paper_balance.json (timestamped).
  - Settles every OPEN/PENDING position whose settlement day is known in
    backtest/daily_data.jsonl, by matching the position ticker against that
    day's `settlements` result (won = result "yes"). Marks it settled+closed,
    books settlement P&L on the held contracts into pnl_realized.
  - Flags clearly-invalid positions (non-KXHIGH ticker or avg_price<=0).
  - Rebuilds the cash balance from first principles:
        cash = initial + Σ realized_pnl(all) − Σ cost_basis(still-open)
    (internally consistent: a win moves a position from open-cost to +realized,
     net effect +payoff; verified by the unit identity in the docstring tests).
  - Prints a reconciled scorecard.

DRY-RUN by default. Pass --apply to write. Idempotent: settled positions are
skipped on re-run. Lives in scripts/ which cron does NOT execute.

  python3 scripts/reconcile_paper.py            # preview
  python3 scripts/reconcile_paper.py --apply     # write changes
"""
from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from position_store import position_transaction  # noqa: E402
from paper_accounting import settle_position_record, rebuild_balance, OPEN_LIKE  # noqa: E402

ET = ZoneInfo("America/New_York")
DAILY = ROOT / "backtest" / "daily_data.jsonl"
PAPER_POSITIONS = config.PAPER_POSITIONS_FILE
PAPER_BALANCE = config.PAPER_BALANCE_FILE
INITIAL = float(config.PAPER_INITIAL_BALANCE)

SETTLEABLE_STATUSES = {"open", "pending_sell"}
_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def parse_ticker_date(ticker: str) -> str | None:
    """KXHIGHCHI-26JUN23-B72.5 -> '2026-06-23' (the LOCAL settlement day)."""
    parts = ticker.split("-")
    if len(parts) < 2:
        return None
    ymd = parts[1]  # e.g. 26JUN23
    try:
        yy, mon, dd = int(ymd[:2]), ymd[2:5].upper(), int(ymd[5:7])
        return f"20{yy:02d}-{_MONTHS[mon]:02d}-{dd:02d}"
    except (ValueError, KeyError):
        return None


def load_settlement_index() -> dict[tuple[str, str], dict[str, str]]:
    """{(series, date): {ticker: result}} from settled outcomes."""
    idx: dict[tuple[str, str], dict[str, str]] = {}
    for line in DAILY.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        date = d.get("date")
        for s in d.get("settlements", []):
            tk = s.get("ticker")
            if not tk:
                continue
            series = tk.split("-")[0]
            idx.setdefault((series, date), {})[tk] = s.get("result")
    return idx


def _settle(positions: list[dict], idx, max_date: str, now, apply: bool) -> dict:
    """Mutate `positions` in place; return stats. Mutation is identical in dry-run
    and apply — only the persistence differs at the call site."""
    st = dict(won=0, lost=0, invalid=0, not_yet=0, data_gap=0, pnl=0.0,
              won_list=[], lost_list=[])
    for p in positions:
        tk = str(p.get("ticker", ""))
        status = p.get("status")

        # cleanup: clearly-invalid entries (junk ticker or non-positive price)
        if not tk.startswith("KXHIGH") or (p.get("avg_price", 0) or 0) <= 0:
            if status in OPEN_LIKE:
                st["invalid"] += 1
                p["status"] = "invalid"
                p.setdefault("notes", []).append(
                    f"{now.isoformat()}: reconcile — flagged invalid "
                    f"(ticker={tk!r}, avg_price={p.get('avg_price')})")
            continue

        if status not in SETTLEABLE_STATUSES:
            continue

        date = parse_ticker_date(tk)
        series = tk.split("-")[0]
        day_results = idx.get((series, date))
        if day_results is None:
            if date and date > max_date:
                st["not_yet"] += 1      # future / today — legitimately still open
            else:
                st["data_gap"] += 1     # in-range but no record collected
            continue

        result = day_results.get(tk)
        if result is None:
            st["data_gap"] += 1
            continue

        won = result == "yes"
        spnl = settle_position_record(p, won, now)  # shared mutation logic
        st["pnl"] += spnl
        (st["won_list"] if won else st["lost_list"]).append(tk)
        st["won" if won else "lost"] += 1
    return st


def reconcile(apply: bool) -> None:
    idx = load_settlement_index()
    max_date = max((k[1] for k in idx), default="")
    now = datetime.now(ET)

    if apply:
        stamp = now.strftime("%Y%m%d_%H%M%S")
        for f in (PAPER_POSITIONS, PAPER_BALANCE):
            if f.exists():
                bak = f.with_suffix(f.suffix + f".bak_{stamp}")
                shutil.copy2(f, bak)
                print(f"  backup: {bak.name}")

    if apply:
        with position_transaction(PAPER_POSITIONS) as positions:
            st = _settle(positions, idx, max_date, now, apply)
            realized_all, open_cost, new_balance, open_count = rebuild_balance(positions, INITIAL)
        # positions written atomically under lock on context exit
    else:
        positions = json.loads(PAPER_POSITIONS.read_text()) if PAPER_POSITIONS.exists() else []
        st = _settle(positions, idx, max_date, now, apply)
        realized_all, open_cost, new_balance, open_count = rebuild_balance(positions, INITIAL)

    settled_won, settled_lost = st["won"], st["lost"]
    settle_pnl_total = st["pnl"]
    not_yet, data_gap, invalid = st["not_yet"], st["data_gap"], st["invalid"]
    won_list = st["won_list"]

    # balance file
    try:
        old_bal = json.loads(PAPER_BALANCE.read_text()).get("balance")
    except Exception:
        old_bal = None

    if apply:
        PAPER_BALANCE.write_text(json.dumps({
            "balance": new_balance,
            "initial_balance": INITIAL,
            "last_updated": now.isoformat(),
            "reconciled_at": now.isoformat(),
            "reconciliation_note": "cash = initial + Σrealized(all) − Σcost_basis(open)",
        }, indent=2))

    # ── scorecard ──
    print("\n" + "═" * 58)
    print(f"  PAPER RECONCILIATION  ({'APPLIED' if apply else 'DRY-RUN'})")
    print("═" * 58)
    print(f"  daily_data settlement coverage thru: {max_date}")
    print(f"  settled WON :   {settled_won}")
    print(f"  settled LOST:   {settled_lost}")
    if settled_won + settled_lost:
        wr = 100 * settled_won / (settled_won + settled_lost)
        print(f"  settle win rate: {wr:.1f}%   settlement P&L: ${settle_pnl_total:+.2f}")
    print(f"  left open (not yet settled / future): {not_yet}")
    print(f"  data-gap (no settlement record):      {data_gap}")
    print(f"  flagged invalid:                      {invalid}")
    print("  " + "-" * 54)
    print(f"  realized P&L (lifetime, all positions): ${realized_all:+.2f}")
    print(f"  capital in open positions (cost basis): ${open_cost:.2f}  ({open_count} open)")
    print(f"  balance was : ${old_bal if old_bal is not None else 'n/a'}")
    print(f"  balance now : ${new_balance:.2f}   (initial ${INITIAL:.0f})")
    print(f"  reconciled equity (cash + open@cost):   ${new_balance + open_cost:.2f}")
    print("═" * 58)
    if won_list:
        print("  winners settled:", ", ".join(won_list[:12]) + (" ..." if len(won_list) > 12 else ""))
    if not apply:
        print("\n  DRY-RUN — no files changed. Re-run with --apply to write.")


if __name__ == "__main__":
    reconcile(apply="--apply" in sys.argv)
