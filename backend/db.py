"""SQLite persistence for users, conversations, and messages.

Schema is created on first connection. Uses `aiosqlite` so DB access is
non-blocking and cooperative with the FastAPI event loop.

One database file per process, path configurable via LAI_DB_PATH env var
(defaults to /app/data/lai.db inside the container). The surrounding
`data/` volume is mounted from the host in docker-compose.yml.

Memory distillation and per-user RAG collections land in Phase 5 —
their tables are added here so Phase 4 can seed them, but they're
unused until Phase 5.
"""

from __future__ import annotations

import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import aiosqlite


DB_PATH = Path(os.getenv("LAI_DB_PATH", "/app/data/lai.db"))


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE DEFAULT '',
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL DEFAULT '',
    is_admin      INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL,
    last_login_at REAL
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
-- idx_users_username is created AFTER the v2->v3 column migration in
-- init_db(); putting it here would fail on legacy DBs that still have
-- only the pre-v3 users table.

CREATE TABLE IF NOT EXISTS conversations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title           TEXT NOT NULL DEFAULT 'New chat',
    tier            TEXT,
    -- When 0, this chat is skipped by memory distillation AND its
    -- messages are NOT appended to the encrypted per-user history log.
    -- Default 1: opt-out, not opt-in.
    memory_enabled  INTEGER NOT NULL DEFAULT 1,
    -- When 1, this chat was created under airgap mode. Its message
    -- content is stored encrypted, its history file is the airgap
    -- variant, and it is hidden from conversation listings unless the
    -- server is currently in the same airgap state.
    airgap          INTEGER NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id  INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role             TEXT NOT NULL,
    -- For airgap conversations this column holds a prefixed base64
    -- AES-256-GCM ciphertext (see history_store.encrypt_value); for
    -- normal conversations it's plaintext. The `encrypted` flag lets
    -- readers route each row to the right decode path.
    content          TEXT NOT NULL,
    encrypted        INTEGER NOT NULL DEFAULT 0,
    tier             TEXT,
    think            INTEGER,
    tokens_in        INTEGER,
    tokens_out       INTEGER,
    created_at       REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conversation_id, created_at);

-- Phase 5: per-user memory entries (distilled from conversations).
-- Airgap memories live in the same table but with airgap=1 and an
-- encrypted content column, plus a separate Qdrant collection. They
-- are not returned from non-airgap listings.
CREATE TABLE IF NOT EXISTS memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    content     TEXT NOT NULL,
    encrypted   INTEGER NOT NULL DEFAULT 0,
    airgap      INTEGER NOT NULL DEFAULT 0,
    source_conv INTEGER REFERENCES conversations(id) ON DELETE SET NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mem_user ON memories(user_id, updated_at DESC);

-- Phase 5: per-user RAG document metadata (actual vectors live in Qdrant).
CREATE TABLE IF NOT EXISTS rag_docs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    filename    TEXT NOT NULL,
    mime_type   TEXT,
    size_bytes  INTEGER,
    chunk_count INTEGER,
    qdrant_ids  TEXT,
    created_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rag_user ON rag_docs(user_id, created_at DESC);

-- Admin dashboard: per-request usage events. user_id is nullable so
-- anonymous requests (no session cookie) still record for VRAM/tier stats.
CREATE TABLE IF NOT EXISTS usage_events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER REFERENCES users(id) ON DELETE SET NULL,
    ts               REAL NOT NULL,
    tier             TEXT NOT NULL,
    think            INTEGER NOT NULL DEFAULT 0,
    multi_agent      INTEGER NOT NULL DEFAULT 0,
    tokens_in        INTEGER NOT NULL DEFAULT 0,
    tokens_out       INTEGER NOT NULL DEFAULT 0,
    latency_ms       INTEGER NOT NULL DEFAULT 0,
    error            TEXT
);

