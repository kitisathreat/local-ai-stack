"""
title: REST Countries — Country Data
author: local-ai-stack
description: Free country information API. Population, capital, currencies, languages, flags, borders, timezones, calling codes for 250+ countries. No API key required.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from pydantic import BaseModel, Field
from typing import Callable, Any, Optional


BASE = "https://restcountries.com/v3.1"


def _fmt(c: dict) -> str:
    name = c.get("name", {}).get("common", "Unknown")
    official = c.get("name", {}).get("official", "")
    cap = ", ".join(c.get("capital", [])) or "—"
    region = c.get("region", "")
    subregion = c.get("subregion", "")
    pop = c.get("population", 0)
    area = c.get("area", 0)
    langs = ", ".join((c.get("languages") or {}).values()) or "—"
    currs = ", ".join(f"{v.get('name','')} ({k})" for k, v in (c.get("currencies") or {}).items()) or "—"
    cca2 = c.get("cca2", "")
    cca3 = c.get("cca3", "")
    tlds = ", ".join(c.get("tld", [])) or "—"
    tz = ", ".join(c.get("timezones", [])[:3]) + ("..." if len(c.get("timezones", [])) > 3 else "")
    borders = ", ".join(c.get("borders", [])) or "None (island/coast)"
    calling = (c.get("idd", {}).get("root", "") or "") + (c.get("idd", {}).get("suffixes", [""])[0] if c.get("idd", {}).get("suffixes") else "")
    flag = c.get("flag", "")
    maps = c.get("maps", {}).get("googleMaps", "")
    return (
        f"## {flag} {name} ({cca2}/{cca3})\n"
        f"**Official:** {official}\n"
        f"**Capital:** {cap}   **Region:** {region} / {subregion}\n"
        f"**Population:** {pop:,}   **Area:** {area:,.0f} km²\n"
        f"**Languages:** {langs}\n"
        f"**Currencies:** {currs}\n"
        f"**Calling code:** {calling}   **TLD:** {tlds}\n"
        f"**Timezones:** {tz}\n"
        f"**Borders:** {borders}\n"
        + (f"🔗 {maps}\n" if maps else "")
    )


class Tools:
    class Valves(BaseModel):
        MAX_RESULTS: int = Field(default=5, description="Maximum results in list views")

    def __init__(self):
        self.valves = self.Valves()

    async def country(self, name: str, __user__: Optional[dict] = None) -> str:
        """
        Look up a country by name, code, or common alias.
        :param name: Country name or ISO code (e.g. "Japan", "US", "DEU", "kingdom of spain")
        :return: Full country profile
        """
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(f"{BASE}/name/{name}")
                if r.status_code == 404:
                    r = await client.get(f"{BASE}/alpha/{name}")
                r.raise_for_status()
                data = r.json()
            if not data:
                return f"No country found for: {name}"
            return _fmt(data[0])
        except Exception as e:
            return f"REST Countries error: {e}"

    async def countries_by_region(
        self, region: str, __user__: Optional[dict] = None,
    ) -> str:
        """
        List countries in a region or subregion (e.g. "Europe", "Caribbean", "Southeast Asia").
        :param region: Region or subregion name
        :return: Summary list of countries in that region
        """
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(f"{BASE}/region/{region}")
                if r.status_code == 404:
                    r = await client.get(f"{BASE}/subregion/{region}")
                r.raise_for_status()
                data = r.json()
            if not data:
                return f"No countries found in region: {region}"
            data.sort(key=lambda c: c.get("population", 0), reverse=True)
            lines = [f"## Countries in {region}\n"]
            for c in data[: self.valves.MAX_RESULTS * 4]:
                name = c.get("name", {}).get("common", "?")
                pop = c.get("population", 0)
                cap = ", ".join(c.get("capital", [])) or "—"
                lines.append(f"- **{name}** — cap {cap}, pop {pop:,}")
            return "\n".join(lines)
        except Exception as e:
            return f"REST Countries error: {e}"

    async def countries_by_currency(
        self, code: str, __user__: Optional[dict] = None,
    ) -> str:
        """
        Find countries that use a given currency (e.g. EUR, USD, XOF).
        :param code: ISO 4217 currency code
        :return: Country names using that currency
        """
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(f"{BASE}/currency/{code}")
                r.raise_for_status()
                data = r.json()
            names = [c.get("name", {}).get("common", "?") for c in data]
            return f"**{len(names)} countries use {code.upper()}:**\n" + ", ".join(sorted(names))
        except Exception as e:
            return f"REST Countries error: {e}"
