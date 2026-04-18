"""
title: OpenSanctions — Global Sanctions & PEP Lists
author: local-ai-stack
description: Search OpenSanctions — a consolidated dataset of sanctioned persons/entities (OFAC, EU, UN, UK), politically-exposed persons (PEPs), warranted/wanted lists, and criminal entities. For compliance research. Free API key optional for higher quotas.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from pydantic import BaseModel, Field
from typing import Optional


BASE = "https://api.opensanctions.org"


class Tools:
    class Valves(BaseModel):
        API_KEY: str = Field(default="", description="Optional OpenSanctions API key (https://www.opensanctions.org/api/)")
        LIMIT: int = Field(default=8, description="Max results")

    def __init__(self):
        self.valves = self.Valves()

    def _headers(self):
        h = {"User-Agent": "local-ai-stack/1.0"}
        if self.valves.API_KEY:
            h["Authorization"] = f"ApiKey {self.valves.API_KEY}"
        return h

    async def search(
        self,
        query: str,
        dataset: str = "default",
        schema: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search OpenSanctions entities (people, companies, vessels, aircraft) by name.
        :param query: Name to search (e.g. "Vladimir Putin", "Wagner Group")
        :param dataset: Dataset scope: "default", "sanctions", "peps", "crime", "all"
        :param schema: Optional schema filter: "Person", "Organization", "Company", "Vessel", "Aircraft"
        :return: Matching entities with listings, countries, and topics
        """
        params = {"q": query, "limit": self.valves.LIMIT}
        if schema:
            params["schema"] = schema
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(f"{BASE}/search/{dataset}", params=params, headers=self._headers())
                r.raise_for_status()
                data = r.json()
            res = data.get("results", [])
            total = data.get("total", {}).get("value", 0)
            if not res:
                return f"No OpenSanctions hits for: {query}"
            lines = [f"## OpenSanctions: {query} ({total:,} hits)\n"]
            for e in res:
                caption = e.get("caption", "")
                eid = e.get("id", "")
                sch = e.get("schema", "")
                datasets = ", ".join(e.get("datasets", [])[:5])
                topics = ", ".join((e.get("properties", {}) or {}).get("topics", [])[:4])
                countries = ", ".join((e.get("properties", {}) or {}).get("country", [])[:3])
                birth = ", ".join((e.get("properties", {}) or {}).get("birthDate", [])[:1])
                lines.append(f"**{caption}** [{sch}]")
                if countries:
                    lines.append(f"   country: {countries}")
                if birth:
                    lines.append(f"   born: {birth}")
                if topics:
                    lines.append(f"   topics: {topics}")
                if datasets:
                    lines.append(f"   sources: {datasets}")
                lines.append(f"   🔗 https://www.opensanctions.org/entities/{eid}/\n")
            return "\n".join(lines)
        except Exception as e:
            return f"OpenSanctions error: {e}"

    async def statistics(self, __user__: Optional[dict] = None) -> str:
        """
        Corpus-wide stats for OpenSanctions.
        :return: Total entities and breakdown by schema / country
        """
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(f"{BASE}/statistics", headers=self._headers())
                r.raise_for_status()
                data = r.json()
            lines = ["## OpenSanctions Statistics\n",
                     f"**Entities:** {data.get('entities', 0):,}",
                     f"**Datasets:** {data.get('datasets', 0):,}"]
            targets = data.get("things", {}).get("total", {})
            if targets:
                lines.append(f"**Targets:** {targets.get('count', 0):,}")
            return "\n".join(lines)
        except Exception as e:
            return f"OpenSanctions error: {e}"
