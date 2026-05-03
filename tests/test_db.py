"""Unit tests for backend/db.py — in-memory-free, per-test SQLite file.

Phase 3: magic-link tables + helpers were replaced by username/password
columns on `users`. All user tests go through `create_user` now;
magic-link tests are gone.
"""

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """Redirect backend.db to a clean sqlite file per test."""
    p = tmp_path / "test.db"
    monkeypatch.setenv("LAI_DB_PATH", str(p))
    monkeypatch.setenv("BCRYPT_ROUNDS", "4")
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


def _mkuser(db_mod, username: str, email: str, *, is_admin: bool = False):
    # Dummy password_hash — not exercised by these tests.
    return run(db_mod.create_user(
        username=username, email=email,
        password_hash="$2b$04$stub",
        is_admin=is_admin,
    ))


# ── Users (password auth schema) ────────────────────────────────────────

def test_create_user_minimal(db):
    u = _mkuser(db, "alice", "Alice@Example.com")
    assert u["username"] == "alice"
    assert u["email"] == "alice@example.com"
    assert u["is_admin"] is False
    assert u["id"] > 0


def test_create_user_is_admin_flag_roundtrips(db):
    u = _mkuser(db, "root", "root@x.io", is_admin=True)
    assert u["is_admin"] is True
    fetched = run(db.get_user(u["id"]))
    assert fetched["is_admin"] is True


def test_create_user_duplicate_username_rejected(db):
    _mkuser(db, "dup", "dup1@x.io")
    import aiosqlite
    with pytest.raises(aiosqlite.IntegrityError):
        _mkuser(db, "dup", "dup2@x.io")


def test_create_user_duplicate_email_rejected(db):
    _mkuser(db, "u1", "same@x.io")
    import aiosqlite
    with pytest.raises(aiosqlite.IntegrityError):
        _mkuser(db, "u2", "same@x.io")


def test_get_user_by_username(db):
    u = _mkuser(db, "lookup", "lookup@x.io")
    fetched = run(db.get_user_by_username("lookup"))
    assert fetched is not None and fetched["id"] == u["id"]
    assert run(db.get_user_by_username("nobody")) is None


def test_mark_login_updates_last_login_at(db):
    u = _mkuser(db, "timestamp", "ts@x.io")
    assert u["last_login_at"] is None
    run(db.mark_login(u["id"]))
    refetched = run(db.get_user(u["id"]))
    assert refetched["last_login_at"] is not None


def test_set_user_password(db):
    u = _mkuser(db, "pwchange", "pw@x.io")
    ok = run(db.set_user_password(u["id"], "$2b$04$newhash"))
    assert ok
    refetched = run(db.get_user(u["id"]))
    assert refetched["password_hash"] == "$2b$04$newhash"


def test_set_user_admin(db):
    u = _mkuser(db, "promote", "promote@x.io")
    assert u["is_admin"] is False
    run(db.set_user_admin(u["id"], True))
    assert run(db.get_user(u["id"]))["is_admin"] is True
    run(db.set_user_admin(u["id"], False))
    assert run(db.get_user(u["id"]))["is_admin"] is False


def test_count_admins(db):
    _mkuser(db, "nonadmin", "n@x.io")
    _mkuser(db, "adminA", "a@x.io", is_admin=True)
    _mkuser(db, "adminB", "b@x.io", is_admin=True)
    assert run(db.count_admins()) == 2


