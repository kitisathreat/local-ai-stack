"""
title: The New York Times — Archive, Top Stories & Books
author: local-ai-stack
description: Search NYT Article Archive (1851+), get Top Stories by section, and pull NYT Bestseller lists. Free API key (per-endpoint apps at developer.nytimes.com).
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from pydantic import BaseModel, Field
from typing import Optional


BASE = "https://api.nytimes.com/svc"


class Tools:
    class Valves(BaseModel):
        API_KEY: str = Field(default="", description="NYT API key (free at https://developer.nytimes.com)")

    def __init__(self):
        self.valves = self.Valves()

    async def search(
        self,
        query: str,
        begin_date: str = "",
        end_date: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search NYT Article Search (1851+).
        :param query: Keywords
        :param begin_date: Optional YYYYMMDD
        :param end_date: Optional YYYYMMDD
        :return: Articles with headline, byline, lead paragraph, and URL
        """
        if not self.valves.API_KEY:
            return "Set NYT API_KEY valve (free at https://developer.nytimes.com)."
        params = {"q": query, "api-key": self.valves.API_KEY}
        if begin_date: params["begin_date"] = begin_date
        if end_date: params["end_date"] = end_date
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(f"{BASE}/search/v2/articlesearch.json", params=params)
                r.raise_for_status()
                data = r.json().get("response", {})
            docs = data.get("docs", [])
            hits = data.get("meta", {}).get("hits", 0)
            if not docs:
                return f"No NYT articles for: {query}"
            lines = [f"## NYT: {query} ({hits:,} matches)\n"]
            for d in docs[:10]:
                headline = (d.get("headline") or {}).get("main", "")
                byline = (d.get("byline") or {}).get("original", "")
                date = d.get("pub_date", "")[:10]
                lead = d.get("lead_paragraph", "")
                url = d.get("web_url", "")
                lines.append(f"**{headline}** — {date}")
                if byline:
                    lines.append(f"   {byline}")
                if lead:
                    lines.append(f"   {lead[:300]}")
                lines.append(f"   🔗 {url}\n")
            return "\n".join(lines)
        except Exception as e:
            return f"NYT error: {e}"

    async def top_stories(
        self,
        section: str = "home",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get NYT Top Stories for a section.
        :param section: e.g. "home", "world", "politics", "business", "technology", "science", "arts", "sports"
        :return: Headlines with summaries and URLs
        """
        if not self.valves.API_KEY:
            return "Set NYT API_KEY valve."
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(
                    f"{BASE}/topstories/v2/{section}.json",
                    params={"api-key": self.valves.API_KEY},
                )
                r.raise_for_status()
                data = r.json()
            items = data.get("results", [])
            if not items:
                return f"No NYT top stories for: {section}"
            lines = [f"## NYT Top Stories — {section.title()}\n"]
            for i in items[:15]:
                t = i.get("title", "")
                ab = i.get("abstract", "")
                byline = i.get("byline", "")
                url = i.get("url", "")
                lines.append(f"**{t}**\n   {byline}\n   {ab}\n   🔗 {url}\n")
            return "\n".join(lines)
        except Exception as e:
            return f"NYT error: {e}"

    async def bestsellers(
        self,
        list_name: str = "hardcover-fiction",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Fetch current NYT Bestseller list.
        :param list_name: e.g. "hardcover-fiction", "combined-print-and-e-book-nonfiction", "young-adult-hardcover"
        :return: Ranked list of books
        """
        if not self.valves.API_KEY:
            return "Set NYT API_KEY valve."
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(
                    f"{BASE}/books/v3/lists/current/{list_name}.json",
                    params={"api-key": self.valves.API_KEY},
                )
                r.raise_for_status()
                data = r.json().get("results", {})
            books = data.get("books", [])
            if not books:
                return f"No bestseller list: {list_name}"
            lines = [f"## NYT {data.get('display_name', list_name)} — {data.get('published_date', '')}\n"]
            for b in books:
                rank = b.get("rank", 0)
                title = b.get("title", "")
                author = b.get("author", "")
                desc = b.get("description", "")
                lines.append(f"**{rank}. {title}** — _{author}_")
                if desc:
                    lines.append(f"   {desc[:220]}")
                lines.append("")
            return "\n".join(lines)
        except Exception as e:
            return f"NYT error: {e}"
