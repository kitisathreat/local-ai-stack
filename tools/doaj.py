"""
title: DOAJ — Directory of Open Access Journals
author: local-ai-stack
description: Search DOAJ's index of 20,000+ peer-reviewed, open-access journals and 10M+ articles. Covers every subject area. Quality-vetted, no APCs required. No API key needed.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from pydantic import BaseModel, Field
from typing import Optional
from urllib.parse import quote


BASE = "https://doaj.org/api/v3"


class Tools:
    class Valves(BaseModel):
        PAGE_SIZE: int = Field(default=10, description="Results per page")

    def __init__(self):
        self.valves = self.Valves()

    async def articles(self, query: str, __user__: Optional[dict] = None) -> str:
        """
        Search DOAJ for open-access articles.
        :param query: Keywords or DOAJ field query (e.g. "title:climate")
        :return: Articles with title, journal, year, DOI, and full-text link
        """
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(
                    f"{BASE}/search/articles/{quote(query)}",
                    params={"pageSize": self.valves.PAGE_SIZE},
                )
                r.raise_for_status()
                data = r.json()
            hits = data.get("results", [])
            total = data.get("total", 0)
            if not hits:
                return f"No DOAJ articles for: {query}"
            lines = [f"## DOAJ Articles: {query} ({total:,} matches)\n"]
            for h in hits:
                bib = h.get("bibjson", {})
                title = bib.get("title", "")
                journal = (bib.get("journal") or {}).get("title", "")
                year = bib.get("year", "")
                authors = ", ".join(a.get("name", "") for a in (bib.get("author") or [])[:3])
                links = bib.get("link", [])
                doi = ""
                for idf in bib.get("identifier", []) or []:
                    if idf.get("type") == "doi":
                        doi = idf.get("id", "")
                fulltext = next((l.get("url", "") for l in links if l.get("type") == "fulltext"), "")
                lines.append(f"**{title}**")
                lines.append(f"   {authors}")
                lines.append(f"   {journal} ({year})")
                if doi:
                    lines.append(f"   DOI: https://doi.org/{doi}")
                if fulltext:
                    lines.append(f"   📄 {fulltext}")
                lines.append("")
            return "\n".join(lines)
        except Exception as e:
            return f"DOAJ error: {e}"

    async def journals(self, subject: str, __user__: Optional[dict] = None) -> str:
        """
        Find open-access journals by subject keyword.
        :param subject: Subject or keyword (e.g. "oncology", "machine learning", "economics")
        :return: Journals with title, ISSN, publisher, country, and APC info
        """
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(
                    f"{BASE}/search/journals/{quote(subject)}",
                    params={"pageSize": self.valves.PAGE_SIZE},
                )
                r.raise_for_status()
                data = r.json()
            hits = data.get("results", [])
            total = data.get("total", 0)
            if not hits:
                return f"No DOAJ journals for: {subject}"
            lines = [f"## DOAJ Journals: {subject} ({total:,} matches)\n"]
            for j in hits:
                bib = j.get("bibjson", {})
                title = bib.get("title", "")
                pub = (bib.get("publisher") or {}).get("name", "")
                country = (bib.get("publisher") or {}).get("country", "")
                eissn = next((i.get("id", "") for i in bib.get("identifier", []) if i.get("type") == "eissn"), "")
                pissn = next((i.get("id", "") for i in bib.get("identifier", []) if i.get("type") == "pissn"), "")
                apc = bib.get("apc", {}) or {}
                has_apc = apc.get("has_apc", False)
                lines.append(f"**{title}** — {pub} ({country})")
                lines.append(f"   ISSN: {pissn or '—'} / {eissn or '—'}")
                lines.append(f"   APC: {'Yes' if has_apc else 'No'}")
                lines.append("")
            return "\n".join(lines)
        except Exception as e:
            return f"DOAJ error: {e}"
