"""
title: Free Music — FMA, Internet Archive Audio, Jamendo
author: local-ai-stack
description: Search and download legitimately-free lossless / high-quality lossy music from three open catalogues: Free Music Archive (CC-licensed FLAC/MP3 from independent artists), Internet Archive Audio (millions of public-domain & CC live concerts, 78rpm rips, vintage broadcasts — often FLAC), and Jamendo (CC-licensed releases from 600k+ artists, FLAC available with a free API key). Pairs with the filesystem tool: the model picks a track, this tool streams the audio file straight to disk.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path
from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field


class Tools:
    class Valves(BaseModel):
        FMA_URL: str = Field(
            default="https://freemusicarchive.org",
            description="Free Music Archive base URL.",
        )
        IA_URL: str = Field(
            default="https://archive.org",
            description="Internet Archive base URL — the audio collection lives at /details/audio.",
        )
        JAMENDO_CLIENT_ID: str = Field(
            default="",
            description="Free Jamendo API client_id from https://devportal.jamendo.com — empty disables Jamendo search.",
        )
        DEFAULT_DOWNLOAD_DIR: str = Field(
            default=str(Path.home() / "Music" / "free-music"),
            description="Where to save downloaded tracks.",
        )
        DEFAULT_LIMIT: int = Field(
            default=20,
            description="Max results per search call.",
        )
        DEFAULT_FORMAT: str = Field(
            default="flac",
            description="Preferred download format when multiple are available: flac, mp3, ogg.",
        )

    def __init__(self):
        self.valves = self.Valves()

    # ── Internet Archive (vast public-domain + CC) ───────────────────────

    async def search_internet_archive(
        self,
        query: str,
        only_lossless: bool = False,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search the Internet Archive's audio catalogue. Hits include vintage
        78rpm rips, live concerts (Grateful Dead etc.), public-domain
        recordings, radio broadcasts. Most predate copyright or carry CC.
        :param query: Search query.
        :param only_lossless: When True, restrict to items tagged with FLAC.
        :return: Identifier, title, year, FLAC/MP3 URLs.
        """
        q = f"({query}) AND mediatype:audio"
        if only_lossless:
            q += " AND format:FLAC"
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(
                f"{self.valves.IA_URL}/advancedsearch.php",
                params={
                    "q": q,
                    "fl[]": "identifier,title,year,creator,downloads",
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
            rows.append(
                f"{(d.get('title') or ident)[:55]:<55}  by {(d.get('creator') or '?')[:25]:<25}  "
                f"year={d.get('year','?')}  dl={d.get('downloads','?')}\n"
                f"  details: {self.valves.IA_URL}/details/{ident}\n"
                f"  files:   {self.valves.IA_URL}/download/{ident}/"
            )
        return "\n".join(rows) if rows else "(no matches)"

    async def list_internet_archive_files(
        self,
        identifier: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        List the audio files inside a specific Internet Archive item. Use
        the identifier from search_internet_archive() to drill in and pick
        a FLAC/MP3 to download.
        :param identifier: IA item identifier (e.g. "gd1977-05-08.sbd.miller.97065.flac16").
        :return: Filename, size, format, direct download URL.
        """
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{self.valves.IA_URL}/metadata/{identifier}/files")
        if r.status_code != 200:
            return f"HTTP {r.status_code}"
        files = (r.json() or {}).get("result", []) or []
        rows = []
        for f in files:
            fmt = (f.get("format") or "").lower()
            if not any(k in fmt for k in ("flac", "mp3", "ogg", "vorbis", "wav")):
                continue
            url = f"{self.valves.IA_URL}/download/{identifier}/{urllib.parse.quote(f.get('name',''))}"
            rows.append(
                f"{(f.get('name') or '')[:55]:<55}  {f.get('format','?'):<14}  "
                f"size={f.get('size','?')}\n  {url}"
            )
        return "\n".join(rows) if rows else "(no audio files)"

    # ── Free Music Archive (CC-licensed indie) ───────────────────────────

    async def search_fma(
        self,
        query: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search Free Music Archive (FMA). FMA hosts CC-licensed indie music
        with free FLAC/MP3 downloads. The `q=` query parameter on the
        listing endpoint returns track-level matches.
        :param query: Search query.
        :return: Track title, artist, license, and a FMA listing URL.
        """
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
            r = await c.get(
                f"{self.valves.FMA_URL}/search",
                params={"quicksearch": query},
                headers={"User-Agent": "local-ai-stack/1.0"},
            )
        if r.status_code != 200:
            return f"HTTP {r.status_code}"
        # FMA stopped publishing a stable JSON API in 2018 — fall back to
        # parsing the HTML for track ids + titles. We extract the data-track-info
        # attributes that the player uses.
        import re
        rows = []
        for m in re.finditer(
            r'data-track-info=\'([^\']+)\'',
            r.text,
        ):
            try:
                info = json.loads(m.group(1).replace("&quot;", '"'))
            except json.JSONDecodeError:
                continue
            rows.append(
                f"{(info.get('title') or '')[:45]:<45}  by {(info.get('artistName') or '?')[:25]:<25}  "
                f"{info.get('license_title','?')}\n  {info.get('playbackUrl','')}"
            )
            if len(rows) >= self.valves.DEFAULT_LIMIT:
                break
        return "\n".join(rows) if rows else "(no matches — try a broader query)"

    # ── Jamendo (CC-licensed, 600k+ artists) ─────────────────────────────

    async def search_jamendo(
        self,
        query: str,
        format: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search Jamendo's CC-licensed catalogue for tracks. Requires a free
        client_id from https://devportal.jamendo.com.
        :param query: Search query (track or artist).
        :param format: flac, mp32 (320kbps), mp31 (96kbps). Default: tool's DEFAULT_FORMAT.
        :return: Track id, name, artist, license, audio download URL.
        """
        if not self.valves.JAMENDO_CLIENT_ID:
            return "JAMENDO_CLIENT_ID not set on the Free Music tool's Valves."
        fmt = format or {"flac": "flac", "mp3": "mp32"}.get(self.valves.DEFAULT_FORMAT, "mp32")
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(
                "https://api.jamendo.com/v3.0/tracks/",
                params={
                    "client_id": self.valves.JAMENDO_CLIENT_ID,
                    "format": "json",
                    "limit": self.valves.DEFAULT_LIMIT,
                    "search": query,
                    "audioformat": fmt,
                    "include": "musicinfo+licenses",
                },
            )
        if r.status_code != 200:
            return f"HTTP {r.status_code}: {r.text[:200]}"
        results = (r.json() or {}).get("results", []) or []
        rows = []
        for t in results:
            rows.append(
                f"{t.get('id'):<10} {(t.get('name') or '')[:40]:<40}  by {(t.get('artist_name') or '?')[:25]:<25}  "
                f"{t.get('license_ccurl','?').rsplit('/',2)[-2:][0] if t.get('license_ccurl') else '?'}\n"
                f"  audio: {t.get('audiodownload') or t.get('audio')}"
            )
        return "\n".join(rows) if rows else "(no matches)"

    # ── Direct download (any audio URL) ──────────────────────────────────

    async def download(
        self,
        url: str,
        save_path: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Stream a free-music audio URL straight to disk. Use with URLs from
        the search methods above (Internet Archive, FMA, Jamendo) or any
        other freely-licensed audio source.
        :param url: Direct .flac / .mp3 / .ogg URL.
        :param save_path: Optional output path. When empty, derives a filename and saves under DEFAULT_DOWNLOAD_DIR.
        :return: Confirmation with bytes written.
        """
        out_dir = Path(self.valves.DEFAULT_DOWNLOAD_DIR).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)
        if save_path:
            out_path = Path(save_path).expanduser()
            out_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            from urllib.parse import urlparse, unquote
            name = unquote(Path(urlparse(url).path).name) or "track.audio"
            out_path = out_dir / name

        total = 0
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as c:
            async with c.stream("GET", url, headers={"User-Agent": "local-ai-stack/1.0"}) as r:
                if r.status_code != 200:
                    return f"HTTP {r.status_code}"
                with out_path.open("wb") as f:
                    async for chunk in r.aiter_bytes():
                        f.write(chunk)
                        total += len(chunk)
        return f"saved {total} bytes -> {out_path}"
