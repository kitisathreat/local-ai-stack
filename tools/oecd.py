"""
title: OECD Data — Statistics Across Member Economies
author: local-ai-stack
description: Fetch OECD statistics via the SDMX-JSON API. GDP, CPI, trade, labour, health, education, environment across 38 OECD members + partners. No API key required.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from pydantic import BaseModel, Field
from typing import Optional


BASE = "https://sdmx.oecd.org/public/rest/data"


class Tools:
    class Valves(BaseModel):
        MAX_ROWS: int = Field(default=60, description="Max observations")

    def __init__(self):
        self.valves = self.Valves()

    async def data(
        self,
        dataflow: str,
        key: str = "all",
        params: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Fetch an OECD dataflow via SDMX-JSON.
        :param dataflow: Dataflow code (e.g. "OECD.SDD.NAD,DSD_NAMAIN1@DF_QNA_EXPENDITURE_USD,1.1" or simple "QNA" - check data-explorer.oecd.org)
        :param key: SDMX key filter (e.g. "USA.B1GQ.LNBQRSA.Q" or "all")
        :param params: Extra query string (e.g. "startPeriod=2020&endPeriod=2024")
        :return: Observations table
        """
        url = f"{BASE}/{dataflow}/{key}"
        if params:
            url += ("?" + params) if "?" not in url else ("&" + params)
        try:
            async with httpx.AsyncClient(timeout=45) as client:
                r = await client.get(url, headers={"Accept": "application/vnd.sdmx.data+json;version=1.0.0-wd"})
                if r.status_code != 200:
                    return f"OECD HTTP {r.status_code}: {r.text[:300]}"
                data = r.json()
            datasets = data.get("data", {}).get("dataSets", [])
            structure = data.get("data", {}).get("structure", {})
            dims = structure.get("dimensions", {}).get("observation", []) + structure.get("dimensions", {}).get("series", [])
            if not datasets:
                return "OECD: no dataset returned."
            lines = [f"## OECD {dataflow}\n"]
            series = (datasets[0].get("series") or {})
            time_vals = []
            for d in structure.get("dimensions", {}).get("observation", []):
                if d.get("id", "").lower().startswith("time"):
                    time_vals = [v.get("id", "") for v in d.get("values", [])]
            lines.append("| Series key | Period | Value |\n|---|---|---|")
            count = 0
            for skey, sdata in series.items():
                obs = sdata.get("observations", {}) or {}
                for oidx, vals in obs.items():
                    try:
                        period = time_vals[int(oidx)] if time_vals and int(oidx) < len(time_vals) else oidx
                    except Exception:
                        period = oidx
                    value = vals[0] if vals else ""
                    lines.append(f"| {skey} | {period} | {value} |")
                    count += 1
                    if count >= self.valves.MAX_ROWS:
                        break
                if count >= self.valves.MAX_ROWS:
                    break
            return "\n".join(lines)
        except Exception as e:
            return f"OECD error: {e}"
