"""
title: Wikipedia Lookup
author: local-ai-stack
description: Search Wikipedia and retrieve article summaries or full content. Gives models access to encyclopedic knowledge.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from pydantic import BaseModel, Field
from typing import Callable, Any, Optional


WIKI_API = "https://en.wikipedia.org/api/rest_v1"
WIKI_SEARCH = "https://en.wikipedia.org/w/api.php"


class Tools:
    class Valves(BaseModel):
        SUMMARY_SENTENCES: int = Field(
            default=5, description="Number of sentences in summary (0 = full intro)"
        )
        LANGUAGE: str = Field(
            default="en", description="Wikipedia language code (en, es, fr, de, ja...)"
        )

    def __init__(self):
        self.valves = self.Valves()

    async def wikipedia_search(
        self,
        query: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search Wikipedia and return a summary of the best matching article.
        :param query: The topic or article title to search for
        :return: Article summary with key facts and a link to the full article
        """
        if __event_emitter__:
            await __event_emitter__(
                {"type": "status", "data": {"description": f"Searching Wikipedia: {query}", "done": False}}
            )

        try:
            lang = self.valves.LANGUAGE
            base = f"https://{lang}.wikipedia.org/api/rest_v1"

            # First try direct page summary
            async with httpx.AsyncClient(timeout=10) as client:
                title_slug = query.strip().replace(" ", "_")
                resp = await client.get(
                    f"{base}/page/summary/{title_slug}",
                    headers={"User-Agent": "local-ai-stack/1.0"},
                )

                if resp.status_code == 404:
                    # Fall back to search
                    search_params = {
                        "action": "query", "list": "search",
                        "srsearch": query, "srlimit": 1,
                        "format": "json",
                    }
                    s_resp = await client.get(
                        f"https://{lang}.wikipedia.org/w/api.php",
                        params=search_params,
                        headers={"User-Agent": "local-ai-stack/1.0"},
                    )
                    results = s_resp.json().get("query", {}).get("search", [])
                    if not results:
                        return f"No Wikipedia article found for: {query}"

                    top_title = results[0]["title"].replace(" ", "_")
                    resp = await client.get(
                        f"{base}/page/summary/{top_title}",
                        headers={"User-Agent": "local-ai-stack/1.0"},
                    )

                if resp.status_code != 200:
                    return f"Wikipedia error: HTTP {resp.status_code}"

                data = resp.json()

            title = data.get("title", query)
            extract = data.get("extract", "No content available.")
            url = data.get("content_urls", {}).get("desktop", {}).get("page", "")
            thumbnail = data.get("thumbnail", {}).get("source", "")

            # Trim to requested sentences
            if self.valves.SUMMARY_SENTENCES > 0:
                sentences = extract.split(". ")
                extract = ". ".join(sentences[:self.valves.SUMMARY_SENTENCES])
                if not extract.endswith("."):
                    extract += "."

            if __event_emitter__:
                await __event_emitter__(
                    {"type": "status", "data": {"description": "Wikipedia article retrieved", "done": True}}
                )

            result = f"## Wikipedia: {title}\n\n{extract}\n"
            if url:
                result += f"\n**Source:** {url}"
            return result

        except httpx.ConnectError:
            return "Error: Cannot reach Wikipedia. Check internet connection."
        except Exception as e:
            return f"Wikipedia error: {str(e)}"

    async def wikipedia_disambiguation(
        self,
        topic: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Find multiple Wikipedia articles related to an ambiguous term.
        :param topic: The ambiguous term to look up (e.g. "Python", "Mercury", "Apple")
        :return: List of related Wikipedia articles for the topic
        """
        try:
            lang = self.valves.LANGUAGE
            params = {
                "action": "query", "list": "search",
                "srsearch": topic, "srlimit": 8,
                "format": "json",
            }
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"https://{lang}.wikipedia.org/w/api.php",
                    params=params,
                    headers={"User-Agent": "local-ai-stack/1.0"},
                )
                data = resp.json()

            results = data.get("query", {}).get("search", [])
            if not results:
                return f"No Wikipedia results for: {topic}"

            lines = [f"## Wikipedia: Disambiguation for '{topic}'\n"]
            for r in results:
                title = r["title"]
                snippet = r.get("snippet", "").replace("<span class=\"searchmatch\">", "").replace("</span>", "")
                url = f"https://{lang}.wikipedia.org/wiki/{title.replace(' ', '_')}"
                lines.append(f"**{title}**")
                lines.append(f"   {snippet}...")
                lines.append(f"   {url}\n")

            return "\n".join(lines)

        except Exception as e:
            return f"Wikipedia search error: {str(e)}"
