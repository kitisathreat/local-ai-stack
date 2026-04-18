"""
title: Google Books — Book Search & Metadata
author: local-ai-stack
description: Search 40M+ titles in Google Books. Get description, authors, publisher, page count, categories, preview/info links, ISBNs, and thumbnails. Works without an API key (generous public limit); add a key for higher quotas.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import os
import httpx
from pydantic import BaseModel, Field
from typing import Optional


BASE = "https://www.googleapis.com/books/v1"


class Tools:
    class Valves(BaseModel):
        API_KEY: str = Field(default_factory=lambda: os.environ.get("GOOGLE_BOOKS_API_KEY", ""), description="Optional Google Books API key (higher quota)")
        MAX_RESULTS: int = Field(default=8, description="Max results")

    def __init__(self):
        self.valves = self.Valves()

    async def search(
        self,
        query: str,
        author: str = "",
        subject: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search Google Books by keyword, with optional author/subject qualifiers.
        :param query: Free-text query, title, or keyword
        :param author: Optional author filter
        :param subject: Optional subject filter
        :return: Books with title, authors, year, rating, and preview link
        """
        q = query
        if author: q += f" inauthor:{author}"
        if subject: q += f" subject:{subject}"
        params = {"q": q, "maxResults": self.valves.MAX_RESULTS}
        if self.valves.API_KEY:
            params["key"] = self.valves.API_KEY
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(f"{BASE}/volumes", params=params)
                r.raise_for_status()
                data = r.json()
            items = data.get("items", [])
            total = data.get("totalItems", 0)
            if not items:
                return f"No Google Books results for: {query}"
            lines = [f"## Google Books: {query} ({total:,} results)\n"]
            for it in items:
                v = it.get("volumeInfo", {})
                title = v.get("title", "")
                subtitle = v.get("subtitle", "")
                authors = ", ".join(v.get("authors", []))
                pub = v.get("publisher", "")
                date = v.get("publishedDate", "")
                pages = v.get("pageCount", "")
                rating = v.get("averageRating")
                desc = (v.get("description") or "")[:220]
                info = v.get("infoLink", "")
                preview = v.get("previewLink", "")
                thumb = (v.get("imageLinks") or {}).get("thumbnail", "")
                isbns = ", ".join(i.get("identifier", "") for i in v.get("industryIdentifiers", [])[:2])
                lines.append(f"**{title}" + (f": {subtitle}" if subtitle else "") + "**")
                if authors:
                    lines.append(f"   by {authors}")
                lines.append(f"   {pub} ({date}) — {pages} pages" + (f" — ⭐ {rating}" if rating else ""))
                if isbns:
                    lines.append(f"   ISBN: {isbns}")
                if desc:
                    lines.append(f"   {desc}...")
                if thumb:
                    lines.append(f"   ![thumb]({thumb})")
                lines.append(f"   🔗 {info or preview}\n")
            return "\n".join(lines)
        except Exception as e:
            return f"Google Books error: {e}"
