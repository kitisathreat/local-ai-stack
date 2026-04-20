"""Auto web-search via SearXNG.

Ported from pipelines/web_search_rag.py. When a user's latest message
contains a "current info" trigger (date words, price/news keywords,
etc.), we search SearXNG and append the top-K results to the user
message before sending to the model.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Iterable

import httpx

from .. import airgap
from ..schemas import ChatMessage


logger = logging.getLogger(__name__)


SEARXNG_URL = os.getenv("SEARXNG_URL", "http://searxng:8080")
MAX_RESULTS = int(os.getenv("SEARXNG_MAX_RESULTS", "3"))
TIMEOUT = int(os.getenv("SEARXNG_TIMEOUT", "8"))


TRIGGER_PATTERNS = [
    r"\b(today|tonight|yesterday|this week|this month|this year|current(ly)?|now|latest|recent(ly)?|new(est)?)\b",
    r"\b(2024|2025|2026)\b",
    r"\b(news|weather|stock|price|score|result|winner|election|update)\b",
    r"\bwhat('s| is) (happening|going on)\b",
    r"\bwho (won|is winning|leads)\b",
    r"\bhow much (does|is|are|do)\b",
]


def needs_search(message: str, always: bool = False) -> bool:
    if always:
        return True
    msg = message.lower()
    return any(re.search(p, msg) for p in TRIGGER_PATTERNS)


async def search(query: str) -> list[dict]:
    params = {
        "q": query,
        "format": "json",
        "engines": "google,bing,duckduckgo",
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.get(f"{SEARXNG_URL.rstrip('/')}/search", params=params)
            resp.raise_for_status()
            return (resp.json() or {}).get("results", [])[:MAX_RESULTS]
    except httpx.HTTPError as e:
        logger.warning("SearXNG query failed: %s", e)
        return []


def format_results(results: list[dict]) -> str:
    if not results:
        return ""
    lines = ["[Web Search Results:"]
    for r in results:
        title = r.get("title") or ""
        snippet = r.get("content") or ""
        url = r.get("url") or ""
        lines.append(f"- {title}: {snippet} ({url})")
    lines.append("]")
    return "\n".join(lines)


async def inject_web_results(
    messages: list[ChatMessage],
    *,
    always: bool = False,
) -> list[ChatMessage]:
    """Mutate-and-return. Appends formatted search results to the last
    user message if trigger patterns match.

    Short-circuits to a no-op when airgap mode is on — the whole point
    of airgap is that we never reach out to SearXNG (or anywhere else
    off the box). Logged at INFO so operators can see the gate firing."""
    if airgap.is_enabled():
        logger.info("Airgap mode ON — skipping SearXNG injection")
        return messages
    last_user_idx: int | None = None
    last_user_text = ""
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].role == "user":
            c = messages[i].content
            if isinstance(c, str):
                last_user_text = c
                last_user_idx = i
            break
    if last_user_idx is None or not last_user_text:
        return messages
    if not needs_search(last_user_text, always=always):
        return messages

    results = await search(last_user_text)
    block = format_results(results)
    if block:
        msg = messages[last_user_idx]
        if isinstance(msg.content, str):
            msg.content = f"{msg.content}\n\n{block}"
    return messages
