"""
title: Web Search (SearXNG)
author: local-ai-stack
description: Search the web in real time using a local SearXNG instance. Gives models access to current news, events, and live information.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from pydantic import BaseModel, Field
from typing import Callable, Any, Optional


class Tools:
    class Valves(BaseModel):
        SEARXNG_URL: str = Field(
            default="http://searxng:8080",
            description="Base URL of the SearXNG instance",
        )
        MAX_RESULTS: int = Field(
            default=5, description="Maximum number of results to return"
        )
        ENGINES: str = Field(
            default="google,bing,duckduckgo",
            description="Comma-separated list of search engines to use",
        )
        TIMEOUT: int = Field(default=10, description="Request timeout in seconds")

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
        Use this when you need up-to-date data, recent news, or information beyond your training cutoff.
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

        try:
            params = {
                "q": query,
                "format": "json",
                "engines": self.valves.ENGINES,
                "language": "en",
            }
            async with httpx.AsyncClient(timeout=self.valves.TIMEOUT) as client:
                resp = await client.get(
                    f"{self.valves.SEARXNG_URL}/search", params=params
                )
                resp.raise_for_status()
                data = resp.json()

            results = data.get("results", [])[: self.valves.MAX_RESULTS]
            if not results:
                return f"No results found for: {query}"

            lines = [f"## Web Search Results: {query}\n"]
            for i, r in enumerate(results, 1):
                title = r.get("title", "No title")
                url = r.get("url", "")
                snippet = r.get("content", "No description available")
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

        except httpx.ConnectError:
            return (
                "Error: SearXNG is not reachable. Ensure the searxng container is running."
            )
        except Exception as e:
            return f"Search error: {str(e)}"
