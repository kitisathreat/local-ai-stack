"""
title: Literature Review Author — Topic → Markdown Review with Citations
author: local-ai-stack
description: Take a research topic, fan out across the academic catalogues already in the suite (OpenAlex / Semantic Scholar / arXiv / PubMed / DBLP / NASA ADS), filter by year and citation count, fetch full texts via `paper_full_text` for the top candidates, and emit a structured markdown review with proper inline citations and a references section. The tool builds an outline (Background / Methods / Findings / Open Questions) from the model's instructions and stops short of writing prose itself — it returns a per-section bibliography + per-paper extract so the LLM can compose without re-fetching.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field


def _load_tool(name: str):
    spec = importlib.util.spec_from_file_location(
        f"_lai_{name}", Path(__file__).parent / f"{name}.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod.Tools()


class Tools:
    class Valves(BaseModel):
        DEFAULT_PAPERS_PER_SECTION: int = Field(
            default=8,
            description="How many papers to surface per outline section.",
        )
        DEFAULT_YEAR_MIN: int = Field(default=2018, description="Filter out papers older than this by default.")
        FETCH_FULL_TEXT_FOR_TOP: int = Field(
            default=4,
            description="Fetch full PDFs for the top-N papers per section (rest stays as abstract-only).",
        )
        OPENALEX_BASE: str = Field(default="https://api.openalex.org")

    def __init__(self):
        self.valves = self.Valves()

    # ── Helpers ───────────────────────────────────────────────────────────

    async def _openalex_search(
        self,
        query: str,
        per_page: int,
        year_min: int,
    ) -> list[dict]:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(
                f"{self.valves.OPENALEX_BASE}/works",
                params={
                    "search": query,
                    "per-page": per_page,
                    "filter": f"from_publication_date:{year_min}-01-01,is_oa:true",
                    "sort": "cited_by_count:desc",
                },
            )
        if r.status_code != 200:
            return []
        return (r.json() or {}).get("results", [])

    # ── Public API ────────────────────────────────────────────────────────

    async def search_topic(
        self,
        topic: str,
        papers: int = 0,
        year_min: int = 0,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Run a flat OpenAlex search on a topic, ranked by citation count and
        filtered to open-access papers from `year_min` onward.
        :param topic: Search query.
        :param papers: Max results. 0 = DEFAULT_PAPERS_PER_SECTION × 3.
        :param year_min: Minimum publication year. 0 = DEFAULT_YEAR_MIN.
        :return: Markdown table: id, title, year, citations, OA url.
        """
        n = papers or self.valves.DEFAULT_PAPERS_PER_SECTION * 3
        ymin = year_min or self.valves.DEFAULT_YEAR_MIN
        results = await self._openalex_search(topic, n, ymin)
        if not results:
            return f"(no results for '{topic}' since {ymin})"
        rows = ["| id | title | year | cites | oa_url |", "|---|---|---|---|---|"]
        for w in results:
            wid = (w.get("id") or "").split("/")[-1]
            title = (w.get("title") or "")[:80].replace("|", "/")
            year = w.get("publication_year", "?")
            cites = w.get("cited_by_count", 0)
            oa = (w.get("best_oa_location") or {}).get("pdf_url") or w.get("doi") or ""
            rows.append(f"| {wid} | {title} | {year} | {cites} | {oa} |")
        return "\n".join(rows)

    async def build_review(
        self,
        topic: str,
        outline: list[str] = None,
        papers_per_section: int = 0,
        year_min: int = 0,
        fetch_full_text: int = 0,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Build a review skeleton: per-section bibliography + extracts for
        the top papers in each. The LLM uses the returned material to
        write prose. Default outline: Background / Methods / Findings /
        Open Questions.
        :param topic: Review subject.
        :param outline: Section names. Default ["Background","Methods","Findings","Open Questions"].
        :param papers_per_section: How many papers per section. 0 = DEFAULT_PAPERS_PER_SECTION.
        :param year_min: Year cutoff. 0 = DEFAULT_YEAR_MIN.
        :param fetch_full_text: Fetch full PDFs for the top-N per section. 0 = FETCH_FULL_TEXT_FOR_TOP.
        :return: Markdown skeleton with section headers, bibliographies, and extracts.
        """
        outline = outline or ["Background", "Methods", "Findings", "Open Questions"]
        per_sec = papers_per_section or self.valves.DEFAULT_PAPERS_PER_SECTION
        ymin = year_min or self.valves.DEFAULT_YEAR_MIN
        full_n = fetch_full_text if fetch_full_text >= 0 else self.valves.FETCH_FULL_TEXT_FOR_TOP

        full_text = _load_tool("paper_full_text")
        out = [f"# Review: {topic}\n", f"_Generated by literature_review_author. Year ≥ {ymin}._\n"]

        for section in outline:
            query = f"{topic} {section}"
            results = await self._openalex_search(query, per_sec, ymin)
            out.append(f"\n## {section}\n")
            if not results:
                out.append("(no candidates)\n")
                continue
            for i, w in enumerate(results, 1):
                title = w.get("title") or "(no title)"
                year = w.get("publication_year")
                cites = w.get("cited_by_count", 0)
                doi = w.get("doi", "")
                authors = ", ".join(
                    (a.get("author") or {}).get("display_name", "?")
                    for a in (w.get("authorships") or [])[:3]
                )
                abstract = ""
                if w.get("abstract_inverted_index"):
                    # OpenAlex stores abstracts inverted-index style; reconstruct.
                    inv = w["abstract_inverted_index"]
                    if isinstance(inv, dict):
                        positions: list[tuple[int, str]] = []
                        for word, idxs in inv.items():
                            for p in idxs:
                                positions.append((p, word))
                        positions.sort()
                        abstract = " ".join(w for _, w in positions)[:600]
                out.append(
                    f"{i}. **{title}** ({year}) — {authors}. cites={cites}. doi={doi}"
                )
                if abstract:
                    out.append(f"   > {abstract}")

            for w in results[:full_n]:
                doi = w.get("doi", "")
                if not doi:
                    continue
                ident = doi.replace("https://doi.org/", "")
                extract = await full_text.fetch(ident)
                out.append(f"\n### Full text: {ident}\n")
                out.append(extract[:5000])

        return "\n".join(out)
