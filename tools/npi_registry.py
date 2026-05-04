"""
title: NPI Registry — US Healthcare Provider Directory
author: local-ai-stack
description: Look up National Provider Identifier (NPI) records from the US CMS NPPES public registry — clinicians, hospitals, group practices. Search by NPI, name, address, taxonomy, organization. No auth required.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel, Field


_API = "https://npiregistry.cms.hhs.gov/api/"


class Tools:
    class Valves(BaseModel):
        TIMEOUT_SEC: int = Field(default=15, description="Per-request timeout.")
        DEFAULT_LIMIT: int = Field(default=10, description="Default page size when caller omits it.")

    def __init__(self) -> None:
        self.valves = self.Valves()

    async def _query(self, **params: Any) -> dict:
        params["version"] = "2.1"
        params["pretty"] = "false"
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT_SEC) as c:
            r = await c.get(_API, params={k: v for k, v in params.items() if v not in (None, "")})
        if r.status_code >= 400:
            raise RuntimeError(f"NPI Registry -> {r.status_code}: {r.text[:300]}")
        return r.json()

    async def lookup_npi(self, npi: str) -> str:
        """Fetch a single provider record by NPI number (10-digit).

        :param npi: NPI number, 10 digits.
        """
        body = await self._query(number=npi)
        return _format(body)

    async def search_individual(
        self,
        first_name: str = "",
        last_name: str = "",
        state: str = "",
        city: str = "",
        taxonomy: str = "",
        limit: int = 0,
    ) -> str:
        """Search for individual clinicians (NPI-1).

        :param first_name: Wildcard supported with trailing *.
        :param last_name: Wildcard supported with trailing *.
        :param state: Two-letter state code.
        :param city: City name.
        :param taxonomy: Taxonomy description (e.g. "Internal Medicine"). Wildcard *.
        :param limit: 1-200.
        """
        body = await self._query(
            enumeration_type="NPI-1",
            first_name=first_name,
            last_name=last_name,
            state=state,
            city=city,
            taxonomy_description=taxonomy,
            limit=_limit(limit, self.valves.DEFAULT_LIMIT),
        )
        return _format(body)

    async def search_organization(
        self,
        name: str = "",
        state: str = "",
        city: str = "",
        taxonomy: str = "",
        limit: int = 0,
    ) -> str:
        """Search for organizations (NPI-2): hospitals, group practices, clinics.

        :param name: Organization name. Wildcard *.
        :param state: Two-letter state code.
        :param city: City name.
        :param taxonomy: Taxonomy description (e.g. "Hospital").
        :param limit: 1-200.
        """
        body = await self._query(
            enumeration_type="NPI-2",
            organization_name=name,
            state=state,
            city=city,
            taxonomy_description=taxonomy,
            limit=_limit(limit, self.valves.DEFAULT_LIMIT),
        )
        return _format(body)

    async def search_by_address(
        self,
        postal_code: str,
        taxonomy: str = "",
        limit: int = 0,
    ) -> str:
        """Search providers in a US ZIP code, optionally by taxonomy.

        :param postal_code: Five-digit ZIP. ZIP+4 also accepted.
        :param taxonomy: Optional taxonomy description.
        :param limit: 1-200.
        """
        body = await self._query(
            postal_code=postal_code,
            taxonomy_description=taxonomy,
            limit=_limit(limit, self.valves.DEFAULT_LIMIT),
        )
        return _format(body)


def _limit(value: int, default: int) -> int:
    n = int(value) if value else default
    return min(max(n, 1), 200)


def _format(body: dict) -> str:
    if body.get("Errors"):
        return "Errors: " + "; ".join(e.get("description", "") for e in body["Errors"])
    results = body.get("results", [])
    if not results:
        return "No matches."
    out = []
    for r in results:
        npi = r.get("number")
        basic = r.get("basic", {}) or {}
        kind = r.get("enumeration_type")
        if kind == "NPI-2":
            name = basic.get("organization_name", "(no name)")
        else:
            name = " ".join(filter(None, [basic.get("first_name"), basic.get("last_name")])) or "(no name)"
        addresses = r.get("addresses", []) or []
        primary = next((a for a in addresses if a.get("address_purpose") == "LOCATION"), addresses[0] if addresses else {})
        loc = ", ".join(filter(None, [primary.get("city"), primary.get("state"), primary.get("postal_code")]))
        taxonomy = next((t.get("desc") for t in (r.get("taxonomies") or []) if t.get("primary")), "")
        out.append(f"- NPI {npi}  {name}  ({taxonomy})  {loc}")
    return "\n".join(out)
