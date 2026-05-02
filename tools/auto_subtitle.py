"""
title: Auto Subtitle — Fetch Subtitles for Films & TV via OpenSubtitles
author: local-ai-stack
description: For each video file in a directory, query OpenSubtitles by filename hash + filename heuristics, download the best-matching subtitle, and drop the .srt/.vtt next to the video. Uses OpenSubtitles' free REST API (free key signup at opensubtitles.com/api). Pairs with media_library — run media_library.organize_films / organize_tv first, then auto_subtitle on the resulting folder.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

import os
import struct
from pathlib import Path
from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field


_VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".webm"}
_BASE = "https://api.opensubtitles.com/api/v1"


def _os_hash(path: Path) -> str:
    """OpenSubtitles' 64-bit hash. First+last 64KB block + filesize."""
    longlong_size = struct.calcsize("q")
    chunk_size = 64 * 1024
    try:
        size = path.stat().st_size
        if size < chunk_size * 2:
            return ""
        h = size
        with path.open("rb") as f:
            for _ in range(chunk_size // longlong_size):
                buf = f.read(longlong_size)
                if len(buf) < longlong_size: break
                h = (h + struct.unpack("<q", buf)[0]) & 0xFFFFFFFFFFFFFFFF
            f.seek(-chunk_size, os.SEEK_END)
            for _ in range(chunk_size // longlong_size):
                buf = f.read(longlong_size)
                if len(buf) < longlong_size: break
                h = (h + struct.unpack("<q", buf)[0]) & 0xFFFFFFFFFFFFFFFF
        return f"{h:016x}"
    except Exception:
        return ""


class Tools:
    class Valves(BaseModel):
        OPENSUBTITLES_API_KEY: str = Field(
            default="",
            description="Free API key from https://www.opensubtitles.com/en/consumers.",
        )
        USERNAME: str = Field(default="", description="OpenSubtitles username (optional but raises rate limits).")
        PASSWORD: str = Field(default="", description="OpenSubtitles password (optional).")
        LANGUAGES: str = Field(default="en", description="Comma-separated ISO codes preference.")
        TIMEOUT: int = Field(default=20)
        UA: str = Field(default="local-ai-stack v1.0")

    def __init__(self):
        self.valves = self.Valves()
        self._token: str = ""

    def _headers(self) -> dict:
        h = {
            "User-Agent": self.valves.UA,
            "Api-Key": self.valves.OPENSUBTITLES_API_KEY,
            "Content-Type": "application/json",
        }
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    async def _login(self, client: httpx.AsyncClient) -> str:
        if self._token or not (self.valves.USERNAME and self.valves.PASSWORD):
            return ""
        r = await client.post(f"{_BASE}/login", json={
            "username": self.valves.USERNAME,
            "password": self.valves.PASSWORD,
        }, headers=self._headers())
        if r.status_code == 200:
            self._token = (r.json() or {}).get("token", "")
        return self._token

    async def _search(
        self,
        client: httpx.AsyncClient,
        moviehash: str,
        query: str,
        languages: str,
    ) -> list[dict]:
        params: dict = {"languages": languages}
        if moviehash:
            params["moviehash"] = moviehash
        if query:
            params["query"] = query
        r = await client.get(f"{_BASE}/subtitles", params=params, headers=self._headers())
        if r.status_code != 200:
            return []
        return (r.json() or {}).get("data", []) or []

    async def _request_download(
        self,
        client: httpx.AsyncClient,
        file_id: int,
    ) -> str | None:
        r = await client.post(f"{_BASE}/download",
                              json={"file_id": file_id},
                              headers=self._headers())
        if r.status_code != 200:
            return None
        return (r.json() or {}).get("link")

    async def fetch(
        self,
        directory: str,
        recursive: bool = True,
        languages: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Walk a directory and drop a `.srt` next to each video without one.
        :param directory: Films / TV folder.
        :param recursive: Walk subdirectories.
        :param languages: Override the default LANGUAGES valve (e.g. "en,es").
        :return: Per-file action log.
        """
        if not self.valves.OPENSUBTITLES_API_KEY:
            return "OPENSUBTITLES_API_KEY not set on the auto_subtitle tool's Valves."
        d = Path(directory).expanduser().resolve()
        if not d.exists():
            return f"Not found: {d}"
        videos = (
            [p for p in d.rglob("*") if p.is_file() and p.suffix.lower() in _VIDEO_EXTS]
            if recursive else
            [p for p in d.iterdir() if p.is_file() and p.suffix.lower() in _VIDEO_EXTS]
        )
        if not videos:
            return f"(no videos under {d})"
        langs = languages or self.valves.LANGUAGES
        log = []
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT) as client:
            await self._login(client)
            for v in videos:
                srt = v.with_suffix(".srt")
                if srt.exists():
                    log.append(f"-- have {srt.name}")
                    continue
                hashed = _os_hash(v)
                results = await self._search(client, hashed, v.stem, langs)
                if not results:
                    log.append(f"-- no subs for {v.name}")
                    continue
                top = results[0]
                attrs = top.get("attributes", {})
                files = attrs.get("files", [])
                if not files:
                    log.append(f"-- no files in match for {v.name}")
                    continue
                file_id = files[0].get("file_id")
                link = await self._request_download(client, file_id)
                if not link:
                    log.append(f"-- download URL refused for {v.name}")
                    continue
                try:
                    rr = await client.get(link, follow_redirects=True)
                    srt.write_bytes(rr.content)
                    log.append(f"OK {srt.name}  lang={attrs.get('language')}  src={attrs.get('release','?')[:50]}")
                except Exception as e:
                    log.append(f"-- write failed for {v.name}: {e}")
        return "\n".join(log)
