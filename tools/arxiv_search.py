"""
title: ArXiv Paper Search
author: local-ai-stack
description: Search and retrieve academic papers from ArXiv. Gives models access to cutting-edge research in AI, physics, math, CS, and more.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
import xml.etree.ElementTree as ET
from pydantic import BaseModel, Field
from typing import Callable, Any, Optional


ARXIV_API = "https://export.arxiv.org/api/query"


class Tools:
    class Valves(BaseModel):
        MAX_RESULTS: int = Field(default=5, description="Maximum papers to return")
        SORT_BY: str = Field(
            default="relevance",
            description="Sort order: 'relevance', 'lastUpdatedDate', 'submittedDate'",
        )

    def __init__(self):
        self.valves = self.Valves()

    def _parse_atom(self, xml_text: str) -> list:
        ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
        root = ET.fromstring(xml_text)
        papers = []
        for entry in root.findall("atom:entry", ns):
            title = entry.findtext("atom:title", namespaces=ns) or ""
            summary = entry.findtext("atom:summary", namespaces=ns) or ""
            published = entry.findtext("atom:published", namespaces=ns) or ""
            arxiv_id = entry.findtext("atom:id", namespaces=ns) or ""
            authors = [
                a.findtext("atom:name", namespaces=ns) or ""
                for a in entry.findall("atom:author", ns)
            ]
            categories = [
                c.get("term", "")
                for c in entry.findall("atom:category", ns)
            ]
            papers.append({
                "title": title.strip().replace("\n", " "),
                "summary": summary.strip().replace("\n", " ")[:400],
                "published": published[:10],
                "url": arxiv_id,
                "authors": authors[:3],
                "categories": categories[:3],
            })
        return papers

    async def search_papers(
        self,
        query: str,
        category: str = "",
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search ArXiv for academic papers on any topic.
        :param query: Research topic or keywords (e.g. "large language models reasoning", "quantum computing")
        :param category: Optional ArXiv category filter (e.g. "cs.AI", "cs.LG", "physics", "math")
        :return: List of papers with titles, authors, abstracts, and links
        """
        if __event_emitter__:
            await __event_emitter__(
                {"type": "status", "data": {"description": f"Searching ArXiv: {query}", "done": False}}
            )

        search_query = f"all:{query}"
        if category:
            search_query += f" AND cat:{category}"

        params = {
            "search_query": search_query,
            "max_results": self.valves.MAX_RESULTS,
            "sortBy": self.valves.SORT_BY,
            "sortOrder": "descending",
        }

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(ARXIV_API, params=params)
                resp.raise_for_status()

            papers = self._parse_atom(resp.text)
            if not papers:
                return f"No ArXiv papers found for: {query}"

            lines = [f"## ArXiv Papers: {query}\n"]
            for p in papers:
                authors_str = ", ".join(p["authors"]) + (" et al." if len(p["authors"]) >= 3 else "")
                cats = " | ".join(p["categories"])
                lines.append(f"**{p['title']}**")
                lines.append(f"   {authors_str} ({p['published']}) [{cats}]")
                lines.append(f"   {p['summary']}...")
                lines.append(f"   {p['url']}\n")

            if __event_emitter__:
                await __event_emitter__(
                    {"type": "status", "data": {"description": f"Found {len(papers)} papers", "done": True}}
                )

            return "\n".join(lines)

        except ET.ParseError:
            return "Error parsing ArXiv response."
        except httpx.ConnectError:
            return "Cannot reach ArXiv. Check internet connection."
        except Exception as e:
            return f"ArXiv search error: {str(e)}"
