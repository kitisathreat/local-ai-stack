"""
title: Songlink / Odesli — Universal Song Resolver
author: local-ai-stack
description: Take a song URL or ISRC from any service (Spotify, Apple Music, Tidal, Deezer, YouTube, YouTube Music, Bandcamp, SoundCloud, Pandora, Amazon Music) and return matching links on every other service via the public Odesli API. No key required for normal use. Useful for "I have a Spotify link, where can I get a free or lossless version?" — the response includes Bandcamp (often free / NYP), YouTube (use yt-dlp), Tidal/Deezer (lossless, paid).
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

from typing import Optional

import httpx
from pydantic import BaseModel, Field


_UA = "local-ai-stack/1.0 songlink"
ODESLI = "https://api.song.link/v1-alpha.1/links"


# Order presented in the rendered output: free / lossless first.
_PROVIDER_ORDER = [
    "bandcamp", "soundcloud", "youtube", "youtubeMusic",
    "tidal", "deezer", "qobuz",
    "appleMusic", "itunes", "spotify",
    "amazonMusic", "amazonStore", "audius",
    "pandora", "yandex", "anghami", "boomplay", "audiomack", "napster",
]

_FREE_OR_LOSSLESS_HINT = {
    "bandcamp":   "🟢 often free / lossless",
    "youtube":    "🟢 free (use yt-dlp)",
    "youtubeMusic": "🟢 free (use yt-dlp)",
    "soundcloud": "🟡 free streaming, lossy",
    "tidal":      "💎 lossless (paid)",
    "deezer":     "💎 lossless (paid HiFi)",
    "qobuz":      "💎 hi-res (paid)",
    "appleMusic": "💎 lossless (paid)",
    "spotify":    "🟡 lossy (paid for offline)",
}


class Tools:
    class Valves(BaseModel):
        USER_COUNTRY: str = Field(
            default="US",
            description="ISO country code passed to Odesli — affects which platforms surface a result.",
        )
        ODESLI_API_KEY: str = Field(
            default="",
            description="Optional Odesli API key (only needed at very high volume).",
        )
        TIMEOUT: int = Field(default=20, description="HTTP timeout per request, seconds.")

    def __init__(self):
        self.valves = self.Valves()

    async def _hit(self, params: dict) -> dict:
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT) as c:
            r = await c.get(ODESLI, params=params, headers={"User-Agent": _UA})
            if r.status_code == 404:
                return {"_err": "not found"}
            if r.status_code >= 400:
                return {"_err": f"odesli {r.status_code}: {r.text[:200]}"}
            return r.json()

    def _format(self, data: dict) -> str:
        if data.get("_err"):
            return f"Songlink error: {data['_err']}"
        entities = data.get("entitiesByUniqueId") or {}
        # The "primary" entity (the one Odesli locked onto first).
        primary_id = data.get("entityUniqueId") or ""
        primary = entities.get(primary_id, {}) or next(iter(entities.values()), {})
        title = primary.get("title", "—")
        artist = primary.get("artistName", "—")
        kind = primary.get("type", "—")

        out = [
            f"## {artist} — {title}",
            f"type: {kind}",
            f"odesli page: {data.get('pageUrl', '—')}",
            "",
            "### Sources (free / lossless first)",
        ]

        links = data.get("linksByPlatform") or {}
        seen = []
        for prov in _PROVIDER_ORDER:
            if prov not in links:
                continue
            url = links[prov].get("url", "")
            if not url:
                continue
            hint = _FREE_OR_LOSSLESS_HINT.get(prov, "")
            out.append(f"- **{prov}**: {url}  {hint}".rstrip())
            seen.append(prov)
        # Anything Odesli returned that we didn't render above.
        for prov, info in links.items():
            if prov in seen:
                continue
            url = info.get("url", "")
            if url:
                out.append(f"- {prov}: {url}")
        return "\n".join(out)

    async def resolve(
        self,
        url: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Resolve a song / album URL from any supported service to its
        equivalent on every other service.
        :param url: Spotify, Apple Music, Tidal, Bandcamp, YouTube, etc.
        :return: Markdown summary, free/lossless sources first.
        """
        params = {"url": url, "userCountry": self.valves.USER_COUNTRY}
        if self.valves.ODESLI_API_KEY:
            params["key"] = self.valves.ODESLI_API_KEY
        return self._format(await self._hit(params))

    async def resolve_isrc(
        self,
        isrc: str,
        kind: str = "song",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Look up by ISRC (12-char code printed on most legitimate releases) so
        the model can find a song without needing a service-specific URL.
        :param isrc: ISRC code (e.g. USRC17607839).
        :param kind: "song" or "album".
        :return: Markdown summary.
        """
        params = {
            "id": isrc,
            "type": kind,
            "platform": "isrc",
            "userCountry": self.valves.USER_COUNTRY,
        }
        if self.valves.ODESLI_API_KEY:
            params["key"] = self.valves.ODESLI_API_KEY
        return self._format(await self._hit(params))

    async def free_sources(
        self,
        url: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Same as `resolve`, but return only the free or lossless sources
        (Bandcamp, YouTube, YouTube Music, SoundCloud, Tidal, Deezer, Qobuz,
        Apple Music) — the ones useful for getting actual audio.
        :param url: Source URL.
        :return: Markdown — free/lossless platforms only.
        """
        params = {"url": url, "userCountry": self.valves.USER_COUNTRY}
        if self.valves.ODESLI_API_KEY:
            params["key"] = self.valves.ODESLI_API_KEY
        data = await self._hit(params)
        if data.get("_err"):
            return f"Songlink error: {data['_err']}"
        ent = next(iter((data.get("entitiesByUniqueId") or {}).values()), {})
        title = ent.get("title", "—")
        artist = ent.get("artistName", "—")
        wanted = ("bandcamp", "youtube", "youtubeMusic", "soundcloud", "tidal", "deezer", "qobuz", "appleMusic")
        links = data.get("linksByPlatform") or {}
        out = [f"## {artist} — {title}", "### Free / lossless sources only"]
        any_hit = False
        for p in wanted:
            if p in links and links[p].get("url"):
                out.append(f"- **{p}**: {links[p]['url']}  {_FREE_OR_LOSSLESS_HINT.get(p, '')}".rstrip())
                any_hit = True
        if not any_hit:
            out.append("_(no free / lossless sources detected by Odesli)_")
        return "\n".join(out)
