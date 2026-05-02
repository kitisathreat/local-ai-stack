"""
title: qBittorrent — Web API Client (add / list / control torrents)
author: local-ai-stack
description: Drive a local or remote qBittorrent instance via its Web API. Add torrents from a magnet URI or a .torrent URL, list and filter the queue, pause / resume / delete (with optional file removal), set per-torrent download limits, and fetch global transfer stats. Auth is handled transparently — the client logs in once per cookie lifetime. Pairs with the torrent_search tool: model picks a magnet from a search result, calls add_torrent, watches progress with list_torrents.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

import json
from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field


def _human_size(n: int | float) -> str:
    n = float(n or 0)
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024
        i += 1
    return f"{n:.2f} {units[i]}"


class Tools:
    class Valves(BaseModel):
        QBT_URL: str = Field(
            default="http://127.0.0.1:8080",
            description="qBittorrent Web UI base URL. Enable Web UI in qBittorrent → Tools → Options → Web UI.",
        )
        QBT_USERNAME: str = Field(
            default="admin",
            description="Web UI username (default 'admin').",
        )
        QBT_PASSWORD: str = Field(
            default="",
            description="Web UI password. On Windows the bootstrap password is shown on first run; rotate it in the Web UI.",
        )
        DEFAULT_SAVE_PATH: str = Field(
            default="",
            description="Optional default save path for new torrents. Leave blank to use qBittorrent's configured default.",
        )

    def __init__(self):
        self.valves = self.Valves()
        self._cookie: str = ""

    # ── Auth ──────────────────────────────────────────────────────────────

    async def _login(self, client: httpx.AsyncClient) -> None:
        if self._cookie:
            return
        url = f"{self.valves.QBT_URL.rstrip('/')}/api/v2/auth/login"
        r = await client.post(
            url,
            data={"username": self.valves.QBT_USERNAME, "password": self.valves.QBT_PASSWORD},
            headers={"Referer": self.valves.QBT_URL.rstrip('/')},
        )
        if r.status_code != 200 or r.text.strip().lower() != "ok.":
            raise RuntimeError(f"qBittorrent login failed: {r.status_code} {r.text[:120]}")
        cookie = r.cookies.get("SID")
        if not cookie:
            raise RuntimeError("qBittorrent login did not return SID cookie")
        self._cookie = cookie

    async def _request(
        self,
        method: str,
        path: str,
        *,
        data: dict | None = None,
        params: dict | None = None,
        files: dict | None = None,
    ) -> httpx.Response:
        async with httpx.AsyncClient(timeout=30) as client:
            await self._login(client)
            cookies = {"SID": self._cookie}
            r = await client.request(
                method, f"{self.valves.QBT_URL.rstrip('/')}{path}",
                data=data, params=params, files=files, cookies=cookies,
                headers={"Referer": self.valves.QBT_URL.rstrip('/')},
            )
            if r.status_code == 403:  # Cookie expired
                self._cookie = ""
                await self._login(client)
                r = await client.request(
                    method, f"{self.valves.QBT_URL.rstrip('/')}{path}",
                    data=data, params=params, files=files,
                    cookies={"SID": self._cookie},
                    headers={"Referer": self.valves.QBT_URL.rstrip('/')},
                )
            return r

    # ── Add ───────────────────────────────────────────────────────────────

    async def add_torrent(
        self,
        magnet_or_url: str,
        save_path: str = "",
        category: str = "",
        paused: bool = False,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Add a torrent to qBittorrent from a magnet URI or a .torrent URL.
        :param magnet_or_url: magnet:?xt=urn:btih:... or https://...torrent URL.
        :param save_path: Optional override for the download directory.
        :param category: Optional category tag (must already exist in qBittorrent).
        :param paused: Add the torrent in paused state.
        :return: Confirmation.
        """
        data: dict[str, Any] = {"urls": magnet_or_url, "paused": "true" if paused else "false"}
        if save_path or self.valves.DEFAULT_SAVE_PATH:
            data["savepath"] = save_path or self.valves.DEFAULT_SAVE_PATH
        if category:
            data["category"] = category
        r = await self._request("POST", "/api/v2/torrents/add", data=data)
        if r.status_code == 200:
            return f"queued: {magnet_or_url[:80]}{'...' if len(magnet_or_url) > 80 else ''}"
        return f"failed: HTTP {r.status_code} {r.text[:200]}"

    async def add_torrent_file(
        self,
        torrent_path: str,
        save_path: str = "",
        category: str = "",
        paused: bool = False,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Add a torrent from a local .torrent file on disk (host-side path).
        :param torrent_path: Absolute path to the .torrent file.
        :param save_path: Optional download directory.
        :param category: Optional category tag.
        :param paused: Add paused.
        :return: Confirmation.
        """
        from pathlib import Path
        p = Path(torrent_path).expanduser().resolve()
        if not p.is_file():
            return f"Not a file: {p}"
        files = {"torrents": (p.name, p.read_bytes(), "application/x-bittorrent")}
        data: dict[str, Any] = {"paused": "true" if paused else "false"}
        if save_path or self.valves.DEFAULT_SAVE_PATH:
            data["savepath"] = save_path or self.valves.DEFAULT_SAVE_PATH
        if category:
            data["category"] = category
        r = await self._request("POST", "/api/v2/torrents/add", data=data, files=files)
        if r.status_code == 200:
            return f"queued: {p.name}"
        return f"failed: HTTP {r.status_code} {r.text[:200]}"

    # ── List / inspect ───────────────────────────────────────────────────

    async def list_torrents(
        self,
        filter: str = "all",
        category: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        List torrents with their progress, speed, and status.
        :param filter: all, downloading, seeding, completed, paused, active, inactive, errored.
        :param category: Optional category filter.
        :return: Hash, name, progress, status, down/up speed.
        """
        params: dict[str, Any] = {"filter": filter}
        if category:
            params["category"] = category
        r = await self._request("GET", "/api/v2/torrents/info", params=params)
        if r.status_code != 200:
            return f"HTTP {r.status_code}"
        items = r.json() or []
        rows = []
        for t in items:
            rows.append(
                f"{t['hash'][:8]}…  {(t.get('name') or '')[:55]:<55}  "
                f"{t.get('progress',0)*100:5.1f}%  {t.get('state','?'):<11}  "
                f"⬇ {_human_size(t.get('dlspeed',0))}/s  ⬆ {_human_size(t.get('upspeed',0))}/s"
            )
        return "\n".join(rows) if rows else "(no torrents)"

    async def torrent_files(
        self,
        infohash: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        List the files inside a torrent by infohash.
        :param infohash: Full info hash from list_torrents.
        :return: index, name, size, progress per file.
        """
        r = await self._request("GET", "/api/v2/torrents/files", params={"hash": infohash})
        if r.status_code != 200:
            return f"HTTP {r.status_code}"
        items = r.json() or []
        rows = [
            f"{i:>3}  {(f.get('name') or '')[:80]:<80}  "
            f"{_human_size(f.get('size',0))}  {f.get('progress',0)*100:5.1f}%"
            for i, f in enumerate(items)
        ]
        return "\n".join(rows) if rows else "(no files)"

    async def transfer_info(self, __user__: Optional[dict] = None) -> str:
        """
        Global transfer statistics — aggregate down/up speed, alltime totals,
        DHT/PeX node counts.
        :return: Multi-line summary.
        """
        r = await self._request("GET", "/api/v2/transfer/info")
        if r.status_code != 200:
            return f"HTTP {r.status_code}"
        info = r.json() or {}
        return (
            f"connection:    {info.get('connection_status')}\n"
            f"down speed:    {_human_size(info.get('dl_info_speed',0))}/s\n"
            f"up speed:      {_human_size(info.get('up_info_speed',0))}/s\n"
            f"down total:    {_human_size(info.get('dl_info_data',0))}\n"
            f"up total:      {_human_size(info.get('up_info_data',0))}\n"
            f"alltime down:  {_human_size(info.get('alltime_dl',0))}\n"
            f"alltime up:    {_human_size(info.get('alltime_ul',0))}\n"
            f"DHT nodes:     {info.get('dht_nodes','?')}"
        )

    # ── Control ───────────────────────────────────────────────────────────

    async def pause(self, infohashes: str, __user__: Optional[dict] = None) -> str:
        """
        Pause one or more torrents.
        :param infohashes: Single hash or pipe-separated list, or "all".
        :return: Confirmation.
        """
        r = await self._request("POST", "/api/v2/torrents/pause", data={"hashes": infohashes})
        return "ok" if r.status_code == 200 else f"HTTP {r.status_code}"

    async def resume(self, infohashes: str, __user__: Optional[dict] = None) -> str:
        """
        Resume one or more torrents.
        :param infohashes: Single hash or pipe-separated list, or "all".
        :return: Confirmation.
        """
        r = await self._request("POST", "/api/v2/torrents/resume", data={"hashes": infohashes})
        return "ok" if r.status_code == 200 else f"HTTP {r.status_code}"

    async def delete(
        self,
        infohashes: str,
        delete_files: bool = False,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Remove one or more torrents from qBittorrent. Set delete_files=True
        to also wipe the downloaded data on disk.
        :param infohashes: Single hash or pipe-separated list.
        :param delete_files: When True, also deletes the downloaded files.
        :return: Confirmation.
        """
        r = await self._request("POST", "/api/v2/torrents/delete", data={
            "hashes": infohashes,
            "deleteFiles": "true" if delete_files else "false",
        })
        return "ok" if r.status_code == 200 else f"HTTP {r.status_code}"

    async def set_download_limit(
        self,
        infohashes: str,
        bytes_per_sec: int,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Cap the download speed for specific torrents (or 0 for unlimited).
        :param infohashes: Single hash or pipe-separated list.
        :param bytes_per_sec: 0 = unlimited.
        :return: Confirmation.
        """
        r = await self._request("POST", "/api/v2/torrents/setDownloadLimit", data={
            "hashes": infohashes, "limit": bytes_per_sec,
        })
        return "ok" if r.status_code == 200 else f"HTTP {r.status_code}"

    async def wait_for_torrent(
        self,
        infohash: str,
        timeout_secs: int = 3600,
        poll_secs: int = 10,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Poll qBittorrent until a torrent reaches 100% progress (or one of
        the seeding states: uploading, stalledUP, queuedUP, forcedUP). Use
        this between `add_torrent` and `wait_and_organize` to gate the
        organize step on completion.
        :param infohash: Full info hash from `list_torrents`.
        :param timeout_secs: Cap on the wait. Default 1 hour.
        :param poll_secs: Polling interval.
        :return: Final state + progress, or a timeout message.
        """
        import asyncio
        import time
        deadline = time.monotonic() + timeout_secs
        terminal = {"uploading", "stalledUP", "queuedUP", "forcedUP", "pausedUP"}
        while time.monotonic() < deadline:
            r = await self._request("GET", "/api/v2/torrents/info",
                                    params={"hashes": infohash})
            if r.status_code != 200:
                return f"HTTP {r.status_code}"
            info = (r.json() or [None])[0]
            if not info:
                return f"no torrent matching {infohash}"
            progress = info.get("progress", 0)
            state = info.get("state", "?")
            if progress >= 1.0 or state in terminal:
                return f"complete: state={state} progress={progress*100:.1f}% save={info.get('save_path')}"
            await asyncio.sleep(poll_secs)
        return f"timeout after {timeout_secs}s — torrent not yet complete"

    async def wait_and_organize(
        self,
        infohash: str,
        kind: str = "auto",
        timeout_secs: int = 3600,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Wait for a torrent to finish, then route its save_path through the
        media_library organizer. `kind` selects which organizer to run
        (auto, audio, books, films, tv, audiobooks); 'auto' detects by
        file extension and dispatches to all matching organizers.
        :param infohash: Full info hash.
        :param kind: One of: auto, audio, books, films, tv, audiobooks.
        :param timeout_secs: Cap on the wait.
        :return: Combined wait + organize log.
        """
        wait = await self.wait_for_torrent(infohash, timeout_secs=timeout_secs)
        if "complete" not in wait:
            return wait
        # Pull save_path out of the wait message.
        import re as _re
        m = _re.search(r"save=(.+)$", wait)
        if not m:
            return f"complete but couldn't parse save_path: {wait}"
        save_path = m.group(1).strip()

        import importlib.util
        from pathlib import Path as _P
        spec = importlib.util.spec_from_file_location(
            "_lai_organize_helper", _P(__file__).parent / "_organize_helper.py",
        )
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        organize = mod.organize
        organized = organize(save_path, kind=kind)
        return f"── wait ──\n{wait}\n\n── organize ──\n{organized}"

    async def app_version(self, __user__: Optional[dict] = None) -> str:
        """
        Return the qBittorrent application + Web API version (smoke test).
        :return: Two-line version summary.
        """
        async with httpx.AsyncClient(timeout=10) as client:
            await self._login(client)
            v = await client.get(
                f"{self.valves.QBT_URL.rstrip('/')}/api/v2/app/version",
                cookies={"SID": self._cookie},
                headers={"Referer": self.valves.QBT_URL.rstrip('/')},
            )
            api = await client.get(
                f"{self.valves.QBT_URL.rstrip('/')}/api/v2/app/webapiVersion",
                cookies={"SID": self._cookie},
                headers={"Referer": self.valves.QBT_URL.rstrip('/')},
            )
        return f"qBittorrent: {v.text.strip()}\nWeb API:     {api.text.strip()}"
