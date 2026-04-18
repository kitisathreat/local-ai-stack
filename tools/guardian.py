"""
title: The Guardian — News & Archive Search
author: local-ai-stack
description: Search The Guardian's 2M+ article archive back to 1999. Politics, world, business, culture, sport, opinion. Free API key (developer tier allows 12 reqs/sec, 5k/day).
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import os
import httpx
from pydantic import BaseModel, Field
from typing import Optional


BASE = "https://content.guardianapis.com"


class Tools:
    class Valves(BaseModel):
        API_KEY: str = Field(default_factory=lambda: os.environ.get("GUARDIAN_API_KEY", ""), description="Guardian Open Platform key (free at https://open-platform.theguardian.com/access/)")
        PAGE_SIZE: int = Field(default=10, description="Results per query")

    def __init__(self):
        self.valves = self.Valves()

    async def search(
        self,
        query: str,
        section: str = "",
        from_date: str = "",
        order: str = "newest",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search Guardian articles.
        :param query: Keywords (e.g. "climate change")
        :param section: Optional section (politics, world, business, technology, culture, sport, environment, commentisfree)
        :param from_date: Optional YYYY-MM-DD lower bound
        :param order: "newest", "oldest", or "relevance"
        :return: Article headlines, section, date, and URLs
        """
        if not self.valves.API_KEY:
            return "Set GUARDIAN API_KEY valve (free at https://open-platform.theguardian.com/access/)."
        params = {
            "q": query, "api-key": self.valves.API_KEY,
            "page-size": self.valves.PAGE_SIZE, "order-by": order,
            "show-fields": "trailText,byline",
        }
        if section: params["section"] = section
        if from_date: params["from-date"] = from_date
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(f"{BASE}/search", params=params)
                r.raise_for_status()
                data = r.json().get("response", {})
            results = data.get("results", [])
            total = data.get("total", 0)
            if not results:
                return f"No Guardian articles for: {query}"
            lines = [f"## Guardian: {query} ({total:,} matches)\n"]
            for r_ in results:
                title = r_.get("webTitle", "")
                sec = r_.get("sectionName", "")
                date = r_.get("webPublicationDate", "")[:10]
                fields = r_.get("fields", {}) or {}
                byline = fields.get("byline", "")
                trail = (fields.get("trailText", "") or "").replace("<p>", "").replace("</p>", "").strip()
                url = r_.get("webUrl", "")
                lines.append(f"**{title}** — {sec} ({date})")
                if byline:
                    lines.append(f"   by {byline}")
                if trail:
                    lines.append(f"   {trail[:250]}")
                lines.append(f"   🔗 {url}\n")
            return "\n".join(lines)
        except Exception as e:
            return f"Guardian error: {e}"