CREATE INDEX IF NOT EXISTS idx_usage_ts   ON usage_events(ts DESC);
CREATE INDEX IF NOT EXISTS idx_usage_user ON usage_events(user_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_usage_tier ON usage_events(tier, ts DESC);
"""


async def _open() -> aiosqlite.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(str(DB_PATH))
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await conn.execute("PRAGMA journal_mode = WAL")
    return conn


@asynccontextmanager
async def get_conn() -> AsyncIterator[aiosqlite.Connection]:
    conn = await _open()
    try:
        yield conn
    finally:
        await conn.close()


async def init_db() -> None:
    """Create tables if missing. Idempotent — safe to call every startup."""
    async with get_conn() as c:
        await c.executescript(SCHEMA)
        await _migrate_add_column(
            c, "conversations", "memory_enabled", "INTEGER NOT NULL DEFAULT 1",
        )
        await _migrate_add_column(
            c, "conversations", "airgap", "INTEGER NOT NULL DEFAULT 0",
        )
        await _migrate_add_column(
            c, "messages", "encrypted", "INTEGER NOT NULL DEFAULT 0",
        )
        await _migrate_add_column(
            c, "memories", "encrypted", "INTEGER NOT NULL DEFAULT 0",
        )
        await _migrate_add_column(
            c, "memories", "airgap", "INTEGER NOT NULL DEFAULT 0",
        )
        # Password-auth migration (Phase 3): add columns on pre-existing
        # users tables, drop the magic_links table entirely.
        await _migrate_add_column(
            c, "users", "username", "TEXT NOT NULL DEFAULT ''",
        )
        await _migrate_add_column(
            c, "users", "password_hash", "TEXT NOT NULL DEFAULT ''",
        )
        await _migrate_add_column(
            c, "users", "is_admin", "INTEGER NOT NULL DEFAULT 0",
        )
        await c.execute("DROP TABLE IF EXISTS magic_links")
        # Safe to create the username index now — the column exists
        # either from a fresh SCHEMA or from the migration above.
        await c.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)"
        )
        await c.commit()


async def _migrate_add_column(
    conn: aiosqlite.Connection, table: str, column: str, type_sql: str,
) -> None:
    """Add a column if it doesn't exist (SQLite has no ADD COLUMN IF NOT EXISTS)."""
    rows = await (await conn.execute(f"PRAGMA table_info({table})")).fetchall()
    if any(r["name"] == column for r in rows):
        return
    await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {type_sql}")


# ── Users (password auth) ───────────────────────────────────────────────

_USER_COLS = "id, username, email, password_hash, is_admin, created_at, last_login_at"


def _user_row(row) -> dict | None:
    if not row:
        return None
    d = dict(row)
    d["is_admin"] = bool(d.get("is_admin"))
    return d


async def create_user(
    *,
    username: str,
    email: str,
    password_hash: str,
    is_admin: bool = False,
) -> dict:
    """Insert a new user. Raises sqlite3.IntegrityError on duplicate
    username or email; callers map that to HTTP 409."""
    username = username.strip()
    email = email.lower().strip()
    if not username:
        raise ValueError("username must not be empty")
    now = time.time()
    async with get_conn() as c:
        cur = await c.execute(
            "INSERT INTO users (username, email, password_hash, is_admin, "
            "created_at, last_login_at) VALUES (?, ?, ?, ?, ?, NULL)",
            (username, email, password_hash, 1 if is_admin else 0, now),
        )
        await c.commit()
        row = await (await c.execute(
            f"SELECT {_USER_COLS} FROM users WHERE id = ?",
            (cur.lastrowid,),
        )).fetchone()
        return _user_row(row)


async def get_user(user_id: int) -> dict | None:
    async with get_conn() as c:
        row = await (await c.execute(
            f"SELECT {_USER_COLS} FROM users WHERE id = ?", (user_id,),
        )).fetchone()
        return _user_row(row)


async def get_user_by_username(username: str) -> dict | None:
    username = username.strip()
    if not username:
        return None
    async with get_conn() as c:
        row = await (await c.execute(
            f"SELECT {_USER_COLS} FROM users WHERE username = ?",
            (username,),
        )).fetchone()
        return _user_row(row)


async def list_users() -> list[dict]:
    async with get_conn() as c:
        rows = await (await c.execute(
            f"SELECT {_USER_COLS} FROM users ORDER BY created_at ASC"
        )).fetchall()
        return [_user_row(r) for r in rows]


async def count_admins() -> int:
    async with get_conn() as c:
        row = await (await c.execute(
            "SELECT COUNT(*) AS n FROM users WHERE is_admin = 1"
        )).fetchone()
        return int(row["n"])


async def mark_login(user_id: int) -> None:
    async with get_conn() as c:
        await c.execute(
            "UPDATE users SET last_login_at = ? WHERE id = ?",
            (time.time(), user_id),
        )
        await c.commit()


async def set_user_password(user_id: int, password_hash: str) -> bool:
    async with get_conn() as c:
        cur = await c.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (password_hash, user_id),
        )
        await c.commit()
        return cur.rowcount > 0


