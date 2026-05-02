"""
title: Free Films & TV — IA, Tubi, Pluto, Crackle, PBS, Kanopy + Torrent Crawl
author: local-ai-stack
description: Search legally-free TV and film catalogues. Internet Archive movies (`collection:moviesandfilms` / `feature_films` — public-domain Hollywood, silent film, government films, anime, classic TV) returns direct MP4/MKV download URLs and is the strongest free legal source. Tubi / Pluto TV / Crackle / PBS / IMDb Freevee / Kanopy are ad-supported or library-card free streaming. The `find_anywhere` aggregator additionally crawls TV/movie torrent repositories (YTS, EZTV, Nyaa, Pirate Bay, Internet Archive .torrent collection) and meta-aggregators (Knaben, Solid Torrents, 1337x, TorrentGalaxy, BTDigg), returning magnet URIs for the qBittorrent tool to download.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.1.0
licence: MIT
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field


def _load_tool(name: str):
    """Sibling-tool loader — same pattern as cross_source_playlist.py."""
    try:
        spec = importlib.util.spec_from_file_location(
            f"_lai_{name}", Path(__file__).parent / f"{name}.py",
        )
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod.Tools()
    except Exception:
        return None


_UA = "Mozilla/5.0 (X11; Linux x86_64) local-ai-stack/1.0 free-films-tv"
IA_API = "https://archive.org/advancedsearch.php"
IA_META = "https://archive.org/metadata"
IA_DOWNLOAD = "https://archive.org/download"


class Tools:
    class Valves(BaseModel):
        MAX_RESULTS: int = Field(default=10, description="Max results per source.")
        TIMEOUT: int = Field(default=20, description="HTTP timeout per request, seconds.")
        TUBI_REGION: str = Field(default="US", description="Region code for Tubi/Pluto search results.")
        PBS_API_KEY: str = Field(default="", description="Optional PBS Media Manager API key. Search works without it (read-only HTML scrape).")

    def __init__(self):
        self.valves = self.Valves()

    # ── Internet Archive Movies & TV ──────────────────────────────────────

    async def archive_org_films(
        self,
        query: str,
        collection: str = "moviesandfilms",
        year_from: int = 0,
        year_to: int = 0,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search Internet Archive movie collections for full-length feature films,
        TV episodes, or shorts. Public domain or rights-cleared material with
        direct MP4 / MKV download URLs.
        :param query: Free text — title, director, era keyword.
        :param collection: IA collection id. Useful values:
                           - "moviesandfilms" (whole movies + films collection),
                           - "feature_films" (public-domain feature films),
                           - "classic_tv" (classic TV episodes),
                           - "silent_films",
                           - "animationandcartoons",
                           - "ephemera" (PSAs, government films).
        :param year_from: Optional lower bound (inclusive). 0 disables.
        :param year_to: Optional upper bound (inclusive). 0 disables.
        :return: Markdown list with year, runtime, formats, IA download URL.
        """
        clauses = [f"collection:({collection})", f"({query})"]
        if year_from and year_to:
            clauses.append(f"year:[{year_from} TO {year_to}]")
        elif year_from:
            clauses.append(f"year:[{year_from} TO 2099]")
        elif year_to:
            clauses.append(f"year:[1800 TO {year_to}]")
        params: dict[str, Any] = {
            "q": " AND ".join(clauses),
            "rows": self.valves.MAX_RESULTS,
            "page": 1,
            "output": "json",
            "sort[]": "downloads desc",
        }
        for i, f in enumerate(["identifier", "title", "creator", "date", "year", "runtime", "format", "downloads"]):
            params[f"fl[{i}]"] = f

        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT) as c:
            try:
                r = await c.get(IA_API, params=params, headers={"User-Agent": _UA})
            except Exception as e:
                return f"IA request failed: {e}"
            if r.status_code >= 400:
                return f"IA returned {r.status_code}"
            docs = (r.json().get("response") or {}).get("docs") or []

        if not docs:
            return f"No IA hits in collection:{collection} for: {query}"

        out = [f"## Internet Archive [{collection}]: {query}\n"]
        for d in docs:
            fmts = d.get("format") or []
            if isinstance(fmts, str):
                fmts = [fmts]
            video = [f for f in fmts if any(k in f.lower() for k in ("mp4", "mkv", "ogv", "matroska", "webm", "mpeg"))]
            badge = "🎬 video" if video else "—"
            out.append(
                f"**{d.get('title', '—')}**  ({d.get('year', '—')})\n"
                f"   {badge}  ·  formats: {', '.join(fmts) or '—'}  ·  downloads: {int(d.get('downloads', 0)):,}\n"
                f"   {IA_DOWNLOAD}/{d.get('identifier', '')}/\n"
            )
        return "\n".join(out)

    async def archive_org_files(
        self,
        identifier: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        List downloadable video files for a given Internet Archive item.
        Use after `archive_org_films`.
        :param identifier: Internet Archive identifier from a previous result.
        :return: List of MP4/MKV/WebM files with sizes and direct download URLs.
        """
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT) as c:
            try:
                r = await c.get(f"{IA_META}/{identifier}", headers={"User-Agent": _UA})
            except Exception as e:
                return f"IA metadata request failed: {e}"
            if r.status_code >= 400:
                return f"IA error {r.status_code}"
            data = r.json()
        files = data.get("files", []) or []
        wanted = [
            f for f in files
            if any(f.get("name", "").lower().endswith("." + e) for e in ("mp4", "mkv", "webm", "ogv", "mpg", "mpeg", "avi"))
        ]
        if not wanted:
            return f"No video files in {identifier}"

        out = [f"## {identifier} — video files\n"]
        for f in wanted[:50]:
            size = int(f.get("size", 0)) if f.get("size") else 0
            out.append(
                f"  {f.get('name')}  ({size:,} bytes)\n"
                f"     {IA_DOWNLOAD}/{identifier}/{quote(f.get('name', ''), safe='')}"
            )
        return "\n".join(out)

    # ── Tubi (free, ad-supported) ─────────────────────────────────────────

    async def tubi_search(
        self,
        query: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search Tubi's free-streaming catalogue.
        :param query: Title query.
        :return: Markdown list with type (movie/series), year, rating, watch URL.
        """
        url = "https://tubitv.com/oz/search/" + quote(query, safe="")
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT, follow_redirects=True) as c:
            try:
                r = await c.get(
                    url,
                    params={"isKidsMode": "false", "useLinearHeader": "true"},
                    headers={"User-Agent": _UA, "Accept": "application/json"},
                )
            except Exception as e:
                return f"Tubi request failed: {e}"
            if r.status_code >= 400:
                return f"Tubi returned {r.status_code}"
            try:
                data = r.json()
            except Exception:
                return "Tubi returned non-JSON; their internal endpoint may have changed."

        # Tubi's oz/search response is a flat array of content nodes.
        items = data if isinstance(data, list) else data.get("contents", [])
        if not items:
            return f"No Tubi titles matched: {query}"

        out = [f"## Tubi (free, ad-supported): {query}\n"]
        for it in items[: self.valves.MAX_RESULTS]:
            kind = it.get("type") or it.get("contentType") or "?"
            title = it.get("title") or it.get("name") or "—"
            year = it.get("year") or it.get("releaseYear") or "—"
            rating = it.get("ratings", [{}])[0].get("value") if it.get("ratings") else (it.get("rating") or "—")
            tid = it.get("id") or it.get("contentId") or ""
            slug = (it.get("permalink") or "").lstrip("/")
            link = f"https://tubitv.com/{slug}" if slug else (
                f"https://tubitv.com/movies/{tid}" if kind.lower() == "movie" else f"https://tubitv.com/series/{tid}"
            )
            out.append(f"**{title}**  _{kind}_  ({year})  rating: {rating}\n   {link}\n")
        return "\n".join(out)

    # ── Pluto TV (free, ad-supported live + on-demand) ────────────────────

    async def pluto_search(
        self,
        query: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search Pluto TV's on-demand catalogue.
        :param query: Title query.
        :return: Markdown list with type, slug, and watch URL.
        """
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT, follow_redirects=True) as c:
            try:
                r = await c.get(
                    "https://service-vod-search.clusters.pluto.tv/v1/search",
                    params={"q": query, "limit": self.valves.MAX_RESULTS},
                    headers={"User-Agent": _UA, "Accept": "application/json"},
                )
            except Exception as e:
                return f"Pluto request failed: {e}"
            if r.status_code >= 400:
                return f"Pluto returned {r.status_code}"
            try:
                data = r.json()
            except Exception:
                return "Pluto returned non-JSON; endpoint may have changed."

        items = data.get("data", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
        if not items:
            return f"No Pluto TV results for: {query}"

        out = [f"## Pluto TV: {query}\n"]
        for it in items[: self.valves.MAX_RESULTS]:
            slug = it.get("slug") or it.get("id") or ""
            kind = it.get("type", "—")
            title = it.get("name") or it.get("title", "—")
            year = it.get("year") or "—"
            link = f"https://pluto.tv/on-demand/{kind}s/{slug}/details"
            out.append(f"**{title}**  _{kind}_  ({year})\n   {link}\n")
        return "\n".join(out)

    # ── Crackle ───────────────────────────────────────────────────────────

    async def crackle_search(
        self,
        query: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search Crackle (Sony's free ad-supported service).
        :param query: Title query.
        :return: Markdown list with watch URLs (US only).
        """
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT, follow_redirects=True) as c:
            try:
                r = await c.get(
                    "https://prod-api.crackle.com/contentdiscovery/search/" + quote(query, safe=""),
                    params={"useFuzzyMatching": "false", "enforcemediaRights": "true", "pageSize": self.valves.MAX_RESULTS},
                    headers={"User-Agent": _UA, "Accept": "application/json"},
                )
            except Exception as e:
                return f"Crackle request failed: {e}"
            if r.status_code >= 400:
                return f"Crackle returned {r.status_code}"
            try:
                data = r.json()
            except Exception:
                return "Crackle returned non-JSON."

        items = (data.get("data") or {}).get("items") or data.get("items") or []
        if not items:
            return f"No Crackle results for: {query}"

        out = [f"## Crackle (free, ad-supported, US): {query}\n"]
        for it in items[: self.valves.MAX_RESULTS]:
            kind = it.get("type", "—")
            title = it.get("title") or it.get("metadata", {}).get("title", "—")
            year = it.get("year") or "—"
            slug = it.get("id") or it.get("metadata", {}).get("id") or ""
            link = f"https://www.crackle.com/watch/{slug}" if slug else "https://www.crackle.com/"
            out.append(f"**{title}**  _{kind}_  ({year})\n   {link}\n")
        return "\n".join(out)

    # ── PBS (free, public broadcasting) ──────────────────────────────────

    async def pbs_search(
        self,
        query: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search PBS (US public broadcaster) for streamable shows. Many series
        are free for everyone; some are PBS Passport (member-only).
        :param query: Show title or topic.
        :return: Markdown list with show URLs.
        """
        url = "https://www.pbs.org/api/search/v1/?q=" + quote(query, safe="")
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT, follow_redirects=True) as c:
            try:
                r = await c.get(url, headers={"User-Agent": _UA, "Accept": "application/json"})
            except Exception as e:
                return f"PBS request failed: {e}"
            if r.status_code >= 400:
                return f"PBS returned {r.status_code}"
            try:
                data = r.json()
            except Exception:
                return "PBS returned non-JSON."

        items = data.get("results") or data.get("hits") or []
        if not items:
            return f"No PBS hits for: {query}"

        out = [f"## PBS: {query}\n"]
        for it in items[: self.valves.MAX_RESULTS]:
            title = it.get("title") or it.get("name", "—")
            kind = it.get("content_type") or it.get("type", "—")
            link = it.get("canonical_url") or it.get("url") or it.get("permalink") or ""
            badge = "🎟️ Passport" if "passport" in (it.get("availability") or "").lower() else "🆓 free"
            out.append(f"**{title}**  _{kind}_  {badge}\n   {link}\n")
        return "\n".join(out)

    # ── Kanopy (library-card free) ───────────────────────────────────────

    async def kanopy_search(
        self,
        query: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search Kanopy's catalogue (free with a US/CA/UK/AU public-library card,
        or a participating university login).
        :param query: Title query.
        :return: Markdown list with Kanopy page URLs.
        """
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT, follow_redirects=True) as c:
            try:
                r = await c.get(
                    "https://www.kanopy.com/api/v1/search",
                    params={"query": query, "page": 1, "items_per_page": self.valves.MAX_RESULTS},
                    headers={"User-Agent": _UA, "Accept": "application/json"},
                )
            except Exception as e:
                return f"Kanopy request failed: {e}"
            if r.status_code >= 400:
                return f"Kanopy returned {r.status_code}"
            try:
                data = r.json()
            except Exception:
                return "Kanopy returned non-JSON."

        items = data.get("results") or data.get("videos") or data.get("data") or []
        if not items:
            return f"No Kanopy hits for: {query}"

        out = [f"## Kanopy (free with library card): {query}\n"]
        for it in items[: self.valves.MAX_RESULTS]:
            title = it.get("title", "—")
            year = it.get("year") or it.get("release_year", "—")
            link = it.get("url") or (f"https://www.kanopy.com/video/{it.get('id')}" if it.get("id") else "")
            out.append(f"**{title}** ({year})\n   {link}\n")
        return "\n".join(out)

    # ── Torrent crawl (delegates to torrent_search + torrent_aggregators) ─

    async def crawl_torrents(
        self,
        query: str,
        kind: str = "all",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Crawl every TV/movie torrent repository and meta-aggregator the
        suite has access to: YTS (movies), EZTV (TV), Nyaa (anime), apibay
        (Pirate Bay), the Internet Archive .torrent collection, Knaben
        meta-search, Solid Torrents, 1337x, TorrentGalaxy, BTDigg.

        Returns magnet URIs and .torrent links — pair with the qBittorrent
        tool to actually download.
        :param query: Title query.
        :param kind: "all", "movie", "tv", "anime". Routes to the right
                     specialist sources.
        :return: Combined Markdown digest of torrent results.
        """
        ts = _load_tool("torrent_search")
        ta = _load_tool("torrent_aggregators")
        if not ts and not ta:
            return "Neither torrent_search nor torrent_aggregators is loadable."

        coros: list[Any] = []
        labels: list[str] = []

        kind_l = (kind or "all").lower()
        if ts:
            if kind_l in ("all", "movie"):
                coros.append(ts.search_movies(query))
                labels.append("YTS (movies)")
            if kind_l in ("all", "tv"):
                coros.append(ts.search_tv(query=query))
                labels.append("EZTV (TV)")
            if kind_l in ("all", "anime"):
                coros.append(ts.search_anime(query))
                labels.append("Nyaa (anime)")
            if kind_l == "all":
                coros.append(ts.search_general(query))
                labels.append("Pirate Bay (general)")
                coros.append(ts.search_internet_archive(query, media_type="movies"))
                labels.append("Internet Archive (.torrent, public domain)")

        if ta:
            cat_map = {"movie": "movie", "tv": "tv", "anime": "anime"}
            cat = cat_map.get(kind_l, "")
            coros.append(ta.search_all(query, category=cat))
            labels.append("Meta aggregators (Knaben/Solid/1337x/TGx/BTDigg)")

        results = await asyncio.gather(*coros, return_exceptions=True)
        out = [f"# Torrent crawl: {query}  (kind: {kind_l})"]
        for label, res in zip(labels, results):
            if isinstance(res, Exception):
                out.append(f"\n## {label}\n_(failed: {res})_")
            else:
                out.append(f"\n## {label}\n{res}")
        return "\n".join(out)

    # ── Aggregator ────────────────────────────────────────────────────────

    async def find_anywhere(
        self,
        query: str,
        include_paid_with_card: bool = True,
        include_torrents: bool = True,
        torrent_kind: str = "all",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Run Internet Archive, Tubi, Pluto, Crackle, PBS in parallel for one
        title — and (when `include_torrents`) crawl YTS/EZTV/Nyaa/Pirate
        Bay/IA/Knaben/Solid Torrents/1337x/TorrentGalaxy/BTDigg in parallel
        too. Internet Archive (downloads) and Tubi/Pluto/Crackle/PBS
        (streams) are the legal free path; torrent results are surfaced as
        a fallback when the title isn't free-to-stream.
        :param query: Title or topic.
        :param include_paid_with_card: Include Kanopy (library-card free).
        :param include_torrents: Crawl torrent indexers + meta-aggregators.
        :param torrent_kind: "all", "movie", "tv", "anime" — narrows the
                             torrent crawl.
        :return: Combined Markdown digest.
        """
        coros = [
            self.archive_org_films(query, collection="feature_films"),
            self.archive_org_films(query, collection="classic_tv"),
            self.tubi_search(query),
            self.pluto_search(query),
            self.crackle_search(query),
            self.pbs_search(query),
        ]
        labels = [
            "Internet Archive — feature films (DOWNLOAD ok)",
            "Internet Archive — classic TV (DOWNLOAD ok)",
            "Tubi (stream, free w/ ads)",
            "Pluto TV (stream, free w/ ads)",
            "Crackle (stream, free w/ ads, US)",
            "PBS (stream, free; some Passport-only)",
        ]
        if include_paid_with_card:
            coros.append(self.kanopy_search(query))
            labels.append("Kanopy (free w/ library card)")
        if include_torrents:
            coros.append(self.crawl_torrents(query, kind=torrent_kind))
            labels.append("Torrent crawl (YTS/EZTV/Nyaa/PB/IA + meta)")

        results = await asyncio.gather(*coros, return_exceptions=True)

        out = [f"# Free films/TV digest: {query}"]
        for label, res in zip(labels, results):
            if isinstance(res, Exception):
                out.append(f"\n## {label}\n_(failed: {res})_")
            else:
                out.append("\n" + str(res))
        return "\n".join(out)
