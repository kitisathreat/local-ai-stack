"""
title: Soulseek — P2P Music Search & Download (via slskd)
author: local-ai-stack
description: Search Soulseek for music files and queue downloads through a local slskd daemon. Exposes search, download, transfer-listing, and cleanup operations over slskd's REST API.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import asyncio
import os
import re
from typing import Any, Callable, Optional
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field


_AUDIO_EXT = (
    ".flac",
    ".mp3",
    ".m4a",
    ".aac",
    ".ogg",
    ".opus",
    ".wav",
    ".wv",
    ".alac",
    ".aiff",
)


def _human_size(n: int) -> str:
    if not n:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    f = float(n)
    while f >= 1024 and i < len(units) - 1:
        f /= 1024
        i += 1
    return f"{f:.1f} {units[i]}"


def _basename(path: str) -> str:
    return re.split(r"[\\/]", path.rstrip("/\\"))[-1]


class Tools:
    class Valves(BaseModel):
        SLSKD_URL: str = Field(
            default="http://slskd:5030",
            description="Base URL of your slskd instance (Soulseek daemon).",
        )
        SLSKD_API_KEY: str = Field(
            default="",
            description="slskd API key (Settings > Options > Security > API keys).",
        )
        SLSKD_USERNAME: str = Field(
            default="",
            description="Optional username for slskd basic auth (if API key not set).",
        )
        SLSKD_PASSWORD: str = Field(
            default="",
            description="Optional password for slskd basic auth (if API key not set).",
        )
        SEARCH_WAIT_SECONDS: int = Field(
            default=15,
            description="How long to let Soulseek collect peers' responses before returning results.",
        )
        MAX_RESULTS: int = Field(
            default=20,
            description="Max number of candidate files returned by a search.",
        )
        MIN_BITRATE: int = Field(
            default=0,
            description="Minimum acceptable bitrate (kbps). 0 = no filter. FLAC counts as lossless.",
        )
        AUDIO_ONLY: bool = Field(
            default=True,
            description="Filter to common audio extensions only (flac/mp3/m4a/ogg/opus/wav).",
        )
        PREFER_LOSSLESS: bool = Field(
            default=True,
            description="Rank FLAC/WAV/ALAC above lossy formats in search output.",
        )
        TIMEOUT: int = Field(default=25, description="HTTP timeout for slskd calls.")

    def __init__(self):
        self.valves = self.Valves()

    # ── HTTP plumbing ────────────────────────────────────────────────────────
    def _headers(self) -> dict:
        h = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.valves.SLSKD_API_KEY:
            h["X-API-Key"] = self.valves.SLSKD_API_KEY
        return h

    def _auth(self):
        if self.valves.SLSKD_API_KEY:
            return None
        if self.valves.SLSKD_USERNAME or self.valves.SLSKD_PASSWORD:
            return (self.valves.SLSKD_USERNAME, self.valves.SLSKD_PASSWORD)
        return None

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.valves.SLSKD_URL.rstrip("/"),
            headers=self._headers(),
            auth=self._auth(),
            timeout=self.valves.TIMEOUT,
        )

    async def _status(self, emitter, msg: str, done: bool = False):
        if emitter:
            await emitter(
                {"type": "status", "data": {"description": msg, "done": done}}
            )

    def _connection_hint(self) -> str:
        return (
            f"Cannot reach slskd at `{self.valves.SLSKD_URL}`.\n"
            "- Make sure the slskd container is running and reachable (see https://github.com/slskd/slskd).\n"
            "- Set SLSKD_URL + SLSKD_API_KEY valves.\n"
            "- The API key is created in slskd under Settings > Options > Security > API Keys."
        )

    # ── Public: server / session info ────────────────────────────────────────
    async def status(self, __user__: Optional[dict] = None) -> str:
        """
        Check whether slskd is running and logged into the Soulseek network.
        :return: Markdown report of server state and active transfers count
        """
        try:
            async with self._client() as c:
                app = (await c.get("/api/v0/application")).json()
                try:
                    dls = (await c.get("/api/v0/transfers/downloads")).json()
                except Exception:
                    dls = []
                try:
                    uls = (await c.get("/api/v0/transfers/uploads")).json()
                except Exception:
                    uls = []
            server = app.get("server", {}) if isinstance(app, dict) else {}
            state = server.get("state") or server.get("status") or "?"
            version = app.get("version") if isinstance(app, dict) else "?"
            return (
                "## slskd status\n"
                f"- URL: {self.valves.SLSKD_URL}\n"
                f"- Version: {version}\n"
                f"- Soulseek server: **{state}**\n"
                f"- Active downloads: {self._count_files(dls)}\n"
                f"- Active uploads: {self._count_files(uls)}\n"
            )
        except httpx.ConnectError:
            return self._connection_hint()
        except Exception as e:
            return f"slskd status error: {e}"

    # ── Search ───────────────────────────────────────────────────────────────
    async def search(
        self,
        query: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search the Soulseek network for audio files matching a query.
        Blocks for up to SEARCH_WAIT_SECONDS while peers respond, then returns the top matches.
        :param query: Free-text search, e.g. "aphex twin selected ambient works flac"
        :return: Markdown table of top files with uploader, size, bitrate and the exact filename to download
        """
        await self._status(__event_emitter__, f"Soulseek searching: {query}")
        try:
            async with self._client() as c:
                r = await c.post(
                    "/api/v0/searches", json={"searchText": query}
                )
                if r.status_code == 401:
                    return "slskd rejected the credentials (401). Set SLSKD_API_KEY."
                r.raise_for_status()
                sid = r.json().get("id") or r.json().get("searchId") or r.json().get("Id")
                if not sid:
                    return f"slskd did not return a search id: {r.text[:200]}"

                # Poll until complete or timeout.
                deadline = asyncio.get_event_loop().time() + self.valves.SEARCH_WAIT_SECONDS
                last_count = 0
                while True:
                    await asyncio.sleep(2)
                    info = (await c.get(f"/api/v0/searches/{sid}")).json()
                    state = (info.get("state") or "").lower()
                    files = info.get("fileCount") or info.get("files") or 0
                    if isinstance(files, int):
                        last_count = files
                    await self._status(
                        __event_emitter__,
                        f"Soulseek responses: {last_count} files ({state})",
                    )
                    if state.startswith("completed") or asyncio.get_event_loop().time() >= deadline:
                        break

                # Fetch responses (one per peer).
                resp = await c.get(f"/api/v0/searches/{sid}/responses")
                responses = resp.json() if resp.status_code == 200 else []
                # Best effort: stop the search once we have what we need.
                try:
                    await c.delete(f"/api/v0/searches/{sid}")
                except Exception:
                    pass
        except httpx.ConnectError:
            return self._connection_hint()
        except httpx.HTTPStatusError as e:
            return f"slskd search HTTP {e.response.status_code}: {e.response.text[:200]}"
        except Exception as e:
            return f"slskd search error: {e}"

        hits = self._flatten(responses)
        hits = self._filter(hits)
        hits = self._rank(hits)[: self.valves.MAX_RESULTS]

        if not hits:
            return f"No Soulseek hits for `{query}` within {self.valves.SEARCH_WAIT_SECONDS}s."

        lines = [
            f"## Soulseek results for `{query}`",
            f"_Top {len(hits)} of {len(self._flatten(responses))} candidates, collected in {self.valves.SEARCH_WAIT_SECONDS}s._\n",
            "| # | File | Size | Bitrate | Uploader (slots/speed) |",
            "|---|------|------|---------|------------------------|",
        ]
        for i, h in enumerate(hits, start=1):
            fname = _basename(h["filename"])
            size = _human_size(h.get("size") or 0)
            br = self._bitrate_label(h)
            up = h.get("username") or "?"
            slots = "✓" if h.get("hasFreeUploadSlot") else "✗"
            speed = _human_size(h.get("uploadSpeed") or 0) + "/s"
            lines.append(
                f"| {i} | `{fname}` | {size} | {br} | {up} ({slots}, {speed}) |"
            )
        lines.append("")
        lines.append(
            "Download a specific result with `download(username, filename)` using the exact filename above, "
            "or call `download_best(query)` to grab the top hit."
        )
        lines.append("\n<details><summary>Full paths</summary>\n")
        for i, h in enumerate(hits, start=1):
            lines.append(f"{i}. `{h.get('username')}` :: `{h.get('filename')}`")
        lines.append("\n</details>")
        await self._status(__event_emitter__, "Soulseek search done", done=True)
        return "\n".join(lines)

    async def download(
        self,
        username: str,
        filename: str,
        size: int = 0,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Queue a download from a specific Soulseek user.
        :param username: Soulseek username that shared the file (from search results)
        :param filename: Full remote file path as returned by search (copy it exactly)
        :param size: Optional file size in bytes (improves slskd accounting)
        :return: Confirmation message with the transfer state
        """
        if not username or not filename:
            return "Both `username` and `filename` are required."
        await self._status(__event_emitter__, f"Queueing download: {_basename(filename)}")
        payload = [{"filename": filename, "size": int(size or 0)}]
        try:
            async with self._client() as c:
                r = await c.post(
                    f"/api/v0/transfers/downloads/{quote(username, safe='')}",
                    json=payload,
                )
                if r.status_code >= 400:
                    return (
                        f"slskd refused download (HTTP {r.status_code}): "
                        f"{r.text[:400]}"
                    )
        except httpx.ConnectError:
            return self._connection_hint()
        except Exception as e:
            return f"slskd download error: {e}"

        await self._status(__event_emitter__, "Queued", done=True)
        return (
            f"Queued download from **{username}**:\n"
            f"- `{filename}`\n\n"
            "Track progress with `list_downloads()`. Completed files land in slskd's configured "
            "download directory (check slskd Options > Directories)."
        )

    async def download_best(
        self,
        query: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search Soulseek and immediately queue the top-ranked file.
        :param query: Free-text search, e.g. "radiohead - kid a flac"
        :return: Summary of the picked result plus download state
        """
        hits = await self._search_raw(query, __event_emitter__)
        if not hits:
            return f"No Soulseek hits for `{query}` within {self.valves.SEARCH_WAIT_SECONDS}s."
        top = hits[0]
        q = await self.download(
            top.get("username", ""),
            top.get("filename", ""),
            int(top.get("size") or 0),
            __event_emitter__,
        )
        return (
            f"## Soulseek lucky download\n"
            f"- Query: `{query}`\n"
            f"- Picked: `{_basename(top.get('filename', ''))}` from **{top.get('username')}** "
            f"({_human_size(top.get('size') or 0)}, {self._bitrate_label(top)})\n\n"
            f"{q}"
        )

    async def list_downloads(self, __user__: Optional[dict] = None) -> str:
        """
        List current and recent Soulseek downloads managed by slskd.
        :return: Markdown table of downloads with state and progress
        """
        try:
            async with self._client() as c:
                r = await c.get("/api/v0/transfers/downloads")
                r.raise_for_status()
                data = r.json()
        except httpx.ConnectError:
            return self._connection_hint()
        except Exception as e:
            return f"slskd list_downloads error: {e}"

        rows = []
        for user in data or []:
            uname = user.get("username", "?")
            for d in (user.get("directories") or []):
                for f in (d.get("files") or []):
                    state = f.get("state", "?")
                    size = _human_size(f.get("size") or 0)
                    trans = _human_size(f.get("bytesTransferred") or 0)
                    name = _basename(f.get("filename") or "")
                    rows.append(
                        f"| `{name}` | {uname} | {state} | {trans} / {size} |"
                    )
        if not rows:
            return "No active or recent Soulseek downloads."
        return (
            "## Soulseek downloads\n"
            "| File | From | State | Progress |\n"
            "|------|------|-------|----------|\n" + "\n".join(rows)
        )

    async def cancel_download(
        self,
        username: str,
        filename: str,
        remove: bool = False,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Cancel a queued or in-progress Soulseek download.
        :param username: Uploader username
        :param filename: Full remote filename as shown in list_downloads
        :param remove: If true, also removes the transfer record from slskd
        :return: Result of the cancel/remove call
        """
        params = {"remove": "true"} if remove else {}
        path = (
            f"/api/v0/transfers/downloads/"
            f"{quote(username, safe='')}/{quote(filename, safe='')}"
        )
        try:
            async with self._client() as c:
                r = await c.delete(path, params=params)
            if r.status_code in (200, 204):
                return f"Cancelled {_basename(filename)} from {username}."
            return f"slskd cancel HTTP {r.status_code}: {r.text[:200]}"
        except httpx.ConnectError:
            return self._connection_hint()
        except Exception as e:
            return f"slskd cancel error: {e}"

    # ── Internals ────────────────────────────────────────────────────────────
    async def _search_raw(self, query: str, emitter) -> list:
        # Identical to search() but returns the filtered+ranked list instead of markdown.
        try:
            async with self._client() as c:
                r = await c.post("/api/v0/searches", json={"searchText": query})
                r.raise_for_status()
                j = r.json()
                sid = j.get("id") or j.get("searchId") or j.get("Id")
                if not sid:
                    return []
                deadline = asyncio.get_event_loop().time() + self.valves.SEARCH_WAIT_SECONDS
                while True:
                    await asyncio.sleep(2)
                    info = (await c.get(f"/api/v0/searches/{sid}")).json()
                    state = (info.get("state") or "").lower()
                    if state.startswith("completed") or asyncio.get_event_loop().time() >= deadline:
                        break
                resp = (await c.get(f"/api/v0/searches/{sid}/responses")).json()
                try:
                    await c.delete(f"/api/v0/searches/{sid}")
                except Exception:
                    pass
        except Exception:
            return []
        hits = self._filter(self._flatten(resp))
        return self._rank(hits)[: self.valves.MAX_RESULTS]

    @staticmethod
    def _flatten(responses) -> list:
        out = []
        for r in responses or []:
            uname = r.get("username") or r.get("Username") or ""
            speed = r.get("uploadSpeed") or r.get("UploadSpeed") or 0
            free = r.get("hasFreeUploadSlot")
            if free is None:
                free = r.get("HasFreeUploadSlot")
            for f in (r.get("files") or r.get("Files") or []):
                out.append(
                    {
                        "username": uname,
                        "uploadSpeed": speed,
                        "hasFreeUploadSlot": bool(free),
                        "filename": f.get("filename") or f.get("Filename") or "",
                        "size": f.get("size") or f.get("Size") or 0,
                        "bitRate": f.get("bitRate") or f.get("BitRate"),
                        "sampleRate": f.get("sampleRate") or f.get("SampleRate"),
                        "bitDepth": f.get("bitDepth") or f.get("BitDepth"),
                        "length": f.get("length") or f.get("Length"),
                        "extension": f.get("extension")
                        or f.get("Extension")
                        or os.path.splitext(
                            (f.get("filename") or f.get("Filename") or "").lower()
                        )[1],
                    }
                )
        return out

    def _filter(self, hits: list) -> list:
        out = []
        for h in hits:
            ext = (h.get("extension") or "").lower()
            if self.valves.AUDIO_ONLY and ext not in _AUDIO_EXT:
                continue
            br = h.get("bitRate") or 0
            if (
                self.valves.MIN_BITRATE
                and ext not in (".flac", ".wav", ".alac", ".aiff", ".wv")
                and br
                and br < self.valves.MIN_BITRATE
            ):
                continue
            out.append(h)
        return out

    def _rank(self, hits: list) -> list:
        def score(h):
            ext = (h.get("extension") or "").lower()
            lossless = ext in (".flac", ".wav", ".alac", ".aiff", ".wv")
            base = 0
            if self.valves.PREFER_LOSSLESS and lossless:
                base += 1_000_000
            base += int(h.get("bitRate") or 0)
            if h.get("hasFreeUploadSlot"):
                base += 500
            base += int((h.get("uploadSpeed") or 0) / 1024)
            return -base  # asc sort → bigger score first

        return sorted(hits, key=score)

    @staticmethod
    def _bitrate_label(h: dict) -> str:
        ext = (h.get("extension") or "").lower()
        if ext in (".flac", ".wav", ".alac", ".aiff", ".wv"):
            sr = h.get("sampleRate")
            bd = h.get("bitDepth")
            if sr and bd:
                return f"{ext[1:].upper()} {bd}/{int(sr) // 1000}"
            return ext[1:].upper()
        br = h.get("bitRate")
        return f"{br} kbps" if br else ext[1:].upper() or "?"

    @staticmethod
    def _count_files(transfers) -> int:
        n = 0
        for u in transfers or []:
            for d in u.get("directories") or []:
                n += len(d.get("files") or [])
        return n

    async def download_and_organize(
        self,
        query: str,
        slskd_download_dir: str,
        kind: str = "audio",
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        End-to-end search → download → organize pipeline. Calls
        `download_best(query)` to queue a download from the top-ranked
        result, then hands the slskd download directory off to the
        media_library organizer (Music/<Artist>/<Album>/...).
        Note: slskd downloads are async — this returns once the queue
        request is accepted. Wait for the transfer to finish (use
        `list_downloads()` to confirm) before re-running the organize step.
        :param query: Search string passed to download_best.
        :param slskd_download_dir: Where slskd writes completed files (Options > Directories).
        :param kind: Organize as 'audio' (default) or 'audiobooks'.
        :return: Combined queue + organize log.
        """
        import importlib.util
        from pathlib import Path as _P
        here = _P(__file__).parent
        spec = importlib.util.spec_from_file_location(
            "_lai_organize_helper", here / "_organize_helper.py",
        )
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        organize = mod.organize

        queued = await self.download_best(
            query, __event_emitter__=__event_emitter__, __user__=__user__,
        )
        organized = organize(slskd_download_dir, kind=kind)
        return (
            f"── search & queue ──\n{queued}\n\n"
            "── organize (run after transfer completes) ──\n"
            f"{organized}"
        )
