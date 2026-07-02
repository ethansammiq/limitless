"""Tests for the shared paper-account settlement + balance math."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from paper_accounting import settle_position_record, rebuild_balance, balance_drift  # noqa: E402

INITIAL = 1000.0


def _pos(ticker="KXHIGHCHI-26JUN23-B72.5", contracts=20, avg_price=40, status="open", pnl=0.0):
    return {"ticker": ticker, "side": "yes", "contracts": contracts,
            "avg_price": avg_price, "status": status, "pnl_realized": pnl}


def test_settle_won_books_payoff_minus_cost():
    p = _pos(contracts=20, avg_price=40)  # cost $8
    spnl = settle_position_record(p, won=True)
    assert spnl == 12.0          # 20*$1 - $8
    assert p["status"] == "settled"
    assert p["settled_result"] == "won"
    assert p["pnl_realized"] == 12.0


def test_settle_lost_books_negative_cost():
    p = _pos(contracts=20, avg_price=40)
    spnl = settle_position_record(p, won=False)
    assert spnl == -8.0
    assert p["settled_result"] == "lost"
    assert p["pnl_realized"] == -8.0


def test_settle_accumulates_onto_existing_pnl():
    p = _pos(contracts=10, avg_price=50, pnl=5.0)  # already +$5 from a freeroll
    settle_position_record(p, won=True)            # +(10 - 5) = +$5
    assert p["pnl_realized"] == 10.0


def test_rebuild_balance_open_position_ties_up_cash():
    positions = [_pos(contracts=20, avg_price=40, status="open")]  # $8 cost
    realized, open_cost, balance, n_open = rebuild_balance(positions, INITIAL)
    assert realized == 0.0
    assert open_cost == 8.0
    assert balance == 992.0
    assert n_open == 1


def test_win_moves_cost_to_realized_net_payoff():
    """The core identity: settling a winner changes cash by exactly +payoff."""
    positions = [_pos(contracts=20, avg_price=40, status="open")]
    _, _, before, _ = rebuild_balance(positions, INITIAL)   # 992.0 (cost tied up)
    settle_position_record(positions[0], won=True)
    _, _, after, _ = rebuild_balance(positions, INITIAL)     # 1012.0
    assert round(after - before, 2) == 20.0                  # 20 contracts * $1 payoff


def test_settled_excluded_from_open_cost_idempotent():
    positions = [_pos(status="settled", pnl=12.0)]
    _, open_cost, balance, n_open = rebuild_balance(positions, INITIAL)
    assert open_cost == 0.0
    assert n_open == 0
    assert balance == 1012.0


def test_balance_drift_zero_when_consistent():
    positions = [_pos(contracts=20, avg_price=40, status="open")]  # ledger = 992.0
    drift, ledger = balance_drift(992.0, positions, INITIAL)
    assert ledger == 992.0
    assert drift == 0.0


def test_balance_drift_detects_inflation():
    positions = [_pos(contracts=20, avg_price=40, status="open")]  # ledger = 992.0
    drift, ledger = balance_drift(3258.62, positions, INITIAL)  # the bug we found
    assert ledger == 992.0
    assert drift == 2266.62  # positive = persisted balance too high


def test_invalid_ticker_and_nonpositive_price_excluded_from_open_cost():
    positions = [
        {"ticker": "T1", "side": "yes", "contracts": 10, "avg_price": -5, "status": "open", "pnl_realized": 0.0},
        _pos(contracts=20, avg_price=0, status="open"),  # zero price -> excluded
    ]
    _, open_cost, balance, _ = rebuild_balance(positions, INITIAL)
    assert open_cost == 0.0
    assert balance == 1000.0
