"""
title: OpenAQ — Global Air Quality Data
author: local-ai-stack
description: Real-time and historical air quality measurements (PM2.5, PM10, NO2, O3, SO2, CO, BC) from 17,000+ monitoring stations in 100+ countries. Aggregated from government and research sources. No API key required (public tier).
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from pydantic import BaseModel, Field
from typing import Optional


BASE = "https://api.openaq.org/v2"


class Tools:
    class Valves(BaseModel):
        LIMIT: int = Field(default=20, description="Max rows per query")
        API_KEY: str = Field(default="", description="Optional OpenAQ API key for higher limits")

    def __init__(self):
        self.valves = self.Valves()

    def _headers(self) -> dict:
        h = {"User-Agent": "local-ai-stack/1.0"}
        if self.valves.API_KEY:
            h["X-API-Key"] = self.valves.API_KEY
        return h

    async def latest(
        self,
        city: str = "",
        country: str = "",
        parameter: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Latest air quality readings for a city or country.
        :param city: City name (e.g. "Delhi", "Los Angeles")
        :param country: ISO country code (e.g. "IN", "US")
        :param parameter: Optional pollutant (pm25, pm10, no2, o3, so2, co, bc)
        :return: Latest measurements by station
        """
        params = {"limit": self.valves.LIMIT, "order_by": "lastUpdated", "sort": "desc"}
        if city: params["city"] = city
        if country: params["country"] = country.upper()
        if parameter: params["parameter"] = parameter
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(f"{BASE}/latest", params=params, headers=self._headers())
                r.raise_for_status()
                data = r.json()
            rows = data.get("results", [])
            if not rows:
                return f"No OpenAQ readings for city={city} country={country}"
            lines = [f"## OpenAQ Latest — {city or country}\n"]
            for loc in rows:
                name = loc.get("location", "")
                cc = loc.get("country", "")
                cty = loc.get("city", "")
                meas = loc.get("measurements", [])
                lines.append(f"**{name}** ({cty}, {cc})")
                for m in meas[:8]:
                    p = m.get("parameter", "")
                    v = m.get("value", "")
                    u = m.get("unit", "")
                    t = m.get("lastUpdated", "")
                    lines.append(f"   - {p.upper():<5} {v} {u}  @ {t[:16]}")
                lines.append("")
            return "\n".join(lines)
        except Exception as e:
            return f"OpenAQ error: {e}"

    async def stations(
        self,
        country: str,
        city: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        List monitoring stations in a country (optionally filtered to a city).
        :param country: ISO country code
        :param city: Optional city name
        :return: Station names, coordinates, and parameters monitored
        """
        params = {"limit": self.valves.LIMIT, "country": country.upper()}
        if city: params["city"] = city
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(f"{BASE}/locations", params=params, headers=self._headers())
                r.raise_for_status()
                data = r.json()
            rows = data.get("results", [])
            if not rows:
                return f"No stations for {country}/{city}"
            lines = [f"## OpenAQ Stations — {country.upper()}" + (f" / {city}" if city else "") + "\n"]
            for s in rows:
                name = s.get("name", "")
                cty = s.get("city", "")
                coords = s.get("coordinates") or {}
                params_ = ", ".join(p.get("parameter", "") for p in s.get("parameters", [])[:6])
                lines.append(f"- **{name}** ({cty}) — {coords.get('latitude')},{coords.get('longitude')} [{params_}]")
            return "\n".join(lines)
        except Exception as e:
            return f"OpenAQ error: {e}"

    async def countries(self, __user__: Optional[dict] = None) -> str:
        """
        List all countries with OpenAQ data and their measurement counts.
        :return: Country list with station & measurement counts
        """
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(f"{BASE}/countries", params={"limit": 200}, headers=self._headers())
                r.raise_for_status()
                data = r.json()
            rows = data.get("results", [])
            rows.sort(key=lambda x: x.get("count", 0), reverse=True)
            lines = ["## OpenAQ Countries\n", "| Code | Country | Stations | Measurements |", "|---|---|---|---|"]
            for c in rows[:50]:
                lines.append(f"| {c.get('code','')} | {c.get('name','')} | {c.get('locations', 0):,} | {c.get('count', 0):,} |")
            return "\n".join(lines)
        except Exception as e:
            return f"OpenAQ error: {e}"
