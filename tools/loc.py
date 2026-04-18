"""
title: Library of Congress — Historical Collections
author: local-ai-stack
description: Search the Library of Congress's 20M+ item digital collections: historic newspapers (Chronicling America), photos, maps, manuscripts, audio recordings, and congressional records. No API key required.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from pydantic import BaseModel, Field
from typing import Optional


LOC = "https://www.loc.gov"
CHRON = "https://chroniclingamerica.loc.gov"


class Tools:
    class Valves(BaseModel):
        MAX_RESULTS: int = Field(default=8, description="Max results")

    def __init__(self):
        self.valves = self.Valves()

    async def search(
        self,
        query: str,
        format: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search the Library of Congress digital collections.
        :param query: Keywords
        :param format: Optional format filter: photo, map, manuscript, audio, film, book
        :return: Matching items with title, date, type, and URL
        """
        url = f"{LOC}/search/"
        if format:
            url = f"{LOC}/{format}s/"
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(url, params={"q": query, "fo": "json", "c": self.valves.MAX_RESULTS})
                r.raise_for_status()
                data = r.json()
            results = data.get("results", [])
            if not results:
                return f"No LoC results for: {query}"
            lines = [f"## Library of Congress: {query}\n"]
            for it in results[: self.valves.MAX_RESULTS]:
                title = it.get("title", "")
                date = it.get("date", "") or ""
                types = ", ".join(it.get("original_format", [])[:3])
                subs = ", ".join(it.get("subject", [])[:3])
                link = it.get("url", "") or it.get("id", "")
                img = (it.get("image_url") or [""])[0] if isinstance(it.get("image_url"), list) else ""
                lines.append(f"**{title}** ({date})")
                lines.append(f"   {types}")
                if subs:
                    lines.append(f"   subjects: {subs}")
                if img:
                    lines.append(f"   ![img]({img})")
                if link:
                    lines.append(f"   🔗 {link}\n")
            return "\n".join(lines)
        except Exception as e:
            return f"LoC error: {e}"

    async def chronicling_america(
        self,
        query: str,
        year: str = "",
        state: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Full-text search historic US newspapers (1777–1963) via Chronicling America.
        :param query: Keywords or phrase
        :param year: Optional 4-digit year
        :param state: Optional state name (e.g. "New York")
        :return: Newspaper page matches with title, date, and image link
        """
        params = {"andtext": query, "format": "json", "rows": self.valves.MAX_RESULTS}
        if year:
            params["dateFilterType"] = "yearRange"
            params["date1"] = year
            params["date2"] = year
        if state:
            params["state"] = state
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(f"{CHRON}/search/pages/results/", params=params)
                r.raise_for_status()
                data = r.json()
            items = data.get("items", [])
            if not items:
                return f"No newspaper matches for: {query}"
            lines = [f"## Chronicling America: {query}\n"]
            for it in items:
                t = it.get("title", "")
                d = it.get("date", "")
                city = it.get("city", "")
                st = it.get("state", [""])[0] if isinstance(it.get("state"), list) else it.get("state", "")
                pg = it.get("page", "")
                url = it.get("url", "").replace(".json", "")
                lines.append(f"**{t}** — {d}, {city}, {st}, page {pg}")
                lines.append(f"   🔗 {url}\n")
            return "\n".join(lines)
        except Exception as e:
            return f"Chronicling America error: {e}"
