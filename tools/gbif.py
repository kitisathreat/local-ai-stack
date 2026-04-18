"""
title: GBIF — Global Biodiversity Information Facility
author: local-ai-stack
description: Search 2.5B+ species occurrence records and the taxonomic backbone (species, genus, family...) from the Global Biodiversity Information Facility. Worldwide coverage. No API key required.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from pydantic import BaseModel, Field
from typing import Optional


BASE = "https://api.gbif.org/v1"


class Tools:
    class Valves(BaseModel):
        LIMIT: int = Field(default=10, description="Max rows per query")

    def __init__(self):
        self.valves = self.Valves()

    async def species(self, name: str, __user__: Optional[dict] = None) -> str:
        """
        Look up a species (or any taxon) in the GBIF backbone.
        :param name: Scientific or common name (e.g. "Panthera leo", "common raven")
        :return: Taxon info with rank, parent taxa, and usage key
        """
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(f"{BASE}/species/match", params={"name": name})
                r.raise_for_status()
                d = r.json()
            if d.get("matchType") == "NONE":
                return f"No GBIF match for: {name}"
            lines = [f"## GBIF: {d.get('scientificName', name)}"]
            lines.append(f"**Rank:** {d.get('rank', '')}")
            lines.append(f"**Status:** {d.get('status', '')}")
            lines.append(f"**Confidence:** {d.get('confidence', '')}%")
            for r_ in ["kingdom", "phylum", "class", "order", "family", "genus", "species"]:
                if d.get(r_):
                    lines.append(f"- {r_.title()}: {d[r_]}")
            lines.append(f"**usageKey:** {d.get('usageKey', '')}")
            lines.append(f"🔗 https://www.gbif.org/species/{d.get('usageKey', '')}")
            return "\n".join(lines)
        except Exception as e:
            return f"GBIF error: {e}"

    async def occurrences(
        self,
        scientific_name: str,
        country: str = "",
        year: int = 0,
        has_coordinate: bool = True,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search GBIF occurrence records for a species.
        :param scientific_name: Scientific name (e.g. "Puma concolor")
        :param country: Optional ISO-2 country code (e.g. "US")
        :param year: Optional year filter
        :param has_coordinate: Require geolocation
        :return: Occurrence records with date, country, and coordinates
        """
        params = {
            "scientificName": scientific_name, "limit": self.valves.LIMIT,
            "hasCoordinate": str(has_coordinate).lower(),
        }
        if country:
            params["country"] = country.upper()
        if year:
            params["year"] = year
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(f"{BASE}/occurrence/search", params=params)
                r.raise_for_status()
                data = r.json()
            recs = data.get("results", [])
            if not recs:
                return f"No occurrences for {scientific_name}"
            lines = [f"## GBIF Occurrences: {scientific_name} ({data.get('count', 0):,} total)\n"]
            for o in recs:
                date = o.get("eventDate", "")[:10]
                c = o.get("country", "")
                lat = o.get("decimalLatitude", "")
                lon = o.get("decimalLongitude", "")
                loc = o.get("locality", "") or o.get("stateProvince", "")
                ds = o.get("datasetName", "")
                lines.append(f"- {date} | {c} {loc} | {lat},{lon} | ds: {ds}")
            return "\n".join(lines)
        except Exception as e:
            return f"GBIF error: {e}"
