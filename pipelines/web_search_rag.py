"""
title: Web Search RAG Pipeline
author: local-ai-stack
description: Automatically detects when the model needs current information and injects web search results from SearXNG before generating a response.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
import re
from typing import Optional, Callable, Any, Generator, Iterator, Union, List
from pydantic import BaseModel, Field


TRIGGER_PATTERNS = [
    r"\b(today|tonight|yesterday|this week|this month|this year|current(ly)?|now|latest|recent(ly)?|new(est)?)\b",
    r"\b(2024|2025|2026)\b",
    r"\b(news|weather|stock|price|score|result|winner|election|update)\b",
    r"\bwhat('s| is) (happening|going on)\b",
    r"\bwho (won|is winning|leads)\b",
    r"\bhow much (does|is|are|do)\b",
]


class Pipeline:
    class Valves(BaseModel):
        SEARXNG_URL: str = Field(
            default="http://searxng:8080",
            description="SearXNG base URL",
        )
        MAX_RESULTS: int = Field(
            default=3,
            description="Web results to inject",
        )
        AUTO_TRIGGER: bool = Field(
            default=True,
            description="Auto-detect queries that need web search",
        )
        ALWAYS_SEARCH: bool = Field(
            default=False,
            description="Search the web on every message (overrides AUTO_TRIGGER)",
        )
        TIMEOUT: int = Field(default=8, description="Search timeout in seconds")

    def __init__(self):
        self.name = "Web Search RAG"
        self.valves = self.Valves()

    def _needs_search(self, message: str) -> bool:
        if self.valves.ALWAYS_SEARCH:
            return True
        if not self.valves.AUTO_TRIGGER:
            return False
        message_lower = message.lower()
        return any(re.search(p, message_lower) for p in TRIGGER_PATTERNS)

    async def _search(self, query: str) -> str:
        try:
            params = {
                "q": query,
                "format": "json",
                "engines": "google,bing,duckduckgo",
            }
            async with httpx.AsyncClient(timeout=self.valves.TIMEOUT) as client:
                resp = await client.get(f"{self.valves.SEARXNG_URL}/search", params=params)
                resp.raise_for_status()
                data = resp.json()

            results = data.get("results", [])[: self.valves.MAX_RESULTS]
            if not results:
                return ""

            lines = ["[Web Search Results:"]
            for r in results:
                title = r.get("title", "")
                snippet = r.get("content", "")
                url = r.get("url", "")
                lines.append(f"- {title}: {snippet} ({url})")
            lines.append("]")
            return "\n".join(lines)
        except Exception:
            return ""

    async def on_startup(self):
        pass

    async def on_shutdown(self):
        pass

    async def pipe(
        self,
        user_message: str,
        model_id: str,
        messages: List[dict],
        body: dict,
        __event_emitter__: Callable[[dict], Any] = None,
    ) -> Union[str, Generator, Iterator]:
        if not self._needs_search(user_message):
            return body

        if __event_emitter__:
            await __event_emitter__(
                {"type": "status", "data": {"description": "Searching web for context...", "done": False}}
            )

        search_results = await self._search(user_message)

        if search_results and messages:
            last_user_idx = None
            for i in range(len(messages) - 1, -1, -1):
                if messages[i].get("role") == "user":
                    last_user_idx = i
                    break

            if last_user_idx is not None:
                messages[last_user_idx]["content"] = (
                    f"{messages[last_user_idx]['content']}\n\n{search_results}"
                )
                body["messages"] = messages

        if __event_emitter__:
            await __event_emitter__(
                {"type": "status", "data": {"description": "Web context injected", "done": True}}
            )

        return body
