"""
title: BLS — U.S. Bureau of Labor Statistics
author: local-ai-stack
description: Fetch US unemployment, CPI inflation, employment (CES), wages, productivity, and more via the BLS Public Data API v2. Free API key required (instant signup for higher rate limits).
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import os
import httpx
from pydantic import BaseModel, Field
from typing import Optional, List


BASE = "https://api.bls.gov/publicAPI/v2/timeseries/data"


class Tools:
    class Valves(BaseModel):
        API_KEY: str = Field(default_factory=lambda: os.environ.get("BLS_API_KEY", ""), description="BLS registration key (free at https://data.bls.gov/registrationEngine/)")

    def __init__(self):
        self.valves = self.Valves()

    async def series(
        self,
        series_ids: str,
        start_year: int,
        end_year: int,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Fetch one or more BLS time series.
        :param series_ids: Comma-separated series IDs (e.g. "LNS14000000" (unemployment rate), "CUUR0000SA0" (CPI-U all items))
        :param start_year: Start year
        :param end_year: End year
        :return: Table of periods and values per series
        """
        payload = {
            "seriesid": [s.strip() for s in series_ids.split(",")],
            "startyear": str(start_year),
            "endyear": str(end_year),
        }
        if self.valves.API_KEY:
            payload["registrationkey"] = self.valves.API_KEY
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(BASE, json=payload)
                r.raise_for_status()
                data = r.json()
            if data.get("status") != "REQUEST_SUCCEEDED":
                return f"BLS error: {data.get('message', 'unknown')}"
            results = data.get("Results", {}).get("series", [])
            lines = [f"## BLS Series {start_year}–{end_year}\n"]
            for s in results:
                sid = s.get("seriesID", "")
                rows = s.get("data", [])
                lines.append(f"### {sid}\n")
                lines.append("| Period | Value |\n|---|---|")
                for row in rows[:30]:
                    period = f"{row.get('year','')} {row.get('periodName','')}"
                    lines.append(f"| {period} | {row.get('value','')} |")
                lines.append("")
            return "\n".join(lines)
        except Exception as e:
            return f"BLS error: {e}"

    async def common(self, indicator: str, year: int = 0, __user__: Optional[dict] = None) -> str:
        """
        Shortcut for common US BLS headline series.
        :param indicator: "unemployment", "cpi", "cpi_core", "payrolls", "wages", "productivity"
        :param year: Optional year (defaults to 5-year window ending current year)
        :return: Recent values
        """
        sid_map = {
            "unemployment": "LNS14000000",
            "cpi": "CUUR0000SA0",
            "cpi_core": "CUUR0000SA0L1E",
            "payrolls": "CES0000000001",
            "wages": "CES0500000003",
            "productivity": "PRS85006092",
        }
        sid = sid_map.get(indicator.lower())
        if not sid:
            return f"Unknown indicator: {indicator}. Choose from {list(sid_map)}"
        import datetime
        now = datetime.date.today().year
        end = year or now
        start = end - 5
        return await self.series(sid, start, end)
