#!/usr/bin/env python3
"""Tests for fills_watch.py — per-strategy P&L attribution."""

import json
import tempfile
from pathlib import Path

_tmpdir = tempfile.mkdtemp()
_test_balance = Path(_tmpdir) / "paper_balance.json"
_test_orders = Path(_tmpdir) / "paper_orders.json"
_test_positions = Path(_tmpdir) / "positions_paper.json"


def _pos(**over):
    base = {
        "ticker": "KXHIGHNY-26FEB11-B36.5", "side": "yes", "avg_price": 25,
        "contracts": 10, "status": "closed", "pnl_realized": 0.0,
        "strategy": "auto_trader", "entry_time": "2026-06-30T15:00:00-04:00",
    }
    base.update(over)
    return base


class TestStrategyPnl:
    """strategy_pnl() aggregation."""

    def test_groups_realized_pnl_by_strategy(self):
        from fills_watch import strategy_pnl
        rows = strategy_pnl([
            _pos(strategy="auto_trader", pnl_realized=2.50),
            _pos(strategy="auto_trader", pnl_realized=-1.00),
            _pos(strategy="peak_trader", pnl_realized=4.00),
        ])
        assert rows["auto_trader"]["pnl"] == 1.50
        assert rows["auto_trader"]["positions"] == 2
        assert rows["peak_trader"]["pnl"] == 4.00

    def test_legacy_records_group_under_untagged(self):
        from fills_watch import strategy_pnl
        legacy = _pos(pnl_realized=3.00)
        del legacy["strategy"]
        rows = strategy_pnl([legacy, _pos(strategy=None, pnl_realized=1.00)])
        assert set(rows) == {"untagged"}
        assert rows["untagged"]["pnl"] == 4.00
        assert rows["untagged"]["positions"] == 2

    def test_excludes_synthetic_and_never_held(self):
        from fills_watch import strategy_pnl
        rows = strategy_pnl([
            _pos(ticker="TEST-SYNTHETIC", pnl_realized=99.0),
            _pos(status="cancelled", pnl_realized=99.0),
            _pos(status="rejected", pnl_realized=99.0),
            _pos(pnl_realized=1.0),
        ])
        assert rows["auto_trader"]["positions"] == 1
        assert rows["auto_trader"]["pnl"] == 1.0

    def test_open_counts_in_flight_statuses(self):
        from fills_watch import strategy_pnl
        rows = strategy_pnl([
            _pos(status="open"),
            _pos(status="resting"),
            _pos(status="pending_sell"),
            _pos(status="closed"),
            _pos(status="settled"),
        ])
        assert rows["auto_trader"]["positions"] == 5
        assert rows["auto_trader"]["open"] == 3

    def test_missing_pnl_treated_as_zero(self):
        from fills_watch import strategy_pnl
        pos = _pos()
        del pos["pnl_realized"]
        rows = strategy_pnl([pos])
        assert rows["auto_trader"]["pnl"] == 0.0

    def test_empty_positions(self):
        from fills_watch import strategy_pnl
        assert strategy_pnl([]) == {}


class TestSnapshotRendering:
    """snapshot() surfaces the strategy breakdown."""

    def test_snapshot_renders_strategy_section(self, monkeypatch):
        import fills_watch
        monkeypatch.setattr(fills_watch, "PAPER_BALANCE_FILE", _test_balance)
        monkeypatch.setattr(fills_watch, "PAPER_ORDERS_FILE", _test_orders)
        monkeypatch.setattr(fills_watch, "PAPER_POSITIONS_FILE", _test_positions)
        _test_balance.write_text(json.dumps({"balance": 505.0, "initial_balance": 500.0}))
        _test_orders.write_text("[]")
        legacy = _pos(ticker="KXHIGHCHI-26JUN30-B85.5", pnl_realized=-1.25)
        del legacy["strategy"]
        _test_positions.write_text(json.dumps([
            _pos(strategy="peak_trader", pnl_realized=3.50),
            legacy,
        ]))

        text, _ = fills_watch.snapshot()
        assert "P&L BY STRATEGY" in text
        assert "peak_trader" in text
        assert "$+3.50" in text
        assert "untagged" in text
        assert "$-1.25" in text
