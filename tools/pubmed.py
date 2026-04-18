"""
title: PubMed / NCBI Biomedical Search
author: local-ai-stack
description: Search PubMed — the world's largest biomedical literature database (35M+ articles). Covers medicine, biology, pharmacology, genetics, neuroscience, and more.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import os
import httpx
import xml.etree.ElementTree as ET
from pydantic import BaseModel, Field
from typing import Callable, Any, Optional


EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


class Tools:
    class Valves(BaseModel):
        NCBI_API_KEY: str = Field(
            default_factory=lambda: os.environ.get("NCBI_API_KEY", ""),
            description="Optional NCBI API key for 10 req/s instead of 3 req/s (free at https://www.ncbi.nlm.nih.gov/account/)",
        )
        MAX_RESULTS: int = Field(default=5, description="Maximum articles to return")
        DATABASE: str = Field(
            default="pubmed",
            description="NCBI database: pubmed, pmc, gene, nucleotide, protein",
        )

    def __init__(self):
        self.valves = self.Valves()

    def _base_params(self) -> dict:
        p = {"retmode": "json", "tool": "local-ai-stack", "email": "local@localhost"}
        if self.valves.NCBI_API_KEY:
            p["api_key"] = self.valves.NCBI_API_KEY
        return p

    async def search_pubmed(
        self,
        query: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search PubMed for biomedical research articles by keywords, disease, drug, gene, or author.
        :param query: Search terms (e.g. "CRISPR cancer therapy", "mRNA vaccine immunogenicity", "Alzheimer amyloid")
        :return: Article titles, authors, journal, year, abstract snippet, and PubMed link
        """
        if __event_emitter__:
            await __event_emitter__(
                {"type": "status", "data": {"description": f"Searching PubMed: {query}", "done": False}}
            )

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                # Step 1: search for PMIDs
                search_params = {
                    **self._base_params(),
                    "db": self.valves.DATABASE,
                    "term": query,
                    "retmax": self.valves.MAX_RESULTS,
                    "usehistory": "y",
                    "sort": "relevance",
                }
                s = await client.get(f"{EUTILS}/esearch.fcgi", params=search_params)
                s.raise_for_status()
                sdata = s.json()

                ids = sdata.get("esearchresult", {}).get("idlist", [])
                if not ids:
                    return f"No PubMed articles found for: {query}"

                # Step 2: fetch summaries
                summary_params = {
                    **self._base_params(),
                    "db": self.valves.DATABASE,
                    "id": ",".join(ids),
                }
                r = await client.get(f"{EUTILS}/esummary.fcgi", params=summary_params)
                r.raise_for_status()
                rdata = r.json()

            uids = rdata.get("result", {}).get("uids", [])
            results = rdata.get("result", {})

            if __event_emitter__:
                await __event_emitter__(
                    {"type": "status", "data": {"description": f"Found {len(uids)} articles", "done": True}}
                )

            lines = [f"## PubMed Results: {query}\n"]
            for uid in uids:
                art = results.get(uid, {})
                title = art.get("title", "No title").rstrip(".")
                authors = art.get("authors", [])
                author_str = ", ".join(a.get("name", "") for a in authors[:3])
                if len(authors) > 3:
                    author_str += " et al."
                source = art.get("source", "")
                pub_date = art.get("pubdate", "")
                url = f"https://pubmed.ncbi.nlm.nih.gov/{uid}/"
                lines.append(f"**{title}**")
                lines.append(f"   {author_str} — *{source}* ({pub_date})")
                lines.append(f"   PMID: {uid} | {url}\n")

            return "\n".join(lines)

        except httpx.ConnectError:
            return "Cannot reach NCBI. Check internet connection."
        except Exception as e:
            return f"PubMed error: {str(e)}"

    async def get_article_abstract(
        self,
        pmid: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Fetch the full abstract of a PubMed article by its PMID.
        :param pmid: PubMed ID number (e.g. "39123456")
        :return: Full title, authors, journal info, and abstract text
        """
        try:
            params = {
                **self._base_params(),
                "db": "pubmed",
                "id": pmid.strip(),
                "rettype": "abstract",
                "retmode": "xml",
            }
            params.pop("retmode", None)
            params["retmode"] = "xml"

            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(f"{EUTILS}/efetch.fcgi", params=params)
                r.raise_for_status()
                root = ET.fromstring(r.text)

            article = root.find(".//Article")
            if article is None:
                return f"Article PMID {pmid} not found."

            title = article.findtext(".//ArticleTitle") or "No title"
            abstract_texts = article.findall(".//AbstractText")
            abstract = " ".join(
                (f"**{a.get('Label', '')}:** " if a.get("Label") else "") + (a.text or "")
                for a in abstract_texts
            ).strip() or "No abstract available."

            journal = article.findtext(".//Journal/Title") or ""
            year = article.findtext(".//PubDate/Year") or ""
            authors = root.findall(".//Author")
            author_list = []
            for a in authors[:5]:
                ln = a.findtext("LastName") or ""
                fn = a.findtext("ForeName") or a.findtext("Initials") or ""
                if ln:
                    author_list.append(f"{ln} {fn}".strip())
            author_str = "; ".join(author_list)
            if len(authors) > 5:
                author_str += " et al."

            return (
                f"## {title}\n\n"
                f"**Authors:** {author_str}\n"
                f"**Journal:** {journal} ({year})\n"
                f"**PMID:** {pmid} | https://pubmed.ncbi.nlm.nih.gov/{pmid}/\n\n"
                f"**Abstract:**\n{abstract}"
            )

        except Exception as e:
            return f"Error fetching PMID {pmid}: {str(e)}"
