"""
title: DBLP Computer Science Bibliography
author: local-ai-stack
description: Search DBLP — the definitive bibliography for computer science research. Find papers from all major CS venues (NeurIPS, ICML, ICLR, CVPR, ACL, SOSP, etc.). No API key required.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from pydantic import BaseModel, Field
from typing import Callable, Any, Optional


DBLP_API = "https://dblp.org/search"


class Tools:
    class Valves(BaseModel):
        MAX_RESULTS: int = Field(default=8, description="Maximum results to return")

    def __init__(self):
        self.valves = self.Valves()

    def _headers(self) -> dict:
        return {"User-Agent": "local-ai-stack/1.0 (https://github.com/kitisathreat/local-ai-stack)"}

    def _fmt_hit(self, info: dict) -> str:
        title = info.get("title", "No title").rstrip(".")
        if isinstance(title, dict):
            title = title.get("#text", "No title")
        authors = info.get("authors", {}).get("author", [])
        if isinstance(authors, dict):
            authors = [authors]
        author_names = [a.get("#text", a) if isinstance(a, dict) else a for a in authors[:4]]
        author_str = ", ".join(author_names)
        if len(authors) > 4:
            author_str += " et al."
        year = info.get("year", "?")
        venue = info.get("venue", "")
        if isinstance(venue, list):
            venue = venue[0] if venue else ""
        ee = info.get("ee", "")
        if isinstance(ee, list):
            ee = ee[0] if ee else ""
        url = info.get("url", "")

        lines = [f"**{title}**"]
        lines.append(f"   {author_str} ({year}) | {venue}")
        if ee:
            lines.append(f"   🔗 {ee}")
        return "\n".join(lines)

    async def search_publications(
        self,
        query: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search DBLP for computer science publications from all major venues.
        :param query: Search terms — title words, author name, or venue (e.g. "attention is all you need", "Geoffrey Hinton", "NeurIPS 2023")
        :return: Papers with titles, authors, venue/conference, year, and links
        """
        if __event_emitter__:
            await __event_emitter__(
                {"type": "status", "data": {"description": f"Searching DBLP: {query}", "done": False}}
            )

        try:
            params = {
                "q": query,
                "format": "json",
                "h": self.valves.MAX_RESULTS,
                "f": 0,
            }
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{DBLP_API}/publ/api",
                    params=params,
                    headers=self._headers(),
                )
                resp.raise_for_status()
                data = resp.json()

            hits = data.get("result", {}).get("hits", {})
            hit_list = hits.get("hit", [])
            total = hits.get("@total", 0)

            if not hit_list:
                return f"No DBLP results for: {query}"

            if __event_emitter__:
                await __event_emitter__(
                    {"type": "status", "data": {"description": f"Found {total} publications", "done": True}}
                )

            lines = [f"## DBLP: {query} ({total} total)\n"]
            for hit in hit_list:
                info = hit.get("info", {})
                lines.append(self._fmt_hit(info))
                lines.append("")

            return "\n".join(lines)

        except httpx.ConnectError:
            return "Cannot reach DBLP. Check internet connection."
        except Exception as e:
            return f"DBLP error: {str(e)}"

    async def search_authors(
        self,
        name: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Find a computer scientist by name on DBLP and see their publication list.
        :param name: Author's full or partial name (e.g. "Yoshua Bengio", "Fei-Fei Li")
        :return: Author profile link and publication count
        """
        try:
            params = {
                "q": name,
                "format": "json",
                "h": 5,
            }
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{DBLP_API}/author/api",
                    params=params,
                    headers=self._headers(),
                )
                resp.raise_for_status()
                data = resp.json()

            hits = data.get("result", {}).get("hits", {}).get("hit", [])
            if not hits:
                return f"No DBLP author found: {name}"

            lines = [f"## DBLP Authors matching '{name}':\n"]
            for h in hits:
                info = h.get("info", {})
                author_name = info.get("author", "Unknown")
                url = info.get("url", "")
                aliases = info.get("aliases", {}).get("alias", [])
                if isinstance(aliases, str):
                    aliases = [aliases]
                lines.append(f"**{author_name}**")
                if aliases:
                    lines.append(f"   Also known as: {', '.join(aliases[:3])}")
                if url:
                    lines.append(f"   Profile: {url}")
                lines.append("")

            return "\n".join(lines)

        except Exception as e:
            return f"DBLP author search error: {str(e)}"
