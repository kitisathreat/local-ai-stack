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
    uid = run(db_mod.get_or_create_user("mem@x.io"))["id"]

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


# ── #21: memory.update round-trip ───────────────────────────────────────

def test_update_memory_roundtrip(db_path, monkeypatch):
    """memory.update edits the SQLite row and re-upserts the vector.
    We stub Qdrant + embedding so the test doesn't need a live stack."""
    import time
    from backend import db as db_mod
    from backend import memory, rag

    uid = run(db_mod.get_or_create_user("mem-edit@x.io"))["id"]

    async def seed():
        async with db_mod.get_conn() as c:
            now = time.time()
            await c.execute(
                "INSERT INTO memories (user_id, content, source_conv, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (uid, "User is a Rustacean.", None, now, now),
            )
            await c.commit()
    run(seed())

    # Patch embedding + Qdrant so no external services are hit.
    async def fake_embed(texts): return [[0.1] * 8 for _ in texts]
    monkeypatch.setattr(memory, "embed", fake_embed)

    deletes_seen: list[tuple] = []
    upserts_seen: list[tuple] = []
    async def fake_delete_by_filter(name, filter_):
        deletes_seen.append((name, filter_)); return 1
    async def fake_upsert(name, points):
        upserts_seen.append((name, points))
    monkeypatch.setattr(rag.qdrant, "delete_by_filter", fake_delete_by_filter)
    monkeypatch.setattr(rag.qdrant, "upsert", fake_upsert)

    rows = run(memory.list_for_user(uid))
    mid = rows[0]["id"]

    updated = run(memory.update(uid, mid, "User actually prefers Python."))
    assert updated is not None
    assert updated["content"] == "User actually prefers Python."

    # SQLite row now carries the new content.
    rows = run(memory.list_for_user(uid))
    assert rows[0]["content"] == "User actually prefers Python."

    # Qdrant: one delete + one upsert.
    assert len(deletes_seen) == 1
    assert len(upserts_seen) == 1
    (_, points), = upserts_seen
    assert points[0]["payload"]["memory_id"] == mid


def test_update_memory_rejects_blank(db_path):
    from backend import memory
    with pytest.raises(ValueError):
        run(memory.update(1, 1, "   "))


def test_update_memory_missing_returns_none(db_path, monkeypatch):
    from backend import memory, rag
    async def fake_embed(texts): return [[0.0] * 8 for _ in texts]
    monkeypatch.setattr(memory, "embed", fake_embed)
    async def noop(*a, **kw): return 0
    monkeypatch.setattr(rag.qdrant, "delete_by_filter", noop)
    monkeypatch.setattr(rag.qdrant, "upsert", noop)
    assert run(memory.update(999, 999, "ghost")) is None
