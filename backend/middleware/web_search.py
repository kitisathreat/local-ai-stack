"""Auto web-search — in-process providers (no SearXNG dependency).

Native mode replaces the SearXNG container with two pure-Python
providers selected via ``WEB_SEARCH_PROVIDER``:

    brave → Brave Search API (requires ``BRAVE_API_KEY``)
    ddg   → DuckDuckGo via the ``ddgs`` package (no key)
    none  → web-search disabled

When the user's latest message contains a "current info" trigger
(date words, price/news keywords, etc.), we search via the selected
provider and append the top-K results to the user message before
sending to the model.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Protocol

import httpx

from .. import airgap
from ..schemas import ChatMessage


logger = logging.getLogger(__name__)


MAX_RESULTS = int(os.getenv("WEB_SEARCH_MAX_RESULTS", "3"))
TIMEOUT = int(os.getenv("WEB_SEARCH_TIMEOUT", "8"))


TRIGGER_PATTERNS = [
    r"\b(today|tonight|yesterday|this week|this month|this year|current(ly)?|now|latest|recent(ly)?|new(est)?)\b",
    r"\b(2024|2025|2026)\b",
    r"\b(news|weather|stock|price|score|result|winner|election|update)\b",
    r"\bwhat('s| is) (happening|going on)\b",
    r"\bwho (won|is winning|leads)\b",
    r"\bhow much (does|is|are|do)\b",
]

# Knowledge-recall patterns — questions where the model is asked to look
# up a specific named fact, regardless of recency. These are the cases
# where web-search augmentation can actually help even when no date
# keyword is present (factual MMLU-style questions, lookup-shaped
# wh-queries about named entities, year-anchored events).
LOOKUP_PATTERNS = [
    # MMLU/quiz-style "Choices:" block followed by A./B./C./D. lines
    r"choices?\s*:\s*\n[\s\S]*?\b[A-D]\.",
    # Wh-questions about specific named entities (capitalized noun)
    r"\bwh(at|o|ere|en|ich)\s+(is|are|was|were|did|do|does|caused|invented|wrote|founded|composed)\b",
    # Imperative lookup directives
    r"\b(find|list|define|name|identify|describe|explain)\s+(the|a|an|all|each|every)\b",
    # Year-anchored historical references (1900–2099)
    r"\b(19|20)\d{2}\b",
    # Specific entity types that almost always benefit from a lookup
    r"\b(capital|population|founder|author|director|president|inventor|composer|equation|formula|theorem|definition)\s+of\b",
]


def needs_search(message: str, always: bool = False) -> bool:
    """Decide whether to inject web-search results into ``message``.

    Order of precedence:
      1. ``always=True`` argument (request-level override).
      2. ``LAI_FORCE_WEB_SEARCH=1`` env (operator override — used by the
         knowledge+tools bench so every problem gets augmented even when
         the prompt doesn't trip the heuristics).
      3. ``TRIGGER_PATTERNS`` — recency / news / current-state words.
      4. ``LOOKUP_PATTERNS`` — factual-lookup questions about specific
         named entities or quiz-style multiple choice.

    Returns False when the message is empty / too short / clearly not a
    lookup-shaped query.
    """
    if always or os.getenv("LAI_FORCE_WEB_SEARCH") == "1":
        return True
    msg = message.lower()
    if any(re.search(p, msg) for p in TRIGGER_PATTERNS):
        return True
    return any(re.search(p, msg, re.MULTILINE) for p in LOOKUP_PATTERNS)


# ── Provider protocol ─────────────────────────────────────────────────────

class WebSearchProvider(Protocol):
    name: str

    async def search(self, query: str, max_results: int) -> list[dict]:
        ...


class BraveSearchProvider:
    """Brave Search API — https://api.search.brave.com/res/v1/web/search

    Free tier is generous (thousands of queries per month). Requires
    ``BRAVE_API_KEY`` in the environment.
    """

    name = "brave"

    def __init__(self, api_key: str, timeout: int = TIMEOUT):
        self._api_key = api_key
        self._timeout = timeout

    async def search(self, query: str, max_results: int) -> list[dict]:
        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": self._api_key,
        }
        params = {"q": query, "count": max_results}
        url = "https://api.search.brave.com/res/v1/web/search"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(url, headers=headers, params=params)
            resp.raise_for_status()
            payload = resp.json() or {}
        results = (payload.get("web") or {}).get("results") or []
        return [
            {
                "title": r.get("title") or "",
                "content": r.get("description") or r.get("snippet") or "",
                "url": r.get("url") or "",
            }
            for r in results[:max_results]
        ]


class DuckDuckGoProvider:
    """DuckDuckGo via the ``ddgs`` package. No API key required.

    ``ddgs.DDGS().text()`` is synchronous; we run it in a thread so
    it doesn't block the event loop.
    """

    name = "ddg"

    def __init__(self, timeout: int = TIMEOUT):
        self._timeout = timeout

    async def search(self, query: str, max_results: int) -> list[dict]:
        def _run() -> list[dict]:
            from ddgs import DDGS
            with DDGS(timeout=self._timeout) as ddg:
                return list(ddg.text(query, max_results=max_results))
        raw = await asyncio.to_thread(_run)
        return [
            {
                "title": r.get("title") or "",
                "content": r.get("body") or r.get("snippet") or "",
                "url": r.get("href") or r.get("url") or "",
            }
            for r in raw[:max_results]
        ]


class NoneProvider:
    name = "none"

    async def search(self, query: str, max_results: int) -> list[dict]:
        return []


def _select_provider() -> WebSearchProvider:
    explicit = (os.getenv("WEB_SEARCH_PROVIDER") or "").strip().lower()
    brave_key = os.getenv("BRAVE_API_KEY") or ""
    if explicit == "none":
        return NoneProvider()
    if explicit == "brave" or (not explicit and brave_key):
        if not brave_key:
            logger.warning("WEB_SEARCH_PROVIDER=brave but BRAVE_API_KEY is empty — disabling web search")
            return NoneProvider()
        return BraveSearchProvider(brave_key)
    if explicit == "ddg" or not explicit:
        try:
            import ddgs  # noqa: F401
        except ImportError:
            logger.warning("DuckDuckGo provider requested but 'ddgs' package not installed — disabling web search")
            return NoneProvider()
        return DuckDuckGoProvider()
    logger.warning("Unknown WEB_SEARCH_PROVIDER=%r — disabling web search", explicit)
    return NoneProvider()


_provider: WebSearchProvider | None = None


def get_provider() -> WebSearchProvider:
    global _provider
    if _provider is None:
        _provider = _select_provider()
    return _provider


async def search(query: str) -> list[dict]:
    try:
        return await get_provider().search(query, MAX_RESULTS)
    except httpx.HTTPError as e:
        logger.warning("Web-search HTTP error: %s", e)
        return []
    except Exception as e:  # pragma: no cover — defence against provider-specific failures
        logger.warning("Web-search failed: %s", e)
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

    Short-circuits to a no-op when:
      - LAI_DISABLE_WEB_SEARCH=1 is set (bench mode — don't pollute prompts
        with multi-megabyte web result blobs that blow past the model's
        context window). Reads the env var on every call so a config
        toggle takes effect without restart.
      - Airgap mode is on — the whole point of airgap is that we never
        reach out to the internet.
    Logged at INFO so operators can see the gate firing."""
    if os.getenv("LAI_DISABLE_WEB_SEARCH") == "1":
        return messages
    if airgap.is_enabled():
        logger.info("Airgap mode ON — skipping web-search injection")
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
    logger.info(
        "web-search inject: results=%d block_len=%d original_prompt_len=%d",
        len(results), len(block), len(last_user_text),
    )
    if block:
        msg = messages[last_user_idx]
        if isinstance(msg.content, str):
            msg.content = f"{msg.content}\n\n{block}"
            logger.info("web-search injected: final_prompt_len=%d head=%r tail=%r",
                        len(msg.content), msg.content[:200], msg.content[-200:])
    return messages
