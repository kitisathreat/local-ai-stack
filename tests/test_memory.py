"""Unit tests for backend/memory.py — plan parser + list/delete."""

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def run(coro):
    return asyncio.run(coro)


# ── _parse_facts ────────────────────────────────────────────────────────

def test_parse_facts_valid_array():
    from backend.memory import _parse_facts
    raw = '["User prefers concise answers.", "User is a Python developer."]'
    facts = _parse_facts(raw)
    assert len(facts) == 2
    assert facts[0].startswith("User prefers")


def test_parse_facts_with_thinking_prefix():
    from backend.memory import _parse_facts
    raw = (
        "<think>Let me figure out what's durable here.</think>"
        '["Works in New York timezone.", "Uses vim."]'
    )
    facts = _parse_facts(raw)
    assert "Works in New York timezone." in facts


def test_parse_facts_empty_array():
    from backend.memory import _parse_facts
    assert _parse_facts("[]") == []


def test_parse_facts_malformed():
    from backend.memory import _parse_facts
    assert _parse_facts("no json here") == []
    assert _parse_facts("this isn't parseable") == []


def test_parse_facts_caps_at_five():
    from backend.memory import _parse_facts
    raw = "[" + ",".join(f'"fact {i}"' for i in range(10)) + "]"
    assert len(_parse_facts(raw)) == 5


def test_parse_facts_filters_invalid_items():
    from backend.memory import _parse_facts
    raw = '["ok fact", 42, null, {"x":1}, "too short", "another valid fact"]'
    facts = _parse_facts(raw)
    assert all(isinstance(f, str) for f in facts)
    # "too short" is length 9 (>=3) so it should be included
    assert "ok fact" in facts
    assert "another valid fact" in facts


# ── format_memory_block ────────────────────────────────────────────────

def test_format_memory_block_empty():
    from backend.memory import format_memory_block
    assert format_memory_block([]) == ""


def test_format_memory_block_bullets():
    from backend.memory import format_memory_block
    hits = [{"content": "Works in Rust.", "memory_id": 1}, {"content": "Prefers tables.", "memory_id": 2}]
    out = format_memory_block(hits)
    assert "- Works in Rust." in out
    assert "- Prefers tables." in out


# ── list_for_user / delete against a tmp DB ─────────────────────────────

@pytest.fixture
def db_path(tmp_path, monkeypatch):
    p = tmp_path / "mem_test.db"
    monkeypatch.setenv("LAI_DB_PATH", str(p))
    import importlib
    from backend import db as db_mod
    importlib.reload(db_mod)
    db_mod.DB_PATH = p
    run(db_mod.init_db())
    return p


def test_list_and_delete_memory(db_path, monkeypatch):
    import time
    from backend import db as db_mod
    from backend import memory

    # Seed a memory directly via SQLite (bypasses Qdrant).
    uid = run(db_mod.create_user(
        username="mem", email="mem@x.io",
        password_hash="$2b$04$stub",
    ))["id"]

    async def seed():
        async with db_mod.get_conn() as c:
            now = time.time()
            await c.execute(
                "INSERT INTO memories (user_id, content, source_conv, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (uid, "User prefers vim.", None, now, now),
            )
            await c.commit()
    run(seed())

    rows = run(memory.list_for_user(uid))
    assert len(rows) == 1
    assert rows[0]["content"] == "User prefers vim."

    # Monkey-patch the Qdrant cleanup to a no-op so the test doesn't need
    # a running Qdrant instance.
    from backend import rag
    async def fake_delete_by_filter(*a, **kw): return 1
    monkeypatch.setattr(rag.qdrant, "delete_by_filter", fake_delete_by_filter)

    deleted = run(memory.delete(uid, rows[0]["id"]))
    assert deleted is True
    assert run(memory.list_for_user(uid)) == []
