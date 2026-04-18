"""
title: Europe PMC — Life Sciences Literature
author: local-ai-stack
description: Search 40M+ abstracts and 9M+ full-text open-access biomedical publications from Europe PMC (PubMed + PMC + agricultural + preprints). Includes links to OA PDFs. No API key required.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from pydantic import BaseModel, Field
from typing import Optional


BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"


class Tools:
    class Valves(BaseModel):
        PAGE_SIZE: int = Field(default=10, description="Results per page")

    def __init__(self):
        self.valves = self.Valves()

    async def search(
        self,
        query: str,
        open_access: bool = False,
        year_from: int = 0,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search Europe PMC for scientific articles.
        :param query: Keywords (e.g. "CRISPR Cas9 gene therapy"), or EPMC query syntax
        :param open_access: Restrict to open-access full text
        :param year_from: Optional earliest publication year
        :return: Articles with title, authors, journal, PMID, and OA link if available
        """
        q = query
        if open_access:
            q += " AND OPEN_ACCESS:Y"
        if year_from:
            q += f" AND PUB_YEAR:[{year_from} TO 3000]"
        params = {"query": q, "format": "json", "pageSize": self.valves.PAGE_SIZE, "resultType": "core"}
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(f"{BASE}/search", params=params)
                r.raise_for_status()
                data = r.json()
            hits = (data.get("resultList") or {}).get("result", [])
            total = data.get("hitCount", 0)
            if not hits:
                return f"No Europe PMC results for: {query}"
            lines = [f"## Europe PMC: {query} ({total:,} matches)\n"]
            for h in hits:
                title = h.get("title", "").rstrip(".")
                authors = h.get("authorString", "")
                journal = h.get("journalTitle", "")
                year = h.get("pubYear", "")
                pmid = h.get("pmid", "")
                pmcid = h.get("pmcid", "")
                doi = h.get("doi", "")
                oa = h.get("isOpenAccess", "")
                lines.append(f"**{title}**")
                lines.append(f"   {authors}")
                lines.append(f"   {journal} ({year})" + (f" | PMID {pmid}" if pmid else "") + (f" | PMC {pmcid}" if pmcid else ""))
                if doi:
                    lines.append(f"   DOI: https://doi.org/{doi}")
                if oa == "Y" and pmcid:
                    lines.append(f"   📄 https://europepmc.org/article/PMC/{pmcid}")
                lines.append("")
            return "\n".join(lines)
        except Exception as e:
            return f"Europe PMC error: {e}"

    async def citations(self, pmid: str, __user__: Optional[dict] = None) -> str:
        """
        Get citations (papers that cite the given one).
        :param pmid: PubMed ID
        :return: Up to 25 citing papers with title, source, year
        """
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(
                    f"{BASE}/MED/{pmid}/citations",
                    params={"format": "json", "pageSize": 25},
                )
                r.raise_for_status()
                data = r.json()
            cits = (data.get("citationList") or {}).get("citation", [])
            if not cits:
                return f"No citations recorded for PMID {pmid}"
            lines = [f"## Citing PMID {pmid} ({data.get('hitCount', 0)} total)\n"]
            for c in cits:
                t = c.get("title", "").rstrip(".")
                a = c.get("authorString", "")
                src = c.get("journalAbbreviation", "")
                y = c.get("pubYear", "")
                p = c.get("id", "")
                lines.append(f"- **{t}** — {src} ({y}) PMID {p}")
            return "\n".join(lines)
        except Exception as e:
            return f"Europe PMC error: {e}"
