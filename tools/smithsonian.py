"""
title: Smithsonian Open Access — 4.5M+ Objects
author: local-ai-stack
description: Search the Smithsonian's 19 museums, libraries, and archives. 4.5M+ open-access (CC0) 2D/3D artworks, specimens, documents, and media. Covers art, natural history, aerospace, science, history. Free API key required (instant signup).
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from pydantic import BaseModel, Field
from typing import Optional


BASE = "https://api.si.edu/openaccess/api/v1.0"


class Tools:
    class Valves(BaseModel):
        API_KEY: str = Field(default="", description="Smithsonian OA API key (get at api.si.edu)")
        MAX_RESULTS: int = Field(default=5, description="Max records returned")

    def __init__(self):
        self.valves = self.Valves()

    async def search(
        self,
        query: str,
        unit: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search the Smithsonian Open Access collection.
        :param query: Keywords
        :param unit: Optional unit code (e.g. "NMAH", "NMNH", "SAAM", "NASM", "FSG")
        :return: Matching items with title, type, unit, and a link
        """
        if not self.valves.API_KEY:
            return "Set SMITHSONIAN API_KEY valve (free, from https://api.si.edu)."
        q = query
        if unit:
            q = f"{q} AND unit_code:{unit}"
        params = {"api_key": self.valves.API_KEY, "q": q, "rows": self.valves.MAX_RESULTS, "start": 0}
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(f"{BASE}/search", params=params)
                r.raise_for_status()
                data = r.json()
            rows = data.get("response", {}).get("rows", [])
            total = data.get("response", {}).get("rowCount", 0)
            if not rows:
                return f"No Smithsonian OA items for: {query}"
            lines = [f"## Smithsonian OA: {query} ({total:,} matches)\n"]
            for it in rows:
                content = it.get("content", {}) or {}
                desc_non = content.get("descriptiveNonRepeating", {}) or {}
                title = it.get("title", "") or content.get("title", "")
                unit_ = it.get("unitCode", "")
                record_id = it.get("id", "")
                online = desc_non.get("online_media", {}) or {}
                media = (online.get("media") or [{}])[0] if online else {}
                img = media.get("content", "")
                guid = (desc_non.get("guid", "") or "").strip()
                lines.append(f"**{title}**  [{unit_}]")
                if img and isinstance(img, str) and img.startswith("http"):
                    lines.append(f"   ![img]({img})")
                if guid:
                    lines.append(f"   🔗 {guid}")
                lines.append(f"   id: {record_id}\n")
            return "\n".join(lines)
        except Exception as e:
            return f"Smithsonian error: {e}"

    async def stats(self, __user__: Optional[dict] = None) -> str:
        """
        Overall Smithsonian OA corpus statistics.
        :return: Total items and online counts
        """
        if not self.valves.API_KEY:
            return "Set SMITHSONIAN API_KEY valve."
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(f"{BASE}/stats", params={"api_key": self.valves.API_KEY})
                r.raise_for_status()
                data = r.json()
            resp = data.get("response", {})
            return (
                "## Smithsonian Open Access Stats\n"
                f"- Total records: {resp.get('totalCount', 0):,}\n"
                f"- With online media: {resp.get('totalWithMediaCount', 0):,}\n"
                f"- CC0 open access: {resp.get('totalCC0Count', 0):,}"
            )
        except Exception as e:
            return f"Smithsonian error: {e}"
