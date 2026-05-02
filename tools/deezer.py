"""
title: Deezer — Public Catalogue Search
author: local-ai-stack
description: Search Deezer's public catalogue for tracks, albums, artists, and playlists. The free public endpoints used here need no key — they cover catalogue search, related artists, charts, and a 30-second preview MP3 per track. Lossless FLAC streaming requires a paid Deezer HiFi subscription and is not exposed by the public API.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field


_UA = "local-ai-stack/1.0 deezer"
DEEZER = "https://api.deezer.com"


class Tools:
    class Valves(BaseModel):
        MAX_RESULTS: int = Field(default=10, description="Max results per query.")
        TIMEOUT: int = Field(default=15, description="HTTP timeout, seconds.")

    def __init__(self):
        self.valves = self.Valves()

    async def _get(self, path: str, params: Optional[dict] = None) -> Any:
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT) as c:
            r = await c.get(f"{DEEZER}{path}", params=params or {}, headers={"User-Agent": _UA})
            if r.status_code >= 400:
                return {"_err": f"deezer {r.status_code}: {r.text[:200]}"}
            try:
                return r.json()
            except Exception:
                return {"_err": "deezer returned non-JSON"}

    # ── Search ────────────────────────────────────────────────────────────

    async def search_tracks(
        self,
        query: str,
        artist: str = "",
        album: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search the catalogue for tracks. Optional `artist`/`album` filters
        translate to Deezer advanced-search qualifiers.
        :param query: Free-text query.
        :param artist: Exact artist name filter.
        :param album: Exact album title filter.
        :return: Markdown list with title, artist, album, duration, preview, page.
        """
        q = query
        if artist:
            q += f' artist:"{artist}"'
        if album:
            q += f' album:"{album}"'
        data = await self._get("/search", {"q": q, "limit": self.valves.MAX_RESULTS})
        if isinstance(data, dict) and data.get("_err"):
            return f"Deezer error: {data['_err']}"
        items = (data or {}).get("data") or []
        if not items:
            return f"No Deezer tracks for: {query}"
        out = [f"## Deezer tracks: {query}\n"]
        for t in items:
            dur = t.get("duration", 0)
            out.append(
                f"**{t.get('title')}** — _{(t.get('artist') or {}).get('name', '?')}_\n"
                f"   album: {(t.get('album') or {}).get('title', '?')}  ·  duration: {dur//60}:{dur%60:02d}\n"
                f"   preview: {t.get('preview', '—')}\n"
                f"   page:    {t.get('link', '—')}\n"
            )
        return "\n".join(out)

    async def search_albums(
        self,
        query: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search albums.
        :param query: Free-text.
        :return: Markdown list with album, artist, year, page.
        """
        data = await self._get("/search/album", {"q": query, "limit": self.valves.MAX_RESULTS})
        if isinstance(data, dict) and data.get("_err"):
            return f"Deezer error: {data['_err']}"
        items = (data or {}).get("data") or []
        if not items:
            return f"No Deezer albums for: {query}"
        out = [f"## Deezer albums: {query}\n"]
        for a in items:
            out.append(
                f"**{a.get('title')}** — _{(a.get('artist') or {}).get('name', '?')}_\n"
                f"   {a.get('record_type', '?')}  ·  tracks: {a.get('nb_tracks', '?')}\n"
                f"   {a.get('link', '—')}\n"
            )
        return "\n".join(out)

    async def search_artists(
        self,
        query: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search artists. Returns top hits with fan counts and Deezer page.
        :param query: Free-text.
        :return: Markdown list.
        """
        data = await self._get("/search/artist", {"q": query, "limit": self.valves.MAX_RESULTS})
        if isinstance(data, dict) and data.get("_err"):
            return f"Deezer error: {data['_err']}"
        items = (data or {}).get("data") or []
        if not items:
            return f"No Deezer artists for: {query}"
        out = [f"## Deezer artists: {query}\n"]
        for a in items:
            out.append(
                f"**{a.get('name')}**  ·  fans: {a.get('nb_fan', 0):,}  ·  albums: {a.get('nb_album', '?')}\n"
                f"   {a.get('link', '—')}\n"
            )
        return "\n".join(out)

    async def related_artists(
        self,
        artist_id: int,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        List artists Deezer relates to a given artist id.
        :param artist_id: Numeric artist id from `search_artists`.
        :return: Markdown list of related artists.
        """
        data = await self._get(f"/artist/{int(artist_id)}/related")
        if isinstance(data, dict) and data.get("_err"):
            return f"Deezer error: {data['_err']}"
        items = (data or {}).get("data") or []
        if not items:
            return f"No related artists for id {artist_id}"
        out = [f"## Related artists (Deezer id {artist_id})\n"]
        for a in items:
            out.append(f"- **{a.get('name')}**  fans: {a.get('nb_fan', 0):,}  ·  {a.get('link', '')}")
        return "\n".join(out)

    async def album_tracks(
        self,
        album_id: int,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        List the tracks on a given Deezer album, with previews.
        :param album_id: Numeric album id.
        :return: Markdown list of tracks.
        """
        data = await self._get(f"/album/{int(album_id)}")
        if isinstance(data, dict) and data.get("_err"):
            return f"Deezer error: {data['_err']}"
        title = data.get("title", "—")
        artist = (data.get("artist") or {}).get("name", "—")
        tracks = (data.get("tracks") or {}).get("data") or []
        out = [f"## {artist} — {title}", f"page: {data.get('link', '—')}", ""]
        for t in tracks:
            dur = t.get("duration", 0)
            out.append(
                f"  {t.get('track_position', '·')}. {t.get('title')}  "
                f"({dur//60}:{dur%60:02d})\n"
                f"     preview: {t.get('preview', '—')}"
            )
        return "\n".join(out)

    async def chart(
        self,
        kind: str = "tracks",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Fetch the Deezer chart.
        :param kind: "tracks", "albums", "artists", or "playlists".
        :return: Markdown ranked list.
        """
        valid = {"tracks", "albums", "artists", "playlists"}
        if kind not in valid:
            return f"Unknown chart kind '{kind}'. Use one of: {', '.join(valid)}"
        data = await self._get("/chart")
        if isinstance(data, dict) and data.get("_err"):
            return f"Deezer error: {data['_err']}"
        section = (data.get(kind) or {}).get("data") or []
        if not section:
            return f"Empty Deezer {kind} chart."
        out = [f"## Deezer {kind} chart\n"]
        for i, item in enumerate(section[: self.valves.MAX_RESULTS], start=1):
            if kind == "tracks":
                out.append(f"  {i}. **{item.get('title')}** — {(item.get('artist') or {}).get('name')}\n     {item.get('link', '')}")
            elif kind == "albums":
                out.append(f"  {i}. **{item.get('title')}** — {(item.get('artist') or {}).get('name')}\n     {item.get('link', '')}")
            elif kind == "artists":
                out.append(f"  {i}. **{item.get('name')}**\n     {item.get('link', '')}")
            else:
                out.append(f"  {i}. **{item.get('title')}**\n     {item.get('link', '')}")
        return "\n".join(out)