async def set_user_admin(user_id: int, is_admin: bool) -> bool:
    async with get_conn() as c:
        cur = await c.execute(
            "UPDATE users SET is_admin = ? WHERE id = ?",
            (1 if is_admin else 0, user_id),
        )
        await c.commit()
        return cur.rowcount > 0


async def update_user_fields(
    user_id: int,
    *,
    username: str | None = None,
    email: str | None = None,
) -> bool:
    sets, params = [], []
    if username is not None:
        sets.append("username = ?"); params.append(username.strip())
    if email is not None:
        sets.append("email = ?"); params.append(email.lower().strip())
    if not sets:
        return False
    params.append(user_id)
    async with get_conn() as c:
        cur = await c.execute(
            f"UPDATE users SET {', '.join(sets)} WHERE id = ?", params,
        )
        await c.commit()
        return cur.rowcount > 0


async def delete_user(user_id: int) -> bool:
    async with get_conn() as c:
        cur = await c.execute("DELETE FROM users WHERE id = ?", (user_id,))
        await c.commit()
        return cur.rowcount > 0


# ── Conversations ─────────────────────────────────────────────────────────

async def list_conversations(
    user_id: int, limit: int = 100, *, airgap: bool | None = None,
) -> list[dict]:
    """List a user's conversations. `airgap=True|False` filters strictly
    by mode so the UI only ever sees chats from the mode it's currently
    rendering; `airgap=None` returns both (admin-only use)."""
    sql = (
        "SELECT id, title, tier, memory_enabled, airgap, created_at, updated_at "
        "FROM conversations WHERE user_id = ?"
    )
    params: list[Any] = [user_id]
    if airgap is not None:
        sql += " AND airgap = ?"
        params.append(1 if airgap else 0)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(limit)
    async with get_conn() as c:
        rows = await (await c.execute(sql, params)).fetchall()
        return [_conv_row(r) for r in rows]


async def create_conversation(
    user_id: int,
    title: str = "New chat",
    tier: str | None = None,
    *,
    memory_enabled: bool = True,
    airgap: bool = False,
) -> dict:
    now = time.time()
    async with get_conn() as c:
        cur = await c.execute(
            "INSERT INTO conversations (user_id, title, tier, memory_enabled, airgap, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, title, tier, 1 if memory_enabled else 0,
             1 if airgap else 0, now, now),
        )
        await c.commit()
        return {
            "id": cur.lastrowid, "title": title, "tier": tier,
            "memory_enabled": bool(memory_enabled),
            "airgap": bool(airgap),
            "created_at": now, "updated_at": now,
        }


async def get_conversation(conv_id: int, user_id: int) -> dict | None:
    async with get_conn() as c:
        row = await (await c.execute(
            "SELECT id, title, tier, memory_enabled, airgap, created_at, updated_at "
            "FROM conversations WHERE id = ? AND user_id = ?",
            (conv_id, user_id),
        )).fetchone()
        return _conv_row(row) if row else None


