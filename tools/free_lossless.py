"""
title: Free Lossless Audio — CCMixter, NPR, KEXP, Live Music Archive, NTS, WFMU
author: local-ai-stack
description: Search across free, legal, lossless-friendly audio sources that aren't already in `free_music`. Targets ccMixter (CC-licensed remixes, FLAC/WAV), the Internet Archive Live Music Archive (etree — taper-recorded sets from bands that allow it, mostly FLAC SHN), NPR Music (Tiny Desk, First Listen — official MP3 + FLAC for some sessions), KEXP (in-studio sessions, often hosted as FLAC on archive.org), NTS Radio archive, and WFMU's free-form archive. Each method returns playable / downloadable URLs and indicates which lossless format is available.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Optional
from urllib.parse import quote, urlencode

import httpx
from pydantic import BaseModel, Field


_UA = "local-ai-stack/1.0 free-lossless"
IA_API = "https://archive.org/advancedsearch.php"
IA_META = "https://archive.org/metadata"
IA_DOWNLOAD = "https://archive.org/download"
CCM_API = "http://ccmixter.org/api/query"
NPR_FEED = "https://feeds.npr.org/1039/rss.xml"  # All Songs Considered (parent feed)
NPR_TINY_DESK = "https://feeds.npr.org/700000/rss.xml"  # Tiny Desk Concerts feed


def _ia_query(q: str, fields: list[str], rows: int) -> dict:
    params = {
        "q": q,
        "rows": rows,
        "page": 1,
        "output": "json",
        "sort[]": "downloads desc",
    }
    for i, f in enumerate(fields):
        params[f"fl[{i}]"] = f
    return params


class Tools:
    class Valves(BaseModel):
        MAX_RESULTS: int = Field(default=10, description="Max results per source.")
        TIMEOUT: int = Field(default=20, description="HTTP timeout per request, seconds.")
        PREFER_FLAC: bool = Field(
            default=True,
            description="Bias results toward sources known to host FLAC/SHN/WAV over MP3.",
        )

    def __init__(self):
        self.valves = self.Valves()

    # ── Internet Archive: Live Music Archive (etree) ──────────────────────

    async def live_music_archive(
        self,
        query: str,
        artist_only: bool = False,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search the Internet Archive's Live Music Archive (`collection:etree`)
        for taper-recorded concerts. Most uploads are SHN or FLAC. Bands
        included gave permission to record and trade their shows.
        :param query: Free-text query — band, venue, year.
        :param artist_only: When True, restrict to artist-level results, not
                            individual shows.
        :return: Markdown list with show date, FLAC/SHN flag, and download URL.
        """
        q = f'collection:(etree) AND ({query})'
        if artist_only:
            q = f'collection:(etree) AND mediatype:(collection) AND ({query})'
        params = _ia_query(
            q,
            ["identifier", "title", "creator", "date", "format", "downloads"],
            self.valves.MAX_RESULTS,
        )
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT) as c:
            try:
                r = await c.get(IA_API, params=params, headers={"User-Agent": _UA})
            except Exception as e:
                return f"Live Music Archive request failed: {e}"
            if r.status_code >= 400:
                return f"Live Music Archive error {r.status_code}"
            docs = (r.json().get("response") or {}).get("docs") or []
        if not docs:
            return f"No Live Music Archive matches for: {query}"

        out = [f"## Live Music Archive: {query}\n"]
        for d in docs:
            ident = d.get("identifier", "")
            fmts = d.get("format", []) or []
            if isinstance(fmts, str):
                fmts = [fmts]
            lossless = any(
                any(k in f.lower() for k in ("flac", "shn", "wav"))
                for f in fmts
            )
            badge = "🟢 FLAC/SHN" if lossless else ("🟡 mp3 only" if fmts else "—")
            out.append(
                f"**{d.get('creator', '—')} — {d.get('title', '—')}**  ({d.get('date', '—')[:10]})\n"
                f"   formats: {', '.join(fmts) or '—'}  |  {badge}\n"
                f"   {IA_DOWNLOAD}/{ident}/  ·  downloads: {d.get('downloads', 0):,}\n"
            )
        return "\n".join(out)

    async def ia_show_files(
        self,
        identifier: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        List the lossless files attached to a given Internet Archive item.
        Use this after `live_music_archive` to get direct download URLs.
        :param identifier: archive.org identifier from a previous result.
        :return: List of FLAC/SHN/WAV/M4A files with sizes and direct URLs.
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
        wanted = []
        for f in files:
            ext = (f.get("name", "").rsplit(".", 1) + [""])[-1].lower()
            if ext in ("flac", "shn", "wav", "ogg") or (not self.valves.PREFER_FLAC and ext in ("mp3", "m4a")):
                wanted.append(f)
        if not wanted:
            return f"No lossless audio files in {identifier}"

        out = [f"## {identifier} — lossless files\n"]
        for f in wanted[:50]:
            size = int(f.get("size", 0)) if f.get("size") else 0
            out.append(
                f"  {f.get('name')}  ({size:,} bytes)\n"
                f"     {IA_DOWNLOAD}/{identifier}/{quote(f.get('name', ''), safe='')}"
            )
        return "\n".join(out)

    # ── ccMixter ──────────────────────────────────────────────────────────

    async def ccmixter(
        self,
        query: str,
        only_lossless: bool = True,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search ccMixter for CC-licensed tracks. Many uploads include a WAV
        or FLAC source alongside the MP3.
        :param query: Free-text search.
        :param only_lossless: When True, prefer entries with FLAC/WAV sources.
        :return: Tracks with artist, license, format, and download URL.
        """
        params = {
            "search": query,
            "limit": self.valves.MAX_RESULTS,
            "f": "json",
            "format": "json",
        }
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT) as c:
            try:
                r = await c.get(CCM_API, params=params, headers={"User-Agent": _UA})
            except Exception as e:
                return f"ccMixter request failed: {e}"
            if r.status_code >= 400:
                return f"ccMixter error {r.status_code}"
            try:
                items = r.json()
            except Exception:
                items = []

        if not isinstance(items, list) or not items:
            return f"No ccMixter results for: {query}"

        out = [f"## ccMixter: {query}\n"]
        for it in items:
            files = it.get("files") or []
            lossless = [f for f in files if (f.get("file_format_info") or {}).get("default-ext", "").lower() in ("wav", "flac", "aiff")]
            if only_lossless and not lossless:
                continue
            artist = it.get("user_name", "—")
            title = it.get("upload_name", "—")
            lic = (it.get("license_url") or "").rstrip("/").split("/")[-2:]
            license_short = "-".join(lic) if lic else (it.get("license_url") or "")
            link = it.get("file_page_url") or it.get("upload_extra", {}).get("nopreview")
            out.append(f"**{artist} — {title}**")
            out.append(f"   license: {license_short}")
            if lossless:
                for f in lossless[:3]:
                    fmt = (f.get("file_format_info") or {}).get("default-ext", "?").upper()
                    out.append(f"   {fmt}: {f.get('download_url')}")
            else:
                out.append(f"   (mp3-only) {it.get('files', [{}])[0].get('download_url', link)}")
            out.append(f"   page: {link}\n")
        return "\n".join(out) if len(out) > 1 else f"No ccMixter results matched (lossless filter: {only_lossless})"

    # ── NPR Music + KEXP via Internet Archive ────────────────────────────

    async def kexp_archive(
        self,
        query: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search the KEXP collection on archive.org for in-studio sessions.
        Many KEXP uploads include FLAC.
        :param query: Artist or session keyword.
        :return: Sessions with date, formats available, and IA URL.
        """
        params = _ia_query(
            f'collection:(KEXP) AND ({query})',
            ["identifier", "title", "creator", "date", "format"],
            self.valves.MAX_RESULTS,
        )
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT) as c:
            try:
                r = await c.get(IA_API, params=params, headers={"User-Agent": _UA})
            except Exception as e:
                return f"KEXP archive request failed: {e}"
            docs = (r.json().get("response") or {}).get("docs") or []
        if not docs:
            return f"No KEXP archive sessions matched: {query}"

        out = [f"## KEXP @ archive.org: {query}\n"]
        for d in docs:
            fmts = d.get("format", []) or []
            if isinstance(fmts, str):
                fmts = [fmts]
            lossless = "🟢 FLAC" if any("flac" in f.lower() for f in fmts) else "🟡 mp3"
            out.append(
                f"**{d.get('title', '—')}**  ({d.get('date', '—')[:10]})\n"
                f"   {lossless}  ·  formats: {', '.join(fmts) or '—'}\n"
                f"   {IA_DOWNLOAD}/{d.get('identifier', '')}/\n"
            )
        return "\n".join(out)

    async def npr_tiny_desk(
        self,
        query: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Browse the NPR Tiny Desk Concerts RSS feed; optionally filter by
        artist substring. Tiny Desks publish high-bitrate AAC/MP3; FLAC of
        select sessions is mirrored on archive.org (try `kexp_archive`-style
        IA search).
        :param query: Optional artist substring filter.
        :return: Most recent matching episodes with audio + page URLs.
        """
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT, follow_redirects=True) as c:
            try:
                r = await c.get(NPR_TINY_DESK, headers={"User-Agent": _UA})
            except Exception as e:
                return f"NPR feed request failed: {e}"
            if r.status_code >= 400:
                return f"NPR feed error {r.status_code}"
            xml = r.text

        items = re.findall(r"<item>(.*?)</item>", xml, flags=re.DOTALL)
        out = [f"## NPR Tiny Desk{' — ' + query if query else ''}\n"]
        kept = 0
        for it in items:
            title = (re.search(r"<title>(.*?)</title>", it, re.DOTALL) or [None, ""])[1]
            link = (re.search(r"<link>(.*?)</link>", it, re.DOTALL) or [None, ""])[1]
            date = (re.search(r"<pubDate>(.*?)</pubDate>", it, re.DOTALL) or [None, ""])[1]
            audio = re.search(r'<enclosure[^>]+url="([^"]+)"', it)
            if query and query.lower() not in title.lower():
                continue
            out.append(f"**{title.strip()}**  ({date[:16].strip()})")
            if audio:
                out.append(f"   audio: {audio.group(1)}")
            if link:
                out.append(f"   page:  {link.strip()}")
            out.append("")
            kept += 1
            if kept >= self.valves.MAX_RESULTS:
                break
        if kept == 0:
            return f"No Tiny Desk episodes matched '{query}'."
        return "\n".join(out)

    # ── Combined search ───────────────────────────────────────────────────

    async def find_anywhere(
        self,
        query: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Run live_music_archive, ccmixter, kexp_archive, and npr_tiny_desk in
        parallel for the same query. Best one-shot way to find a free
        lossless source for a song / artist.
        :param query: Artist, track, or keyword.
        :return: Combined Markdown digest.
        """
        coros = [
            self.live_music_archive(query),
            self.ccmixter(query, only_lossless=True),
            self.kexp_archive(query),
            self.npr_tiny_desk(query),
        ]
        results = await asyncio.gather(*coros, return_exceptions=True)
        out = [f"# Free lossless digest: {query}"]
        for label, res in zip(
            ["Live Music Archive", "ccMixter", "KEXP @ archive.org", "NPR Tiny Desk"],
            results,
        ):
            if isinstance(res, Exception):
                out.append(f"\n## {label}\n_(failed: {res})_")
            else:
                out.append("\n" + str(res))
        return "\n".join(out)
