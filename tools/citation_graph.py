"""
title: Citation Graph — Navigate References & Cited-By via OpenAlex
author: local-ai-stack
description: Walk the academic citation graph. Given a paper's DOI / arXiv id / OpenAlex work id, fetch its references (papers it cites) and its cited-by list (papers that cite it), with depth-N traversal and ranking by citation count. Useful for tracing the lineage of an idea or finding follow-up work to a foundational paper. Pair with `paper_full_text` to read the most-cited descendants.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field


class Tools:
    class Valves(BaseModel):
        OPENALEX_BASE: str = Field(default="https://api.openalex.org")
        DEFAULT_LIMIT: int = Field(default=20, description="Max nodes returned per call.")
        TIMEOUT: int = Field(default=20)

    def __init__(self):
        self.valves = self.Valves()

    async def _resolve(self, ident: str) -> dict | None:
        """Coerce any common identifier into an OpenAlex work record."""
        s = ident.strip()
        url = ""
        if s.startswith("W") and s[1:].isdigit():
            url = f"{self.valves.OPENALEX_BASE}/works/{s}"
        elif s.startswith("10.") or "doi.org" in s:
            doi = s.replace("https://doi.org/", "").replace("http://doi.org/", "")
            url = f"{self.valves.OPENALEX_BASE}/works/doi:{doi}"
        elif s.lower().startswith("arxiv:") or (len(s.split(".")) == 2 and s.split(".")[0].isdigit()):
            ident2 = s.replace("arxiv:", "").strip()
            url = f"{self.valves.OPENALEX_BASE}/works/arxiv:{ident2}"
        else:
            url = f"{self.valves.OPENALEX_BASE}/works/{s}"
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT) as c:
            r = await c.get(url)
        if r.status_code != 200:
            return None
        return r.json()

    def _format_work(self, w: dict) -> str:
        wid = (w.get("id") or "").split("/")[-1]
        title = (w.get("title") or "")[:90]
        year = w.get("publication_year", "?")
        cites = w.get("cited_by_count", 0)
        return f"{wid:<14} ({year}) cites={cites:>5}  {title}"

    # ── Public API ────────────────────────────────────────────────────────

    async def lookup(self, identifier: str, __user__: Optional[dict] = None) -> str:
        """
        Resolve an identifier to its OpenAlex record summary.
        :param identifier: DOI, arXiv id, or OpenAlex work id.
        :return: Multi-line summary.
        """
        w = await self._resolve(identifier)
        if not w:
            return f"not found: {identifier}"
        authors = ", ".join((a.get("author") or {}).get("display_name", "?")
                            for a in (w.get("authorships") or [])[:5])
        return (
            f"id:           {(w.get('id') or '').split('/')[-1]}\n"
            f"title:        {w.get('title')}\n"
            f"year:         {w.get('publication_year')}\n"
            f"cited_by:     {w.get('cited_by_count', 0)}\n"
            f"authors:      {authors}\n"
            f"doi:          {w.get('doi','-')}\n"
            f"oa_url:       {(w.get('best_oa_location') or {}).get('pdf_url','-')}"
        )

    async def references(
        self,
        identifier: str,
        limit: int = 0,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        List the papers that this paper cites (its bibliography).
        :param identifier: DOI / arXiv / OpenAlex id.
        :param limit: Max papers. 0 = DEFAULT_LIMIT.
        :return: One row per cited paper, ranked by citation count.
        """
        w = await self._resolve(identifier)
        if not w:
            return f"not found: {identifier}"
        n = limit or self.valves.DEFAULT_LIMIT
        ref_ids = (w.get("referenced_works") or [])[:n * 2]
        if not ref_ids:
            return "(no references)"
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT) as c:
            r = await c.get(
                f"{self.valves.OPENALEX_BASE}/works",
                params={"filter": "openalex:" + "|".join(rid.split("/")[-1] for rid in ref_ids[:50]),
                        "per-page": min(n * 2, 100)},
            )
        if r.status_code != 200:
            return f"HTTP {r.status_code}"
        works = (r.json() or {}).get("results", [])
        works.sort(key=lambda w: w.get("cited_by_count", 0), reverse=True)
        return "\n".join(self._format_work(w) for w in works[:n])

    async def cited_by(
        self,
        identifier: str,
        limit: int = 0,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        List the papers that cite this paper (descendants / follow-ups).
        :param identifier: DOI / arXiv / OpenAlex id.
        :param limit: Max papers. 0 = DEFAULT_LIMIT.
        :return: Ranked list of citing papers.
        """
        w = await self._resolve(identifier)
        if not w:
            return f"not found: {identifier}"
        n = limit or self.valves.DEFAULT_LIMIT
        cited_url = w.get("cited_by_api_url")
        if not cited_url:
            return "(no cited_by_api_url on record)"
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT) as c:
            r = await c.get(cited_url, params={"per-page": n, "sort": "cited_by_count:desc"})
        if r.status_code != 200:
            return f"HTTP {r.status_code}"
        results = (r.json() or {}).get("results", [])
        return "\n".join(self._format_work(w) for w in results[:n])

    async def trace_lineage(
        self,
        identifier: str,
        depth: int = 2,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Walk the references graph backward N levels. Each level shows the
        most-cited papers feeding into the previous one. Caps at 10 papers
        per level to keep output bounded.
        :param identifier: Starting paper.
        :param depth: Levels to walk (max 4).
        :return: Indented multi-level lineage tree.
        """
        depth = max(1, min(depth, 4))
        per_level = 10
        out: list[str] = []
        seen: set[str] = set()
        frontier = [identifier]
        for d in range(depth):
            out.append(f"\n── level {d} ──")
            next_frontier: list[str] = []
            for ident in frontier[:per_level]:
                if ident in seen:
                    continue
                seen.add(ident)
                w = await self._resolve(ident)
                if not w:
                    continue
                out.append(("  " * d) + self._format_work(w))
                next_frontier.extend(w.get("referenced_works") or [])
            frontier = [x.split("/")[-1] for x in next_frontier]
            if not frontier:
                break
        return "\n".join(out)
