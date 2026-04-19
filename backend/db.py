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
    email         TEXT NOT NULL UNIQUE,
    created_at    REAL NOT NULL,
    last_login_at REAL
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);

CREATE TABLE IF NOT EXISTS magic_links (
    token      TEXT PRIMARY KEY,
    email      TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    used_at    REAL,
    ip         TEXT
);

CREATE INDEX IF NOT EXISTS idx_magic_links_email ON magic_links(email, created_at);

CREATE TABLE IF NOT EXISTS conversations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title       TEXT NOT NULL DEFAULT 'New chat',
    tier        TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id  INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role             TEXT NOT NULL,
    content          TEXT NOT NULL,
    tier             TEXT,
    think            INTEGER,
    tokens_in        INTEGER,
    tokens_out       INTEGER,
    created_at       REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conversation_id, created_at);

-- Phase 5: per-user memory entries (distilled from conversations).
CREATE TABLE IF NOT EXISTS memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    content     TEXT NOT NULL,
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
        await c.commit()


# ── Users ────────────────────────────────────────────────────────────────

async def get_or_create_user(email: str) -> dict[str, Any]:
    email = email.lower().strip()
    now = time.time()
    async with get_conn() as c:
        row = await (await c.execute(
            "SELECT id, email, created_at, last_login_at FROM users WHERE email = ?",
            (email,),
        )).fetchone()
        if row:
            await c.execute(
                "UPDATE users SET last_login_at = ? WHERE id = ?",
                (now, row["id"]),
            )
            await c.commit()
            return dict(row)
        await c.execute(
            "INSERT INTO users (email, created_at, last_login_at) VALUES (?, ?, ?)",
            (email, now, now),
        )
        await c.commit()
        row = await (await c.execute(
            "SELECT id, email, created_at, last_login_at FROM users WHERE email = ?",
            (email,),
        )).fetchone()
        return dict(row)


async def get_user(user_id: int) -> dict | None:
    async with get_conn() as c:
        row = await (await c.execute(
            "SELECT id, email, created_at, last_login_at FROM users WHERE id = ?",
            (user_id,),
        )).fetchone()
        return dict(row) if row else None


# ── Magic links ──────────────────────────────────────────────────────────

async def create_magic_link(email: str, expiry_seconds: int, ip: str | None) -> str:
    email = email.lower().strip()
    token = secrets.token_urlsafe(32)
    now = time.time()
    async with get_conn() as c:
        await c.execute(
            "INSERT INTO magic_links (token, email, created_at, expires_at, ip) "
            "VALUES (?, ?, ?, ?, ?)",
            (token, email, now, now + expiry_seconds, ip),
        )
        await c.commit()
    return token


async def count_recent_magic_links_for_email(email: str, window_seconds: int) -> int:
    email = email.lower().strip()
    since = time.time() - window_seconds
    async with get_conn() as c:
        row = await (await c.execute(
            "SELECT COUNT(*) AS n FROM magic_links WHERE email = ? AND created_at >= ?",
            (email, since),
        )).fetchone()
        return int(row["n"])


async def consume_magic_link(token: str) -> dict | None:
    """Verify + mark a magic-link token as used. Returns {email} on success,
    None if missing, expired, or already consumed."""
    now = time.time()
    async with get_conn() as c:
        row = await (await c.execute(
            "SELECT token, email, expires_at, used_at FROM magic_links WHERE token = ?",
            (token,),
        )).fetchone()
        if not row or row["used_at"] is not None or now > row["expires_at"]:
            return None
        await c.execute(
            "UPDATE magic_links SET used_at = ? WHERE token = ?", (now, token),
        )
        await c.commit()
        return {"email": row["email"]}


# ── Conversations ─────────────────────────────────────────────────────────

async def list_conversations(user_id: int, limit: int = 100) -> list[dict]:
    async with get_conn() as c:
        rows = await (await c.execute(
            "SELECT id, title, tier, created_at, updated_at FROM conversations "
            "WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?",
            (user_id, limit),
        )).fetchall()
        return [dict(r) for r in rows]


async def create_conversation(user_id: int, title: str = "New chat", tier: str | None = None) -> dict:
    now = time.time()
    async with get_conn() as c:
        cur = await c.execute(
            "INSERT INTO conversations (user_id, title, tier, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, title, tier, now, now),
        )
        await c.commit()
        return {"id": cur.lastrowid, "title": title, "tier": tier,
                "created_at": now, "updated_at": now}


async def get_conversation(conv_id: int, user_id: int) -> dict | None:
    async with get_conn() as c:
        row = await (await c.execute(
            "SELECT id, title, tier, created_at, updated_at FROM conversations "
            "WHERE id = ? AND user_id = ?",
            (conv_id, user_id),
        )).fetchone()
        return dict(row) if row else None


async def update_conversation(conv_id: int, user_id: int, *, title: str | None = None, tier: str | None = None) -> bool:
    now = time.time()
    sets, params = [], []
    if title is not None:
        sets.append("title = ?"); params.append(title)
    if tier is not None:
        sets.append("tier = ?"); params.append(tier)
    sets.append("updated_at = ?"); params.append(now)
    params.extend([conv_id, user_id])
    async with get_conn() as c:
        cur = await c.execute(
            f"UPDATE conversations SET {', '.join(sets)} WHERE id = ? AND user_id = ?",
            params,
        )
        await c.commit()
        return cur.rowcount > 0


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
    async with get_conn() as c:
        rows = await (await c.execute(
            "SELECT id, role, content, tier, think, tokens_in, tokens_out, created_at "
            "FROM messages WHERE conversation_id = ? ORDER BY created_at ASC",
            (conv_id,),
        )).fetchall()
        return [dict(r) for r in rows]


async def add_message(
    conv_id: int, role: str, content: str,
    *, tier: str | None = None, think: bool | None = None,
    tokens_in: int | None = None, tokens_out: int | None = None,
) -> dict:
    now = time.time()
    async with get_conn() as c:
        cur = await c.execute(
            "INSERT INTO messages (conversation_id, role, content, tier, think, "
            "tokens_in, tokens_out, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (conv_id, role, content, tier, 1 if think else (0 if think is False else None),
             tokens_in, tokens_out, now),
        )
        await c.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (now, conv_id),
        )
        await c.commit()
        return {"id": cur.lastrowid, "role": role, "content": content,
                "tier": tier, "think": think, "created_at": now}
