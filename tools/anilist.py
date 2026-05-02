"""
title: AniList — Anime & Manga Metadata (GraphQL, no key)
author: local-ai-stack
description: Search AniList for anime and manga. Free public GraphQL API — no key required for read endpoints. Covers detailed metadata (title, season, episodes, score, status, studios, genres), upcoming releases, currently-airing schedule, and per-character / per-staff lookups. Pairs with Nyaa torrent search and yt-dlp for actually getting episodes.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field


_UA = "local-ai-stack/1.0 anilist"
ANILIST = "https://graphql.anilist.co"


_SEARCH = """
query ($q: String!, $type: MediaType!, $perPage: Int!) {
  Page(page: 1, perPage: $perPage) {
    media(search: $q, type: $type, sort: POPULARITY_DESC) {
      id
      idMal
      title { romaji english native }
      type
      format
      status
      episodes
      chapters
      volumes
      duration
      season
      seasonYear
      averageScore
      meanScore
      popularity
      genres
      studios(isMain: true) { nodes { name } }
      siteUrl
      description(asHtml: false)
    }
  }
}
"""

_DETAIL = """
query ($id: Int!) {
  Media(id: $id) {
    id
    idMal
    title { romaji english native }
    type
    format
    status
    episodes
    chapters
    volumes
    duration
    season
    seasonYear
    averageScore
    meanScore
    popularity
    favourites
    genres
    tags { name rank }
    studios(isMain: true) { nodes { name } }
    nextAiringEpisode { airingAt timeUntilAiring episode }
    streamingEpisodes { title url site }
    siteUrl
    description(asHtml: false)
    relations { edges { relationType node { id title { romaji english } siteUrl } } }
    recommendations(perPage: 10) { nodes { mediaRecommendation { id title { romaji english } siteUrl } } }
  }
}
"""

_AIRING = """
query ($season: MediaSeason!, $year: Int!, $perPage: Int!) {
  Page(page: 1, perPage: $perPage) {
    media(season: $season, seasonYear: $year, type: ANIME, sort: POPULARITY_DESC) {
      id
      title { romaji english }
      format
      status
      episodes
      averageScore
      genres
      siteUrl
      nextAiringEpisode { airingAt episode }
    }
  }
}
"""


def _t(node: dict) -> str:
    title = node.get("title") or {}
    return title.get("english") or title.get("romaji") or title.get("native") or "—"


class Tools:
    class Valves(BaseModel):
        MAX_RESULTS: int = Field(default=10, description="Max results per query.")
        TIMEOUT: int = Field(default=20, description="HTTP timeout, seconds.")

    def __init__(self):
        self.valves = self.Valves()

    async def _gql(self, query: str, variables: dict) -> dict:
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT) as c:
            try:
                r = await c.post(
                    ANILIST,
                    json={"query": query, "variables": variables},
                    headers={"User-Agent": _UA, "Content-Type": "application/json", "Accept": "application/json"},
                )
            except Exception as e:
                return {"_err": f"{e}"}
            if r.status_code >= 400:
                return {"_err": f"anilist {r.status_code}: {r.text[:200]}"}
            data = r.json()
            if "errors" in data:
                return {"_err": str(data["errors"])[:300]}
            return data.get("data") or {}

    # ── Search ────────────────────────────────────────────────────────────

    async def search_anime(
        self,
        query: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search AniList for anime.
        :param query: Free-text title.
        :return: Markdown list with id, year, format, score, genres, AniList URL.
        """
        data = await self._gql(_SEARCH, {"q": query, "type": "ANIME", "perPage": self.valves.MAX_RESULTS})
        if data.get("_err"):
            return f"AniList error: {data['_err']}"
        items = (data.get("Page") or {}).get("media") or []
        if not items:
            return f"No AniList anime for: {query}"
        out = [f"## AniList anime: {query}\n"]
        for m in items:
            studios = ", ".join((s.get("name") for s in (m.get("studios") or {}).get("nodes", [])) or [])
            out.append(
                f"**{_t(m)}**  _{m.get('format', '?')}_  ({m.get('seasonYear', '—')})\n"
                f"   id: {m.get('id')}  ·  status: {m.get('status', '?')}  ·  episodes: {m.get('episodes') or '?'}\n"
                f"   score: {m.get('averageScore') or '—'}  ·  studios: {studios or '—'}\n"
                f"   genres: {', '.join(m.get('genres', []) or ['—'])}\n"
                f"   {m.get('siteUrl', '')}\n"
            )
        return "\n".join(out)

    async def search_manga(
        self,
        query: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search AniList for manga.
        :param query: Free-text title.
        :return: Markdown list.
        """
        data = await self._gql(_SEARCH, {"q": query, "type": "MANGA", "perPage": self.valves.MAX_RESULTS})
        if data.get("_err"):
            return f"AniList error: {data['_err']}"
        items = (data.get("Page") or {}).get("media") or []
        if not items:
            return f"No AniList manga for: {query}"
        out = [f"## AniList manga: {query}\n"]
        for m in items:
            out.append(
                f"**{_t(m)}**  _{m.get('format', '?')}_\n"
                f"   id: {m.get('id')}  ·  status: {m.get('status', '?')}  ·  chapters: {m.get('chapters') or '?'}  ·  volumes: {m.get('volumes') or '?'}\n"
                f"   score: {m.get('averageScore') or '—'}  ·  genres: {', '.join(m.get('genres', []) or ['—'])}\n"
                f"   {m.get('siteUrl', '')}\n"
            )
        return "\n".join(out)

    # ── Detail ────────────────────────────────────────────────────────────

    async def details(
        self,
        media_id: int,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Detailed metadata for an AniList id (works for both anime and manga).
        Includes next-airing episode, free-streaming sources where available,
        related works, and recommendations.
        :param media_id: AniList numeric id.
        :return: Markdown summary.
        """
        data = await self._gql(_DETAIL, {"id": int(media_id)})
        if data.get("_err"):
            return f"AniList error: {data['_err']}"
        m = data.get("Media")
        if not m:
            return f"No AniList media with id {media_id}"
        out = [
            f"## {_t(m)}",
            f"type: {m.get('type', '?')} {m.get('format', '?')}  ·  status: {m.get('status', '?')}",
            f"id: {m.get('id')}  (mal: {m.get('idMal') or '—'})",
            f"score: {m.get('averageScore') or '—'}  ·  popularity: {m.get('popularity', 0):,}  ·  favourites: {m.get('favourites', 0):,}",
            f"genres: {', '.join(m.get('genres', []) or ['—'])}",
            f"{m.get('siteUrl', '')}",
        ]
        nxt = m.get("nextAiringEpisode") or {}
        if nxt:
            secs = nxt.get("timeUntilAiring") or 0
            d, rem = divmod(int(secs), 86400)
            h, _ = divmod(rem, 3600)
            out.append(f"\n### Next episode\n  Episode {nxt.get('episode')} airs in {d}d {h}h (unix: {nxt.get('airingAt')})")

        streams = m.get("streamingEpisodes") or []
        if streams:
            out.append("\n### Free / official streaming episodes")
            for s in streams[:30]:
                out.append(f"  - {s.get('title', '—')} [{s.get('site', '?')}]  {s.get('url', '')}")

        if m.get("description"):
            desc = m["description"].replace("<br>", "\n").replace("</br>", "")
            out.append(f"\n### Description\n{desc[:1200]}")

        rels = ((m.get("relations") or {}).get("edges") or [])
        if rels:
            out.append("\n### Related")
            for e in rels[:12]:
                node = e.get("node") or {}
                out.append(f"  - {e.get('relationType', '?')}: {_t(node)}  {node.get('siteUrl', '')}")

        recs = ((m.get("recommendations") or {}).get("nodes") or [])
        if recs:
            out.append("\n### Recommendations")
            for r in recs[:10]:
                rec = r.get("mediaRecommendation") or {}
                out.append(f"  - {_t(rec)}  {rec.get('siteUrl', '')}")
        return "\n".join(out)

    # ── Seasonal ──────────────────────────────────────────────────────────

    async def airing_this_season(
        self,
        season: str = "",
        year: int = 0,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        List anime airing in a given season.
        :param season: WINTER, SPRING, SUMMER, or FALL. Empty = current season.
        :param year: 4-digit year. 0 = current year.
        :return: Markdown ranked list.
        """
        from datetime import datetime
        now = datetime.utcnow()
        if not season:
            month = now.month
            season = ("WINTER" if month <= 3 else
                      "SPRING" if month <= 6 else
                      "SUMMER" if month <= 9 else "FALL")
        season = season.upper()
        if season not in ("WINTER", "SPRING", "SUMMER", "FALL"):
            return f"Invalid season '{season}' (need WINTER/SPRING/SUMMER/FALL)."
        if not year:
            year = now.year

        data = await self._gql(
            _AIRING,
            {"season": season, "year": int(year), "perPage": self.valves.MAX_RESULTS},
        )
        if data.get("_err"):
            return f"AniList error: {data['_err']}"
        items = (data.get("Page") or {}).get("media") or []
        if not items:
            return f"No anime airing in {season} {year}."
        out = [f"## Anime airing: {season} {year}\n"]
        for m in items:
            ep_now = (m.get("nextAiringEpisode") or {}).get("episode")
            score = m.get("averageScore") or "—"
            out.append(
                f"**{_t(m)}**  _{m.get('format', '?')}_\n"
                f"   id: {m.get('id')}  ·  status: {m.get('status', '?')}  ·  episodes: {m.get('episodes') or '?'}"
                + (f"  ·  next ep: {ep_now}" if ep_now else "") + "\n"
                f"   score: {score}  ·  genres: {', '.join(m.get('genres', []) or ['—'])}\n"
                f"   {m.get('siteUrl', '')}\n"
            )
        return "\n".join(out)
