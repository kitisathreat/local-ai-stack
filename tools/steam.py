"""
title: Steam — Launch Games + Library Inspection
author: local-ai-stack
description: Launch installed Steam games via the steam:// URL protocol, list installed games by parsing libraryfolders.vdf + appmanifest_*.acf, and (with a free Steam Web API key) fetch a public profile's owned-games list, recently played, and player summary. No game files are read — only manifest metadata.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
from pydantic import BaseModel, Field


_STEAM_API = "https://api.steampowered.com"
_STORE_API = "https://store.steampowered.com/api"


def _default_steam_root() -> str:
    candidates = [
        Path(r"C:\Program Files (x86)\Steam"),
        Path(r"C:\Program Files\Steam"),
        Path.home() / "Library/Application Support/Steam",  # macOS
        Path.home() / ".steam/steam",                       # Linux
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return str(candidates[0])


def _parse_vdf(text: str) -> Any:
    """Tiny VDF parser for Steam's keyvalue files (libraryfolders.vdf,
    appmanifest_*.acf). Handles nested objects and quoted strings only —
    sufficient for our needs."""
    tokens = re.findall(r'"((?:\\.|[^"\\])*)"|([{}])', text)
    pos = 0

    def parse_object() -> dict:
        nonlocal pos
        obj: dict = {}
        while pos < len(tokens):
            tok = tokens[pos]
            if tok[1] == "}":
                pos += 1
                return obj
            key = tok[0]
            pos += 1
            if pos >= len(tokens):
                break
            nxt = tokens[pos]
            if nxt[1] == "{":
                pos += 1
                obj[key] = parse_object()
            else:
                obj[key] = nxt[0]
                pos += 1
        return obj

    return parse_object()


class Tools:
    class Valves(BaseModel):
        STEAM_ROOT: str = Field(
            default_factory=_default_steam_root,
            description="Path to the Steam install folder (contains steamapps/).",
        )
        STEAM_EXE: str = Field(
            default=r"C:\Program Files (x86)\Steam\steam.exe",
            description="Steam.exe — used as a fallback when the steam:// URL handler isn't registered.",
        )
        STEAM_API_KEY: str = Field(
            default="",
            description="Optional Steam Web API key from https://steamcommunity.com/dev/apikey — required for owned_games / recently_played / profile lookups.",
        )

    def __init__(self):
        self.valves = self.Valves()

    # ── Helpers ───────────────────────────────────────────────────────────

    def _open_url(self, url: str) -> str:
        if sys.platform == "win32":
            os.startfile(url)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", url])
        else:
            subprocess.Popen(["xdg-open", url])
        return url

    def _libraryfolders(self) -> list[Path]:
        root = Path(self.valves.STEAM_ROOT).expanduser()
        vdf = root / "steamapps" / "libraryfolders.vdf"
        libs = [root / "steamapps"]
        if vdf.exists():
            try:
                data = _parse_vdf(vdf.read_text(encoding="utf-8", errors="ignore"))
                folders = data.get("libraryfolders", {}) or data.get("LibraryFolders", {})
                for v in folders.values():
                    if isinstance(v, dict) and "path" in v:
                        libs.append(Path(v["path"]) / "steamapps")
            except Exception:
                pass
        return [p for p in libs if p.exists()]

    # ── GUI launches ──────────────────────────────────────────────────────

    def launch_steam(self, __user__: Optional[dict] = None) -> str:
        """
        Open the Steam client.
        :return: Confirmation.
        """
        return f"opened: {self._open_url('steam://open/main')}"

    def launch_game(self, app_id: int, __user__: Optional[dict] = None) -> str:
        """
        Launch an installed Steam game by its appid (e.g. Half-Life 2 = 220,
        Dota 2 = 570). Uses the steam:// URL protocol — Steam handles auth and
        launch.
        :param app_id: Steam application id.
        :return: Confirmation with the steam:// URL.
        """
        if app_id <= 0:
            return f"invalid appid: {app_id}"
        return f"launching: {self._open_url(f'steam://run/{app_id}')}"

    def install_game(self, app_id: int, __user__: Optional[dict] = None) -> str:
        """
        Open the Steam install dialog for an appid (only works for games on
        the user's account).
        :param app_id: Steam application id.
        :return: Confirmation.
        """
        return f"install dialog: {self._open_url(f'steam://install/{app_id}')}"

    def open_store_page(self, app_id: int, __user__: Optional[dict] = None) -> str:
        """
        Open the Steam Store page for an appid in the Steam client.
        :param app_id: Steam application id.
        :return: Confirmation.
        """
        return f"store page: {self._open_url(f'steam://store/{app_id}')}"

    # ── Local library inspection (no API key needed) ─────────────────────

    def list_installed_games(self, __user__: Optional[dict] = None) -> str:
        """
        Enumerate installed Steam games by reading every steamapps library
        on the host. No network and no API key needed.
        :return: appid, name, install dir, and size for each installed game.
        """
        rows: list[str] = []
        for lib in self._libraryfolders():
            for acf in sorted(lib.glob("appmanifest_*.acf")):
                try:
                    data = _parse_vdf(acf.read_text(encoding="utf-8", errors="ignore"))
                    state = data.get("AppState", {})
                    appid = state.get("appid", "?")
                    name = state.get("name", "?")
                    installdir = state.get("installdir", "?")
                    size = int(state.get("SizeOnDisk", 0) or 0)
                    rows.append(f"{appid:>10}  {name:<40}  {size/1e9:>6.2f} GB  {lib / 'common' / installdir}")
                except Exception as e:
                    rows.append(f"err {acf.name}: {e}")
        return "\n".join(rows) if rows else "(no installed games found)"

    # ── Steam Web API (needs key) ─────────────────────────────────────────

    async def _api_get(self, path: str, params: dict) -> dict | str:
        if not self.valves.STEAM_API_KEY:
            return "STEAM_API_KEY not set on the Steam tool's Valves."
        params = dict(params, key=self.valves.STEAM_API_KEY, format="json")
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(f"{_STEAM_API}{path}", params=params)
            if r.status_code != 200:
                return f"HTTP {r.status_code}: {r.text[:300]}"
            return r.json()

    async def get_owned_games(
        self,
        steam_id: str,
        include_played_free: bool = True,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Fetch the games owned by a public 64-bit Steam ID.
        :param steam_id: SteamID64 (17-digit, e.g. 76561197960287930).
        :param include_played_free: When True, also surface free-to-play games the user has played.
        :return: Game count + per-game name, appid, total playtime (minutes).
        """
        out = await self._api_get(
            "/IPlayerService/GetOwnedGames/v1/",
            {"steamid": steam_id, "include_appinfo": 1,
             "include_played_free_games": int(include_played_free)},
        )
        if isinstance(out, str):
            return out
        resp = (out or {}).get("response", {})
        games = resp.get("games", [])
        rows = [f"{g.get('appid'):>8}  {g.get('name','?'):<40}  {g.get('playtime_forever',0):>6} min"
                for g in games]
        return f"{resp.get('game_count', len(games))} games\n" + "\n".join(rows[:200])

    async def get_recently_played(
        self,
        steam_id: str,
        count: int = 10,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Last N games played by a Steam user (last 2 weeks).
        :param steam_id: SteamID64.
        :param count: Max games to return.
        :return: Recent activity list.
        """
        out = await self._api_get(
            "/IPlayerService/GetRecentlyPlayedGames/v1/",
            {"steamid": steam_id, "count": count},
        )
        if isinstance(out, str):
            return out
        games = (out or {}).get("response", {}).get("games", [])
        rows = [f"{g.get('appid'):>8}  {g.get('name','?'):<40}  2wk={g.get('playtime_2weeks',0)}m  total={g.get('playtime_forever',0)}m"
                for g in games]
        return "\n".join(rows) if rows else "(no recent activity)"

    async def get_player_summary(
        self,
        steam_id: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Public profile info for a Steam user.
        :param steam_id: SteamID64.
        :return: Display name, profile state, current game (if any), location.
        """
        out = await self._api_get(
            "/ISteamUser/GetPlayerSummaries/v2/",
            {"steamids": steam_id},
        )
        if isinstance(out, str):
            return out
        players = (out or {}).get("response", {}).get("players", [])
        if not players:
            return "(no profile found — is it public?)"
        p = players[0]
        return (
            f"name:        {p.get('personaname')}\n"
            f"profile:     {p.get('profileurl')}\n"
            f"state:       {p.get('personastate')}\n"
            f"playing:     {p.get('gameextrainfo','-')} (appid {p.get('gameid','-')})\n"
            f"country:     {p.get('loccountrycode','-')}"
        )

    async def search_store(
        self,
        query: str,
        country: str = "us",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search the Steam Store by name (no API key needed).
        :param query: Game name or keyword.
        :param country: ISO country code for pricing (us, gb, de, jp, ...).
        :return: appid + name + price for matches.
        """
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{_STORE_API}/storesearch/",
                params={"term": query, "cc": country, "l": "english"},
            )
        if r.status_code != 200:
            return f"HTTP {r.status_code}"
        items = (r.json() or {}).get("items", [])
        rows = []
        for it in items[:25]:
            price = (it.get("price") or {}).get("final_formatted") or ("Free" if it.get("price") is None else "?")
            rows.append(f"{it.get('id'):>8}  {it.get('name'):<50}  {price}")
        return "\n".join(rows) if rows else "(no matches)"
