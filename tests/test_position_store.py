#!/usr/bin/env python3
"""Tests for position_store.py — file locking, atomic writes, registration."""

import json
import tempfile
from pathlib import Path

import pytest

# We need to patch paths BEFORE importing position_store
_tmpdir = tempfile.mkdtemp()
_test_positions = Path(_tmpdir) / "positions.json"
_test_lock = Path(_tmpdir) / ".positions.lock"


@pytest.fixture(autouse=True)
def _patch_paths(monkeypatch):
    """Redirect position_store to temp files for every test."""
    import position_store
    monkeypatch.setattr(position_store, "POSITIONS_FILE", _test_positions)
    monkeypatch.setattr(position_store, "LOCK_FILE", _test_lock)
    # Clean state each test
    if _test_positions.exists():
        _test_positions.unlink()
    if _test_lock.exists():
        _test_lock.unlink()
    yield


class TestLoadSave:
    """Basic load/save operations."""

    def test_load_empty(self):
        from position_store import load_positions
        assert load_positions() == []

    def test_save_and_load(self):
        from position_store import load_positions, save_positions
        positions = [
            {"ticker": "KXHIGHNY-26FEB11-B36.5", "side": "yes",
             "avg_price": 25, "contracts": 10, "status": "open"},
        ]
        save_positions(positions)
        loaded = load_positions()
        assert len(loaded) == 1
        assert loaded[0]["ticker"] == "KXHIGHNY-26FEB11-B36.5"

    def test_save_is_atomic(self):
        """Verify save uses temp file + rename (no partial writes)."""
        from position_store import save_positions
        positions = [
            {"ticker": "T1", "side": "yes", "avg_price": 20, "contracts": 5, "status": "open"},
            {"ticker": "T2", "side": "no", "avg_price": 30, "contracts": 3, "status": "open"},
        ]
        save_positions(positions)
        # File should be valid JSON at all times
        raw = _test_positions.read_text()
        parsed = json.loads(raw)
        assert len(parsed) == 2

    def test_load_filters_invalid_entries(self):
        """Invalid entries (missing required keys) are silently filtered."""
        from position_store import load_positions
        _test_positions.write_text(json.dumps([
            {"ticker": "VALID", "side": "yes", "avg_price": 20, "contracts": 5, "status": "open"},
            {"ticker": "INVALID"},  # Missing keys
            "not_a_dict",
        ]))
        loaded = load_positions()
        assert len(loaded) == 1
        assert loaded[0]["ticker"] == "VALID"

    def test_load_corrupted_json(self):
        """Corrupted JSON file gets backed up and returns empty."""
        from position_store import load_positions
        _test_positions.write_text("{not valid json}")
        loaded = load_positions()
        assert loaded == []
        # Original file should be renamed to .corrupted.*
        assert not _test_positions.exists() or _test_positions.read_text().strip() == ""


class TestPositionTransaction:
    """Transactional read-modify-write."""

    def test_transaction_modifies_in_place(self):
        from position_store import save_positions, position_transaction, load_positions
        save_positions([
            {"ticker": "T1", "side": "yes", "avg_price": 20, "contracts": 5, "status": "open"},
        ])
        with position_transaction() as positions:
            for p in positions:
                if p["ticker"] == "T1":
                    p["status"] = "closed"
        loaded = load_positions()
        assert loaded[0]["status"] == "closed"

    def test_transaction_rollback_on_error(self):
        """If an exception occurs inside transaction, changes are NOT saved."""
        from position_store import save_positions, position_transaction, load_positions
        save_positions([
            {"ticker": "T1", "side": "yes", "avg_price": 20, "contracts": 5, "status": "open"},
        ])
        with pytest.raises(ValueError):
            with position_transaction() as positions:
                positions[0]["status"] = "should_not_persist"
                raise ValueError("rollback!")
        loaded = load_positions()
        assert loaded[0]["status"] == "open"


class TestRegisterPosition:
    """Position registration (new + averaging)."""

    def test_register_new_position(self):
        from position_store import register_position, load_positions
        register_position("KXHIGHNY-26FEB11-B36.5", "yes", 25, 10, "order123", "RESTING")
        loaded = load_positions()
        assert len(loaded) == 1
        assert loaded[0]["ticker"] == "KXHIGHNY-26FEB11-B36.5"
        assert loaded[0]["avg_price"] == 25
        assert loaded[0]["contracts"] == 10
        assert loaded[0]["status"] == "resting"  # RESTING → resting

    def test_register_executed_status(self):
        from position_store import register_position, load_positions
        register_position("T1", "yes", 30, 5, "order456", "EXECUTED")
        loaded = load_positions()
        assert loaded[0]["status"] == "open"  # EXECUTED → open

    def test_average_into_existing(self):
        from position_store import register_position, load_positions
        register_position("T1", "yes", 20, 10, "order1", "EXECUTED")
        register_position("T1", "yes", 30, 10, "order2", "EXECUTED")
        loaded = load_positions()
        assert len(loaded) == 1  # Should merge, not create second
        assert loaded[0]["contracts"] == 20
        assert loaded[0]["avg_price"] == 25.0  # (20*10 + 30*10) / 20

    def test_average_rejects_zero_total(self):
        """Averaging that would result in zero/negative total raises ValueError."""
        from position_store import register_position, load_positions, save_positions
        save_positions([
            {"ticker": "T1", "side": "yes", "avg_price": 20, "contracts": 0,
             "status": "open", "exit_rules": {}, "notes": []},
        ])
        with pytest.raises(ValueError, match="AVERAGING REJECTED"):
            register_position("T1", "yes", 25, 0, "order_bad", "EXECUTED")
        loaded = load_positions()
        # Original position should be unchanged
        assert loaded[0]["contracts"] == 0

    def test_average_rejects_corrupted_existing(self):
        """Averaging into position with negative price is rejected."""
        from position_store import register_position, load_positions, save_positions
        save_positions([
            {"ticker": "T1", "side": "yes", "avg_price": -5, "contracts": 10,
             "status": "open", "exit_rules": {}, "notes": []},
        ])
        with pytest.raises(ValueError, match="AVERAGING REJECTED"):
            register_position("T1", "yes", 25, 5, "order_bad", "EXECUTED")
        loaded = load_positions()
        # Original position should be unchanged (averaging rejected)
        assert loaded[0]["avg_price"] == -5

    def test_exit_rules_set_on_new_position(self):
        from position_store import register_position, load_positions
        register_position("T1", "yes", 25, 10, "order1", "EXECUTED")
        loaded = load_positions()
        rules = loaded[0].get("exit_rules", {})
        assert rules.get("freeroll_at") == 50  # 25 * 2
        assert rules.get("efficiency_exit") == 90
        assert rules.get("trailing_offset") == 8


class TestValidation:
    """Schema validation."""

    def test_validate_position_all_keys(self):
        from position_store import _validate_position
        valid = {"ticker": "T1", "side": "yes", "avg_price": 20, "contracts": 5, "status": "open"}
        assert _validate_position(valid) is True

    def test_validate_position_missing_key(self):
        from position_store import _validate_position
        invalid = {"ticker": "T1", "side": "yes"}
        assert _validate_position(invalid) is False

    def test_validate_position_not_dict(self):
        from position_store import _validate_position
        assert _validate_position("not a dict") is False
        assert _validate_position(None) is False
        assert _validate_position([]) is False
