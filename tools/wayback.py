"""
title: Wayback Machine — Historical Web Archive
author: local-ai-stack
description: Query the Internet Archive's Wayback Machine. Find archived snapshots of URLs, look up the closest snapshot to a date, and list historical captures. No API key required.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from pydantic import BaseModel, Field
from typing import Callable, Any, Optional


AVAIL = "https://archive.org/wayback/available"
CDX = "https://web.archive.org/cdx/search/cdx"


class Tools:
    class Valves(BaseModel):
        MAX_RESULTS: int = Field(default=10, description="Max history snapshots")

    def __init__(self):
        self.valves = self.Valves()

    async def closest_snapshot(
        self,
        url: str,
        timestamp: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Find the archived snapshot of a URL closest to a date.
        :param url: Target URL (e.g. "nytimes.com")
        :param timestamp: Optional YYYYMMDD or YYYY — closest snapshot to that date (default: most recent)
        :return: Wayback URL, snapshot time, and HTTP status
        """
        params = {"url": url}
        if timestamp:
            params["timestamp"] = timestamp
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(AVAIL, params=params)
                r.raise_for_status()
                data = r.json()
            snap = data.get("archived_snapshots", {}).get("closest")
            if not snap or not snap.get("available"):
                return f"No Wayback snapshot found for: {url}"
            return (
                f"## Wayback Snapshot: {url}\n"
                f"**Captured:** {snap.get('timestamp', '')}\n"
                f"**Status:** HTTP {snap.get('status', '?')}\n"
                f"🔗 {snap.get('url', '')}"
            )
        except Exception as e:
            return f"Wayback error: {e}"

    async def history(
        self,
        url: str,
        from_date: str = "",
        to_date: str = "",
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        List archived captures of a URL over time using the CDX API.
        :param url: Target URL
        :param from_date: Optional start date YYYY or YYYYMMDD
        :param to_date: Optional end date YYYY or YYYYMMDD
        :return: Timeline of snapshots with timestamp and status
        """
        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Fetching history: {url}", "done": False}})
        params = {"url": url, "output": "json", "limit": self.valves.MAX_RESULTS, "collapse": "timestamp:6"}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(CDX, params=params)
                r.raise_for_status()
                rows = r.json()
            if len(rows) < 2:
                return f"No captures found for: {url}"
            headers = rows[0]
            idx_ts = headers.index("timestamp")
            idx_st = headers.index("statuscode")
            idx_ur = headers.index("original")
            lines = [f"## Wayback History: {url}\n", "| Timestamp | Status | Wayback URL |", "|---|---|---|"]
            for row in rows[1:]:
                ts = row[idx_ts]
                pretty = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"
                lines.append(f"| {pretty} | {row[idx_st]} | https://web.archive.org/web/{ts}/{row[idx_ur]} |")
            return "\n".join(lines)
        except Exception as e:
            return f"Wayback error: {e}"

    async def save_url(
        self,
        url: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Ask the Wayback Machine to archive a URL right now (Save Page Now).
        :param url: URL to archive
        :return: Resulting archive URL or status
        """
        try:
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                r = await client.get(f"https://web.archive.org/save/{url}")
            loc = r.headers.get("Content-Location") or r.headers.get("Location") or ""
            if loc:
                return f"Saved: https://web.archive.org{loc}"
            return f"Save request returned HTTP {r.status_code}. Try the URL directly: https://web.archive.org/save/{url}"
        except Exception as e:
            return f"Wayback Save error: {e}"
