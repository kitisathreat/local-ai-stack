"""
title: PDF Viewer — Read, Search, Extract from Local & Remote PDFs
author: local-ai-stack
description: Read PDF documents from a local path or URL, extract per-page text with stable [page N] anchors, list metadata, search for substrings across pages, dump tables and outlines (table of contents). Mirrors the Claude `pdf-viewer` desktop connector.
required_open_webui_version: 0.4.0
requirements: pypdf httpx
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

import io
import os
import tempfile
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field


class Tools:
    class Valves(BaseModel):
        DOWNLOAD_DIR: str = Field(
            default="",
            description=(
                "Optional cache directory for downloaded PDFs (URL-fetched). "
                "When empty, downloads stream into a temp file and are deleted "
                "after the call returns."
            ),
        )
        TIMEOUT_SEC: int = Field(default=30, description="HTTP timeout when fetching from a URL.")
        MAX_BYTES: int = Field(
            default=50 * 1024 * 1024,
            description="Reject downloads larger than this (default 50 MB).",
        )

    def __init__(self) -> None:
        self.valves = self.Valves()

    async def _resolve_to_path(self, source: str) -> tuple[Path, bool]:
        """Return (local_path, is_temp). Local paths are returned as-is;
        URLs are fetched into a temp file (or DOWNLOAD_DIR cache)."""
        if source.startswith(("http://", "https://")):
            return await self._fetch(source), True
        p = Path(source).expanduser().resolve()
        if not p.exists():
            raise FileNotFoundError(str(p))
        return p, False

    async def _fetch(self, url: str) -> Path:
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT_SEC, follow_redirects=True) as c:
            async with c.stream("GET", url) as r:
                if r.status_code >= 400:
                    raise RuntimeError(f"PDF fetch -> {r.status_code}: {url}")
                content_length = int(r.headers.get("content-length") or 0)
                if content_length and content_length > self.valves.MAX_BYTES:
                    raise RuntimeError(f"PDF too large ({content_length} > {self.valves.MAX_BYTES} bytes).")
                if self.valves.DOWNLOAD_DIR:
                    cache = Path(self.valves.DOWNLOAD_DIR).expanduser().resolve()
                    cache.mkdir(parents=True, exist_ok=True)
                    name = url.rsplit("/", 1)[-1].split("?", 1)[0] or "download.pdf"
                    if not name.lower().endswith(".pdf"):
                        name += ".pdf"
                    out = cache / name
                else:
                    fd, tmp = tempfile.mkstemp(suffix=".pdf", prefix="lai-pdf-")
                    os.close(fd)
                    out = Path(tmp)
                size = 0
                with out.open("wb") as f:
                    async for chunk in r.aiter_bytes():
                        size += len(chunk)
                        if size > self.valves.MAX_BYTES:
                            f.close()
                            out.unlink(missing_ok=True)
                            raise RuntimeError(f"PDF exceeds MAX_BYTES while streaming.")
                        f.write(chunk)
        return out

    def _open(self, path: Path):
        try:
            from pypdf import PdfReader
        except ImportError as e:
            raise RuntimeError(
                "pypdf is not installed. Run `pip install pypdf` in the backend venv."
            ) from e
        return PdfReader(str(path))

    # ── Public methods ────────────────────────────────────────────────────

    async def get_metadata(self, source: str) -> str:
        """Return title, author, page count, and producer for a PDF.

        :param source: Local path or http(s):// URL.
        """
        path, is_temp = await self._resolve_to_path(source)
        try:
            reader = self._open(path)
            md = reader.metadata or {}
            out = [
                f"path: {path}",
                f"pages: {len(reader.pages)}",
                f"title: {md.get('/Title','(none)')}",
                f"author: {md.get('/Author','(none)')}",
                f"producer: {md.get('/Producer','(none)')}",
                f"creator: {md.get('/Creator','(none)')}",
                f"subject: {md.get('/Subject','(none)')}",
                f"created: {md.get('/CreationDate','(none)')}",
            ]
            return "\n".join(out)
        finally:
            if is_temp and not self.valves.DOWNLOAD_DIR:
                Path(path).unlink(missing_ok=True)

    async def read_pages(
        self,
        source: str,
        start_page: int = 1,
        end_page: int = 0,
        max_chars: int = 12000,
    ) -> str:
        """Extract text from a range of pages. Each page is anchored with a
        `[page N]` marker so the model can reference exact locations.

        :param source: Local path or http(s) URL.
        :param start_page: 1-based start page.
        :param end_page: 1-based end (inclusive). 0 = until last.
        :param max_chars: Truncate the combined output.
        """
        path, is_temp = await self._resolve_to_path(source)
        try:
            reader = self._open(path)
            n = len(reader.pages)
            start = max(1, int(start_page))
            end = n if end_page <= 0 else min(int(end_page), n)
            chunks: list[str] = []
            total = 0
            for i in range(start, end + 1):
                page_text = (reader.pages[i - 1].extract_text() or "").strip()
                section = f"[page {i}]\n{page_text}"
                if total + len(section) > max_chars:
                    chunks.append(f"\n... [truncated at {max_chars} chars; pages {i}-{end} omitted]")
                    break
                chunks.append(section)
                total += len(section)
            return "\n\n".join(chunks)
        finally:
            if is_temp and not self.valves.DOWNLOAD_DIR:
                Path(path).unlink(missing_ok=True)

    async def search(self, source: str, query: str, max_hits: int = 25) -> str:
        """Substring search across every page. Case-insensitive.

        :param source: Local path or URL.
        :param query: Substring to find.
        :param max_hits: Max page hits to return.
        """
        path, is_temp = await self._resolve_to_path(source)
        try:
            reader = self._open(path)
            q = query.lower()
            hits: list[str] = []
            for i, page in enumerate(reader.pages, start=1):
                text = (page.extract_text() or "").lower()
                idx = text.find(q)
                if idx < 0:
                    continue
                # Show a 160-char window around the first hit on this page.
                lo = max(0, idx - 80)
                hi = min(len(text), idx + 80 + len(q))
                hits.append(f"[page {i}] …{text[lo:hi].replace(chr(10), ' ')}…")
                if len(hits) >= max_hits:
                    break
            if not hits:
                return f"No matches for {query!r}."
            return "\n".join(hits)
        finally:
            if is_temp and not self.valves.DOWNLOAD_DIR:
                Path(path).unlink(missing_ok=True)

    async def get_outline(self, source: str) -> str:
        """Return the bookmarks / table of contents (if present).

        :param source: Local path or URL.
        """
        path, is_temp = await self._resolve_to_path(source)
        try:
            reader = self._open(path)
            outline = getattr(reader, "outline", None) or []
            lines: list[str] = []
            _walk_outline(outline, reader, lines, depth=0)
            return "\n".join(lines) or "No outline / bookmarks."
        finally:
            if is_temp and not self.valves.DOWNLOAD_DIR:
                Path(path).unlink(missing_ok=True)

    async def page_count(self, source: str) -> int:
        """Return only the page count (cheap)."""
        path, is_temp = await self._resolve_to_path(source)
        try:
            return len(self._open(path).pages)
        finally:
            if is_temp and not self.valves.DOWNLOAD_DIR:
                Path(path).unlink(missing_ok=True)


def _walk_outline(items: list, reader, out: list[str], depth: int) -> None:
    pad = "  " * depth
    for entry in items:
        if isinstance(entry, list):
            _walk_outline(entry, reader, out, depth + 1)
            continue
        title = getattr(entry, "title", None) or str(entry)
        try:
            page_num = reader.get_destination_page_number(entry) + 1
            out.append(f"{pad}- {title}  [page {page_num}]")
        except Exception:
            out.append(f"{pad}- {title}")