async def update_conversation(
    conv_id: int,
    user_id: int,
    *,
    title: str | None = None,
    tier: str | None = None,
    memory_enabled: bool | None = None,
) -> bool:
    now = time.time()
    sets, params = [], []
    if title is not None:
        sets.append("title = ?"); params.append(title)
    if tier is not None:
        sets.append("tier = ?"); params.append(tier)
    if memory_enabled is not None:
        sets.append("memory_enabled = ?"); params.append(1 if memory_enabled else 0)
    sets.append("updated_at = ?"); params.append(now)
    params.extend([conv_id, user_id])
    async with get_conn() as c:
        cur = await c.execute(
            f"UPDATE conversations SET {', '.join(sets)} WHERE id = ? AND user_id = ?",
            params,
        )
        await c.commit()
        return cur.rowcount > 0


def _conv_row(row: aiosqlite.Row) -> dict:
    d = dict(row)
    # SQLite stores booleans as 0/1 INTEGER; normalize for the API.
    if "memory_enabled" in d:
        d["memory_enabled"] = bool(d["memory_enabled"])
    if "airgap" in d:
        d["airgap"] = bool(d["airgap"])
    return d


async def delete_conversation(conv_id: int, user_id: int) -> bool:
    async with get_conn() as c:
        cur = await c.execute(
            "DELETE FROM conversations WHERE id = ? AND user_id = ?",
            (conv_id, user_id),
        )
        await c.commit()
        return cur.rowcount > 0


# ── Messages ──────────────────────────────────────────────────────────────

async def list_messages(conv_id: int) -> list[dict]:
    """Return messages in insertion order, decrypting any rows whose
    `encrypted=1` marker is set. Decryption keys are derived from the
    owning user's airgap salt — we look up the user_id once from the
    conversation row and pass it to the decrypt helper."""
    from . import history_store
    async with get_conn() as c:
        owner = await (await c.execute(
            "SELECT user_id, airgap FROM conversations WHERE id = ?", (conv_id,),
        )).fetchone()
        if not owner:
            return []
        user_id = int(owner["user_id"])
        rows = await (await c.execute(
            "SELECT id, role, content, encrypted, tier, think, tokens_in, tokens_out, created_at "
            "FROM messages WHERE conversation_id = ? ORDER BY created_at ASC",
            (conv_id,),
        )).fetchall()
        out: list[dict] = []
        for r in rows:
            d = dict(r)
            if d.pop("encrypted", 0):
                d["content"] = history_store.decrypt_value(
                    user_id, d.get("content") or "", scope="msg", airgap=True,
                )
            out.append(d)
        return out


async def add_message(
    conv_id: int, role: str, content: str,
    *, tier: str | None = None, think: bool | None = None,
    tokens_in: int | None = None, tokens_out: int | None = None,
) -> dict:
    """Insert a message. For airgap conversations the content is
    encrypted on the way in; non-airgap conversations store plaintext."""
    from . import history_store
    now = time.time()
    async with get_conn() as c:
        conv = await (await c.execute(
            "SELECT user_id, airgap FROM conversations WHERE id = ?", (conv_id,),
        )).fetchone()
        if not conv:
            raise ValueError(f"Conversation {conv_id} not found")
        is_airgap = bool(conv["airgap"])
        stored = content
        encrypted_flag = 0
        if is_airgap:
            stored = history_store.encrypt_value(
                int(conv["user_id"]), content, scope="msg", airgap=True,
            )
            encrypted_flag = 1
        cur = await c.execute(
            "INSERT INTO messages (conversation_id, role, content, encrypted, tier, "
            "think, tokens_in, tokens_out, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (conv_id, role, stored, encrypted_flag, tier,
             1 if think else (0 if think is False else None),
             tokens_in, tokens_out, now),
        )
        await c.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (now, conv_id),
        )
        await c.commit()
        return {"id": cur.lastrowid, "role": role, "content": content,
                "tier": tier, "think": think, "created_at": now}
