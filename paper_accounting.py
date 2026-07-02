"""Shared paper-account settlement + balance math — single source of truth.

Used by both scripts/reconcile_paper.py (settles against the historical
backtest/daily_data.jsonl) and position_monitor.py (settles live against Kalshi
market results each cycle). Keeping the cash formula in one place stops the two
paths from drifting apart.

Cash model (idempotent, self-healing):
    balance = initial + Σ realized_pnl(all positions) − Σ cost_basis(open positions)
A win moves a position from the open-cost term into the realized term, for a net
change of +payoff; a loss just drops the cost. Because balance is RECOMPUTED
from the position ledger (not incrementally mutated), concurrent-write races
self-correct on the next cycle instead of accumulating — which is the bug this
replaces (paper_balance.json had drifted to $3,258 vs a true ~$1,044).

This module is import-only; cron does not execute it directly.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import config

ET = ZoneInfo("America/New_York")

# Statuses whose capital is still tied up in the market (counted in open_cost).
OPEN_LIKE = {"open", "pending_sell", "resting"}


def settle_position_record(pos: dict, won: bool, now: datetime | None = None) -> float:
    """Mark a held position settled and book settlement P&L on its held
    contracts. Mutates `pos` in place; returns the settlement P&L in dollars.

    Idempotent at the caller level: callers only pass positions still in an
    open-like status, and this sets status="settled" so a re-run skips it.
    """
    now = now or datetime.now(ET)
    held = int(pos.get("contracts", 0) or 0)
    cost = held * float(pos.get("avg_price", 0) or 0) / 100.0
    payoff = held * 1.0 if won else 0.0
    spnl = round(payoff - cost, 2)
    pos["pnl_realized"] = round(float(pos.get("pnl_realized", 0) or 0) + spnl, 2)
    pos["status"] = "settled"
    pos["settled_result"] = "won" if won else "lost"
    pos["settled_at"] = now.isoformat()
    pos.setdefault("notes", []).append(
        f"{now.isoformat()}: SETTLED {'WON' if won else 'LOST'} "
        f"({held}x, payoff ${payoff:.2f}, cost ${cost:.2f}, pnl ${spnl:+.2f})"
    )
    return spnl


def rebuild_balance(positions: list[dict], initial: float | None = None):
    """Recompute paper cash from first principles.

    Returns (realized_all, open_cost, balance, open_count).
    """
    initial = float(config.PAPER_INITIAL_BALANCE if initial is None else initial)
    realized_all = round(
        sum(float(p.get("pnl_realized", 0) or 0) for p in positions), 2
    )
    open_cost = round(
        sum(
            int(p.get("contracts", 0) or 0) * float(p.get("avg_price", 0) or 0) / 100.0
            for p in positions
            if p.get("status") in OPEN_LIKE
            and str(p.get("ticker", "")).startswith("KXHIGH")
            and (p.get("avg_price", 0) or 0) > 0
        ),
        2,
    )
    balance = round(initial + realized_all - open_cost, 2)
    open_count = sum(1 for p in positions if p.get("status") in OPEN_LIKE)
    return realized_all, open_cost, balance, open_count


def balance_drift(loaded_balance: float, positions: list[dict], initial: float | None = None):
    """How far the persisted cash has drifted from what the ledger implies.

    Returns (drift, ledger_balance) where drift = loaded − ledger (positive means
    the stored balance is too high — the corruption signature). A healthy account
    has |drift| ≈ 0; sub-dollar values are transient timing noise (e.g. a paper
    sell that filled but whose pnl_realized hasn't been booked yet).
    """
    _, _, ledger, _ = rebuild_balance(positions, initial)
    return round(float(loaded_balance) - ledger, 2), ledger
