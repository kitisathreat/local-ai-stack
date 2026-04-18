"""
title: Hacker News
author: local-ai-stack
description: Browse Hacker News top stories, new posts, Ask HN, and Show HN. Search past discussions via Algolia HN API. No API key required.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from pydantic import BaseModel, Field
from typing import Callable, Any, Optional


HN_API     = "https://hacker-news.firebaseio.com/v0"
HN_ALGOLIA = "https://hn.algolia.com/api/v1"


class Tools:
    class Valves(BaseModel):
        MAX_STORIES: int = Field(default=10, description="Maximum stories to fetch")

    def __init__(self):
        self.valves = self.Valves()

    async def get_top_stories(
        self,
        category: str = "top",
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get the latest stories from Hacker News.
        :param category: Story category: 'top', 'new', 'best', 'ask', 'show', 'job'
        :return: Story titles, scores, comment counts, and links
        """
        endpoint_map = {
            "top": "topstories", "new": "newstories", "best": "beststories",
            "ask": "askstories", "show": "showstories", "job": "jobstories",
        }
        endpoint = endpoint_map.get(category.lower(), "topstories")

        if __event_emitter__:
            await __event_emitter__(
                {"type": "status", "data": {"description": f"Fetching HN {category} stories", "done": False}}
            )

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                ids_resp = await client.get(f"{HN_API}/{endpoint}.json")
                ids_resp.raise_for_status()
                ids = ids_resp.json()[:self.valves.MAX_STORIES]

                stories = []
                for item_id in ids:
                    r = await client.get(f"{HN_API}/item/{item_id}.json")
                    if r.status_code == 200:
                        item = r.json()
                        if item and item.get("type") in ("story", "job", "ask"):
                            stories.append(item)

            if not stories:
                return f"No stories found for category: {category}"

            if __event_emitter__:
                await __event_emitter__(
                    {"type": "status", "data": {"description": f"Got {len(stories)} stories", "done": True}}
                )

            label = {"top": "Top", "new": "New", "best": "Best", "ask": "Ask HN", "show": "Show HN", "job": "Jobs"}.get(category.lower(), category.title())
            lines = [f"## Hacker News — {label} Stories\n"]
            for s in stories:
                title = s.get("title", "No title")
                url = s.get("url", f"https://news.ycombinator.com/item?id={s.get('id')}")
                score = s.get("score", 0)
                comments = s.get("descendants", 0)
                author = s.get("by", "")
                time_ago = s.get("time", 0)

                lines.append(f"**{title}**")
                lines.append(f"   ▲ {score} pts | 💬 {comments} comments | by {author}")
                lines.append(f"   🔗 {url}")
                hn_url = f"https://news.ycombinator.com/item?id={s.get('id')}"
                if url != hn_url:
                    lines.append(f"   💬 Discussion: {hn_url}")
                lines.append("")

            return "\n".join(lines)

        except Exception as e:
            return f"Hacker News error: {str(e)}"

    async def search_hn(
        self,
        query: str,
        search_type: str = "story",
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search Hacker News discussions via the Algolia search API.
        :param query: Search terms (e.g. "local LLM inference", "Rust vs Go", "startup idea")
        :param search_type: What to search: 'story', 'comment', or 'all'
        :return: Matching posts or comments with links and scores
        """
        tag_map = {"story": "story", "comment": "comment", "all": ""}
        tag = tag_map.get(search_type.lower(), "story")

        params = {"query": query, "hitsPerPage": self.valves.MAX_STORIES}
        if tag:
            params["tags"] = tag

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{HN_ALGOLIA}/search",
                    params=params,
                )
                resp.raise_for_status()
                hits = resp.json().get("hits", [])

            if not hits:
                return f"No HN results for: {query}"

            lines = [f"## Hacker News Search: {query}\n"]
            for h in hits:
                title = h.get("title") or h.get("story_title") or h.get("comment_text", "")[:80] or "No title"
                url = h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}"
                score = h.get("points", 0)
                comments = h.get("num_comments", 0)
                author = h.get("author", "")
                hn_url = f"https://news.ycombinator.com/item?id={h.get('objectID')}"

                lines.append(f"**{title}**")
                if score or comments:
                    lines.append(f"   ▲ {score} | 💬 {comments} | {author}")
                lines.append(f"   🔗 {url}")
                if url != hn_url:
                    lines.append(f"   Discussion: {hn_url}")
                lines.append("")

            return "\n".join(lines)

        except Exception as e:
            return f"HN search error: {str(e)}"
