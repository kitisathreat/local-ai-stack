"""
title: IMF Data — International Monetary Fund Statistics
author: local-ai-stack
description: Access IMF macro data: International Financial Statistics (IFS), World Economic Outlook (WEO), Balance of Payments, Government Finance Statistics. Via the SDMX JSON API. No API key required.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from pydantic import BaseModel, Field
from typing import Optional


BASE = "https://www.imf.org/external/datamapper/api/v1"


class Tools:
    class Valves(BaseModel):
        DEFAULT_INDICATOR: str = Field(default="NGDPD", description="Default indicator (NGDPD=GDP $B, PCPIPCH=Inflation, LUR=Unemployment)")

    def __init__(self):
        self.valves = self.Valves()

    async def indicators(self, __user__: Optional[dict] = None) -> str:
        """
        List available IMF Datamapper indicators.
        :return: Indicators with code, description, and dataset
        """
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(f"{BASE}/indicators")
                r.raise_for_status()
                data = r.json()
            inds = data.get("indicators", {})
            lines = ["## IMF Indicators (first 40)\n", "| Code | Label | Unit |", "|---|---|---|"]
            for k, v in list(inds.items())[:40]:
                lines.append(f"| `{k}` | {v.get('label','')} | {v.get('unit','')} |")
            return "\n".join(lines)
        except Exception as e:
            return f"IMF error: {e}"

    async def series(
        self,
        indicator: str = "",
        countries: str = "USA,CHN,DEU,JPN",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Fetch time series values for countries.
        :param indicator: Indicator code (e.g. NGDPD, PCPIPCH, LUR, GGXWDG_NGDP, BCA_NGDPD)
        :param countries: Comma-separated ISO-3 country codes (default: major economies)
        :return: Year-by-year table of values
        """
        ind = indicator or self.valves.DEFAULT_INDICATOR
        url = f"{BASE}/{ind}/" + "/".join(c.strip().upper() for c in countries.split(","))
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(url)
                r.raise_for_status()
                data = r.json()
            vals = (data.get("values") or {}).get(ind, {})
            if not vals:
                return f"No IMF data for {ind} / {countries}"
            years = sorted({y for c, series in vals.items() for y in series.keys()})
            years = years[-20:]  # last 20 years
            countries_list = list(vals.keys())
            header = "| Year | " + " | ".join(countries_list) + " |"
            sep = "|---|" + "|".join("---" for _ in countries_list) + "|"
            lines = [f"## IMF {ind}\n", header, sep]
            for y in years:
                row = ["{:>6}".format(y)]
                for c in countries_list:
                    v = vals.get(c, {}).get(y, "")
                    row.append(f"{v:,.2f}" if isinstance(v, (int, float)) else str(v))
                lines.append("| " + " | ".join(row) + " |")
            return "\n".join(lines)
        except Exception as e:
            return f"IMF error: {e}"

    async def country_profile(
        self,
        country: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Headline macro snapshot for a single country (GDP, GDP per capita, inflation, unemployment, debt, current account).
        :param country: ISO-3 code (e.g. "USA", "CHN", "DEU")
        :return: Most recent values across key IMF series
        """
        country = country.strip().upper()
        indicators = {
            "NGDPD": "GDP ($ bn)",
            "NGDPDPC": "GDP per capita ($)",
            "PCPIPCH": "Inflation (% YoY)",
            "LUR": "Unemployment (%)",
            "GGXWDG_NGDP": "Govt debt (% GDP)",
            "BCA_NGDPD": "Current account (% GDP)",
        }
        lines = [f"## IMF Profile: {country}\n", "| Indicator | Latest year | Value |", "|---|---|---|"]
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                for code, label in indicators.items():
                    r = await client.get(f"{BASE}/{code}/{country}")
                    if r.status_code != 200:
                        continue
                    data = r.json().get("values", {}).get(code, {}).get(country, {})
                    if not data:
                        continue
                    latest_y = max(data.keys())
                    v = data[latest_y]
                    lines.append(f"| {label} | {latest_y} | {v:,.2f} |" if isinstance(v, (int, float)) else f"| {label} | {latest_y} | {v} |")
            return "\n".join(lines)
        except Exception as e:
            return f"IMF error: {e}"
