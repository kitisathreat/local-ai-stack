"""Per-user long-term memory.

After each completed conversation, a background task asks the Versatile
tier to distill any memorable facts into 1–5 short bullets. Each bullet
becomes a row in `memories` (SQLite) with an embedded vector in Qdrant
(`user_{id}_memory` collection). On every new chat, we retrieve the
top-K most-similar memories for the user's first message and inject them
into the system prompt.

Distillation prompt is deliberately conservative — we only want
durable, recurring preferences / facts, not trivia from one session.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from . import db, history_store
from .rag import embed, qdrant, memory_collection_name


def _coll_name(user_id: int, *, airgap: bool) -> str:
    """Airgap memories live in a dedicated Qdrant collection so they can
    never surface in a normal retrieval pass, even if the code path
    forgets to filter."""
    base = memory_collection_name(user_id)
    return f"{base}_airgap" if airgap else base


logger = logging.getLogger(__name__)


DISTILLATION_SYSTEM = """You extract durable facts about a user from
their recent conversation. Output ONLY a JSON array of 0 to 5 short,
self-contained statements. Return [] if no durable facts are present.

Durable facts include:
  - Stated preferences ("I prefer concise tables")
  - Identity / role ("I'm a Python dev")
  - Ongoing projects / context ("I'm building a FastAPI app")
  - Long-term interests ("I'm interested in Rust systems programming")

Do NOT include:
  - One-off questions
  - Facts the user learned in THIS conversation (they already know them)
  - The assistant's responses

Format: ["fact 1", "fact 2", ...]
"""


async def distill_and_store(
    user_id: int, conversation_id: int, ollama_client, versatile_tier,
    *, airgap: bool = False,
) -> list[str]:
    """Run distillation on a completed conversation. Returns stored facts.

    Airgap conversations route to a dedicated Qdrant collection and
    store each fact as AES-256-GCM ciphertext in `memories.content`
    so the SQLite row is opaque at rest."""
    from .schemas import ChatMessage

    msgs = await db.list_messages(conversation_id)
    if len(msgs) < 2:
        return []

    # Build a compact transcript for the distiller.
    transcript_lines = []
    for m in msgs:
        prefix = "User" if m["role"] == "user" else "Assistant"
        text = (m["content"] or "").strip()[:800]
        transcript_lines.append(f"{prefix}: {text}")
    transcript = "\n".join(transcript_lines[-30:])   # cap to last 30 turns

    prompt = [
        ChatMessage(role="system", content=DISTILLATION_SYSTEM),
        ChatMessage(role="user", content=f"Conversation transcript:\n\n{transcript}"),
    ]

    try:
        raw = await ollama_client.chat_once(
            versatile_tier, prompt, think=False, keep_alive="5m",
        )
    except Exception as e:
        logger.warning("Memory distillation failed for conv %d: %s", conversation_id, e)
        return []

    facts = _parse_facts(raw)
    if not facts:
        return []

    # Embed all facts once and upsert to both SQLite (for listing/delete)
    # and Qdrant (for similarity retrieval).
    vectors = await embed(facts)
    if len(vectors) != len(facts):
        return []

    coll = _coll_name(user_id, airgap=airgap)
    await qdrant.ensure_collection(coll)

    now = time.time()
    points = []
    stored: list[str] = []
    async with db.get_conn() as c:
        for fact, vec in zip(facts, vectors):
            row_content = (
                history_store.encrypt_value(user_id, fact, scope="mem", airgap=True)
                if airgap else fact
            )
            cur = await c.execute(
                "INSERT INTO memories (user_id, content, encrypted, airgap, "
                "source_conv, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, row_content, 1 if airgap else 0, 1 if airgap else 0,
                 conversation_id, now, now),
            )
            mem_id = cur.lastrowid
            # For airgap memories we still have to put _something_ next to
            # the vector so the retrieve path can find the fact, but we
            # must not leak the plaintext into Qdrant. Store the
            # encrypted token there too; the retrieval step decrypts it
            # with the same per-user key.
            payload = {
                "memory_id": mem_id,
                "content": row_content,
                "encrypted": bool(airgap),
                "source_conv": conversation_id,
                "created_at": now,
            }
            points.append({
                "id": str(uuid.uuid4()),
                "vector": vec,
                "payload": payload,
            })
            stored.append(fact)
        await c.commit()
    await qdrant.upsert(coll, points)
    return stored


async def retrieve_for_user(
    user_id: int, query: str, k: int = 3, *, airgap: bool = False,
) -> list[dict]:
    """Similarity-search the user's memory store. Airgap callers hit a
    separate Qdrant collection and decrypt hit payloads in-process."""
    coll = _coll_name(user_id, airgap=airgap)
    vectors = await embed([query])
    if not vectors:
        return []
    try:
        hits = await qdrant.search(coll, vectors[0], limit=k)
    except Exception as e:
        logger.debug("Memory retrieve failed (%s) — collection may not exist yet", e)
        return []
    out = []
    for h in hits:
        payload = h.get("payload") or {}
        raw = payload.get("content", "") or ""
        if payload.get("encrypted"):
            raw = history_store.decrypt_value(user_id, raw, scope="mem", airgap=True)
        out.append({
            "memory_id": payload.get("memory_id"),
            "content": raw,
            "score": h.get("score"),
        })
    return out


def format_memory_block(hits: list[dict]) -> str:
    if not hits:
        return ""
    lines = ["[Things to remember about this user from past conversations:]"]
    for h in hits:
        lines.append(f"- {h['content']}")
    lines.append("")
    return "\n".join(lines)


async def list_for_user(user_id: int, *, airgap: bool = False) -> list[dict]:
    async with db.get_conn() as c:
        rows = await (await c.execute(
            "SELECT id, content, encrypted, airgap, source_conv, created_at, updated_at "
            "FROM memories WHERE user_id = ? AND airgap = ? ORDER BY updated_at DESC",
            (user_id, 1 if airgap else 0),
        )).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if d.pop("encrypted", 0):
                d["content"] = history_store.decrypt_value(
                    user_id, d.get("content") or "", scope="mem", airgap=True,
                )
            d["airgap"] = bool(d.get("airgap"))
            out.append(d)
        return out


async def delete(user_id: int, memory_id: int) -> bool:
    """Remove a memory from SQLite and Qdrant. Looks up the row to
    figure out which Qdrant collection (normal vs airgap) owns it so
    the cleanup hits the right collection."""
    async with db.get_conn() as c:
        row = await (await c.execute(
            "SELECT airgap FROM memories WHERE id = ? AND user_id = ?",
            (memory_id, user_id),
        )).fetchone()
        if not row:
            return False
        is_airgap = bool(row["airgap"])
        cur = await c.execute(
            "DELETE FROM memories WHERE id = ? AND user_id = ?",
            (memory_id, user_id),
        )
        await c.commit()
        if cur.rowcount == 0:
            return False
    try:
        coll = _coll_name(user_id, airgap=is_airgap)
        await qdrant.delete_by_filter(
            coll,
            {"must": [{"key": "memory_id", "match": {"value": memory_id}}]},
        )
    except Exception as e:
        logger.warning("Qdrant cleanup for memory %d failed: %s", memory_id, e)
    return True


def _parse_facts(raw: str) -> list[str]:
    import json, re
    # Strip think blocks
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", raw, flags=re.I)
    m = re.search(r"\[[\s\S]*\]", cleaned)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(arr, list):
        return []
    out: list[str] = []
    for item in arr:
        if isinstance(item, str) and 3 <= len(item) <= 500:
            out.append(item.strip())
    return out[:5]
