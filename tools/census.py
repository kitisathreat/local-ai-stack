"""
title: US Census Bureau — Demographic & Economic Data
author: local-ai-stack
description: US Census Bureau open data: American Community Survey (ACS), Decennial Census, Population Estimates, Economic Indicators, Small Area Income & Poverty. Free API key required (instant signup).
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from pydantic import BaseModel, Field
from typing import Optional


BASE = "https://api.census.gov/data"


class Tools:
    class Valves(BaseModel):
        API_KEY: str = Field(default="", description="US Census API key (free at https://api.census.gov/data/key_signup.html)")

    def __init__(self):
        self.valves = self.Valves()

    async def acs5(
        self,
        year: int,
        variables: str,
        for_geo: str = "state:*",
        in_geo: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        ACS 5-year estimates — deep demographic & economic detail.
        :param year: ACS 5-year vintage (e.g. 2022)
        :param variables: Comma-separated variable codes (e.g. "NAME,B01001_001E,B19013_001E" — population, median HH income)
        :param for_geo: Geographic target (default "state:*"; use "county:*", "place:*", "tract:*", etc.)
        :param in_geo: Optional parent scope (e.g. "state:06" for CA)
        :return: Table of variables by geography
        """
        params = {"get": variables, "for": for_geo}
        if in_geo:
            params["in"] = in_geo
        if self.valves.API_KEY:
            params["key"] = self.valves.API_KEY
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(f"{BASE}/{year}/acs/acs5", params=params)
                if r.status_code != 200:
                    return f"Census API returned HTTP {r.status_code}: {r.text[:300]}"
                rows = r.json()
            if not rows or len(rows) < 2:
                return "No Census data returned."
            header = rows[0]
            body = rows[1:100]
            lines = [f"## Census ACS5 {year}\n"]
            lines.append("| " + " | ".join(header) + " |")
            lines.append("|" + "|".join("---" for _ in header) + "|")
            for row in body:
                lines.append("| " + " | ".join(str(c) for c in row) + " |")
            if len(rows) > 101:
                lines.append(f"\n_Truncated to first 100 of {len(rows)-1} rows._")
            return "\n".join(lines)
        except Exception as e:
            return f"Census error: {e}"

    async def population_by_state(
        self,
        year: int = 2022,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Quick: population for every US state from ACS 5-year.
        :param year: Vintage year
        :return: Ranked state population table
        """
        return await self.acs5(year, "NAME,B01001_001E", "state:*")
