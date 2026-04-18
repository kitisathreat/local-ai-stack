"""
title: USPTO PatentsView — US Patents & Inventors
author: local-ai-stack
description: Search USPTO PatentsView — 8M+ granted patents and 4M+ pre-grant applications. Inventors, assignees, classifications, citations, and abstracts. Free, no API key.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from pydantic import BaseModel, Field
from typing import Optional


BASE = "https://search.patentsview.org/api/v1/patent"


class Tools:
    class Valves(BaseModel):
        LIMIT: int = Field(default=10, description="Max patents per query")
        API_KEY: str = Field(default="", description="Optional PatentsView API key (recommended for higher limits — request free at patentsview.org)")

    def __init__(self):
        self.valves = self.Valves()

    def _headers(self):
        h = {"User-Agent": "local-ai-stack/1.0"}
        if self.valves.API_KEY:
            h["X-Api-Key"] = self.valves.API_KEY
        return h

    async def search(
        self,
        query: str,
        from_date: str = "",
        to_date: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search US patents by keyword in title/abstract.
        :param query: Keywords (e.g. "lithium battery anode")
        :param from_date: Optional YYYY-MM-DD
        :param to_date: Optional YYYY-MM-DD
        :return: Patents with number, date, title, inventors, assignee
        """
        q = {"_or": [
            {"_text_any": {"patent_title": query}},
            {"_text_any": {"patent_abstract": query}},
        ]}
        if from_date or to_date:
            date_filter = {"_and": [{"_gte": {"patent_date": from_date or "1976-01-01"}}]}
            if to_date:
                date_filter["_and"].append({"_lte": {"patent_date": to_date}})
            q = {"_and": [q, date_filter]}
        body = {
            "q": q,
            "f": ["patent_id", "patent_title", "patent_date", "patent_abstract", "inventors", "assignees"],
            "s": [{"patent_date": "desc"}],
            "o": {"size": self.valves.LIMIT},
        }
        try:
            async with httpx.AsyncClient(timeout=45) as client:
                r = await client.post(BASE, json=body, headers=self._headers())
                r.raise_for_status()
                data = r.json()
            pats = data.get("patents", []) or []
            total = data.get("total_hits", 0)
            if not pats:
                return f"No US patents for: {query}"
            lines = [f"## USPTO PatentsView: {query} ({total:,} hits)\n"]
            for p in pats:
                num = p.get("patent_id", "")
                title = p.get("patent_title", "")
                date = p.get("patent_date", "")
                abst = (p.get("patent_abstract") or "")[:220]
                invs = ", ".join(
                    f"{i.get('inventor_name_first','')} {i.get('inventor_name_last','')}".strip()
                    for i in (p.get("inventors") or [])[:3]
                )
                asg = ", ".join(a.get("assignee_organization", "") for a in (p.get("assignees") or [])[:2])
                lines.append(f"**{num}** — {date} — {title}")
                if invs:
                    lines.append(f"   inventors: {invs}")
                if asg:
                    lines.append(f"   assignee: {asg}")
                if abst:
                    lines.append(f"   {abst}")
                lines.append(f"   🔗 https://patents.google.com/patent/US{num}\n")
            return "\n".join(lines)
        except Exception as e:
            return f"USPTO error: {e}"
