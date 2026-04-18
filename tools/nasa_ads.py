"""
title: NASA ADS — Astrophysics Data System
author: local-ai-stack
description: Search NASA's Astrophysics Data System — 15M+ papers in astronomy, astrophysics, physics, and planetary science. Free API token required (takes 1 minute to get).
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from pydantic import BaseModel, Field
from typing import Callable, Any, Optional


ADS_API = "https://api.adsabs.harvard.edu/v1"


class Tools:
    class Valves(BaseModel):
        ADS_API_KEY: str = Field(
            default="",
            description="NASA ADS API token — free at https://ui.adsabs.harvard.edu/user/settings/token (account required)",
        )
        MAX_RESULTS: int = Field(default=5, description="Maximum papers to return")

    def __init__(self):
        self.valves = self.Valves()

    def _headers(self) -> dict:
        if not self.valves.ADS_API_KEY:
            return {}
        return {
            "Authorization": f"Bearer {self.valves.ADS_API_KEY}",
            "User-Agent": "local-ai-stack/1.0",
        }

    async def search_papers(
        self,
        query: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search NASA ADS for astronomy and astrophysics papers.
        :param query: Search terms (e.g. "black hole merger gravitational waves", "exoplanet atmosphere JWST", "dark matter distribution")
        :return: Papers with titles, authors, journal, year, citation counts, and ADS links
        """
        if not self.valves.ADS_API_KEY:
            return (
                "NASA ADS requires a free API token.\n"
                "Get one at: https://ui.adsabs.harvard.edu/user/settings/token\n"
                "Then add it in Open WebUI > Admin > Tools > NASA ADS > Valves > ADS_API_KEY"
            )

        if __event_emitter__:
            await __event_emitter__(
                {"type": "status", "data": {"description": f"Searching NASA ADS: {query}", "done": False}}
            )

        params = {
            "q": query,
            "fl": "title,author,year,bibcode,citation_count,identifier,abstract,pub",
            "rows": self.valves.MAX_RESULTS,
            "sort": "citation_count desc",
        }

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{ADS_API}/search/query",
                    params=params,
                    headers=self._headers(),
                )
                resp.raise_for_status()
                docs = resp.json().get("response", {}).get("docs", [])

            if not docs:
                return f"No ADS papers found for: {query}"

            if __event_emitter__:
                await __event_emitter__(
                    {"type": "status", "data": {"description": f"Found {len(docs)} papers", "done": True}}
                )

            lines = [f"## NASA ADS: {query}\n"]
            for d in docs:
                title = d.get("title", ["No title"])[0]
                authors = d.get("author", [])[:3]
                author_str = ", ".join(authors)
                if len(d.get("author", [])) > 3:
                    author_str += " et al."
                year = d.get("year", "?")
                pub = d.get("pub", "")
                cites = d.get("citation_count", 0)
                bibcode = d.get("bibcode", "")
                ads_url = f"https://ui.adsabs.harvard.edu/abs/{bibcode}" if bibcode else ""

                # Look for ArXiv identifier
                ids = d.get("identifier", [])
                arxiv_id = next((i.replace("arXiv:", "") for i in ids if "arXiv" in i), "")
                arxiv_url = f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else ""

                lines.append(f"**{title}**")
                lines.append(f"   {author_str} ({year}) | {pub} | ⭐ {cites:,} citations")
                if ads_url:
                    lines.append(f"   🔭 ADS: {ads_url}")
                if arxiv_url:
                    lines.append(f"   📄 ArXiv: {arxiv_url}")
                lines.append("")

            return "\n".join(lines)

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                return "Invalid ADS API key. Check your token at https://ui.adsabs.harvard.edu/user/settings/token"
            return f"NASA ADS error: HTTP {e.response.status_code}"
        except Exception as e:
            return f"NASA ADS error: {str(e)}"

    async def get_citations(
        self,
        bibcode: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get a list of papers that cite a specific ADS paper by its bibcode.
        :param bibcode: ADS bibcode (e.g. "2023ApJ...945L..55A")
        :return: List of citing papers
        """
        if not self.valves.ADS_API_KEY:
            return "NASA ADS API key required. See tool settings."

        try:
            params = {
                "q": f"citations(bibcode:{bibcode})",
                "fl": "title,author,year,citation_count,bibcode",
                "rows": 8,
                "sort": "citation_count desc",
            }
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{ADS_API}/search/query",
                    params=params,
                    headers=self._headers(),
                )
                resp.raise_for_status()
                docs = resp.json().get("response", {}).get("docs", [])

            if not docs:
                return f"No citations found for bibcode: {bibcode}"

            lines = [f"## Papers citing {bibcode}:\n"]
            for d in docs:
                title = (d.get("title") or ["Untitled"])[0]
                authors = (d.get("author") or [])[:2]
                year = d.get("year", "?")
                cites = d.get("citation_count", 0)
                bc = d.get("bibcode", "")
                url = f"https://ui.adsabs.harvard.edu/abs/{bc}" if bc else ""
                lines.append(f"- **{title}** — {', '.join(authors)} ({year}) | {cites} citations")
                if url:
                    lines.append(f"  {url}")

            return "\n".join(lines)

        except Exception as e:
            return f"ADS citations error: {str(e)}"
