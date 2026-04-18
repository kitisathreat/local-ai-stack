"""
title: CourtListener — US Court Opinions & Dockets
author: local-ai-stack
description: Free Law Project's CourtListener — 9M+ US court opinions, oral arguments, dockets, and PACER filings. All US federal circuits + SCOTUS + state supreme courts. API key optional (free, increases limits).
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import os
import httpx
from pydantic import BaseModel, Field
from typing import Optional


BASE = "https://www.courtlistener.com/api/rest/v4"


class Tools:
    class Valves(BaseModel):
        API_TOKEN: str = Field(default_factory=lambda: os.environ.get("COURTLISTENER_API_TOKEN", ""), description="CourtListener API token (free at https://www.courtlistener.com/help/api/rest/#authentication)")
        LIMIT: int = Field(default=8, description="Max results")

    def __init__(self):
        self.valves = self.Valves()

    def _headers(self):
        h = {"User-Agent": "local-ai-stack/1.0"}
        if self.valves.API_TOKEN:
            h["Authorization"] = f"Token {self.valves.API_TOKEN}"
        return h

    async def search_opinions(
        self,
        query: str,
        court: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search US court opinions by keyword.
        :param query: Keywords (case name, citation, or phrase)
        :param court: Optional court code (e.g. "scotus", "ca9", "cafc")
        :return: Opinions with case, date, citation, and snippet
        """
        params = {"q": query, "type": "o", "order_by": "score desc"}
        if court: params["court"] = court
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(f"{BASE}/search/", params=params, headers=self._headers())
                r.raise_for_status()
                data = r.json()
            res = data.get("results", [])
            total = data.get("count", 0)
            if not res:
                return f"No CourtListener opinions for: {query}"
            lines = [f"## CourtListener: {query} ({total:,} matches)\n"]
            for r_ in res[: self.valves.LIMIT]:
                case = r_.get("caseName", "")
                dt = r_.get("dateFiled", "")
                ct = r_.get("court", "")
                cite = ", ".join(r_.get("citation", [])[:3]) if isinstance(r_.get("citation"), list) else r_.get("citation", "")
                snippet = (r_.get("snippet", "") or "").replace("<mark>", "**").replace("</mark>", "**")
                abs_url = r_.get("absolute_url", "")
                lines.append(f"**{case}** — {dt}")
                lines.append(f"   {ct} | {cite}")
                if snippet:
                    lines.append(f"   {snippet[:240]}...")
                if abs_url:
                    lines.append(f"   🔗 https://www.courtlistener.com{abs_url}\n")
            return "\n".join(lines)
        except Exception as e:
            return f"CourtListener error: {e}"

    async def dockets(self, query: str, __user__: Optional[dict] = None) -> str:
        """
        Search PACER-linked dockets by keyword.
        :param query: Keywords or party name
        :return: Dockets with court, case number, and parties
        """
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(f"{BASE}/search/", params={"q": query, "type": "r"}, headers=self._headers())
                r.raise_for_status()
                res = r.json().get("results", [])
            if not res:
                return f"No dockets for: {query}"
            lines = [f"## CourtListener Dockets: {query}\n"]
            for r_ in res[: self.valves.LIMIT]:
                case = r_.get("caseName", "")
                num = r_.get("docketNumber", "")
                court = r_.get("court", "")
                dt = r_.get("dateFiled", "") or r_.get("date_filed", "")
                abs_url = r_.get("absolute_url", "")
                lines.append(f"**{case}** — {court} #{num}  {dt}")
                if abs_url:
                    lines.append(f"   🔗 https://www.courtlistener.com{abs_url}\n")
            return "\n".join(lines)
        except Exception as e:
            return f"CourtListener error: {e}"
