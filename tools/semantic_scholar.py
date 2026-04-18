"""
title: Semantic Scholar
author: local-ai-stack
description: Search 220M+ academic papers across all fields via Semantic Scholar's AI-powered API. Returns citations, abstracts, open-access PDFs, and author info. No API key required for basic use.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import os
import httpx
from pydantic import BaseModel, Field
from typing import Callable, Any, Optional


S2_API = "https://api.semanticscholar.org/graph/v1"
PAPER_FIELDS = "title,authors,year,abstract,url,citationCount,openAccessPdf,venue,externalIds"


class Tools:
    class Valves(BaseModel):
        S2_API_KEY: str = Field(
            default_factory=lambda: os.environ.get("S2_API_KEY", ""),
            description="Optional Semantic Scholar API key for higher rate limits (free at https://www.semanticscholar.org/product/api)",
        )
        MAX_RESULTS: int = Field(default=5, description="Maximum papers to return")

    def __init__(self):
        self.valves = self.Valves()

    def _headers(self) -> dict:
        h = {"User-Agent": "local-ai-stack/1.0"}
        if self.valves.S2_API_KEY:
            h["x-api-key"] = self.valves.S2_API_KEY
        return h

    async def search_papers(
        self,
        query: str,
        year_range: str = "",
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search Semantic Scholar for academic papers across all disciplines.
        :param query: Research topic or keywords (e.g. "transformer attention mechanisms", "climate change sea level")
        :param year_range: Optional year filter like "2020-2024" or "2023-"
        :return: Papers with titles, authors, citation counts, open-access links, and abstracts
        """
        if __event_emitter__:
            await __event_emitter__(
                {"type": "status", "data": {"description": f"Searching Semantic Scholar: {query}", "done": False}}
            )

        params = {
            "query": query,
            "limit": self.valves.MAX_RESULTS,
            "fields": PAPER_FIELDS,
        }
        if year_range:
            params["year"] = year_range

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{S2_API}/paper/search",
                    params=params,
                    headers=self._headers(),
                )
                resp.raise_for_status()
                data = resp.json()

            papers = data.get("data", [])
            if not papers:
                return f"No papers found on Semantic Scholar for: {query}"

            if __event_emitter__:
                await __event_emitter__(
                    {"type": "status", "data": {"description": f"Found {len(papers)} papers", "done": True}}
                )

            lines = [f"## Semantic Scholar: {query}\n"]
            for p in papers:
                title = p.get("title", "No title")
                authors = [a.get("name", "") for a in p.get("authors", [])[:3]]
                author_str = ", ".join(authors) + (" et al." if len(p.get("authors", [])) > 3 else "")
                year = p.get("year") or "?"
                venue = p.get("venue") or ""
                citations = p.get("citationCount", 0)
                url = p.get("url") or ""
                pdf = p.get("openAccessPdf", {})
                pdf_url = pdf.get("url", "") if pdf else ""
                abstract = (p.get("abstract") or "")[:300]
                if abstract and len(p.get("abstract", "")) > 300:
                    abstract += "..."

                lines.append(f"**{title}**")
                lines.append(f"   {author_str} ({year}) | {venue} | ⭐ {citations:,} citations")
                if abstract:
                    lines.append(f"   {abstract}")
                if pdf_url:
                    lines.append(f"   📄 PDF: {pdf_url}")
                if url:
                    lines.append(f"   🔗 {url}")
                lines.append("")

            return "\n".join(lines)

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                return "Rate limit hit. Add an S2_API_KEY in tool settings for higher limits."
            return f"Semantic Scholar error: HTTP {e.response.status_code}"
        except Exception as e:
            return f"Semantic Scholar error: {str(e)}"

    async def get_paper_details(
        self,
        paper_id: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get detailed information about a specific paper including full abstract, references count, and links.
        :param paper_id: Semantic Scholar paper ID, ArXiv ID (e.g. "arXiv:2310.12345"), or DOI
        :return: Full paper details with abstract and citation info
        """
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{S2_API}/paper/{paper_id}",
                    params={"fields": PAPER_FIELDS + ",references.title,citationCount,referenceCount"},
                    headers=self._headers(),
                )
                resp.raise_for_status()
                p = resp.json()

            authors = [a.get("name", "") for a in p.get("authors", [])[:5]]
            author_str = "; ".join(authors)
            if len(p.get("authors", [])) > 5:
                author_str += " et al."
            pdf = p.get("openAccessPdf", {})
            pdf_url = pdf.get("url", "") if pdf else ""
            ext_ids = p.get("externalIds", {}) or {}
            doi = ext_ids.get("DOI", "")
            arxiv_id = ext_ids.get("ArXiv", "")

            result = (
                f"## {p.get('title', 'Unknown')}\n\n"
                f"**Authors:** {author_str}\n"
                f"**Year:** {p.get('year', '?')} | **Venue:** {p.get('venue', 'N/A')}\n"
                f"**Citations:** {p.get('citationCount', 0):,} | **References:** {p.get('referenceCount', 0):,}\n"
            )
            if doi:
                result += f"**DOI:** {doi}\n"
            if arxiv_id:
                result += f"**ArXiv:** https://arxiv.org/abs/{arxiv_id}\n"
            if pdf_url:
                result += f"**Open Access PDF:** {pdf_url}\n"
            abstract = p.get("abstract") or "No abstract available."
            result += f"\n**Abstract:**\n{abstract}"
            return result

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return f"Paper not found: {paper_id}"
            return f"API error: HTTP {e.response.status_code}"
        except Exception as e:
            return f"Error fetching paper: {str(e)}"

    async def get_author_papers(
        self,
        author_name: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Find papers by a specific researcher or author on Semantic Scholar.
        :param author_name: Full author name (e.g. "Geoffrey Hinton", "Yann LeCun")
        :return: Author profile and their most-cited papers
        """
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                # Search author
                resp = await client.get(
                    f"{S2_API}/author/search",
                    params={"query": author_name, "limit": 1, "fields": "name,hIndex,citationCount,paperCount"},
                    headers=self._headers(),
                )
                resp.raise_for_status()
                authors = resp.json().get("data", [])
                if not authors:
                    return f"Author not found: {author_name}"

                author = authors[0]
                author_id = author.get("authorId")

                # Get their papers
                papers_resp = await client.get(
                    f"{S2_API}/author/{author_id}/papers",
                    params={"fields": "title,year,citationCount,venue", "limit": 8, "sort": "citationCount"},
                    headers=self._headers(),
                )
                papers_resp.raise_for_status()
                papers = papers_resp.json().get("data", [])

            lines = [
                f"## Semantic Scholar: {author.get('name', author_name)}\n",
                f"- **h-index:** {author.get('hIndex', 'N/A')}",
                f"- **Total citations:** {author.get('citationCount', 0):,}",
                f"- **Papers:** {author.get('paperCount', 0):,}",
                f"\n**Top papers:**",
            ]
            for p in papers[:6]:
                year = p.get("year") or "?"
                cites = p.get("citationCount", 0)
                lines.append(f"- {p.get('title', 'Untitled')} ({year}) — {cites:,} citations")

            return "\n".join(lines)

        except Exception as e:
            return f"Author search error: {str(e)}"
