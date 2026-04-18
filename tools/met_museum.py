"""
title: MET Museum — Open Access Art Collection
author: local-ai-stack
description: Search The Metropolitan Museum of Art's 470,000+ object open-access collection. Paintings, sculptures, photographs, decorative arts, and more. Includes high-resolution images (CC0) where available. No API key required.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from pydantic import BaseModel, Field
from typing import Optional


BASE = "https://collectionapi.metmuseum.org/public/collection/v1"


class Tools:
    class Valves(BaseModel):
        MAX_RESULTS: int = Field(default=5, description="Max objects to detail")

    def __init__(self):
        self.valves = self.Valves()

    async def search(
        self,
        query: str,
        has_images: bool = True,
        department_id: int = 0,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search MET's collection by keyword.
        :param query: Artist, title, or keyword (e.g. "Van Gogh", "Egyptian amulet", "sunflowers")
        :param has_images: Restrict to objects with public-domain images
        :param department_id: Optional department ID (use list_departments to find)
        :return: Object titles, artist, period, and URLs
        """
        params = {"q": query}
        if has_images:
            params["hasImages"] = "true"
        if department_id:
            params["departmentId"] = department_id
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(f"{BASE}/search", params=params)
                r.raise_for_status()
                data = r.json()
                ids = data.get("objectIDs") or []
                total = data.get("total", 0)
                if not ids:
                    return f"No MET objects for: {query}"
                lines = [f"## MET Museum: {query} ({total:,} matches)\n"]
                for oid in ids[: self.valves.MAX_RESULTS]:
                    o = await client.get(f"{BASE}/objects/{oid}")
                    if o.status_code != 200:
                        continue
                    obj = o.json()
                    title = obj.get("title", "Untitled")
                    artist = obj.get("artistDisplayName", "") or "Unknown"
                    date = obj.get("objectDate", "")
                    dept = obj.get("department", "")
                    culture = obj.get("culture", "")
                    img = obj.get("primaryImageSmall", "")
                    url = obj.get("objectURL", "")
                    lines.append(f"**{title}** ({date})")
                    lines.append(f"   {artist} — {culture or dept}")
                    if img:
                        lines.append(f"   ![{title}]({img})")
                    if url:
                        lines.append(f"   🔗 {url}\n")
                return "\n".join(lines)
        except Exception as e:
            return f"MET error: {e}"

    async def object(self, object_id: int, __user__: Optional[dict] = None) -> str:
        """
        Look up a MET object by ID.
        :param object_id: MET object ID (integer)
        :return: Full object metadata
        """
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(f"{BASE}/objects/{object_id}")
                if r.status_code == 404:
                    return f"Object {object_id} not found."
                r.raise_for_status()
                o = r.json()
            out = [
                f"## {o.get('title', 'Untitled')}",
                f"**Artist:** {o.get('artistDisplayName', 'Unknown')}",
                f"**Date:** {o.get('objectDate', '')}",
                f"**Medium:** {o.get('medium', '')}",
                f"**Department:** {o.get('department', '')}",
                f"**Culture:** {o.get('culture', '')}",
                f"**Classification:** {o.get('classification', '')}",
                f"**Credit:** {o.get('creditLine', '')}",
            ]
            if o.get("primaryImage"):
                out.append(f"\n![image]({o['primaryImage']})")
            if o.get("objectURL"):
                out.append(f"\n🔗 {o['objectURL']}")
            return "\n".join(out)
        except Exception as e:
            return f"MET error: {e}"

    async def list_departments(self, __user__: Optional[dict] = None) -> str:
        """
        List MET departments and their IDs.
        :return: Department table
        """
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(f"{BASE}/departments")
                r.raise_for_status()
                depts = r.json().get("departments", [])
            lines = ["## MET Departments\n", "| ID | Name |", "|---|---|"]
            for d in depts:
                lines.append(f"| {d['departmentId']} | {d['displayName']} |")
            return "\n".join(lines)
        except Exception as e:
            return f"MET error: {e}"
