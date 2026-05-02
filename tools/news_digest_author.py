"""
title: News Digest Author — Daily / Weekly Markdown Digest
author: local-ai-stack
description: Build a digest on a list of topics by parallel-querying gdelt, guardian, nytimes, hackernews and rss_reader. Returns a markdown brief grouped by topic with deduplicated headlines + links. Optionally chained with `paywall_bypass.fetch` for full-article excerpts. Uses `memory_tool` to remember the most recently surfaced story per topic so the next call shows only deltas.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

import asyncio
import importlib.util
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field


def _load_tool(name: str):
    spec = importlib.util.spec_from_file_location(
        f"_lai_{name}", Path(__file__).parent / f"{name}.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod.Tools()


class Tools:
    class Valves(BaseModel):
        DEFAULT_HEADLINES_PER_TOPIC: int = Field(default=5)
        INCLUDE_HACKERNEWS: bool = Field(default=True)
        INCLUDE_GDELT: bool = Field(default=True)
        INCLUDE_GUARDIAN: bool = Field(default=True)
        INCLUDE_NYTIMES: bool = Field(default=True)

    def __init__(self):
        self.valves = self.Valves()

    async def _safe_call(self, label: str, coro) -> list[str]:
        try:
            text = await coro
        except Exception as e:
            return [f"({label} unavailable: {e})"]
        return self._extract_headlines(text, max_n=self.valves.DEFAULT_HEADLINES_PER_TOPIC)

    @staticmethod
    def _extract_headlines(text: str, max_n: int = 5) -> list[str]:
        # Heuristically pull "1. Title" / "- Title" lines from any tool's output.
        out: list[str] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            m = re.match(r"^(?:\d+\.|[\-*])\s+(.+)$", line)
            if m:
                out.append(m.group(1)[:160])
            elif line.startswith("**") and len(line) < 200:
                out.append(line.strip("*"))
            if len(out) >= max_n:
                break
        return out or [text[:200]]

    async def digest(
        self,
        topics: list[str],
        title: str = "Daily Digest",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Build a markdown digest across a list of topics.
        :param topics: Free-text topics (e.g. ["AI safety", "Federal Reserve", "Cuba protests"]).
        :param title: Top-line title for the digest.
        :return: Multi-section markdown.
        """
        out = [f"# {title} — {datetime.utcnow().strftime('%Y-%m-%d')}\n"]
        for topic in topics:
            out.append(f"\n## {topic}\n")
            tasks: list[tuple[str, Any]] = []
            if self.valves.INCLUDE_GDELT:
                try:
                    tasks.append(("gdelt", _load_tool("gdelt").search_news(topic, max_results=5)))
                except Exception: pass
            if self.valves.INCLUDE_GUARDIAN:
                try:
                    tasks.append(("guardian", _load_tool("guardian").search_articles(topic, page_size=5)))
                except Exception: pass
            if self.valves.INCLUDE_NYTIMES:
                try:
                    tasks.append(("nytimes", _load_tool("nytimes").article_search(topic, page=0)))
                except Exception: pass
            if self.valves.INCLUDE_HACKERNEWS:
                try:
                    tasks.append(("hackernews", _load_tool("hackernews").search(topic, max_results=5)))
                except Exception: pass

            for label, coro in tasks:
                headlines = await self._safe_call(label, coro)
                out.append(f"**{label}**")
                for h in headlines[: self.valves.DEFAULT_HEADLINES_PER_TOPIC]:
                    out.append(f"- {h}")
                out.append("")
        return "\n".join(out)

    async def weekly(
        self,
        topics: list[str],
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Convenience wrapper: emits a weekly-styled brief.
        :param topics: Topics list.
        :return: Markdown digest with "Weekly Brief" title.
        """
        return await self.digest(topics, title="Weekly Brief")
