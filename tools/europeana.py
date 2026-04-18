"""
title: Europeana — European Cultural Heritage
author: local-ai-stack
description: Search 50+ million cultural heritage objects from 2,500+ European museums, archives, libraries, and galleries. Discover paintings, manuscripts, photographs, maps, audio recordings, and 3D objects from institutions across Europe. Filter by country, time period, media type, and rights status. Free API key at pro.europeana.eu.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import os
import httpx
from typing import Callable, Any, Optional
from pydantic import BaseModel, Field

BASE = "https://api.europeana.eu/record/v2"


class Tools:
    class Valves(BaseModel):
        EUROPEANA_API_KEY: str = Field(
            default_factory=lambda: os.environ.get("EUROPEANA_API_KEY", ""),
            description="Europeana API key — free at https://pro.europeana.eu/page/apis",
        )

    def __init__(self):
        self.valves = self.Valves()

    def _check_key(self) -> Optional[str]:
        if not self.valves.EUROPEANA_API_KEY:
            return (
                "Europeana API key required.\n"
                "Register free at: https://pro.europeana.eu/page/apis\n"
                "Add it in Open WebUI > Tools > Europeana > EUROPEANA_API_KEY"
            )
        return None

    async def search(
        self,
        query: str,
        media_type: str = "",
        country: str = "",
        year_from: int = 0,
        year_to: int = 0,
        open_access_only: bool = False,
        limit: int = 10,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search Europeana for cultural heritage objects: paintings, manuscripts, maps, photos, music, 3D objects.
        :param query: Search terms (e.g. 'Napoleon Bonaparte', 'Gothic cathedral', 'World War I poster', 'ancient Rome map')
        :param media_type: Filter by type: 'IMAGE', 'TEXT', 'VIDEO', 'AUDIO', '3D'
        :param country: Filter by providing institution country (e.g. 'France', 'Germany', 'Netherlands', 'Italy')
        :param year_from: Filter by earliest year (e.g. 1400)
        :param year_to: Filter by latest year (e.g. 1900)
        :param open_access_only: If True, show only freely reusable items
        :param limit: Number of results (max 50)
        :return: Cultural objects with title, institution, date, type, and Europeana URL
        """
        err = self._check_key()
        if err:
            return err

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Searching Europeana for '{query}'...", "done": False}})

        params = {
            "wskey": self.valves.EUROPEANA_API_KEY,
            "query": query,
            "rows": min(limit, 50),
            "profile": "rich",
            "sort": "score desc",
        }

        # Refinements (qf filters)
        qf = []
        if media_type:
            qf.append(f"TYPE:{media_type.upper()}")
        if country:
            qf.append(f"COUNTRY:{country.lower()}")
        if open_access_only:
            qf.append("RIGHTS:*open*")
        if year_from and year_to:
            qf.append(f"YEAR:[{year_from} TO {year_to}]")
        elif year_from:
            qf.append(f"YEAR:[{year_from} TO *]")
        elif year_to:
            qf.append(f"YEAR:[* TO {year_to}]")

        if qf:
            params["qf"] = qf

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(f"{BASE}/search.json", params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            return f"Europeana error: {str(e)}"

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        total = data.get("totalResults", 0)
        items = data.get("items", [])

        if not items:
            return f"No Europeana results for '{query}'. Try broader search terms."

        lines = [f"## Europeana: '{query}'\n"]
        filters = []
        if media_type:
            filters.append(f"Type: {media_type}")
        if country:
            filters.append(f"Country: {country}")
        if year_from or year_to:
            filters.append(f"Years: {year_from or '?'} – {year_to or 'present'}")
        if filters:
            lines.append(f"**Filters:** {' | '.join(filters)}")
        lines.append(f"**Total matches:** {total:,} | Showing {len(items)}\n")

        for item in items:
            title = (item.get("title") or ["Untitled"])[0][:80]
            item_type = item.get("type", "")
            year = item.get("year", [None])[0] if item.get("year") else None
            country_item = (item.get("country") or [""])[0]
            provider = (item.get("dataProvider") or ["Unknown"])[0][:50]
            eu_url = item.get("guid", "")
            thumbnail = item.get("edmPreview", [None])[0] if item.get("edmPreview") else None
            rights = (item.get("rights") or [""])[0]
            creator = (item.get("dcCreator") or [""])[0][:50] if item.get("dcCreator") else ""

            lines.append(f"### {title}")
            meta = []
            if creator:
                meta.append(f"**Creator:** {creator}")
            if year:
                meta.append(f"**Year:** {year}")
            if item_type:
                meta.append(f"**Type:** {item_type}")
            if provider:
                meta.append(f"**Institution:** {provider}")
            if country_item:
                meta.append(f"**Country:** {country_item.title()}")
            if meta:
                lines.append(" | ".join(meta))
            if rights and ("open" in rights.lower() or "public" in rights.lower()):
                lines.append("♻️ Open access")
            if eu_url:
                lines.append(f"🔗 [{eu_url}]({eu_url})")
            if thumbnail:
                lines.append(f"🖼️ Preview: {thumbnail}")
            lines.append("")

        return "\n".join(lines)

    async def get_record(
        self,
        record_id: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get detailed metadata for a specific Europeana record by its ID.
        :param record_id: Europeana record ID in format '/provider_id/object_id' (e.g. '/9200338/BibliographicResource_3000118435009')
        :return: Full metadata including description, creator, dates, subjects, and rights
        """
        err = self._check_key()
        if err:
            return err

        record_id = record_id.strip("/")

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{BASE}/{record_id}.json",
                    params={"wskey": self.valves.EUROPEANA_API_KEY, "profile": "full"},
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            return f"Europeana record error: {str(e)}"

        record = data.get("object", {})
        proxies = record.get("proxies", [{}])
        proxy = next((p for p in proxies if not p.get("europeanaProxy")), proxies[0] if proxies else {})
        aggregation = (record.get("aggregations") or [{}])[0]
        about = record.get("about", "")

        def get_field(d, key):
            val = d.get(key, {})
            if isinstance(val, dict):
                return (list(val.values()) or [[]])[0]
            return val or []

        title_vals = get_field(proxy, "dcTitle")
        desc_vals = get_field(proxy, "dcDescription")
        creator_vals = get_field(proxy, "dcCreator")
        date_vals = get_field(proxy, "dcDate") or get_field(proxy, "dctermsCreated")
        subject_vals = get_field(proxy, "dcSubject")
        type_vals = get_field(proxy, "dcType")
        rights_vals = aggregation.get("edmRights", {})
        provider = (aggregation.get("edmDataProvider") or {})

        def first(lst):
            return lst[0] if lst else ""

        lines = [f"## Europeana Record\n"]
        if title_vals:
            lines.append(f"### {first(title_vals)}")
        if creator_vals:
            lines.append(f"**Creator:** {', '.join(str(v) for v in creator_vals[:3])}")
        if date_vals:
            lines.append(f"**Date:** {', '.join(str(v) for v in date_vals[:3])}")
        if type_vals:
            lines.append(f"**Type:** {', '.join(str(v) for v in type_vals[:3])}")
        if subject_vals:
            lines.append(f"**Subjects:** {', '.join(str(v) for v in subject_vals[:8])}")
        if isinstance(provider, dict):
            prov_name = list(provider.values())[0] if provider else ""
            if prov_name:
                lines.append(f"**Institution:** {first(prov_name) if isinstance(prov_name, list) else prov_name}")
        if rights_vals:
            rights_url = list(rights_vals.values())[0] if isinstance(rights_vals, dict) else str(rights_vals)
            lines.append(f"**Rights:** {first(rights_url) if isinstance(rights_url, list) else rights_url}")
        lines.append(f"**Europeana:** https://www.europeana.eu/item/{record_id}")

        if desc_vals:
            lines.append(f"\n**Description:**\n{first(desc_vals)[:500]}")

        # Media
        web_resources = aggregation.get("webResources", [])
        for wr in web_resources[:3]:
            url = wr.get("about", "")
            if url:
                lines.append(f"\n**Media:** {url}")

        return "\n".join(lines)

    async def search_by_institution(
        self,
        institution: str,
        media_type: str = "",
        limit: int = 10,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Browse digitized collections from a specific museum, library, or archive on Europeana.
        :param institution: Institution name (e.g. 'Rijksmuseum', 'British Library', 'Louvre', 'Uffizi', 'Smithsonian')
        :param media_type: Filter by type: 'IMAGE', 'TEXT', 'VIDEO', 'AUDIO', '3D'
        :param limit: Number of results (max 50)
        :return: Items from that institution with titles, dates, and links
        """
        return await self.search(
            query=f'"{institution}"',
            media_type=media_type,
            limit=limit,
            __event_emitter__=__event_emitter__,
            __user__=__user__,
        )
