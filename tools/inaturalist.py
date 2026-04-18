"""
title: iNaturalist — Community Species Observations
author: local-ai-stack
description: Search 200M+ wildlife observations from the iNaturalist citizen-science platform. Identify species, check local biodiversity, find research-grade photos. No API key required.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from pydantic import BaseModel, Field
from typing import Optional


BASE = "https://api.inaturalist.org/v1"


class Tools:
    class Valves(BaseModel):
        LIMIT: int = Field(default=10, description="Max results")

    def __init__(self):
        self.valves = self.Valves()

    async def taxa(self, query: str, __user__: Optional[dict] = None) -> str:
        """
        Search iNaturalist taxa (species, genera, families).
        :param query: Common or scientific name (e.g. "red fox", "Vulpes vulpes")
        :return: Taxa with rank, observations count, and thumbnail
        """
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(f"{BASE}/taxa", params={"q": query, "per_page": self.valves.LIMIT})
                r.raise_for_status()
                data = r.json()
            res = data.get("results", [])
            if not res:
                return f"No iNat taxa for: {query}"
            lines = [f"## iNaturalist Taxa: {query}\n"]
            for t in res:
                sci = t.get("name", "")
                common = t.get("preferred_common_name", "")
                rank = t.get("rank", "")
                obs = t.get("observations_count", 0)
                photo = (t.get("default_photo") or {}).get("square_url", "")
                tid = t.get("id", "")
                lines.append(f"**{common or sci}** — _{sci}_ ({rank})")
                lines.append(f"   Observations: {obs:,}")
                if photo:
                    lines.append(f"   ![photo]({photo})")
                lines.append(f"   🔗 https://www.inaturalist.org/taxa/{tid}\n")
            return "\n".join(lines)
        except Exception as e:
            return f"iNaturalist error: {e}"

    async def observations(
        self,
        taxon_name: str = "",
        place: str = "",
        quality: str = "research",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Find recent observations (sightings).
        :param taxon_name: Scientific or common name filter
        :param place: Place name (e.g. "California", "Japan")
        :param quality: "research", "needs_id", or "any"
        :return: Recent observations with user, date, place, and photos
        """
        params = {"per_page": self.valves.LIMIT, "order_by": "created_at", "order": "desc"}
        if taxon_name: params["taxon_name"] = taxon_name
        if place: params["place_id"] = place  # works with numeric id or name if iNat resolves
        if quality == "research": params["quality_grade"] = "research"
        elif quality == "needs_id": params["quality_grade"] = "needs_id"
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(f"{BASE}/observations", params=params)
                r.raise_for_status()
                data = r.json()
            res = data.get("results", [])
            if not res:
                return f"No observations for {taxon_name or place}"
            lines = [f"## iNaturalist Observations — {taxon_name or place}\n"]
            for o in res:
                taxon = (o.get("taxon") or {}).get("preferred_common_name") or (o.get("taxon") or {}).get("name", "")
                sci = (o.get("taxon") or {}).get("name", "")
                user = (o.get("user") or {}).get("login", "")
                at = o.get("observed_on_string", "") or o.get("observed_on", "")
                where = o.get("place_guess", "")
                photos = (o.get("photos") or [])
                photo_url = photos[0].get("url", "") if photos else ""
                obs_id = o.get("id", "")
                lines.append(f"- **{taxon}** (_{sci}_) by @{user} — {at}, {where}")
                if photo_url:
                    lines.append(f"    ![photo]({photo_url})")
                lines.append(f"    🔗 https://www.inaturalist.org/observations/{obs_id}")
            return "\n".join(lines)
        except Exception as e:
            return f"iNaturalist error: {e}"
