"""RAG document upload/list/delete + memory list/delete endpoints.

RAG: per-user document indexing into Qdrant for retrieval-augmented
generation. Memory: per-user durable facts distilled from chat history
by `backend.memory.distill_and_store`.

Both surfaces filter by airgap mode so a user in airgap mode can't
accidentally see content from their normal session, and vice versa.
"""

from __future__ import annotations

import json as _json
import logging
import time

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from .. import airgap, auth, db, memory, rag


logger = logging.getLogger(__name__)

router = APIRouter(tags=["rag-memory"])


@router.post("/rag/upload")
async def rag_upload(
    file: UploadFile = File(...),
    user: dict = Depends(auth.current_user),
):
    content = await file.read()
    if not content:
        raise HTTPException(400, "Empty file")
    if len(content) > 20 * 1024 * 1024:
        raise HTTPException(413, "File too large (>20MB)")
    try:
        ingest = await rag.ingest_document(
            user["id"], file.filename or "upload", content, mime=file.content_type,
        )
    except Exception as e:
        logger.exception("RAG ingest failed")
        raise HTTPException(500, f"Ingest failed: {e}")

    now = time.time()
    async with db.get_conn() as c:
        await c.execute(
            "INSERT INTO rag_docs (user_id, filename, mime_type, size_bytes, chunk_count, qdrant_ids, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                user["id"], file.filename, file.content_type, len(content),
                ingest["chunks"], _json.dumps(ingest["qdrant_ids"]), now,
            ),
        )
        await c.commit()
    return {"ok": True, "chunks": ingest["chunks"], "filename": file.filename}


@router.get("/rag/docs")
async def rag_list(user: dict = Depends(auth.current_user)):
    async with db.get_conn() as c:
        rows = await (await c.execute(
            "SELECT id, filename, mime_type, size_bytes, chunk_count, created_at "
            "FROM rag_docs WHERE user_id = ? ORDER BY created_at DESC",
            (user["id"],),
        )).fetchall()
        return {"data": [dict(r) for r in rows]}


@router.delete("/rag/docs/{doc_id}")
async def rag_delete(doc_id: int, user: dict = Depends(auth.current_user)):
    async with db.get_conn() as c:
        row = await (await c.execute(
            "SELECT id, qdrant_ids FROM rag_docs WHERE id = ? AND user_id = ?",
            (doc_id, user["id"]),
        )).fetchone()
        if not row:
            raise HTTPException(404, "Document not found")
        ids = _json.loads(row["qdrant_ids"] or "[]")
        await c.execute("DELETE FROM rag_docs WHERE id = ?", (doc_id,))
        await c.commit()
    if ids:
        coll = rag.collection_name(user["id"])
        async with httpx.AsyncClient(timeout=30.0) as cx:
            await cx.post(
                f"{rag.QDRANT_URL.rstrip('/')}/collections/{coll}/points/delete",
                json={"points": ids},
                params={"wait": "true"},
            )
    return {"ok": True}


@router.get("/memory")
async def memory_list(user: dict = Depends(auth.current_user)):
    """List the user's stored memories for the *current* mode only.
    Airgap and non-airgap memories never appear together so a user in
    airgap mode can't accidentally see distilled facts from their
    normal conversations (or vice versa)."""
    rows = await memory.list_for_user(user["id"], airgap=airgap.is_enabled())
    return {"data": rows}


@router.delete("/memory/{memory_id}")
async def memory_delete(memory_id: int, user: dict = Depends(auth.current_user)):
    ok = await memory.delete(user["id"], memory_id)
    if not ok:
        raise HTTPException(404, "Memory not found")
    return {"ok": True}
