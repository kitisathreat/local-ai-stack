"""
title: Web Search
author: local-ai-stack
description: Search the web in real time via the configured provider (Brave API or DuckDuckGo). Gives models access to current news, events, and live information.
required_open_webui_version: 0.4.0
requirements: httpx, ddgs
version: 2.0.0
licence: MIT
"""

from typing import Any, Callable, Optional

from pydantic import BaseModel, Field


class Tools:
    class Valves(BaseModel):
        MAX_RESULTS: int = Field(
            default=5, description="Maximum number of results to return"
        )

    def __init__(self):
        self.valves = self.Valves()

    async def web_search(
        self,
        query: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search the web for current information on any topic.
        Use this when you need up-to-date data, recent news, or information
        beyond your training cutoff.

        Native mode uses an in-process provider (Brave API when
        BRAVE_API_KEY is set, otherwise the DuckDuckGo ``ddgs`` package).
        There is no SearXNG dependency.

        :param query: The search query string
        :return: Formatted search results with titles, URLs, and snippets
        """
        if __event_emitter__:
            await __event_emitter__(
                {
                    "type": "status",
                    "data": {"description": f"Searching: {query}", "done": False},
                }
            )

        from backend.middleware.web_search import get_provider

        try:
            results = await get_provider().search(query, self.valves.MAX_RESULTS)
        except Exception as exc:
            return f"Web search failed: {exc}"

        if not results:
            return f"No results found for: {query}"

        lines = [f"## Web Search Results: {query}\n"]
        for i, r in enumerate(results, 1):
            title = r.get("title") or "No title"
            url = r.get("url") or ""
            snippet = r.get("content") or "No description available"
            lines.append(f"**{i}. {title}**")
            lines.append(f"   {url}")
            lines.append(f"   {snippet}\n")

        if __event_emitter__:
            await __event_emitter__(
                {
                    "type": "status",
                    "data": {
                        "description": f"Found {len(results)} results",
                        "done": True,
                    },
                }
            )
        return "\n".join(lines)
