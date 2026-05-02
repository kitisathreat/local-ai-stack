"""
title: JustWatch — Where Can I Watch / Stream / Rent / Buy
author: local-ai-stack
description: Look up where a film or TV show is currently streaming, free with ads, rentable, or buyable in a chosen country. Uses JustWatch's public Apollo GraphQL endpoint (the same one their website uses) — no key required. Returns offers grouped by monetisation type (FLATRATE / FREE / ADS / RENT / BUY) with deep links to each provider, plus a free-only filter.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field


_UA = "Mozilla/5.0 (X11; Linux x86_64) local-ai-stack/1.0 justwatch"
JW_GRAPHQL = "https://apis.justwatch.com/graphql"

_SEARCH_QUERY = """
query GetSearchTitles($searchTitlesFilter: TitleFilter!, $country: Country!, $language: Language!, $first: Int!) {
  popularTitles(country: $country, filter: $searchTitlesFilter, first: $first, sortBy: POPULAR) {
    edges {
      node {
        id
        objectType
        objectId
        content(country: $country, language: $language) {
          title
          fullPath
          originalReleaseYear
          shortDescription
          externalIds { imdbId tmdbId }
        }
      }
    }
  }
}
"""

_OFFERS_QUERY = """
query GetTitleOffers($nodeId: ID!, $country: Country!, $language: Language!) {
  node(id: $nodeId) {
    ... on MovieOrShow {
      offers(country: $country, platform: WEB) {
        monetizationType
        presentationType
        retailPrice(language: $language)
        currency
        standardWebURL
        package { clearName technicalName }
      }
      content(country: $country, language: $language) {
        title
        originalReleaseYear
        fullPath
      }
    }
  }
}
"""

_MON_ICON = {
    "FLATRATE": "📺 subscription",
    "FREE":     "🟢 free",
    "ADS":      "🟢 free w/ ads",
    "RENT":     "💵 rent",
    "BUY":      "💰 buy",
    "CINEMA":   "🎟️ cinema",
}


class Tools:
    class Valves(BaseModel):
        COUNTRY: str = Field(default="US", description="ISO country code, e.g. US, GB, DE, JP, AU.")
        LANGUAGE: str = Field(default="en", description="2-letter language code.")
        MAX_RESULTS: int = Field(default=8, description="Max search results.")
        TIMEOUT: int = Field(default=20, description="HTTP timeout, seconds.")

    def __init__(self):
        self.valves = self.Valves()

    async def _gql(self, query: str, variables: dict) -> dict:
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT) as c:
            try:
                r = await c.post(
                    JW_GRAPHQL,
                    json={"query": query, "variables": variables},
                    headers={
                        "User-Agent": _UA,
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                )
            except Exception as e:
                return {"_err": f"{e}"}
            if r.status_code >= 400:
                return {"_err": f"justwatch {r.status_code}: {r.text[:200]}"}
            data = r.json()
            if "errors" in data:
                return {"_err": str(data["errors"])[:300]}
            return data.get("data") or {}

    # ── Search ────────────────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        kind: str = "ALL",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search JustWatch by title.
        :param query: Title query.
        :param kind: "ALL", "MOVIE", or "SHOW".
        :return: Markdown list with title, year, JustWatch path, and node id
                 (used by `where_to_watch`).
        """
        kind_u = kind.upper()
        filt: dict[str, Any] = {"searchQuery": query}
        if kind_u in ("MOVIE", "SHOW"):
            filt["objectTypes"] = [kind_u]

        data = await self._gql(_SEARCH_QUERY, {
            "searchTitlesFilter": filt,
            "country": self.valves.COUNTRY,
            "language": self.valves.LANGUAGE,
            "first": self.valves.MAX_RESULTS,
        })
        if data.get("_err"):
            return f"JustWatch error: {data['_err']}"
        edges = (data.get("popularTitles") or {}).get("edges") or []
        if not edges:
            return f"No JustWatch results for: {query}"

        out = [f"## JustWatch ({self.valves.COUNTRY}): {query}\n"]
        for e in edges:
            n = e.get("node") or {}
            ct = n.get("content") or {}
            ext = ct.get("externalIds") or {}
            out.append(
                f"**{ct.get('title', '—')}**  _{n.get('objectType', '?')}_  ({ct.get('originalReleaseYear', '—')})\n"
                f"   imdb: {ext.get('imdbId', '—')}  ·  tmdb: {ext.get('tmdbId', '—')}  ·  node id: `{n.get('id')}`\n"
                f"   https://www.justwatch.com{ct.get('fullPath', '')}\n"
            )
        return "\n".join(out)

    # ── Offers ────────────────────────────────────────────────────────────

    async def where_to_watch(
        self,
        node_id: str,
        free_only: bool = False,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        List all offers (subscription / free-with-ads / rent / buy) for a
        title in the configured country.
        :param node_id: Node id from `search` (the long base64-ish string).
        :param free_only: When True, drop RENT/BUY/CINEMA — keep FLATRATE,
                          FREE, ADS only.
        :return: Markdown grouped by monetisation type.
        """
        data = await self._gql(_OFFERS_QUERY, {
            "nodeId": node_id,
            "country": self.valves.COUNTRY,
            "language": self.valves.LANGUAGE,
        })
        if data.get("_err"):
            return f"JustWatch error: {data['_err']}"
        node = data.get("node") or {}
        if not node:
            return f"No JustWatch node {node_id}."
        ct = node.get("content") or {}
        offers = node.get("offers") or []
        if not offers:
            return f"No streaming/rent/buy offers in {self.valves.COUNTRY} for {ct.get('title', node_id)}."

        # Group by monetisation type.
        groups: dict[str, list[dict]] = {}
        for o in offers:
            mon = o.get("monetizationType", "?").upper()
            if free_only and mon not in ("FREE", "ADS", "FLATRATE"):
                continue
            groups.setdefault(mon, []).append(o)
        if not groups:
            return f"No matching offers (free_only={free_only})."

        out = [f"## {ct.get('title', '—')} ({ct.get('originalReleaseYear', '—')}) — {self.valves.COUNTRY}",
               f"https://www.justwatch.com{ct.get('fullPath', '')}",
               ""]
        order = ["FREE", "ADS", "FLATRATE", "RENT", "BUY", "CINEMA"]
        for mon in order:
            if mon not in groups:
                continue
            out.append(f"### {_MON_ICON.get(mon, mon)}")
            seen = set()
            for o in groups[mon]:
                pkg = (o.get("package") or {}).get("clearName") or "?"
                pres = o.get("presentationType") or ""
                price = o.get("retailPrice") or ""
                cur = o.get("currency") or ""
                url = o.get("standardWebURL") or ""
                key = (pkg, pres, price)
                if key in seen:
                    continue
                seen.add(key)
                line = f"- **{pkg}** [{pres}]"
                if price:
                    line += f" — {price} {cur}"
                if url:
                    line += f"\n  {url}"
                out.append(line)
            out.append("")
        return "\n".join(out)

    async def find_free_offers(
        self,
        query: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        One-shot: search by title, take the top hit, return only its FREE /
        FREE-WITH-ADS / FLATRATE offers. The fastest "where can I watch this
        without paying à la carte?" path.
        :param query: Title query.
        :return: Markdown digest.
        """
        # Reuse search() but pull node id of first hit.
        data = await self._gql(_SEARCH_QUERY, {
            "searchTitlesFilter": {"searchQuery": query},
            "country": self.valves.COUNTRY,
            "language": self.valves.LANGUAGE,
            "first": 1,
        })
        if data.get("_err"):
            return f"JustWatch error: {data['_err']}"
        edges = (data.get("popularTitles") or {}).get("edges") or []
        if not edges:
            return f"No JustWatch hit for: {query}"
        node_id = (edges[0].get("node") or {}).get("id")
        if not node_id:
            return f"JustWatch top hit had no node id for: {query}"
        return await self.where_to_watch(node_id, free_only=True)
