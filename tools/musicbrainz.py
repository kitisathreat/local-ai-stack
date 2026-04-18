"""
title: MusicBrainz — Open Music Metadata
author: local-ai-stack
description: Search the MusicBrainz open music encyclopedia. Artists, releases, recordings, labels, works with ISRCs/IDs/relationships. Covers Dylan to K-pop to classical. No API key required (please set USER_AGENT).
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from pydantic import BaseModel, Field
from typing import Optional


BASE = "https://musicbrainz.org/ws/2"
UA = "local-ai-stack/1.0 (musicbrainz tool)"


class Tools:
    class Valves(BaseModel):
        MAX_RESULTS: int = Field(default=8, description="Max results per query")

    def __init__(self):
        self.valves = self.Valves()

    async def _get(self, path: str, params: dict) -> dict:
        p = {"fmt": "json", **params}
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(f"{BASE}/{path}", params=p, headers={"User-Agent": UA})
            r.raise_for_status()
            return r.json()

    async def search_artist(self, query: str, __user__: Optional[dict] = None) -> str:
        """
        Search for musical artists.
        :param query: Artist name
        :return: Top artists with MBID, type, country, tags, and disambiguation
        """
        try:
            data = await self._get("artist", {"query": query, "limit": self.valves.MAX_RESULTS})
            arts = data.get("artists", [])
            if not arts:
                return f"No artist found for: {query}"
            lines = [f"## MusicBrainz Artist: {query}\n"]
            for a in arts:
                name = a.get("name", "")
                mbid = a.get("id", "")
                typ = a.get("type", "")
                country = a.get("country", "")
                life = a.get("life-span", {}) or {}
                begin = life.get("begin", "")
                end = life.get("end", "")
                dis = a.get("disambiguation", "")
                tags = ", ".join(t.get("name", "") for t in (a.get("tags") or [])[:5])
                lines.append(f"**{name}** ({typ}, {country})")
                if dis:
                    lines.append(f"   _{dis}_")
                if begin:
                    lines.append(f"   Active: {begin} → {end or 'present'}")
                if tags:
                    lines.append(f"   Tags: {tags}")
                lines.append(f"   MBID: `{mbid}`")
                lines.append(f"   🔗 https://musicbrainz.org/artist/{mbid}\n")
            return "\n".join(lines)
        except Exception as e:
            return f"MusicBrainz error: {e}"

    async def search_release(
        self,
        query: str,
        artist: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search for album/release by title (and optional artist).
        :param query: Release (album) title
        :param artist: Optional artist name
        :return: Top releases with date, country, track count
        """
        q = query + (f' AND artist:"{artist}"' if artist else "")
        try:
            data = await self._get("release-group", {"query": q, "limit": self.valves.MAX_RESULTS})
            rels = data.get("release-groups", [])
            if not rels:
                return f"No release found: {query}"
            lines = [f"## MusicBrainz Release: {query}\n"]
            for r in rels:
                t = r.get("title", "")
                mbid = r.get("id", "")
                pt = r.get("primary-type", "")
                date = r.get("first-release-date", "")
                artists = ", ".join(a.get("name", "") for a in (r.get("artist-credit") or []))
                lines.append(f"**{t}** [{pt}] — {date}")
                lines.append(f"   by {artists}")
                lines.append(f"   🔗 https://musicbrainz.org/release-group/{mbid}\n")
            return "\n".join(lines)
        except Exception as e:
            return f"MusicBrainz error: {e}"

    async def search_recording(
        self,
        query: str,
        artist: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search for a track/recording.
        :param query: Track title
        :param artist: Optional artist name
        :return: Top matching recordings with release and duration
        """
        q = query + (f' AND artist:"{artist}"' if artist else "")
        try:
            data = await self._get("recording", {"query": q, "limit": self.valves.MAX_RESULTS})
            recs = data.get("recordings", [])
            if not recs:
                return f"No track found: {query}"
            lines = [f"## MusicBrainz Track: {query}\n"]
            for r in recs:
                t = r.get("title", "")
                mbid = r.get("id", "")
                length = r.get("length", 0) or 0
                mins = int(length / 60000)
                secs = int((length % 60000) / 1000)
                artists = ", ".join(a.get("name", "") for a in (r.get("artist-credit") or []))
                releases = ", ".join(rel.get("title", "") for rel in (r.get("releases") or [])[:2])
                lines.append(f"**{t}** ({mins}:{secs:02d})")
                lines.append(f"   by {artists}")
                if releases:
                    lines.append(f"   on {releases}")
                lines.append(f"   🔗 https://musicbrainz.org/recording/{mbid}\n")
            return "\n".join(lines)
        except Exception as e:
            return f"MusicBrainz error: {e}"
