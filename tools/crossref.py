"""
title: CrossRef — DOI & Citation Lookup
author: local-ai-stack
description: Look up any academic paper by DOI, search 150M+ works, get citation counts, and resolve references via CrossRef. Essential for fact-checking citations and finding publication metadata.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from pydantic import BaseModel, Field
from typing import Callable, Any, Optional


CROSSREF_API = "https://api.crossref.org"


class Tools:
    class Valves(BaseModel):
        CONTACT_EMAIL: str = Field(
            default="local@localhost",
            description="Your email for CrossRef polite pool (faster responses)",
        )
        MAX_RESULTS: int = Field(default=5, description="Maximum works to return")

    def __init__(self):
        self.valves = self.Valves()

    def _headers(self) -> dict:
        return {
            "User-Agent": f"local-ai-stack/1.0 (mailto:{self.valves.CONTACT_EMAIL})",
        }

    def _fmt_work(self, w: dict) -> str:
        title = " ".join(w.get("title", ["No title"]))
        authors = w.get("author", [])
        author_str = ", ".join(
            f"{a.get('family', '')} {a.get('given', '')[:1]}".strip()
            for a in authors[:3]
        )
        if len(authors) > 3:
            author_str += " et al."
        date_parts = w.get("published", {}).get("date-parts", [[]])[0]
        year = date_parts[0] if date_parts else "?"
        journal = (w.get("container-title") or [""])[0]
        doi = w.get("DOI", "")
        cited_by = w.get("is-referenced-by-count", 0)
        doi_url = f"https://doi.org/{doi}" if doi else ""

        lines = [f"**{title}**"]
        lines.append(f"   {author_str} ({year}) | {journal}")
        lines.append(f"   Cited by: {cited_by:,} | DOI: {doi_url}")
        return "\n".join(lines)

    async def lookup_doi(
        self,
        doi: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Look up full publication metadata for any DOI (Digital Object Identifier).
        :param doi: The DOI string, with or without 'https://doi.org/' prefix (e.g. "10.1038/s41586-021-03819-2")
        :return: Full title, authors, journal, year, citation count, and abstract if available
        """
        doi = doi.strip()
        doi = doi.replace("https://doi.org/", "").replace("http://doi.org/", "")

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{CROSSREF_API}/works/{doi}",
                    headers=self._headers(),
                )
                resp.raise_for_status()
                w = resp.json().get("message", {})

            title = " ".join(w.get("title", ["No title"]))
            authors = w.get("author", [])
            author_list = []
            for a in authors:
                name = f"{a.get('given', '')} {a.get('family', '')}".strip()
                affil = (a.get("affiliation") or [{}])[0].get("name", "")
                author_list.append(f"{name}" + (f" ({affil})" if affil else ""))
            date_parts = w.get("published", {}).get("date-parts", [[]])[0]
            year = date_parts[0] if date_parts else "?"
            month = date_parts[1] if len(date_parts) > 1 else ""
            journal = (w.get("container-title") or [""])[0]
            publisher = w.get("publisher", "")
            cited_by = w.get("is-referenced-by-count", 0)
            ref_count = w.get("references-count", 0)
            work_type = w.get("type", "")
            abstract = w.get("abstract", "")
            # Strip JATS XML tags from abstract
            import re
            abstract = re.sub(r"<[^>]+>", "", abstract).strip() if abstract else ""

            result = (
                f"## {title}\n\n"
                f"**Authors:** {'; '.join(author_list[:6])}\n"
                f"**Published:** {month}/{year} in *{journal}*\n"
                f"**Publisher:** {publisher}\n"
                f"**Type:** {work_type}\n"
                f"**Cited by:** {cited_by:,} | **References:** {ref_count:,}\n"
                f"**DOI:** https://doi.org/{doi}\n"
            )
            if abstract:
                result += f"\n**Abstract:**\n{abstract}"
            return result

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return f"DOI not found: {doi}"
            return f"CrossRef error: HTTP {e.response.status_code}"
        except Exception as e:
            return f"DOI lookup error: {str(e)}"

    async def search_works(
        self,
        query: str,
        filter_type: str = "",
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search CrossRef's index of 150M+ scholarly works by keyword, author, or title.
        :param query: Search terms (e.g. "deep learning image recognition", "COVID-19 vaccine efficacy")
        :param filter_type: Optional type filter: 'journal-article', 'book', 'conference-paper', 'dataset'
        :return: Matching works with DOIs and citation counts
        """
        if __event_emitter__:
            await __event_emitter__(
                {"type": "status", "data": {"description": f"Searching CrossRef: {query}", "done": False}}
            )

        params = {
            "query": query,
            "rows": self.valves.MAX_RESULTS,
            "select": "DOI,title,author,published,container-title,is-referenced-by-count,type",
            "sort": "relevance",
        }
        if filter_type:
            params["filter"] = f"type:{filter_type}"

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{CROSSREF_API}/works",
                    params=params,
                    headers=self._headers(),
                )
                resp.raise_for_status()
                items = resp.json().get("message", {}).get("items", [])

            if not items:
                return f"No CrossRef results for: {query}"

            if __event_emitter__:
                await __event_emitter__(
                    {"type": "status", "data": {"description": f"Found {len(items)} works", "done": True}}
                )

            lines = [f"## CrossRef: {query}\n"]
            for w in items:
                lines.append(self._fmt_work(w))
                lines.append("")

            return "\n".join(lines)

        except Exception as e:
            return f"CrossRef search error: {str(e)}"

    async def get_journal_info(
        self,
        issn: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get metadata for an academic journal by ISSN.
        :param issn: Journal ISSN (e.g. "0028-0836" for Nature, "1476-4687" for Nature online)
        :return: Journal name, publisher, article count, and coverage info
        """
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{CROSSREF_API}/journals/{issn.strip()}",
                    headers=self._headers(),
                )
                resp.raise_for_status()
                j = resp.json().get("message", {})

            return (
                f"## Journal: {j.get('title', issn)}\n"
                f"- **Publisher:** {j.get('publisher', 'N/A')}\n"
                f"- **ISSN:** {', '.join(j.get('ISSN', []))}\n"
                f"- **Total articles indexed:** {j.get('counts', {}).get('total-dois', 0):,}\n"
                f"- **Current DOI deposits:** {j.get('counts', {}).get('current-dois', 0):,}\n"
                f"- **Coverage:** {j.get('coverage-type', {})}"
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return f"Journal ISSN not found: {issn}"
            return f"CrossRef error: HTTP {e.response.status_code}"
        except Exception as e:
            return f"Journal lookup error: {str(e)}"
