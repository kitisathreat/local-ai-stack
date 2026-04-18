"""
title: Stack Exchange — Stack Overflow, Math, Server Fault & More
author: local-ai-stack
description: Search 170+ Stack Exchange Q&A sites including Stack Overflow, Math, Server Fault, Super User, Ask Ubuntu, Cross Validated (stats), TeX, and Unix. Find answers to technical questions. No API key required (public tier).
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import os
import httpx
import html as html_mod
from pydantic import BaseModel, Field
from typing import Optional


BASE = "https://api.stackexchange.com/2.3"


class Tools:
    class Valves(BaseModel):
        SITE: str = Field(
            default="stackoverflow",
            description="Default Stack Exchange site (stackoverflow, math, serverfault, superuser, askubuntu, stats, tex, unix, apple, gaming, cooking, ...)",
        )
        MAX_RESULTS: int = Field(default=5, description="Max results per query")
        API_KEY: str = Field(default_factory=lambda: os.environ.get("STACKEXCHANGE_API_KEY", ""), description="Optional app key for higher rate limits")

    def __init__(self):
        self.valves = self.Valves()

    def _params(self, extra: dict) -> dict:
        p = {"site": self.valves.SITE, **extra}
        if self.valves.API_KEY:
            p["key"] = self.valves.API_KEY
        return p

    async def search(
        self,
        query: str,
        site: str = "",
        tagged: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search Stack Exchange for questions matching a keyword/tag.
        :param query: Free-text query
        :param site: Optional site override (e.g. "math", "serverfault", "askubuntu")
        :param tagged: Optional semicolon-separated tags (e.g. "python;pandas")
        :return: Top matching questions with title, score, answers, and link
        """
        if site:
            self.valves.SITE = site
        params = self._params({
            "order": "desc", "sort": "relevance", "q": query,
            "pagesize": self.valves.MAX_RESULTS, "filter": "default",
        })
        if tagged:
            params["tagged"] = tagged
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(f"{BASE}/search/advanced", params=params)
                r.raise_for_status()
                items = r.json().get("items", [])
            if not items:
                return f"No Stack Exchange results for: {query}"
            lines = [f"## Stack Exchange [{self.valves.SITE}]: {query}\n"]
            for it in items:
                title = html_mod.unescape(it.get("title", ""))
                score = it.get("score", 0)
                ans = it.get("answer_count", 0)
                answered = "✅" if it.get("is_answered") else "❓"
                tags = ", ".join(it.get("tags", [])[:4])
                link = it.get("link", "")
                lines.append(f"{answered} **{title}**")
                lines.append(f"   score {score} | {ans} answers | tags: {tags}")
                lines.append(f"   {link}\n")
            return "\n".join(lines)
        except Exception as e:
            return f"Stack Exchange error: {e}"

    async def top_answer(
        self,
        question_id: int,
        site: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Fetch the highest-voted answer body for a given question ID.
        :param question_id: Stack Exchange question ID (integer in the URL)
        :param site: Optional site override
        :return: Accepted or top-voted answer body
        """
        if site:
            self.valves.SITE = site
        params = self._params({
            "order": "desc", "sort": "votes", "pagesize": 1,
            "filter": "withbody",
        })
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(f"{BASE}/questions/{question_id}/answers", params=params)
                r.raise_for_status()
                items = r.json().get("items", [])
            if not items:
                return f"No answers for question {question_id}"
            a = items[0]
            body = html_mod.unescape(a.get("body", ""))
            body = body.replace("<p>", "").replace("</p>", "\n\n").replace("<pre><code>", "\n```\n").replace("</code></pre>", "\n```\n").replace("<code>", "`").replace("</code>", "`")
            return (
                f"## Answer (score {a.get('score', 0)})\n"
                f"{'**Accepted**' if a.get('is_accepted') else ''}\n\n"
                f"{body[:3000]}\n\n"
                f"🔗 https://{self.valves.SITE}.com/a/{a.get('answer_id', '')}"
            )
        except Exception as e:
            return f"Stack Exchange error: {e}"
