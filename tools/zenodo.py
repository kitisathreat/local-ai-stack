"""
title: Zenodo — Open Science Repository
author: local-ai-stack
description: Search Zenodo (CERN) — an open repository for research papers, datasets, software, posters, and presentations. Covers all scientific disciplines with persistent DOIs.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import os
import httpx
from pydantic import BaseModel, Field
from typing import Callable, Any, Optional


ZENODO_API = "https://zenodo.org/api"


class Tools:
    class Valves(BaseModel):
        ZENODO_TOKEN: str = Field(
            default_factory=lambda: os.environ.get("ZENODO_TOKEN", ""),
            description="Optional Zenodo personal access token (free at https://zenodo.org/account/settings/applications/tokens/new/)",
        )
        MAX_RESULTS: int = Field(default=5, description="Maximum results to return")

    def __init__(self):
        self.valves = self.Valves()

    def _headers(self) -> dict:
        h = {"User-Agent": "local-ai-stack/1.0"}
        if self.valves.ZENODO_TOKEN:
            h["Authorization"] = f"Bearer {self.valves.ZENODO_TOKEN}"
        return h

    def _fmt_record(self, r: dict) -> str:
        meta = r.get("metadata", {})
        title = meta.get("title", "No title")
        resource_type = meta.get("resource_type", {}).get("type", "unknown")
        creators = meta.get("creators", [])[:3]
        author_str = ", ".join(c.get("name", "") for c in creators)
        if len(meta.get("creators", [])) > 3:
            author_str += " et al."
        pub_date = meta.get("publication_date", "?")[:4]
        doi = r.get("doi", "")
        doi_url = f"https://doi.org/{doi}" if doi else ""
        downloads = r.get("stats", {}).get("downloads", 0)
        views = r.get("stats", {}).get("views", 0)
        access = meta.get("access_right", "")

        lines = [f"**{title}** [{resource_type}]"]
        lines.append(f"   {author_str} ({pub_date}) | 👁 {views} views | ⬇ {downloads} downloads | {access}")
        if doi_url:
            lines.append(f"   🔗 {doi_url}")
        return "\n".join(lines)

    async def search_records(
        self,
        query: str,
        record_type: str = "",
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search Zenodo for open research records including datasets, papers, software, and presentations.
        :param query: Search terms (e.g. "single cell RNA sequencing", "exoplanet detection", "climate model")
        :param record_type: Filter by type: 'dataset', 'software', 'publication', 'presentation', 'poster', 'image', 'video'
        :return: Records with titles, authors, DOIs, and download stats
        """
        if __event_emitter__:
            await __event_emitter__(
                {"type": "status", "data": {"description": f"Searching Zenodo: {query}", "done": False}}
            )

        params = {
            "q": query,
            "size": self.valves.MAX_RESULTS,
            "sort": "mostviewed",
            "access_right": "open",
        }
        if record_type:
            params["type"] = record_type

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{ZENODO_API}/records",
                    params=params,
                    headers=self._headers(),
                )
                resp.raise_for_status()
                data = resp.json()

            records = data.get("hits", {}).get("hits", [])
            if not records:
                return f"No Zenodo records found for: {query}"

            if __event_emitter__:
                await __event_emitter__(
                    {"type": "status", "data": {"description": f"Found {len(records)} records", "done": True}}
                )

            lines = [f"## Zenodo: {query}\n"]
            for r in records:
                lines.append(self._fmt_record(r))
                lines.append("")

            return "\n".join(lines)

        except httpx.ConnectError:
            return "Cannot reach Zenodo. Check internet connection."
        except Exception as e:
            return f"Zenodo error: {str(e)}"

    async def get_record(
        self,
        record_id: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get full details for a specific Zenodo record by its ID.
        :param record_id: Zenodo record ID or DOI (e.g. "10495529" or "10.5281/zenodo.10495529")
        :return: Full record metadata including description, files, and download links
        """
        rid = record_id.strip()
        if "zenodo." in rid:
            rid = rid.split("zenodo.")[-1]

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{ZENODO_API}/records/{rid}",
                    headers=self._headers(),
                )
                resp.raise_for_status()
                r = resp.json()

            meta = r.get("metadata", {})
            title = meta.get("title", "No title")
            desc = meta.get("description", "No description available.")
            import re
            desc = re.sub(r"<[^>]+>", "", desc).strip()[:500]
            files = r.get("files", [])
            file_list = [f"{f.get('key', '')} ({round(f.get('size', 0)/1024/1024, 1)} MB)" for f in files[:5]]

            result = self._fmt_record(r)
            result += f"\n\n**Description:** {desc}"
            if file_list:
                result += f"\n\n**Files:**\n" + "\n".join(f"- {f}" for f in file_list)

            return result

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return f"Zenodo record not found: {record_id}"
            return f"Zenodo API error: HTTP {e.response.status_code}"
        except Exception as e:
            return f"Zenodo lookup error: {str(e)}"