def test_migrate_v2_to_v3_adds_columns(db_path):
    """Pre-Phase-3 DBs (no username/password columns, magic_links table
    present) must upgrade cleanly."""
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE,
            created_at REAL NOT NULL,
            last_login_at REAL
        );
        CREATE TABLE magic_links (
            token TEXT PRIMARY KEY,
            email TEXT NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            used_at REAL,
            ip TEXT
        );
        INSERT INTO users (email, created_at) VALUES ('legacy@x.io', 0);
        """
    )
    conn.commit()
    conn.close()

    # Run init_db() to trigger the migration path.
    from backend import db as db_mod
    import importlib
    importlib.reload(db_mod)
    db_mod.DB_PATH = db_path
    run(db_mod.init_db())

    conn = sqlite3.connect(str(db_path))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    assert {"username", "password_hash", "is_admin"}.issubset(cols)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "magic_links" not in tables
    conn.close()


# ── Conversations ────────────────────────────────────────────────────────

def test_conversation_crud(db):
    u = _mkuser(db, "g", "g@x.io")
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


def test_conversation_memory_enabled_defaults_on(db):
    u = _mkuser(db, "memdefault", "mem-default@x.io")
    conv = run(db.create_conversation(u["id"], title="Default"))
    assert conv["memory_enabled"] is True
    fetched = run(db.get_conversation(conv["id"], u["id"]))
    assert fetched["memory_enabled"] is True


def test_conversation_memory_enabled_toggle(db):
    u = _mkuser(db, "memtoggle", "mem-toggle@x.io")
    conv = run(db.create_conversation(u["id"], memory_enabled=True))
    ok = run(db.update_conversation(conv["id"], u["id"], memory_enabled=False))
    assert ok
    assert run(db.get_conversation(conv["id"], u["id"]))["memory_enabled"] is False
    convs = run(db.list_conversations(u["id"]))
    assert convs[0]["memory_enabled"] is False


def test_conversation_user_isolation(db):
    a = _mkuser(db, "h", "h@x.io")
    b = _mkuser(db, "i", "i@x.io")
    conv = run(db.create_conversation(a["id"], title="A's chat"))

    assert run(db.get_conversation(conv["id"], b["id"])) is None
    assert run(db.list_conversations(b["id"])) == []

    assert run(db.delete_conversation(conv["id"], b["id"])) is False
    assert run(db.get_conversation(conv["id"], a["id"])) is not None


# ── Messages ─────────────────────────────────────────────────────────────

def test_messages_add_and_list(db):
    u = _mkuser(db, "j", "j@x.io")
    conv = run(db.create_conversation(u["id"]))
    run(db.add_message(conv["id"], "user", "Hello", tier="versatile", think=False))
    run(db.add_message(conv["id"], "assistant", "Hi there", tier="versatile", think=True,
                       tokens_in=5, tokens_out=12))
    msgs = run(db.list_messages(conv["id"]))
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user" and msgs[0]["content"] == "Hello"
    assert msgs[1]["role"] == "assistant" and msgs[1]["think"] == 1


def test_deleting_conversation_cascades_messages(db):
    u = _mkuser(db, "k", "k@x.io")
    conv = run(db.create_conversation(u["id"]))
    run(db.add_message(conv["id"], "user", "x"))
    run(db.delete_conversation(conv["id"], u["id"]))
    assert run(db.list_messages(conv["id"])) == []


def test_delete_messages_from_truncates_conversation(db):
    """Edit-and-rewind flow: delete_messages_from drops the pivot row
    AND every later row, leaving prior turns intact."""
    u = _mkuser(db, "rw", "rw@x.io")
    conv = run(db.create_conversation(u["id"]))
    m1 = run(db.add_message(conv["id"], "user", "first"))
    m2 = run(db.add_message(conv["id"], "assistant", "first reply"))
    m3 = run(db.add_message(conv["id"], "user", "second"))
    run(db.add_message(conv["id"], "assistant", "second reply"))
    removed = run(db.delete_messages_from(conv["id"], u["id"], m3["id"]))
    assert removed == 2
    msgs = run(db.list_messages(conv["id"]))
    assert [m["id"] for m in msgs] == [m1["id"], m2["id"]]


def test_delete_messages_from_rejects_other_users(db):
    """Cross-user truncate must be a no-op so users can't nuke each
    other's conversations by guessing message ids."""
    owner = _mkuser(db, "own", "own@x.io")
    other = _mkuser(db, "oth", "oth@x.io")
    conv = run(db.create_conversation(owner["id"]))
    m = run(db.add_message(conv["id"], "user", "mine"))
    removed = run(db.delete_messages_from(conv["id"], other["id"], m["id"]))
    assert removed == 0
    assert len(run(db.list_messages(conv["id"]))) == 1


def test_delete_messages_from_unknown_pivot_is_noop(db):
    u = _mkuser(db, "np", "np@x.io")
    conv = run(db.create_conversation(u["id"]))
    run(db.add_message(conv["id"], "user", "x"))
    removed = run(db.delete_messages_from(conv["id"], u["id"], 999_999))
    assert removed == 0
    assert len(run(db.list_messages(conv["id"]))) == 1
