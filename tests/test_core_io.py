"""Tests for the atomic-write helpers."""
import json

from core.io import atomic_write_json, atomic_write_text


def test_atomic_write_text_writes_and_cleans_tmp(tmp_path):
    p = tmp_path / "state.json"
    atomic_write_text(p, "hello")
    assert p.read_text() == "hello"
    assert not (tmp_path / "state.json.tmp").exists()   # tmp renamed away


def test_atomic_write_json_roundtrips(tmp_path):
    p = tmp_path / "s.json"
    atomic_write_json(p, {"seen": {"MDW:042136": "t"}})
    assert json.loads(p.read_text())["seen"]["MDW:042136"] == "t"


def test_overwrite_is_atomic_replace(tmp_path):
    p = tmp_path / "s.json"
    atomic_write_json(p, {"v": 1})
    atomic_write_json(p, {"v": 2})
    assert json.loads(p.read_text())["v"] == 2
