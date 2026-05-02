"""RAG — per-user document retrieval via Qdrant.

Each user gets their own Qdrant collection named `user_{id}_docs`. On
upload we extract text (PDF/MD/TXT/HTML), chunk it, embed each chunk
via the always-on `embedding` tier (a llama-server pre-spawned with
``--embedding``), and upsert into Qdrant. On chat we retrieve top-K and
return them as a context block to inject into the system prompt.

Vector size is determined by the embedding model — Qwen3-Embedding-4B
emits 2 560-dim vectors. (Was nomic-embed-text-v1.5 at 768 dim before
the migration; if you have legacy 768-dim collections, run
``scripts/reembed_knowledge.py`` to rebuild them at the new dim.)
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


EMBED_DIM = int(os.getenv("RAG_EMBED_DIM", "2560"))
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
CHUNK_SIZE = int(os.getenv("RAG_CHUNK_SIZE", "800"))
CHUNK_OVERLAP = int(os.getenv("RAG_CHUNK_OVERLAP", "100"))

# Reranker (Qwen3-Reranker-0.6B served by llama-server --reranking on 8091).
# Fetch RAG_RERANK_OVERSAMPLE × k candidates from Qdrant, rerank, return top-k.
# 4× has been the sweet spot in published benches: enough headroom for the
# reranker to find the right doc, low enough that latency stays sub-200ms.
RERANKER_URL = os.getenv("RERANKER_URL", "http://127.0.0.1:8091")
RERANK_ENABLED = os.getenv("RAG_RERANK_ENABLED", "1").strip() in ("1", "true", "yes")
RERANK_OVERSAMPLE = int(os.getenv("RAG_RERANK_OVERSAMPLE", "4"))

# Wired by main.py at startup. Stays None in unit tests where the
# embedding endpoint isn't running; embed() raises a clear error.
_EMBED_TIER = None         # type: ignore[var-annotated]
_EMBED_CLIENT = None       # type: ignore[var-annotated]


def configure_embedding(cfg, llama_client) -> None:
    """Plug the live AppConfig + LlamaCppClient into the module so embed()
    knows which endpoint to call."""
    global _EMBED_TIER, _EMBED_CLIENT
    _EMBED_TIER = cfg.models.tiers.get("embedding")
    _EMBED_CLIENT = llama_client
# Phase 6: minimum cosine similarity for a chunk to be injected. Tuned to
# filter out near-noise hits while keeping genuinely relevant ones. Set to
# 0 via env to disable the gate.
MIN_SCORE = float(os.getenv("RAG_MIN_SCORE", "0.55"))


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
    """Embed a batch of texts via the embedding tier (llama-server with
    --embedding) configured by configure_embedding()."""
    if not texts:
        return []
    if _EMBED_CLIENT is None or _EMBED_TIER is None:
        raise RuntimeError(
            "Embedding tier not configured — call rag.configure_embedding() at startup"
        )
    return await _EMBED_CLIENT.embed(_EMBED_TIER, texts)


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


async def rerank(query: str, documents: list[str]) -> list[float] | None:
    """Score (query, doc) pairs with the Qwen3-Reranker llama-server.

    Returns one float per document (higher = more relevant) in the input
    order, or None when the reranker is disabled / unreachable / errors —
    callers should fall back to the embedding-cosine order.

    The reranker model produces a more accurate query-document relevance
    signal than embedding cosine, especially for queries that need
    cross-attention (multi-hop facts, paraphrased entities, negation).
    Cost: one HTTP call per chat turn that runs RAG, ~50–150 ms for 20
    candidate chunks against a 0.6B model.
    """
    if not RERANK_ENABLED or not documents:
        return None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                f"{RERANKER_URL.rstrip('/')}/v1/rerank",
                json={
                    "model": "reranker",
                    "query": query,
                    "documents": documents,
                },
            )
            if r.status_code != 200:
                logger.warning("Reranker HTTP %d — falling back to embed-cosine order", r.status_code)
                return None
            data = r.json()
    except Exception as exc:
        logger.warning("Reranker call failed (%s) — falling back to embed-cosine order", exc)
        return None

    # llama-server returns {"results":[{"index":i,"relevance_score":s}, ...]}
    # in arbitrary order. Map back to the input-document order.
    results = data.get("results") or data.get("data") or []
    scores = [0.0] * len(documents)
    for r in results:
        idx = r.get("index")
        score = r.get("relevance_score") or r.get("score")
        if isinstance(idx, int) and 0 <= idx < len(documents) and score is not None:
            scores[idx] = float(score)
    return scores


async def retrieve(
    user_id: int, query: str, k: int = 5, min_score: float | None = None,
) -> list[dict]:
    """Retrieve top-K relevant chunks for a user query, filtered by a
    minimum similarity score. Returns payload dicts, highest-scoring first.

    `min_score=None` uses the module default (RAG_MIN_SCORE env, 0.55).

    When the reranker is available, fetches RAG_RERANK_OVERSAMPLE × k
    candidates from Qdrant (embedding-cosine), re-scores them with the
    Qwen3-Reranker, and returns the top-k by reranker score. The
    `score` field in each returned dict is the reranker relevance score
    in that case; falls back to the cosine score when the reranker is
    unavailable. The min_score threshold applies to the reranker score
    too — values 0–1.
    """
    threshold = MIN_SCORE if min_score is None else float(min_score)
    coll = collection_name(user_id)
    vectors = await embed([query])
    if not vectors:
        return []

    # Oversample from Qdrant so the reranker has a real candidate pool.
    fetch_k = max(k, k * RERANK_OVERSAMPLE) if RERANK_ENABLED else k
    hits = await qdrant.search(coll, vectors[0], limit=fetch_k)
    if not hits:
        return []

    # Build candidate list in cosine order.
    candidates: list[dict] = []
    for h in hits:
        candidates.append({
            "cosine_score": h.get("score") or 0.0,
            "filename": (h.get("payload") or {}).get("filename"),
            "chunk_index": (h.get("payload") or {}).get("chunk_index"),
            "text": (h.get("payload") or {}).get("chunk_text", ""),
        })

    # Try the reranker. On any failure, fall through to the cosine order.
    docs = [c["text"] for c in candidates]
    rerank_scores = await rerank(query, docs)

    if rerank_scores is not None:
        # Reorder by reranker score; keep its score as the primary `score`.
        for c, s in zip(candidates, rerank_scores):
            c["score"] = s
            c["reranked"] = True
        candidates.sort(key=lambda c: c["score"], reverse=True)
        # Reranker scores have a different distribution from cosine. Use a
        # separate (and gentler) gate here — the reranker already does most
        # of the noise filtering by surfacing the best docs even from a
        # mediocre embedding pool. Default 0 = no extra filter.
        rerank_threshold = float(os.getenv("RAG_RERANK_MIN_SCORE", "0.0"))
        filtered = [c for c in candidates if c["score"] >= rerank_threshold][:k]
    else:
        # Fall back to cosine ordering with the cosine threshold.
        for c in candidates:
            c["score"] = c["cosine_score"]
            c["reranked"] = False
        filtered = [c for c in candidates if c["score"] >= threshold][:k]
    # Drop the cosine_score key from the returned dicts (keep `score`,
    # `reranked` for observability).
    for c in filtered:
        c.pop("cosine_score", None)
    return filtered


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
