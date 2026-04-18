"""
title: OpenAlex — Open Scholarly Catalog
author: local-ai-stack
description: Search OpenAlex — a fully open catalog of 250M+ scientific works, authors, institutions, and concepts. No API key required. Covers all fields of research.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from pydantic import BaseModel, Field
from typing import Callable, Any, Optional


OA_API = "https://api.openalex.org"


class Tools:
    class Valves(BaseModel):
        CONTACT_EMAIL: str = Field(
            default="local@localhost",
            description="Email for OpenAlex polite pool (better rate limits)",
        )
        MAX_RESULTS: int = Field(default=5, description="Maximum results to return")

    def __init__(self):
        self.valves = self.Valves()

    def _params(self, extras: dict = None) -> dict:
        p = {"mailto": self.valves.CONTACT_EMAIL, "per-page": self.valves.MAX_RESULTS}
        if extras:
            p.update(extras)
        return p

    async def search_works(
        self,
        query: str,
        year_from: int = 0,
        open_access_only: bool = False,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search OpenAlex for academic works across all scientific disciplines.
        :param query: Research topic or keywords (e.g. "large language models", "quantum entanglement")
        :param year_from: Only include works published from this year onward (e.g. 2020)
        :param open_access_only: If true, return only open-access works with free PDFs
        :return: Works with titles, authors, venue, citation counts, and open-access links
        """
        if __event_emitter__:
            await __event_emitter__(
                {"type": "status", "data": {"description": f"Searching OpenAlex: {query}", "done": False}}
            )

        filters = [f"default.search:{query}"]
        if year_from:
            filters.append(f"from_publication_date:{year_from}-01-01")
        if open_access_only:
            filters.append("is_oa:true")

        params = self._params({
            "filter": ",".join(filters),
            "select": "id,title,authorships,publication_year,primary_location,cited_by_count,open_access,doi",
            "sort": "relevance_score:desc",
        })

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(f"{OA_API}/works", params=params)
                resp.raise_for_status()
                data = resp.json()

            results = data.get("results", [])
            if not results:
                return f"No OpenAlex results for: {query}"

            if __event_emitter__:
                await __event_emitter__(
                    {"type": "status", "data": {"description": f"Found {len(results)} works", "done": True}}
                )

            lines = [f"## OpenAlex: {query}\n"]
            for w in results:
                title = w.get("title") or "No title"
                year = w.get("publication_year") or "?"
                cited = w.get("cited_by_count", 0)
                doi = w.get("doi") or ""
                doi_url = doi if doi.startswith("http") else (f"https://doi.org/{doi}" if doi else "")

                authorships = w.get("authorships", [])[:3]
                authors = [
                    a.get("author", {}).get("display_name", "")
                    for a in authorships
                ]
                author_str = ", ".join(authors)
                if len(w.get("authorships", [])) > 3:
                    author_str += " et al."

                loc = w.get("primary_location") or {}
                source = (loc.get("source") or {}).get("display_name", "")
                oa = w.get("open_access", {}) or {}
                pdf = oa.get("oa_url", "")

                lines.append(f"**{title}**")
                lines.append(f"   {author_str} ({year}) | {source} | 📊 {cited:,} citations")
                if pdf:
                    lines.append(f"   📄 Free PDF: {pdf}")
                elif doi_url:
                    lines.append(f"   🔗 {doi_url}")
                lines.append("")

            return "\n".join(lines)

        except Exception as e:
            return f"OpenAlex error: {str(e)}"

    async def get_concept(
        self,
        concept: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get an overview of a scientific concept — how many papers exist, top related concepts, and key works.
        :param concept: Scientific concept or field (e.g. "machine learning", "genomics", "climate change")
        :return: Concept description with paper count, related topics, and most-cited works
        """
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{OA_API}/concepts",
                    params=self._params({"search": concept, "per-page": 1}),
                )
                resp.raise_for_status()
                results = resp.json().get("results", [])
                if not results:
                    return f"Concept not found: {concept}"

                c = results[0]
                cid = c.get("id", "").split("/")[-1]

                # Get top works for concept
                works_resp = await client.get(
                    f"{OA_API}/works",
                    params=self._params({
                        "filter": f"concepts.id:{cid}",
                        "sort": "cited_by_count:desc",
                        "select": "title,cited_by_count,publication_year",
                        "per-page": 5,
                    }),
                )
                works_resp.raise_for_status()
                top_works = works_resp.json().get("results", [])

            related = [r.get("display_name", "") for r in (c.get("related_concepts") or [])[:8]]
            lines = [
                f"## Scientific Concept: {c.get('display_name', concept)}\n",
                f"- **Level:** {c.get('level', 'N/A')} (0=broadest, 5=most specific)",
                f"- **Works count:** {c.get('works_count', 0):,}",
                f"- **Citations:** {c.get('cited_by_count', 0):,}",
                f"- **Related concepts:** {', '.join(related)}",
                f"\n**Top cited works in this field:**",
            ]
            for w in top_works:
                lines.append(f"- {w.get('title', 'Untitled')} ({w.get('publication_year', '?')}) — {w.get('cited_by_count', 0):,} citations")

            return "\n".join(lines)

        except Exception as e:
            return f"OpenAlex concept error: {str(e)}"

    async def get_institution(
        self,
        institution_name: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Look up a research institution's publication output and impact on OpenAlex.
        :param institution_name: Name of a university, lab, or research org (e.g. "MIT", "CERN", "NIH")
        :return: Institution details, country, paper count, and citation totals
        """
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{OA_API}/institutions",
                    params=self._params({"search": institution_name, "per-page": 1}),
                )
                resp.raise_for_status()
                results = resp.json().get("results", [])
                if not results:
                    return f"Institution not found: {institution_name}"
                i = results[0]

            return (
                f"## Institution: {i.get('display_name', institution_name)}\n"
                f"- **Type:** {i.get('type', 'N/A')}\n"
                f"- **Country:** {i.get('country_code', 'N/A')}\n"
                f"- **Works:** {i.get('works_count', 0):,}\n"
                f"- **Citations:** {i.get('cited_by_count', 0):,}\n"
                f"- **Homepage:** {i.get('homepage_url', 'N/A')}\n"
                f"- **Concepts:** {', '.join(c.get('display_name', '') for c in (i.get('x_concepts') or [])[:6])}"
            )

        except Exception as e:
            return f"Institution lookup error: {str(e)}"
