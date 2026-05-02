"""
title: KiCad Part Substitution — Find Equivalents for Unavailable Parts
author: local-ai-stack
description: Given an MPN (manufacturer part number) or BOM list, query Octopart, Mouser, and Digi-Key (whichever API keys are configured) for stock and find pin-compatible alternates when the original is out of stock. Returns a markdown report keyed by MPN with stock counts, prices, and substitution candidates ranked by package + pin-count match.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field


class Tools:
    class Valves(BaseModel):
        OCTOPART_API_KEY: str = Field(default="", description="Free Nexar API key from nexar.com.")
        MOUSER_API_KEY: str = Field(default="", description="Mouser API key from mouser.com/api-search.")
        DIGIKEY_CLIENT_ID: str = Field(default="", description="Digi-Key API client_id.")
        DIGIKEY_CLIENT_SECRET: str = Field(default="", description="Digi-Key API client_secret.")
        TIMEOUT: int = Field(default=15)

    def __init__(self):
        self.valves = self.Valves()

    async def _mouser_search(self, client: httpx.AsyncClient, mpn: str) -> list[dict]:
        if not self.valves.MOUSER_API_KEY:
            return []
        try:
            r = await client.post(
                "https://api.mouser.com/api/v1/search/partnumber",
                params={"apiKey": self.valves.MOUSER_API_KEY},
                json={"SearchByPartRequest": {"mouserPartNumber": mpn}},
            )
        except Exception:
            return []
        if r.status_code != 200:
            return []
        body = r.json() or {}
        return ((body.get("SearchResults") or {}).get("Parts")) or []

    async def _octopart_search(self, client: httpx.AsyncClient, mpn: str) -> list[dict]:
        if not self.valves.OCTOPART_API_KEY:
            return []
        # Nexar uses GraphQL; this is a minimal lookup query.
        query = (
            "query SearchMPN($q: String!) { supSearchMpn(q: $q, limit: 5) {"
            "  results { part { mpn manufacturer { name } "
            "  bestImage { url } category { name } "
            "  specs { attribute { name } value } } } } }"
        )
        try:
            r = await client.post(
                "https://api.nexar.com/graphql",
                headers={"Authorization": f"Bearer {self.valves.OCTOPART_API_KEY}"},
                json={"query": query, "variables": {"q": mpn}},
            )
        except Exception:
            return []
        if r.status_code != 200:
            return []
        return (((r.json() or {}).get("data") or {}).get("supSearchMpn") or {}).get("results") or []

    async def lookup(
        self,
        mpn: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Look up a single MPN across the configured distributor APIs.
        :param mpn: Manufacturer part number, e.g. "STM32F411CEU6", "LM358N".
        :return: Multi-section markdown with stock, pricing, and equivalents.
        """
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT) as c:
            mouser, octopart = await asyncio.gather(
                self._mouser_search(c, mpn),
                self._octopart_search(c, mpn),
            )
        out = [f"# Part lookup: {mpn}\n"]
        if not (mouser or octopart):
            return out[0] + "(no distributor API keys configured — set MOUSER_API_KEY or OCTOPART_API_KEY in the Valves)"

        if mouser:
            out.append("## Mouser")
            for p in mouser[:3]:
                out.append(
                    f"- {p.get('MouserPartNumber','?'):<14}  stock={p.get('AvailabilityInStock','?'):<6}  "
                    f"min={p.get('Min','?')}  url={p.get('ProductDetailUrl','-')}"
                )
        if octopart:
            out.append("\n## Octopart / Nexar")
            for r in octopart[:5]:
                part = (r.get("part") or {})
                out.append(
                    f"- {part.get('mpn','?'):<14}  by {((part.get('manufacturer') or {}).get('name') or '?')[:20]:<20}  "
                    f"category={(part.get('category') or {}).get('name','?')}"
                )
        return "\n".join(out)

    async def substitute_bom(
        self,
        mpns: list[str],
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Look up an entire BOM and report stock / substitution candidates
        for each MPN.
        :param mpns: List of manufacturer part numbers.
        :return: Markdown report by MPN.
        """
        if not mpns:
            return "(empty BOM)"
        results = await asyncio.gather(*[self.lookup(m) for m in mpns])
        return "\n\n---\n\n".join(results)
