"""Conversation CRUD + chat-attachment upload endpoints.

The /chats endpoints manage conversations as durable resources (titles,
tier pin, archived state, memory-distillation toggle). /api/chat/upload
stashes a file the chat composer attaches to the next user turn —
distinct from /rag/upload (which indexes documents into the per-user
RAG store).
"""

from __future__ import annotations

import logging
import os
import re
import secrets as _secrets
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from .. import airgap, auth, db, memory
from ..schemas import (
    ConversationListResponse,
    ConversationSummary,
    ConversationUpdate,
    ConversationWithMessages,
    MessageOut,
)


logger = logging.getLogger(__name__)

router = APIRouter(tags=["chats"])


_ATTACH_MAX_BYTES = 20 * 1024 * 1024   # 20 MB per file, like /rag/upload


def _attachments_dir(user_id: int) -> Path:
    base = Path(os.getenv("LAI_DATA_DIR") or
                Path(__file__).resolve().parent.parent.parent / "data")
    d = base / "uploads" / str(user_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


async def _maybe_evict_unused_tier(tier_name: str | None) -> None:
    """If no other live (non-archived) conversation across any user is
    pinned to ``tier_name`` and the scheduler shows the tier idle
    (refcount == 0, not pinned, not in the auto-warm set), unload it.

    Called from update_chat (when archived=True flips on) and
    delete_chat — together with the no-eager-load policy this enforces
    "models loaded only during active sessions". The auto-warm set
    (``_DEFAULT_WARM_TIERS``) is treated as always-active so a freshly
    archived chat doesn't immediately tear down versatile/fast.
    """
    # Lazy-import to avoid the route module loading main.py before
    # state has been initialized.
    from .. import main as backend_main

    if not tier_name:
        return
    if tier_name in backend_main._DEFAULT_WARM_TIERS:
        return
    try:
        async with db.get_conn() as c:
            cur = await c.execute(
                "SELECT 1 FROM conversations WHERE tier = ? "
                "AND archived = 0 LIMIT 1",
                (tier_name,),
            )
            row = await cur.fetchone()
        if row is not None:
            return
        sched = getattr(backend_main.state, "scheduler", None)
        if sched is None:
            return
        async with sched._lock:
            m = sched.loaded.get(tier_name)
            if m is None:
                return
            if m.pinned or m.refcount > 0:
                return
            if m.state.value != "resident":
                return
            await sched._unload(m)
        logger.info(
            "Evicted tier %r — last active conversation closed/archived",
            tier_name,
        )
    except Exception as exc:
        logger.debug("Idle-evict skipped for tier %r: %s", tier_name, exc)


@router.get("/chats", response_model=ConversationListResponse)
async def list_chats(
    user: dict = Depends(auth.current_user),
    include_archived: bool = False,
    archived_only: bool = False,
):
    """Return conversations matching the current airgap mode.
    By default archived chats are hidden from the sidebar. Pass
    `?archived_only=true` to list only archived (used by the
    "Archived" view) or `?include_archived=true` to list both."""
    if archived_only:
        archived_filter: bool | None = True
    elif include_archived:
        archived_filter = None
    else:
        archived_filter = False
    rows = await db.list_conversations(
        user["id"], airgap=airgap.is_enabled(), archived=archived_filter,
    )
    return ConversationListResponse(data=[ConversationSummary(**r) for r in rows])


@router.post("/chats", response_model=ConversationSummary)
async def create_chat(
    body: ConversationUpdate,
    user: dict = Depends(auth.current_user),
):
    is_airgap = airgap.is_enabled()
    conv = await db.create_conversation(
        user["id"],
        title=body.title or "New chat",
        tier=body.tier,
        memory_enabled=True if body.memory_enabled is None else body.memory_enabled,
        airgap=is_airgap,
    )
    return ConversationSummary(**conv)


@router.get("/chats/{conv_id}", response_model=ConversationWithMessages)
async def get_chat(conv_id: int, user: dict = Depends(auth.current_user)):
    conv = await db.get_conversation(conv_id, user["id"])
    if not conv:
        raise HTTPException(404, "Conversation not found")
    if bool(conv.get("airgap")) != airgap.is_enabled():
        raise HTTPException(
            404,
            "Conversation not found (owned by the other airgap mode).",
        )
    msgs = await db.list_messages(conv_id)
    return ConversationWithMessages(
        **conv,
        messages=[MessageOut(**m) for m in msgs],
    )


@router.patch("/chats/{conv_id}", response_model=ConversationSummary)
async def update_chat(
    conv_id: int,
    body: ConversationUpdate,
    user: dict = Depends(auth.current_user),
):
    prev = await db.get_conversation(conv_id, user["id"])
    ok = await db.update_conversation(
        conv_id, user["id"],
        title=body.title, tier=body.tier,
        memory_enabled=body.memory_enabled,
        archived=body.archived,
    )
    if not ok:
        raise HTTPException(404, "Conversation not found")
    conv = await db.get_conversation(conv_id, user["id"])
    if body.archived and prev and not prev.get("archived"):
        await _maybe_evict_unused_tier(prev.get("tier"))
    return ConversationSummary(**conv)


@router.delete("/chats/{conv_id}")
async def delete_chat(
    conv_id: int,
    user: dict = Depends(auth.current_user),
    keep_summary: bool = True,
):
    """Delete a conversation. By default, distill it into a memory entry
    first (`?keep_summary=true`, default) so the user keeps the gist
    without the verbatim transcript. Pass `?keep_summary=false` to
    skip distillation (e.g., for accidental chats with nothing worth
    remembering)."""
    from .. import main as backend_main

    conv = await db.get_conversation(conv_id, user["id"])
    if not conv:
        raise HTTPException(404, "Conversation not found")
    if bool(conv.get("airgap")) != airgap.is_enabled():
        raise HTTPException(404, "Conversation not found (other airgap mode)")
    if keep_summary and conv.get("memory_enabled", True):
        try:
            versatile_tier = backend_main.state.config.models.tiers.get("versatile")
            if versatile_tier is not None:
                await memory.distill_and_store(
                    user["id"], conv_id, backend_main.state.llama_cpp, versatile_tier,
                    airgap=bool(conv.get("airgap")),
                )
                logger.info("Pre-delete summary stored for conv %d", conv_id)
        except Exception as e:
            logger.warning("Pre-delete summary failed for conv %d: %s", conv_id, e)
    ok = await db.delete_conversation(conv_id, user["id"])
    if ok:
        await _maybe_evict_unused_tier(conv.get("tier"))
    return {"ok": True, "summarized": keep_summary, "deleted": ok}


@router.post("/api/chat/upload")
async def chat_upload(
    file: UploadFile = File(...),
    user: dict = Depends(auth.current_user),
):
    """Stash a file for the user's next chat turn. Returns an opaque id
    the chat composer attaches to the next /v1/chat/completions request
    via attachment_ids=[...]. Files are NOT indexed into RAG (use
    /rag/upload for that)."""
    content = await file.read()
    if not content:
        raise HTTPException(400, "Empty file")
    if len(content) > _ATTACH_MAX_BYTES:
        raise HTTPException(413, f"File too large (>{_ATTACH_MAX_BYTES // (1024*1024)} MB)")
    aid = _secrets.token_urlsafe(12)
    fname = (file.filename or "upload").lower()
    ext = Path(fname).suffix[:8] or ""
    if ext and not re.fullmatch(r"\.[a-z0-9]+", ext):
        ext = ""
    dest = _attachments_dir(user["id"]) / f"{aid}{ext}"
    dest.write_bytes(content)
    return {
        "id": aid,
        "name": file.filename or "upload",
        "size": len(content),
        "content_type": file.content_type or "application/octet-stream",
    }
