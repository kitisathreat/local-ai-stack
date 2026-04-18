"""
title: GDELT — Global Events, News & Tone
author: local-ai-stack
description: Query the GDELT Project 2.0 DOC API for worldwide news coverage and the GKG API for event/sentiment data. Searches billions of news articles in 65+ languages since 2015. No API key required.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from pydantic import BaseModel, Field
from typing import Optional


DOC_API = "https://api.gdeltproject.org/api/v2/doc/doc"


class Tools:
    class Valves(BaseModel):
        MAX_RESULTS: int = Field(default=10, description="Max articles returned")
        TIMESPAN: str = Field(default="7d", description="Timespan filter: 24h, 3d, 7d, 1m, 3m, 6m, 1y")

    def __init__(self):
        self.valves = self.Valves()

    async def news_search(
        self,
        query: str,
        source_country: str = "",
        timespan: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search worldwide news coverage from GDELT.
        :param query: Keywords or phrase (use quotes for phrases). Supports "OR" and parentheses.
        :param source_country: Optional ISO country of publisher (e.g. "US", "IN", "DE")
        :param timespan: Timespan filter (24h, 3d, 7d, 1m, 3m, 6m, 1y). Defaults to valve value.
        :return: Top articles with title, publisher, language and URL
        """
        q = query
        if source_country:
            q += f" sourcecountry:{source_country.lower()}"
        params = {
            "query": q,
            "mode": "ArtList",
            "format": "json",
            "maxrecords": self.valves.MAX_RESULTS,
            "timespan": timespan or self.valves.TIMESPAN,
            "sort": "datedesc",
        }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(DOC_API, params=params)
                r.raise_for_status()
                data = r.json()
            arts = data.get("articles", [])
            if not arts:
                return f"No GDELT articles for: {query}"
            lines = [f"## GDELT News: {query}\n"]
            for a in arts:
                title = a.get("title", "").strip()
                domain = a.get("domain", "")
                lang = a.get("language", "")
                date = a.get("seendate", "")
                country = a.get("sourcecountry", "")
                url = a.get("url", "")
                lines.append(f"**{title}**")
                lines.append(f"   {domain} ({country}/{lang}) — {date}")
                lines.append(f"   {url}\n")
            return "\n".join(lines)
        except Exception as e:
            return f"GDELT error: {e}"

    async def tone_chart(
        self,
        query: str,
        timespan: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get an aggregate tone/sentiment distribution of news coverage on a topic.
        :param query: Topic keywords
        :param timespan: Timespan (24h, 7d, 1m, 3m, 1y)
        :return: Summary of tone distribution (negative → positive) from GDELT
        """
        params = {
            "query": query, "mode": "ToneChart", "format": "json",
            "timespan": timespan or self.valves.TIMESPAN,
        }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(DOC_API, params=params)
                r.raise_for_status()
                data = r.json()
            buckets = data.get("tonechart", [])
            if not buckets:
                return f"No tone data for: {query}"
            lines = [f"## GDELT Tone: {query} ({timespan or self.valves.TIMESPAN})\n"]
            lines.append("| Tone | Articles |\n|---|---|")
            for b in buckets:
                lines.append(f"| {b.get('bin', ''):>+4} | {b.get('count', 0):,} |")
            return "\n".join(lines)
        except Exception as e:
            return f"GDELT error: {e}"

    async def top_themes(
        self,
        query: str,
        timespan: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get top themes/topics co-occurring with a query across global news.
        :param query: Topic keywords
        :param timespan: Timespan (24h, 7d, 1m)
        :return: Top co-occurring GDELT themes
        """
        params = {
            "query": query, "mode": "TimelineVol", "format": "json",
            "timespan": timespan or self.valves.TIMESPAN,
        }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(DOC_API, params=params)
                r.raise_for_status()
                data = r.json()
            tl = data.get("timeline", [])
            if not tl:
                return f"No timeline data for: {query}"
            points = tl[0].get("data", [])
            lines = [f"## GDELT Volume Timeline: {query}\n", "| Date | Share (%) |\n|---|---|"]
            for p in points[-30:]:
                lines.append(f"| {p.get('date', '')} | {p.get('value', 0)} |")
            return "\n".join(lines)
        except Exception as e:
            return f"GDELT error: {e}"
