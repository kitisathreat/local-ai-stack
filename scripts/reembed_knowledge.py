"""One-shot Qdrant re-embedding script.

Run this after upgrading the embedding tier to a model with a different
output dimension (e.g. nomic-embed 768 -> Qwen3-Embedding-4B 2560).

What it does, per user collection:

  1. Reads every point's payload (chunk_text, filename, chunk_index, etc.)
  2. Drops the collection
  3. Recreates it at the new vector dim
  4. Re-embeds every chunk via the live embedding tier
  5. Re-upserts the points with their original payload

Collections without a `chunk_text` payload field are skipped (they
predate this script's expectations and would need manual handling).

Usage:

    python scripts/reembed_knowledge.py --dry-run   # enumerate, show plan
    python scripts/reembed_knowledge.py             # do it
    python scripts/reembed_knowledge.py --user 7    # restrict to user_7_*

Requires the backend's embedding tier to be running (the launcher's
default --Start brings it up alongside Qdrant).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
import uuid
from pathlib import Path

# Make backend imports work when run from the repo root.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from backend.config import AppConfig  # noqa: E402
from backend.backends.llama_cpp import LlamaCppClient  # noqa: E402
from backend.rag import EMBED_DIM, QDRANT_URL, configure_embedding, embed  # noqa: E402


logger = logging.getLogger("reembed")


USER_COLLECTION_RE = re.compile(r"^user_(?P<uid>\d+)_(?P<kind>docs|memory)$")


async def list_collections(client: httpx.AsyncClient) -> list[str]:
    r = await client.get(f"{QDRANT_URL.rstrip('/')}/collections")
    r.raise_for_status()
    cols = (r.json().get("result") or {}).get("collections") or []
    return [c.get("name") for c in cols if c.get("name")]


async def scroll_all_points(
    client: httpx.AsyncClient, collection: str, batch: int = 256,
) -> list[dict]:
    """Pull every point + payload + vector from a Qdrant collection."""
    out: list[dict] = []
    offset = None
    while True:
        body = {"limit": batch, "with_payload": True, "with_vector": False}
        if offset is not None:
            body["offset"] = offset
        r = await client.post(
            f"{QDRANT_URL.rstrip('/')}/collections/{collection}/points/scroll",
            json=body,
        )
        if r.status_code == 404:
            return out
        r.raise_for_status()
        result = r.json().get("result") or {}
        points = result.get("points") or []
        out.extend(points)
        offset = result.get("next_page_offset")
        if not offset:
            break
    return out


async def drop_collection(client: httpx.AsyncClient, name: str) -> None:
    r = await client.delete(f"{QDRANT_URL.rstrip('/')}/collections/{name}")
    if r.status_code not in (200, 202, 404):
        r.raise_for_status()


async def create_collection(
    client: httpx.AsyncClient, name: str, dim: int,
) -> None:
    body = {"vectors": {"size": dim, "distance": "Cosine"}}
    r = await client.put(
        f"{QDRANT_URL.rstrip('/')}/collections/{name}", json=body,
    )
    r.raise_for_status()


async def upsert_points(
    client: httpx.AsyncClient, name: str, points: list[dict],
) -> None:
    if not points:
        return
    r = await client.put(
        f"{QDRANT_URL.rstrip('/')}/collections/{name}/points",
        json={"points": points},
        params={"wait": "true"},
    )
    r.raise_for_status()


async def reembed_collection(
    client: httpx.AsyncClient,
    collection: str,
    dim: int,
    *,
    dry_run: bool,
    batch_size: int = 32,
) -> tuple[int, int]:
    """Returns (points_processed, points_skipped)."""
    points = await scroll_all_points(client, collection)
    if not points:
        logger.info("[%s] empty — nothing to do", collection)
        return 0, 0

    # Filter to points that carry chunk_text — those we can actually re-embed.
    usable: list[dict] = []
    skipped = 0
    for p in points:
        payload = p.get("payload") or {}
        text = payload.get("chunk_text") or payload.get("text")
        if not text:
            skipped += 1
            continue
        usable.append({
            "id": p.get("id") or str(uuid.uuid4()),
            "payload": payload,
            "_text": text,
        })
    logger.info(
        "[%s] %d total, %d usable, %d skipped (no chunk_text)",
        collection, len(points), len(usable), skipped,
    )
    if dry_run:
        return len(usable), skipped

    if not usable:
        return 0, skipped

    # Recreate collection at the new dim.
    await drop_collection(client, collection)
    await create_collection(client, collection, dim)

    # Embed in batches to avoid hammering the embedding endpoint.
    written = 0
    for start in range(0, len(usable), batch_size):
        chunk = usable[start:start + batch_size]
        texts = [c["_text"] for c in chunk]
        vectors = await embed(texts)
        if len(vectors) != len(chunk):
            raise RuntimeError(
                f"[{collection}] embedding returned {len(vectors)} vectors "
                f"for {len(chunk)} chunks"
            )
        new_points = [
            {"id": c["id"], "vector": vectors[i], "payload": c["payload"]}
            for i, c in enumerate(chunk)
        ]
        await upsert_points(client, collection, new_points)
        written += len(new_points)
        logger.info("[%s] wrote %d/%d", collection, written, len(usable))

    return written, skipped


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="enumerate only")
    parser.add_argument(
        "--user", type=int, default=None,
        help="restrict to user_<N>_docs and user_<N>_memory",
    )
    parser.add_argument(
        "--batch-size", type=int, default=32,
        help="embed/upsert batch size",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    cfg = AppConfig.load()
    llama = LlamaCppClient(cfg)
    configure_embedding(cfg, llama)

    target_dim = EMBED_DIM
    logger.info("Target embedding dim: %d", target_dim)

    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=5.0)) as client:
        all_cols = await list_collections(client)
        target_cols: list[str] = []
        for name in all_cols:
            m = USER_COLLECTION_RE.match(name)
            if not m:
                continue
            if args.user is not None and int(m.group("uid")) != args.user:
                continue
            target_cols.append(name)

        logger.info("Found %d user collection(s): %s", len(target_cols), target_cols)

        total_written = total_skipped = 0
        for name in target_cols:
            try:
                w, s = await reembed_collection(
                    client, name, target_dim,
                    dry_run=args.dry_run,
                    batch_size=args.batch_size,
                )
                total_written += w
                total_skipped += s
            except Exception:
                logger.exception("[%s] failed", name)

        logger.info(
            "Done. processed=%d skipped=%d (dry_run=%s)",
            total_written, total_skipped, args.dry_run,
        )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
