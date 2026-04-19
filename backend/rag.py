"""RAG — per-user document retrieval via Qdrant.

Each user gets their own Qdrant collection named `user_{id}_docs`. On
upload we extract text (PDF/MD/TXT/HTML), chunk it, embed each chunk
via Ollama's `nomic-embed-text` model, and upsert into Qdrant. On chat
we retrieve top-K and return them as a context block to inject into
the system prompt.

Vector size is determined by the embedding model — nomic-embed-text
emits 768-dim vectors.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx


logger = logging.getLogger(__name__)


EMBED_MODEL = os.getenv("RAG_EMBED_MODEL", "nomic-embed-text")
EMBED_DIM = int(os.getenv("RAG_EMBED_DIM", "768"))
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://ollama:11434")
QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333")
CHUNK_SIZE = int(os.getenv("RAG_CHUNK_SIZE", "800"))
CHUNK_OVERLAP = int(os.getenv("RAG_CHUNK_OVERLAP", "100"))


def collection_name(user_id: int) -> str:
    return f"user_{user_id}_docs"


def memory_collection_name(user_id: int) -> str:
    return f"user_{user_id}_memory"


# ── Text extraction ──────────────────────────────────────────────────────

def extract_text(filename: str, content: bytes) -> str:
    """Extract plain text from a file. Supports PDF, TXT, MD, HTML."""
    name = filename.lower()
    if name.endswith(".pdf"):
        try:
            from pypdf import PdfReader
        except ImportError:
            raise RuntimeError("pypdf not installed — PDF upload unavailable")
        reader = PdfReader(io.BytesIO(content))
        return "\n\n".join((p.extract_text() or "") for p in reader.pages)
    if name.endswith(".html") or name.endswith(".htm"):
        # Cheap tag strip; upgrade to BeautifulSoup if needed.
        text = content.decode("utf-8", errors="replace")
        text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.I)
        text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        return re.sub(r"\s+", " ", text)
    # TXT, MD, CSV, everything else — treat as UTF-8 text.
    return content.decode("utf-8", errors="replace")


def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Chunk text by sentences, packing up to `size` chars per chunk with
    `overlap` of sentence carry-over between consecutive chunks."""
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    # Sentence split on period/question/exclamation followed by space+capital.
    sents = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", text)
    chunks: list[str] = []
    buf: list[str] = []
    buflen = 0
    for s in sents:
        if buflen + len(s) + 1 > size and buf:
            chunks.append(" ".join(buf))
            # Carry the last sentence(s) forward to produce overlap.
            carry: list[str] = []
            carry_len = 0
            for prev in reversed(buf):
                if carry_len + len(prev) > overlap:
                    break
                carry.insert(0, prev); carry_len += len(prev)
            buf = carry
            buflen = carry_len
        buf.append(s); buflen += len(s) + 1
    if buf:
        chunks.append(" ".join(buf))
    return chunks


# ── Embedding ────────────────────────────────────────────────────────────

async def embed(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts via Ollama's /api/embed."""
    if not texts:
        return []
    payload = {"model": EMBED_MODEL, "input": texts}
    async with httpx.AsyncClient(timeout=60.0) as c:
        r = await c.post(f"{OLLAMA_URL}/api/embed", json=payload)
        r.raise_for_status()
        data = r.json()
    return data.get("embeddings") or []


# ── Qdrant client (thin) ─────────────────────────────────────────────────

class Qdrant:
    def __init__(self, base_url: str = QDRANT_URL):
        self.base = base_url.rstrip("/")
        self.timeout = httpx.Timeout(30.0, connect=5.0)

    async def ensure_collection(self, name: str, dim: int = EMBED_DIM) -> None:
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.get(f"{self.base}/collections/{name}")
            if r.status_code == 200:
                return
            body = {
                "vectors": {"size": dim, "distance": "Cosine"},
            }
            await c.put(f"{self.base}/collections/{name}", json=body)

    async def upsert(self, name: str, points: list[dict]) -> None:
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            await c.put(
                f"{self.base}/collections/{name}/points",
                json={"points": points},
                params={"wait": "true"},
            )

    async def search(self, name: str, vector: list[float], limit: int = 5) -> list[dict]:
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.post(
                f"{self.base}/collections/{name}/points/search",
                json={"vector": vector, "limit": limit, "with_payload": True},
            )
            if r.status_code == 404:
                return []
            r.raise_for_status()
            return r.json().get("result", []) or []

    async def delete_by_filter(self, name: str, filter_: dict) -> int:
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.post(
                f"{self.base}/collections/{name}/points/delete",
                json={"filter": filter_},
                params={"wait": "true"},
            )
            if r.status_code == 404:
                return 0
            r.raise_for_status()
            return (r.json().get("result") or {}).get("operation_id") or 0


# ── Ingest / retrieve ───────────────────────────────────────────────────

qdrant = Qdrant()


async def ingest_document(
    user_id: int, filename: str, content: bytes, mime: str | None = None,
) -> dict:
    """Extract → chunk → embed → upsert. Returns {chunks, qdrant_ids}."""
    text = extract_text(filename, content)
    chunks = chunk_text(text)
    if not chunks:
        return {"chunks": 0, "qdrant_ids": []}

    embeddings = await embed(chunks)
    if len(embeddings) != len(chunks):
        raise RuntimeError(
            f"Embedding returned {len(embeddings)} vectors for {len(chunks)} chunks"
        )

    coll = collection_name(user_id)
    await qdrant.ensure_collection(coll)

    doc_id = hashlib.sha256(f"{user_id}:{filename}:{time.time()}".encode()).hexdigest()[:16]
    points = [
        {
            "id": str(uuid.uuid4()),
            "vector": vec,
            "payload": {
                "doc_id": doc_id,
                "filename": filename,
                "chunk_index": i,
                "chunk_text": chunks[i],
                "mime": mime,
                "uploaded_at": time.time(),
            },
        }
        for i, vec in enumerate(embeddings)
    ]
    await qdrant.upsert(coll, points)
    return {
        "doc_id": doc_id,
        "chunks": len(chunks),
        "qdrant_ids": [p["id"] for p in points],
    }


async def retrieve(user_id: int, query: str, k: int = 5) -> list[dict]:
    """Retrieve top-K relevant chunks for a user query. Returns payload
    dicts, highest-scoring first."""
    coll = collection_name(user_id)
    vectors = await embed([query])
    if not vectors:
        return []
    hits = await qdrant.search(coll, vectors[0], limit=k)
    return [
        {
            "score": h.get("score"),
            "filename": (h.get("payload") or {}).get("filename"),
            "chunk_index": (h.get("payload") or {}).get("chunk_index"),
            "text": (h.get("payload") or {}).get("chunk_text", ""),
        }
        for h in hits
    ]


def format_context_block(hits: list[dict]) -> str:
    """Render retrieved chunks as a system-prompt injection."""
    if not hits:
        return ""
    lines = ["[Knowledge base retrieved from user's uploaded documents:]"]
    for i, h in enumerate(hits, start=1):
        lines.append(f"\n[{i}] ({h['filename']}, chunk {h['chunk_index']})")
        lines.append(h["text"].strip()[:1500])
    lines.append("")
    return "\n".join(lines)


async def delete_doc(user_id: int, doc_id: str) -> int:
    """Delete all points for a doc_id from the user's collection."""
    coll = collection_name(user_id)
    return await qdrant.delete_by_filter(
        coll,
        {"must": [{"key": "doc_id", "match": {"value": doc_id}}]},
    )
