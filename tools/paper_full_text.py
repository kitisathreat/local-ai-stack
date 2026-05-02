"""
title: Paper Full-Text — Resolve DOI/arXiv → Fetch PDF → Extract Text
author: local-ai-stack
description: Pipeline tool that turns an academic identifier (DOI, arXiv id, OpenAlex/Semantic Scholar work id) into the actual full-text body. Uses Unpaywall to find the open-access URL when paywalled, falls back to arXiv/Crossref direct fetch when applicable, downloads the PDF, and extracts paragraph-anchored text via pypdf. Output chunks carry [page N ¶M] anchors so the model can cite precisely. Pair with `literature_review_author` for synthesis across many papers.
required_open_webui_version: 0.4.0
requirements: httpx, pypdf
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field


_ARXIV_RE = re.compile(r"\b(\d{4}\.\d{4,5})(v\d+)?\b")
_DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s\"<>]+", re.IGNORECASE)


class Tools:
    class Valves(BaseModel):
        UNPAYWALL_EMAIL: str = Field(
            default="local-ai-stack@example.com",
            description="Unpaywall API requires an email for rate limiting (no signup, no key).",
        )
        CACHE_DIR: str = Field(
            default=str(Path.home() / ".cache" / "local-ai-stack" / "papers"),
            description="Where to cache downloaded PDFs + extracted text.",
        )
        MAX_PDF_BYTES: int = Field(default=40_000_000, description="Cap on PDF size (~40MB).")
        MAX_TEXT_CHARS: int = Field(default=120_000, description="Cap on returned extract.")

    def __init__(self):
        self.valves = self.Valves()

    # ── Identifiers ──────────────────────────────────────────────────────

    def _parse_id(self, ident: str) -> tuple[str, str]:
        s = ident.strip()
        if s.startswith("https://arxiv.org/abs/") or s.startswith("http://arxiv.org/abs/"):
            return "arxiv", s.rsplit("/", 1)[-1]
        if _ARXIV_RE.fullmatch(s) or s.startswith("arxiv:"):
            return "arxiv", s.replace("arxiv:", "").strip()
        if _DOI_RE.search(s):
            return "doi", _DOI_RE.search(s).group(0)
        if s.startswith("https://doi.org/"):
            return "doi", s.split("doi.org/", 1)[1]
        if s.startswith("W") and s[1:].isdigit():
            return "openalex", s
        return "doi", s   # best-effort fallback

    def _cache_path(self, key: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", key)
        d = Path(self.valves.CACHE_DIR).expanduser()
        d.mkdir(parents=True, exist_ok=True)
        return d / safe

    # ── PDF resolution ────────────────────────────────────────────────────

    async def _resolve_pdf_url(self, kind: str, ident: str) -> str | None:
        if kind == "arxiv":
            return f"https://arxiv.org/pdf/{ident}.pdf"
        if kind == "doi":
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(
                    f"https://api.unpaywall.org/v2/{ident}",
                    params={"email": self.valves.UNPAYWALL_EMAIL},
                )
            if r.status_code != 200:
                return None
            data = r.json()
            best = (data or {}).get("best_oa_location") or {}
            if best.get("url_for_pdf"):
                return best["url_for_pdf"]
            for loc in (data or {}).get("oa_locations") or []:
                if loc.get("url_for_pdf"):
                    return loc["url_for_pdf"]
            return None
        if kind == "openalex":
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(f"https://api.openalex.org/works/{ident}")
            if r.status_code != 200:
                return None
            data = r.json() or {}
            return (data.get("best_oa_location") or {}).get("pdf_url")
        return None

    async def _download(self, url: str, dest: Path) -> tuple[bool, str]:
        try:
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
                async with c.stream("GET", url, headers={"User-Agent": "local-ai-stack/1.0"}) as r:
                    if r.status_code != 200:
                        return False, f"HTTP {r.status_code}"
                    written = 0
                    with dest.open("wb") as f:
                        async for chunk in r.aiter_bytes(64 * 1024):
                            f.write(chunk)
                            written += len(chunk)
                            if written > self.valves.MAX_PDF_BYTES:
                                return False, f"exceeded MAX_PDF_BYTES ({self.valves.MAX_PDF_BYTES})"
            return True, f"saved {written} bytes"
        except Exception as e:
            return False, f"error: {e}"

    # ── Text extraction ──────────────────────────────────────────────────

    def _extract(self, pdf_path: Path) -> list[tuple[int, str]]:
        """Return [(page_no_1based, text)] chunks."""
        try:
            from pypdf import PdfReader  # type: ignore
        except ImportError:
            return []
        try:
            reader = PdfReader(str(pdf_path))
        except Exception:
            return []
        out: list[tuple[int, str]] = []
        for i, page in enumerate(reader.pages, start=1):
            try:
                txt = page.extract_text() or ""
            except Exception:
                txt = ""
            if txt.strip():
                out.append((i, txt))
        return out

    # ── Public API ────────────────────────────────────────────────────────

    async def fetch(
        self,
        identifier: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Resolve a DOI / arXiv id / OpenAlex work id to its full text.
        Caches the PDF + extract on disk so repeat calls are instant.
        :param identifier: DOI like "10.1038/nature12373", arXiv id like "2103.12345", or OpenAlex work id "W123…".
        :return: Multi-section response with metadata + paragraph-anchored extract.
        """
        kind, ident = self._parse_id(identifier)
        cache = self._cache_path(f"{kind}_{ident}")
        pdf_path = cache.with_suffix(".pdf")
        txt_path = cache.with_suffix(".txt")

        if txt_path.exists():
            text = txt_path.read_text(encoding="utf-8")
            return f"({kind}={ident}) cached extract\n{text[:self.valves.MAX_TEXT_CHARS]}"

        pdf_url = await self._resolve_pdf_url(kind, ident)
        if not pdf_url:
            return f"could not find OA PDF for {kind}={ident}"

        ok, msg = await self._download(pdf_url, pdf_path)
        if not ok:
            return f"download failed: {msg}"

        chunks = self._extract(pdf_path)
        if not chunks:
            return f"PDF saved at {pdf_path} but text extraction failed (pypdf may need install)"

        body_lines = []
        for page_no, txt in chunks:
            paras = [p.strip() for p in re.split(r"\n\s*\n", txt) if p.strip()]
            for j, p in enumerate(paras, start=1):
                body_lines.append(f"[p{page_no} ¶{j}] {p}")
        text = "\n\n".join(body_lines)[: self.valves.MAX_TEXT_CHARS]
        txt_path.write_text(text, encoding="utf-8")
        return f"({kind}={ident}) {len(chunks)} pages, source: {pdf_url}\n\n{text}"

    def cache_summary(self, __user__: Optional[dict] = None) -> str:
        """
        Show cached PDF + extract sizes.
        :return: One row per cached paper.
        """
        d = Path(self.valves.CACHE_DIR).expanduser()
        if not d.exists():
            return f"(no cache yet) {d}"
        rows = []
        for p in sorted(d.glob("*.pdf")):
            sz = p.stat().st_size
            t = p.with_suffix(".txt")
            tsz = t.stat().st_size if t.exists() else 0
            rows.append(f"{p.name:<60}  pdf={sz/1e6:.1f}MB  txt={tsz/1e3:.0f}KB")
        return "\n".join(rows) if rows else "(empty)"
