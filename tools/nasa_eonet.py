"""
title: NASA EONET — Earth Observatory Natural Event Tracker
author: local-ai-stack
description: Live feed of natural events (wildfires, storms, volcanoes, floods, icebergs, sea/lake ice, dust/haze, earthquakes) worldwide. Data from NASA's Earth Observatory. No API key required.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from pydantic import BaseModel, Field
from typing import Optional


BASE = "https://eonet.gsfc.nasa.gov/api/v3"


class Tools:
    class Valves(BaseModel):
        LIMIT: int = Field(default=25, description="Max events to list")
        DAYS: int = Field(default=30, description="Lookback window in days")

    def __init__(self):
        self.valves = self.Valves()

    async def list_events(
        self,
        category: str = "",
        status: str = "open",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        List recent natural events from NASA EONET.
        :param category: Optional category: wildfires, severeStorms, volcanoes, floods, drought, seaLakeIce, earthquakes, landslides, snow, dustHaze, manmade, tempExtremes, waterColor
        :param status: "open" (ongoing), "closed" (ended), or "all"
        :return: Event titles, dates, categories, and sources
        """
        params = {"limit": self.valves.LIMIT, "days": self.valves.DAYS, "status": status}
        if category:
            params["category"] = category
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(f"{BASE}/events", params=params)
                r.raise_for_status()
                data = r.json()
            events = data.get("events", [])
            if not events:
                return "No EONET events match filters."
            lines = [f"## NASA EONET Events ({status}, last {self.valves.DAYS} d)\n"]
            for e in events:
                title = e.get("title", "")
                cats = ", ".join(c.get("title", "") for c in e.get("categories", []))
                geos = e.get("geometry", [])
                coords = ""
                if geos:
                    g = geos[-1]
                    c = g.get("coordinates", [])
                    d = g.get("date", "")
                    if isinstance(c, list) and len(c) >= 2 and isinstance(c[0], (int, float)):
                        coords = f"{c[1]:.2f},{c[0]:.2f} @ {d[:10]}"
                srcs = ", ".join(s.get("id", "") for s in e.get("sources", [])[:3])
                link = e.get("link", "")
                lines.append(f"- **{title}** [{cats}]")
                if coords:
                    lines.append(f"    {coords}")
                if srcs:
                    lines.append(f"    sources: {srcs}")
                if link:
                    lines.append(f"    🔗 {link}")
            return "\n".join(lines)
        except Exception as e:
            return f"EONET error: {e}"

    async def categories(self, __user__: Optional[dict] = None) -> str:
        """
        List all EONET event categories and their event counts.
        :return: Category table
        """
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(f"{BASE}/categories")
                r.raise_for_status()
                cats = r.json().get("categories", [])
            lines = ["## EONET Categories\n"]
            for c in cats:
                lines.append(f"- **{c.get('title', '')}** (`{c.get('id', '')}`) — {c.get('description', '')[:100]}")
            return "\n".join(lines)
        except Exception as e:
            return f"EONET error: {e}"
