"""
title: Torrent Search — YTS, EZTV, Nyaa, Pirate Bay, Internet Archive, Jackett
author: local-ai-stack
description: Search public torrent indexers for movies (YTS — yts.mx), TV (EZTV), anime (Nyaa.si), and the long-tail (apibay.org / The Pirate Bay JSON, Internet Archive's torrent collection of fully public-domain & CC-licensed material). Optional: when a Jackett or Prowlarr instance is configured, route a unified search through it to hit 100+ trackers at once. Returns name, seeders, size, and a magnet URI / .torrent URL that can be handed to the torrent client tool. This is a discovery layer only — the user's torrent client (e.g. qBittorrent) is what actually downloads.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field


# Default well-known trackers for magnet URIs that come without them.
_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://tracker.dler.org:6969/announce",
    "udp://open.stealth.si:80/announce",
    "udp://9.rarbg.to:2710/announce",
    "udp://tracker.tiny-vps.com:6969/announce",
]


def _hash_to_magnet(infohash: str, name: str = "") -> str:
    qs = "&".join(["xt=urn:btih:" + infohash]
                  + ([f"dn={urllib.parse.quote(name)}"] if name else [])
                  + [f"tr={urllib.parse.quote(t)}" for t in _TRACKERS])
    return f"magnet:?{qs}"


def _human_size(n: int | str) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    units = ["B", "KB", "MB", "GB", "TB"]
    i, f = 0, float(n)
    while f >= 1024 and i < len(units) - 1:
        f /= 1024
        i += 1
    return f"{f:.2f} {units[i]}"


class Tools:
    class Valves(BaseModel):
        YTS_URL: str = Field(
            default="https://yts.mx",
            description="YTS base URL (yts.mx, yts.rs, ...). The official JSON API hangs off /api/v2/.",
        )
        EZTV_URL: str = Field(
            default="https://eztv.re",
            description="EZTV base URL. JSON: /api/get-torrents.",
        )
        APIBAY_URL: str = Field(
            default="https://apibay.org",
            description="The Pirate Bay JSON API (apibay.org).",
        )
        NYAA_URL: str = Field(
            default="https://nyaa.si",
            description="Nyaa base URL (anime/asian media). RSS+JSON.",
        )
        IA_URL: str = Field(
            default="https://archive.org",
            description="Internet Archive base URL — search the torrent-bearing public-domain catalog.",
        )
        JACKETT_URL: str = Field(
            default="",
            description="Optional Jackett/Prowlarr base URL (e.g. http://127.0.0.1:9117). Empty = disabled.",
        )
        JACKETT_API_KEY: str = Field(
            default="",
            description="Jackett/Prowlarr API key (Settings → API key in the web UI).",
        )
        DEFAULT_LIMIT: int = Field(
            default=20,
            description="Max results returned per indexer.",
        )

    def __init__(self):
        self.valves = self.Valves()

    # ── YTS (movies) ──────────────────────────────────────────────────────

    async def search_movies(
        self,
        query: str,
        quality: str = "",
        min_seeders: int = 0,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search YTS for movies. YTS publishes high-quality x264/x265 movie
        rips with an open JSON API.
        :param query: Movie title or keyword.
        :param quality: Optional filter — "720p", "1080p", "2160p", "3D".
        :param min_seeders: Drop any torrent with fewer seeders than this.
        :return: Movie title, year, IMDB id, plus quality/size/seeders/magnet rows.
        """
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(
                f"{self.valves.YTS_URL}/api/v2/list_movies.json",
                params={"query_term": query, "limit": self.valves.DEFAULT_LIMIT,
                        "sort_by": "seeds", "order_by": "desc"},
            )
        if r.status_code != 200:
            return f"HTTP {r.status_code}"
        movies = (r.json() or {}).get("data", {}).get("movies", []) or []
        rows: list[str] = []
        for m in movies:
            title = f"{m.get('title')} ({m.get('year')})"
            for t in m.get("torrents", []):
                if quality and t.get("quality") != quality:
                    continue
                seeds = int(t.get("seeds") or 0)
                if seeds < min_seeders:
                    continue
                magnet = _hash_to_magnet(t["hash"], title)
                rows.append(
                    f"{title:<48} {t.get('quality',''):>5} {t.get('type',''):<6} "
                    f"S={seeds:>4} P={t.get('peers'):>4} {t.get('size',''):>10}  imdb={m.get('imdb_code','-')}\n"
                    f"  {magnet}"
                )
        return "\n".join(rows) if rows else "(no matches)"

    # ── EZTV (TV) ─────────────────────────────────────────────────────────

    async def search_tv(
        self,
        query: str = "",
        imdb_id: str = "",
        page: int = 1,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search EZTV for TV episodes by show title or IMDB id (without the
        "tt" prefix).
        :param query: Show title (used to filter the response client-side).
        :param imdb_id: Numeric IMDB id, e.g. "0903747" for Breaking Bad. Recommended.
        :param page: 1-based page index.
        :return: Episode title, season/episode, size, seeders, magnet.
        """
        params: dict[str, Any] = {"limit": self.valves.DEFAULT_LIMIT, "page": page}
        if imdb_id:
            params["imdb_id"] = imdb_id.lstrip("tt")
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{self.valves.EZTV_URL}/api/get-torrents", params=params)
        if r.status_code != 200:
            return f"HTTP {r.status_code}"
        items = (r.json() or {}).get("torrents", []) or []
        if query and not imdb_id:
            q = query.lower()
            items = [t for t in items if q in (t.get("title") or "").lower()]
        rows = [
            f"S{int(t.get('season') or 0):02d}E{int(t.get('episode') or 0):02d}  "
            f"{(t.get('title') or '')[:60]:<60}  S={t.get('seeds',0):>4}  "
            f"{_human_size(t.get('size_bytes',0))}\n  {t.get('magnet_url','')}"
            for t in items
        ]
        return "\n".join(rows) if rows else "(no matches)"

    # ── Nyaa (anime / J-media) ───────────────────────────────────────────

    async def search_anime(
        self,
        query: str,
        category: str = "1_2",  # Anime - English-translated
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search Nyaa.si for anime / Asian media releases. Nyaa exposes an RSS
        feed; we parse the JSON-ish XML into rows.
        :param query: Search query (supports Nyaa operators).
        :param category: Nyaa category id (1_2 = English-translated anime, 1_1 = subbed, 0_0 = all).
        :return: Title, size, seeders, magnet for each result.
        """
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(
                f"{self.valves.NYAA_URL}/",
                params={"page": "rss", "q": query, "c": category, "s": "seeders", "o": "desc"},
            )
        if r.status_code != 200:
            return f"HTTP {r.status_code}"
        # Light XML parse — avoid lxml dependency.
        import xml.etree.ElementTree as ET
        try:
            root = ET.fromstring(r.text)
        except ET.ParseError as e:
            return f"parse error: {e}"
        ns = {"nyaa": "https://nyaa.si/xmlns/nyaa"}
        rows: list[str] = []
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            seeds = item.findtext("nyaa:seeders", default="?", namespaces=ns)
            size = item.findtext("nyaa:size", default="?", namespaces=ns)
            rows.append(f"{title[:70]:<70}  S={seeds:>4}  {size}\n  {link}")
            if len(rows) >= self.valves.DEFAULT_LIMIT:
                break
        return "\n".join(rows) if rows else "(no matches)"

    # ── Pirate Bay (general) ─────────────────────────────────────────────

    async def search_general(
        self,
        query: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search apibay.org (The Pirate Bay JSON API) for any category. Best
        general-purpose fallback when YTS / EZTV / Nyaa don't have it.
        :param query: Search query.
        :return: Title, category, size, seeders, magnet.
        """
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{self.valves.APIBAY_URL}/q.php", params={"q": query})
        if r.status_code != 200:
            return f"HTTP {r.status_code}"
        items = r.json() or []
        if isinstance(items, dict):
            return f"error: {items}"
        # apibay returns [{name, info_hash, leechers, seeders, size, ...}]
        rows: list[str] = []
        for t in items[: self.valves.DEFAULT_LIMIT]:
            ih = t.get("info_hash", "")
            if not ih or ih == "0":
                continue
            magnet = _hash_to_magnet(ih, t.get("name", ""))
            rows.append(
                f"{(t.get('name') or '')[:60]:<60}  cat={t.get('category','?')}  "
                f"S={t.get('seeders','?'):>4}  L={t.get('leechers','?'):>4}  "
                f"{_human_size(t.get('size','0'))}\n  {magnet}"
            )
        return "\n".join(rows) if rows else "(no matches)"

    # ── Internet Archive (public domain & CC) ────────────────────────────

    async def search_internet_archive(
        self,
        query: str,
        media_type: str = "movies",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search the Internet Archive's torrent-distributed catalogue. Every
        result here is fully public-domain or Creative-Commons-licensed —
        a legal source for old films, TV broadcasts, concerts, ebooks.
        :param query: Search query.
        :param media_type: movies, audio, texts, software, etis (educational), data.
        :return: Title, identifier, and a direct .torrent URL.
        """
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(
                f"{self.valves.IA_URL}/advancedsearch.php",
                params={
                    "q": f"{query} AND mediatype:{media_type}",
                    "fl[]": "identifier,title,year,downloads",
                    "rows": self.valves.DEFAULT_LIMIT,
                    "page": 1,
                    "output": "json",
                    "sort[]": "downloads desc",
                },
            )
        if r.status_code != 200:
            return f"HTTP {r.status_code}"
        docs = (r.json() or {}).get("response", {}).get("docs", [])
        rows = []
        for d in docs:
            ident = d.get("identifier", "")
            torrent = f"{self.valves.IA_URL}/download/{ident}/{ident}_archive.torrent"
            rows.append(
                f"{d.get('title', ident)[:60]:<60}  year={d.get('year','?')}  "
                f"dl={d.get('downloads','?')}\n  {torrent}"
            )
        return "\n".join(rows) if rows else "(no matches)"

    # ── Jackett / Prowlarr (meta) ────────────────────────────────────────

    async def search_jackett(
        self,
        query: str,
        category: int = 0,
        indexer: str = "all",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Route a search through a Jackett (or Prowlarr) instance — hits every
        configured tracker at once. Requires JACKETT_URL + JACKETT_API_KEY.
        :param query: Search query.
        :param category: Newznab/Torznab category id (2000=movies, 5000=TV, 3000=audio, 7000=books, 0=any).
        :param indexer: Jackett indexer slug or "all".
        :return: Tracker, title, size, seeders, magnet/link.
        """
        if not self.valves.JACKETT_URL or not self.valves.JACKETT_API_KEY:
            return "JACKETT_URL / JACKETT_API_KEY not set on the Torrent Search tool's Valves."
        params: dict[str, Any] = {
            "apikey": self.valves.JACKETT_API_KEY,
            "Query": query,
            "Tracker[]": indexer,
        }
        if category:
            params["Category[]"] = category
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(
                f"{self.valves.JACKETT_URL.rstrip('/')}/api/v2.0/indexers/{indexer}/results",
                params=params,
            )
        if r.status_code != 200:
            return f"HTTP {r.status_code}: {r.text[:300]}"
        results = (r.json() or {}).get("Results", []) or []
        rows = []
        for t in results[: self.valves.DEFAULT_LIMIT]:
            link = t.get("MagnetUri") or t.get("Link") or t.get("Guid")
            rows.append(
                f"[{t.get('Tracker','?'):>12}] {(t.get('Title') or '')[:55]:<55}  "
                f"S={t.get('Seeders',0):>4}  {_human_size(t.get('Size',0))}\n  {link}"
            )
        return "\n".join(rows) if rows else "(no matches)"
