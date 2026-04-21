"""Tests for per-user preferences (#17 + #20)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    p = tmp_path / "prefs.db"
    monkeypatch.setenv("LAI_DB_PATH", str(p))
    import importlib
    from backend import db as db_mod
    importlib.reload(db_mod)
    db_mod.DB_PATH = p
    from backend import preferences as pref_mod
    importlib.reload(pref_mod)
    run(db_mod.init_db())
    return p


def _make_user(email: str = "u@x.io") -> int:
    from backend import db as db_mod
    u = run(db_mod.get_or_create_user(email))
    return u["id"]


def test_defaults_when_no_row(db_path):
    from backend import preferences as pref_mod
    uid = _make_user()
    p = run(pref_mod.get_for_user(uid))
    assert p.inject_clarification is True
    assert p.auto_web_search is True
    assert p.rag_top_k == 3
    assert p.rag_min_score == 0.55


def test_patch_upsert_and_readback(db_path):
    from backend import preferences as pref_mod
    uid = _make_user()
    updated = run(pref_mod.update_for_user(uid, {
        "inject_clarification": False,
        "rag_top_k": 7,
        "rag_min_score": 0.7,
    }))
    assert updated.inject_clarification is False
    assert updated.rag_top_k == 7
    # Unrelated keys still at defaults.
    assert updated.auto_web_search is True
    # Read-back matches.
    again = run(pref_mod.get_for_user(uid))
    assert again.inject_clarification is False
    assert again.rag_top_k == 7


def test_patch_clamps_out_of_range(db_path):
    from backend import preferences as pref_mod
    uid = _make_user()
    p = run(pref_mod.update_for_user(uid, {
        "rag_top_k": 10_000,          # → 20
        "rag_min_score": 5.0,         # → 1.0
        "memory_top_k": -3,           # → 1
    }))
    assert p.rag_top_k == 20
    assert p.rag_min_score == 1.0
    assert p.memory_top_k == 1


def test_patch_drops_unknown_keys(db_path):
    from backend import preferences as pref_mod
    uid = _make_user()
    p = run(pref_mod.update_for_user(uid, {
        "bogus_field": "ignored",
        "inject_memories": False,
    }))
    assert p.inject_memories is False


def test_patch_preserves_across_users(db_path):
    from backend import preferences as pref_mod
    a = _make_user("a@x.io")
    b = _make_user("b@x.io")
    run(pref_mod.update_for_user(a, {"inject_memories": False}))
    assert run(pref_mod.get_for_user(a)).inject_memories is False
    # User b should still have defaults.
    assert run(pref_mod.get_for_user(b)).inject_memories is True
