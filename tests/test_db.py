"""Unit tests for backend/db.py — in-memory-free, per-test SQLite file."""

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """Redirect backend.db to a clean sqlite file per test."""
    p = tmp_path / "test.db"
    monkeypatch.setenv("LAI_DB_PATH", str(p))
    # backend.db caches DB_PATH at import; force re-read.
    import importlib
    from backend import db as db_mod
    db_mod.DB_PATH = p
    importlib.reload(db_mod)
    db_mod.DB_PATH = p   # reload reset it
    return p


@pytest.fixture
def db(db_path):
    from backend import db as db_mod
    asyncio.run(db_mod.init_db())
    return db_mod


def run(coro):
    return asyncio.run(coro)


# ── Users ────────────────────────────────────────────────────────────────

def test_create_user(db):
    u = run(db.get_or_create_user("Alice@Example.com"))
    assert u["email"] == "alice@example.com"
    assert u["id"] > 0


def test_create_user_is_idempotent(db):
    u1 = run(db.get_or_create_user("a@x.io"))
    u2 = run(db.get_or_create_user("a@x.io"))
    assert u1["id"] == u2["id"]


def test_get_user(db):
    u = run(db.get_or_create_user("b@x.io"))
    fetched = run(db.get_user(u["id"]))
    assert fetched["email"] == "b@x.io"
    assert run(db.get_user(99999)) is None


# ── Magic links ──────────────────────────────────────────────────────────

def test_magic_link_roundtrip(db):
    tok = run(db.create_magic_link("c@x.io", expiry_seconds=60, ip="1.2.3.4"))
    assert tok
    consumed = run(db.consume_magic_link(tok))
    assert consumed == {"email": "c@x.io"}


def test_magic_link_single_use(db):
    tok = run(db.create_magic_link("d@x.io", expiry_seconds=60, ip=None))
    assert run(db.consume_magic_link(tok))
    assert run(db.consume_magic_link(tok)) is None


def test_magic_link_expires(db):
    tok = run(db.create_magic_link("e@x.io", expiry_seconds=-1, ip=None))
    assert run(db.consume_magic_link(tok)) is None


def test_magic_link_unknown_token(db):
    assert run(db.consume_magic_link("does-not-exist")) is None


def test_magic_link_rate_counter(db):
    for _ in range(3):
        run(db.create_magic_link("f@x.io", 60, None))
    assert run(db.count_recent_magic_links_for_email("f@x.io", 3600)) == 3
    assert run(db.count_recent_magic_links_for_email("nobody@x.io", 3600)) == 0


# ── Conversations ────────────────────────────────────────────────────────

def test_conversation_crud(db):
    u = run(db.get_or_create_user("g@x.io"))
    assert run(db.list_conversations(u["id"])) == []

    conv = run(db.create_conversation(u["id"], title="Hello", tier="versatile"))
    assert conv["id"] > 0 and conv["title"] == "Hello"

    convs = run(db.list_conversations(u["id"]))
    assert len(convs) == 1 and convs[0]["title"] == "Hello"

    fetched = run(db.get_conversation(conv["id"], u["id"]))
    assert fetched["tier"] == "versatile"

    ok = run(db.update_conversation(conv["id"], u["id"], title="Renamed"))
    assert ok
    assert run(db.get_conversation(conv["id"], u["id"]))["title"] == "Renamed"

    assert run(db.delete_conversation(conv["id"], u["id"]))
    assert run(db.get_conversation(conv["id"], u["id"])) is None


def test_conversation_user_isolation(db):
    a = run(db.get_or_create_user("h@x.io"))
    b = run(db.get_or_create_user("i@x.io"))
    conv = run(db.create_conversation(a["id"], title="A's chat"))

    # b cannot see a's conversation
    assert run(db.get_conversation(conv["id"], b["id"])) is None
    assert run(db.list_conversations(b["id"])) == []

    # b cannot delete a's conversation
    assert run(db.delete_conversation(conv["id"], b["id"])) is False
    assert run(db.get_conversation(conv["id"], a["id"])) is not None


# ── Messages ─────────────────────────────────────────────────────────────

def test_messages_add_and_list(db):
    u = run(db.get_or_create_user("j@x.io"))
    conv = run(db.create_conversation(u["id"]))
    run(db.add_message(conv["id"], "user", "Hello", tier="versatile", think=False))
    run(db.add_message(conv["id"], "assistant", "Hi there", tier="versatile", think=True,
                       tokens_in=5, tokens_out=12))
    msgs = run(db.list_messages(conv["id"]))
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user" and msgs[0]["content"] == "Hello"
    assert msgs[1]["role"] == "assistant" and msgs[1]["think"] == 1


def test_deleting_conversation_cascades_messages(db):
    u = run(db.get_or_create_user("k@x.io"))
    conv = run(db.create_conversation(u["id"]))
    run(db.add_message(conv["id"], "user", "x"))
    run(db.delete_conversation(conv["id"], u["id"]))
    # After conversation is gone, list_messages returns empty
    assert run(db.list_messages(conv["id"])) == []
