"""
title: Data.gov — US Government Open Data Catalog
author: local-ai-stack
description: Search Data.gov's CKAN catalog of 300,000+ open datasets from 100+ US federal, state, and local agencies. Find datasets by topic, agency, or keyword. No API key required.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from pydantic import BaseModel, Field
from typing import Optional


BASE = "https://catalog.data.gov/api/3/action"


class Tools:
    class Valves(BaseModel):
        LIMIT: int = Field(default=8, description="Max datasets")

    def __init__(self):
        self.valves = self.Valves()

    async def search(
        self,
        query: str,
        organization: str = "",
        format: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search Data.gov for open datasets.
        :param query: Keywords (e.g. "wildfire", "housing prices", "air traffic")
        :param organization: Optional agency slug (e.g. "usgs-gov", "noaa-gov", "epa-gov")
        :param format: Optional format filter (CSV, JSON, GeoJSON, Shapefile, API)
        :return: Datasets with title, publisher, and resource download links
        """
        fq = []
        if organization:
            fq.append(f'organization:"{organization}"')
        if format:
            fq.append(f'res_format:"{format.upper()}"')
        params = {"q": query, "rows": self.valves.LIMIT}
        if fq:
            params["fq"] = " AND ".join(fq)
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(f"{BASE}/package_search", params=params)
                r.raise_for_status()
                data = r.json().get("result", {})
            datasets = data.get("results", [])
            total = data.get("count", 0)
            if not datasets:
                return f"No Data.gov datasets for: {query}"
            lines = [f"## Data.gov: {query} ({total:,} hits)\n"]
            for d in datasets:
                title = d.get("title", "")
                org = (d.get("organization") or {}).get("title", "")
                notes = (d.get("notes") or "")[:220].replace("\n", " ")
                slug = d.get("name", "")
                resources = d.get("resources", [])
                formats = ", ".join(sorted({(r_.get("format") or "").upper() for r_ in resources} - {""}))
                lines.append(f"**{title}** — {org}")
                if notes:
                    lines.append(f"   {notes}...")
                if formats:
                    lines.append(f"   formats: {formats}")
                lines.append(f"   🔗 https://catalog.data.gov/dataset/{slug}\n")
            return "\n".join(lines)
        except Exception as e:
            return f"Data.gov error: {e}"

    async def organizations(self, query: str = "", __user__: Optional[dict] = None) -> str:
        """
        List or search Data.gov publishing organizations.
        :param query: Optional keyword filter
        :return: Organizations with dataset counts
        """
        params = {"all_fields": True, "limit": 50}
        if query:
            params["q"] = query
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(f"{BASE}/organization_list", params=params)
                r.raise_for_status()
                orgs = r.json().get("result", [])
            lines = [f"## Data.gov Organizations {'('+query+')' if query else ''}\n", "| Slug | Title | Datasets |", "|---|---|---|"]
            for o in orgs[:50]:
                lines.append(f"| `{o.get('name','')}` | {o.get('title','')} | {o.get('package_count', 0)} |")
            return "\n".join(lines)
        except Exception as e:
            return f"Data.gov error: {e}"
