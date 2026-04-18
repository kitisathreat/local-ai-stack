"""
title: Qobuz-DL — Hi-Res Music Search & Download
author: local-ai-stack
description: Search the Qobuz catalogue (tracks, albums, artists) and download lossless/hi-res audio via qobuz-dl. Uses the Qobuz public API for structured search and shells out to the qobuz-dl CLI for downloads.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import asyncio
import os
import re
import shlex
import shutil
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
from pydantic import BaseModel, Field


QOBUZ_API = "https://www.qobuz.com/api.json/0.2"
# Known public app_id shipped with qobuz-dl; users can override via valve.
DEFAULT_APP_ID = "950096963"
UA = "Mozilla/5.0 local-ai-stack qobuz-dl-tool/1.0"

_URL_RE = re.compile(r"https?://(?:open|play|www)\.qobuz\.com/\S+", re.IGNORECASE)


def _fmt_duration(ms_or_s: int) -> str:
    if not ms_or_s:
        return ""
    secs = ms_or_s // 1000 if ms_or_s > 10_000 else ms_or_s
    return f"{secs // 60}:{secs % 60:02d}"


class Tools:
    class Valves(BaseModel):
        QOBUZ_APP_ID: str = Field(
            default=DEFAULT_APP_ID,
            description="Qobuz public API app_id. Override with your own if the default is rate-limited.",
        )
        QOBUZ_USER_AUTH_TOKEN: str = Field(
            default_factory=lambda: os.environ.get("QOBUZ_USER_AUTH_TOKEN", ""),
            description="Optional Qobuz user_auth_token (for catalogue calls that need login).",
        )
        QOBUZ_DL_BIN: str = Field(
            default="qobuz-dl",
            description="Path to the qobuz-dl executable. Run `qobuz-dl -r` once on the host to configure credentials.",
        )
        DOWNLOAD_DIR: str = Field(
            default="",
            description="Override download directory. Empty uses qobuz-dl's configured default.",
        )
        QUALITY: str = Field(
            default="6",
            description="Audio quality: 5=MP3-320, 6=FLAC 16/44.1, 7=FLAC 24/96, 27=FLAC 24/192",
        )
        MAX_RESULTS: int = Field(default=10, description="Max search results returned")
        TIMEOUT: int = Field(default=25, description="HTTP timeout in seconds")
        DOWNLOAD_TIMEOUT: int = Field(
            default=1800,
            description="Max seconds to wait for a qobuz-dl download to complete",
        )

    def __init__(self):
        self.valves = self.Valves()

    def _headers(self) -> dict:
        h = {"User-Agent": UA, "X-App-Id": self.valves.QOBUZ_APP_ID}
        if self.valves.QOBUZ_USER_AUTH_TOKEN:
            h["X-User-Auth-Token"] = self.valves.QOBUZ_USER_AUTH_TOKEN
        return h

    async def _get(self, path: str, params: dict) -> dict:
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT) as client:
            r = await client.get(
                f"{QOBUZ_API}/{path}", params=params, headers=self._headers()
            )
            r.raise_for_status()
            return r.json()

    async def _status(self, emitter, msg: str, done: bool = False):
        if emitter:
            await emitter(
                {"type": "status", "data": {"description": msg, "done": done}}
            )

    # ── Search ───────────────────────────────────────────────────────────────
    async def search_tracks(
        self,
        query: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search Qobuz for tracks (songs) matching a query.
        :param query: Free-text query, e.g. "miles davis so what" or "radiohead creep"
        :return: Markdown list of matches with track IDs, album, duration and Qobuz URLs
        """
        await self._status(__event_emitter__, f"Searching Qobuz tracks: {query}")
        try:
            data = await self._get(
                "track/search",
                {"query": query, "limit": self.valves.MAX_RESULTS},
            )
            items = (data.get("tracks") or {}).get("items") or []
            if not items:
                return f"No Qobuz tracks found for: {query}"
            lines = [f"## Qobuz Tracks: {query}\n"]
            for t in items:
                tid = t.get("id", "")
                title = t.get("title", "")
                version = t.get("version") or ""
                artist = ((t.get("performer") or {}).get("name")) or (
                    (t.get("album") or {}).get("artist") or {}
                ).get("name", "")
                album = (t.get("album") or {}).get("title", "")
                dur = _fmt_duration(int(t.get("duration") or 0))
                hires = "🎧 Hi-Res" if t.get("hires") else ""
                lines.append(f"**{title}** {f'({version})' if version else ''}".strip())
                lines.append(f"   by {artist} · on *{album}*")
                meta = " · ".join(x for x in [dur, hires] if x)
                if meta:
                    lines.append(f"   {meta}")
                lines.append(f"   Track ID: `{tid}`")
                lines.append(f"   🔗 https://open.qobuz.com/track/{tid}\n")
            await self._status(__event_emitter__, "Qobuz search complete", done=True)
            return "\n".join(lines)
        except httpx.HTTPStatusError as e:
            return self._http_error(e, "track search")
        except Exception as e:
            return f"Qobuz search error: {e}"

    async def search_albums(
        self,
        query: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search Qobuz for albums matching a query.
        :param query: Free-text query, e.g. "kind of blue miles davis" or "OK Computer"
        :return: Markdown list of albums with IDs, track count, release date and Qobuz URLs
        """
        await self._status(__event_emitter__, f"Searching Qobuz albums: {query}")
        try:
            data = await self._get(
                "album/search",
                {"query": query, "limit": self.valves.MAX_RESULTS},
            )
            items = (data.get("albums") or {}).get("items") or []
            if not items:
                return f"No Qobuz albums found for: {query}"
            lines = [f"## Qobuz Albums: {query}\n"]
            for a in items:
                aid = a.get("id", "")
                title = a.get("title", "")
                artist = (a.get("artist") or {}).get("name", "")
                tracks = a.get("tracks_count", "?")
                date = a.get("release_date_original") or a.get("released_at") or ""
                if isinstance(date, int):
                    # unix timestamp
                    from datetime import datetime, timezone

                    date = datetime.fromtimestamp(date, tz=timezone.utc).strftime(
                        "%Y-%m-%d"
                    )
                hires = "🎧 Hi-Res" if a.get("hires") else ""
                lines.append(f"**{title}** — {artist}")
                meta = " · ".join(
                    x for x in [f"{tracks} tracks", str(date), hires] if x
                )
                if meta:
                    lines.append(f"   {meta}")
                lines.append(f"   Album ID: `{aid}`")
                lines.append(f"   🔗 https://open.qobuz.com/album/{aid}\n")
            await self._status(__event_emitter__, "Qobuz search complete", done=True)
            return "\n".join(lines)
        except httpx.HTTPStatusError as e:
            return self._http_error(e, "album search")
        except Exception as e:
            return f"Qobuz search error: {e}"

    async def search_artists(
        self,
        query: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search Qobuz for artists.
        :param query: Artist name, e.g. "Radiohead"
        :return: Markdown list of artist matches with IDs and Qobuz URLs
        """
        try:
            data = await self._get(
                "artist/search",
                {"query": query, "limit": self.valves.MAX_RESULTS},
            )
            items = (data.get("artists") or {}).get("items") or []
            if not items:
                return f"No Qobuz artists found for: {query}"
            lines = [f"## Qobuz Artists: {query}\n"]
            for a in items:
                aid = a.get("id", "")
                name = a.get("name", "")
                albums = a.get("albums_count", "?")
                lines.append(f"**{name}** — {albums} albums · ID `{aid}`")
                lines.append(f"   🔗 https://open.qobuz.com/artist/{aid}\n")
            return "\n".join(lines)
        except httpx.HTTPStatusError as e:
            return self._http_error(e, "artist search")
        except Exception as e:
            return f"Qobuz search error: {e}"

    # ── Download ─────────────────────────────────────────────────────────────
    async def download_track(
        self,
        track_id: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Download a single Qobuz track by its numeric track ID.
        :param track_id: Qobuz track ID (from search_tracks). You can also pass a full Qobuz track URL.
        :return: Summary of the downloaded file(s) on disk
        """
        url = self._coerce_url(track_id, kind="track")
        return await self._run_qobuz_dl(url, __event_emitter__)

    async def download_album(
        self,
        album_id: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Download an entire Qobuz album by its numeric album ID.
        :param album_id: Qobuz album ID (from search_albums). You can also pass a full Qobuz album URL.
        :return: Summary of the downloaded files on disk
        """
        url = self._coerce_url(album_id, kind="album")
        return await self._run_qobuz_dl(url, __event_emitter__)

    async def download_from_url(
        self,
        url: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Download any Qobuz resource by URL (track, album, playlist, or artist discography).
        :param url: Full Qobuz URL, e.g. https://open.qobuz.com/album/123456
        :return: Summary of the downloaded files on disk
        """
        if not _URL_RE.match(url.strip()):
            return (
                "That does not look like a Qobuz URL. "
                "Expected something like https://open.qobuz.com/album/12345."
            )
        return await self._run_qobuz_dl(url.strip(), __event_emitter__)

    async def lucky_download(
        self,
        query: str,
        kind: str = "track",
        count: int = 1,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        "I'm feeling lucky" — search Qobuz for a query and download the first result(s).
        :param query: Free-text query, e.g. "aphex twin xtal"
        :param kind: One of track | album | artist | playlist (default: track)
        :param count: Number of top results to grab (default: 1)
        :return: Summary of the downloaded files on disk
        """
        kind = kind.lower().strip()
        if kind not in {"track", "album", "artist", "playlist"}:
            return f"Invalid kind `{kind}`. Use one of: track, album, artist, playlist."
        args = ["lucky", "--type", kind, "-n", str(max(1, int(count))), query]
        return await self._run_qobuz_dl_args(args, __event_emitter__, label=f"lucky {kind}: {query}")

    # ── Internals ────────────────────────────────────────────────────────────
    def _coerce_url(self, ident: str, kind: str) -> str:
        s = ident.strip()
        if _URL_RE.match(s):
            return s
        if s.isdigit():
            return f"https://open.qobuz.com/{kind}/{s}"
        return s  # let qobuz-dl raise a clear error

    async def _run_qobuz_dl(
        self, url: str, emitter: Callable[[dict], Any] = None
    ) -> str:
        return await self._run_qobuz_dl_args(["dl", url], emitter, label=url)

    async def _run_qobuz_dl_args(
        self,
        extra: list,
        emitter: Callable[[dict], Any] = None,
        label: str = "",
    ) -> str:
        bin_path = shutil.which(self.valves.QOBUZ_DL_BIN) or self.valves.QOBUZ_DL_BIN
        if not Path(bin_path).exists() and not shutil.which(bin_path):
            return (
                f"`qobuz-dl` executable not found (looked for `{self.valves.QOBUZ_DL_BIN}`).\n\n"
                "Install it on the host or in the container:\n"
                "```\npip install qobuz-dl\nqobuz-dl -r  # one-time config: email, password, quality, download dir\n```\n"
                "Then set the QOBUZ_DL_BIN valve if it lives outside your PATH."
            )

        cmd: list = [bin_path]
        if self.valves.DOWNLOAD_DIR:
            # qobuz-dl accepts -d/--directory for download root
            cmd += ["-d", self.valves.DOWNLOAD_DIR]
        if self.valves.QUALITY:
            cmd += ["-q", self.valves.QUALITY]
        cmd += extra

        await self._status(emitter, f"qobuz-dl starting: {label or extra[-1]}")

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
            try:
                stdout_b, _ = await asyncio.wait_for(
                    proc.communicate(), timeout=self.valves.DOWNLOAD_TIMEOUT
                )
            except asyncio.TimeoutError:
                proc.kill()
                return (
                    f"qobuz-dl timed out after {self.valves.DOWNLOAD_TIMEOUT}s. "
                    "Increase DOWNLOAD_TIMEOUT valve or run manually."
                )
        except FileNotFoundError:
            return f"qobuz-dl executable not runnable: {bin_path}"
        except Exception as e:
            return f"qobuz-dl launch error: {e}"

        out = stdout_b.decode("utf-8", errors="replace")
        tail = out[-4000:] if len(out) > 4000 else out
        saved = self._extract_saved_paths(out)

        await self._status(emitter, "qobuz-dl finished", done=True)

        header = (
            f"## Qobuz Download\n"
            f"Command: `{' '.join(shlex.quote(c) for c in cmd)}`\n"
            f"Exit code: **{proc.returncode}**\n"
        )
        if saved:
            header += f"\n### Files saved ({len(saved)})\n" + "\n".join(
                f"- `{p}`" for p in saved[:50]
            )
            if len(saved) > 50:
                header += f"\n- …and {len(saved) - 50} more"
        body = f"\n\n<details><summary>qobuz-dl output</summary>\n\n```\n{tail}\n```\n</details>"
        return header + body

    @staticmethod
    def _extract_saved_paths(output: str) -> list:
        paths: list = []
        for line in output.splitlines():
            # qobuz-dl prints lines like: "Completed: /music/Artist/Album/01 - Track.flac"
            m = re.search(r"[:\-]\s+(/[^\s].+?\.(?:flac|mp3|m4a|ogg))\s*$", line)
            if m:
                paths.append(m.group(1))
        # dedupe preserving order
        seen = set()
        uniq: list = []
        for p in paths:
            if p not in seen:
                seen.add(p)
                uniq.append(p)
        return uniq

    @staticmethod
    def _http_error(e: httpx.HTTPStatusError, label: str) -> str:
        code = e.response.status_code
        if code in (401, 403):
            return (
                f"Qobuz {label} denied (HTTP {code}). "
                "Your APP_ID may be invalid / rate-limited. "
                "Install qobuz-dl locally and run `qobuz-dl -r` to fetch fresh credentials, "
                "then set QOBUZ_APP_ID."
            )
        return f"Qobuz {label} failed: HTTP {code} — {e.response.text[:200]}"
