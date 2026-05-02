"""
title: Spotify — Web API Search + Playback Control
author: local-ai-stack
description: Hit Spotify's Web API for catalogue search, playlist browsing, and playback control. Two auth modes: (1) Client-credentials (paste a client_id + client_secret in Valves) gives access to public catalogue endpoints — search, album/artist/track metadata, audio features. (2) User auth via a long-lived refresh_token additionally unlocks playback control: play / pause / next / previous / queue / get-currently-playing / list-and-modify-user-playlists. Tokens are refreshed transparently and never logged.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field


_API = "https://api.spotify.com/v1"
_TOKEN = "https://accounts.spotify.com/api/token"


class Tools:
    class Valves(BaseModel):
        CLIENT_ID: str = Field(
            default="",
            description="Spotify app client_id (https://developer.spotify.com/dashboard).",
        )
        CLIENT_SECRET: str = Field(
            default="",
            description="Spotify app client_secret.",
        )
        REFRESH_TOKEN: str = Field(
            default="",
            description=(
                "Long-lived user refresh token. Required for playback control endpoints. "
                "Generate once via the OAuth code flow with scopes "
                "`user-read-playback-state user-modify-playback-state user-read-currently-playing "
                "playlist-read-private playlist-modify-private playlist-modify-public`."
            ),
        )
        DEFAULT_MARKET: str = Field(
            default="US",
            description="ISO market code for catalogue queries (US, GB, DE, JP, ...).",
        )

    def __init__(self):
        self.valves = self.Valves()
        self._app_token: tuple[str, float] | None = None    # (token, expiry_unix)
        self._user_token: tuple[str, float] | None = None

    # ── Token management ──────────────────────────────────────────────────

    async def _client_creds_token(self) -> str:
        if self._app_token and self._app_token[1] - 30 > time.time():
            return self._app_token[0]
        if not self.valves.CLIENT_ID or not self.valves.CLIENT_SECRET:
            raise PermissionError("CLIENT_ID/CLIENT_SECRET not set on the Spotify tool.")
        basic = base64.b64encode(
            f"{self.valves.CLIENT_ID}:{self.valves.CLIENT_SECRET}".encode()
        ).decode()
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                _TOKEN,
                data={"grant_type": "client_credentials"},
                headers={"Authorization": f"Basic {basic}"},
            )
        if r.status_code != 200:
            raise RuntimeError(f"client_credentials failed: {r.status_code} {r.text[:200]}")
        body = r.json()
        self._app_token = (body["access_token"], time.time() + int(body.get("expires_in", 3600)))
        return self._app_token[0]

    async def _user_token_refresh(self) -> str:
        if self._user_token and self._user_token[1] - 30 > time.time():
            return self._user_token[0]
        if not self.valves.REFRESH_TOKEN:
            raise PermissionError(
                "REFRESH_TOKEN not set on the Spotify tool — needed for playback control."
            )
        basic = base64.b64encode(
            f"{self.valves.CLIENT_ID}:{self.valves.CLIENT_SECRET}".encode()
        ).decode()
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                _TOKEN,
                data={"grant_type": "refresh_token", "refresh_token": self.valves.REFRESH_TOKEN},
                headers={"Authorization": f"Basic {basic}"},
            )
        if r.status_code != 200:
            raise RuntimeError(f"refresh_token failed: {r.status_code} {r.text[:200]}")
        body = r.json()
        self._user_token = (body["access_token"], time.time() + int(body.get("expires_in", 3600)))
        return self._user_token[0]

    async def _api(
        self,
        method: str,
        path: str,
        *,
        user: bool,
        params: dict | None = None,
        json_body: Any = None,
    ) -> Any:
        token = await (self._user_token_refresh() if user else self._client_creds_token())
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.request(method, f"{_API}{path}", headers=headers,
                                params=params, json=json_body)
        if 200 <= r.status_code < 300:
            if r.status_code == 204 or not r.content:
                return {"ok": True}
            try:
                return r.json()
            except json.JSONDecodeError:
                return r.text
        return {"error": f"HTTP {r.status_code}", "body": r.text[:300]}

    # ── Public catalogue (client_credentials) ────────────────────────────

    async def search(
        self,
        query: str,
        kind: str = "track",
        limit: int = 10,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search Spotify's catalogue for tracks, albums, artists, or playlists.
        :param query: Search query.
        :param kind: track, album, artist, playlist (or comma-list, e.g. "track,album").
        :param limit: 1-50.
        :return: Top matches with name, id, artists, and Spotify URI.
        """
        out = await self._api("GET", "/search", user=False, params={
            "q": query, "type": kind, "limit": max(1, min(limit, 50)),
            "market": self.valves.DEFAULT_MARKET,
        })
        if isinstance(out, dict) and "error" in out:
            return f"{out['error']}: {out['body']}"
        rows: list[str] = []
        for t in (out.get("tracks") or {}).get("items", []) or []:
            artists = ", ".join(a["name"] for a in t.get("artists", []))
            rows.append(f"track  {t['id']:<22}  {t['name']:<40}  by {artists}  uri={t['uri']}")
        for a in (out.get("albums") or {}).get("items", []) or []:
            artists = ", ".join(x["name"] for x in a.get("artists", []))
            rows.append(f"album  {a['id']:<22}  {a['name']:<40}  by {artists}  uri={a['uri']}")
        for ar in (out.get("artists") or {}).get("items", []) or []:
            rows.append(f"artist {ar['id']:<22}  {ar['name']:<40}  followers={ar.get('followers',{}).get('total','?')}")
        for pl in (out.get("playlists") or {}).get("items", []) or []:
            if not pl:
                continue
            rows.append(f"plist  {pl['id']:<22}  {pl['name']:<40}  by {pl.get('owner',{}).get('display_name','?')}")
        return "\n".join(rows) if rows else "(no matches)"

    async def get_track(self, track_id: str, __user__: Optional[dict] = None) -> str:
        """
        Fetch full metadata for a Spotify track id.
        :param track_id: 22-char Spotify track id.
        :return: Track details (name, artists, album, duration, popularity, preview_url).
        """
        out = await self._api("GET", f"/tracks/{track_id}", user=False)
        if isinstance(out, dict) and "error" in out:
            return f"{out['error']}: {out['body']}"
        return json.dumps(out, indent=2)[:2000]

    async def get_album_tracks(
        self,
        album_id: str,
        limit: int = 50,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        List tracks on an album.
        :param album_id: 22-char Spotify album id.
        :param limit: 1-50.
        :return: Track list with id, name, duration.
        """
        out = await self._api("GET", f"/albums/{album_id}/tracks", user=False,
                              params={"limit": limit, "market": self.valves.DEFAULT_MARKET})
        if isinstance(out, dict) and "error" in out:
            return f"{out['error']}: {out['body']}"
        items = out.get("items", [])
        rows = [f"{i+1:>3}. {t['id']:<22}  {t['name']:<40}  {t.get('duration_ms',0)//1000}s"
                for i, t in enumerate(items)]
        return "\n".join(rows) if rows else "(empty album)"

    # ── Playback control (user token) ────────────────────────────────────

    async def now_playing(self, __user__: Optional[dict] = None) -> str:
        """
        Fetch the user's currently playing item.
        :return: Track name, artists, progress, device.
        """
        out = await self._api("GET", "/me/player/currently-playing", user=True)
        if isinstance(out, dict) and out.get("ok"):
            return "(nothing playing)"
        if isinstance(out, dict) and "error" in out:
            return f"{out['error']}: {out['body']}"
        item = out.get("item") or {}
        artists = ", ".join(a["name"] for a in item.get("artists", []))
        prog = (out.get("progress_ms") or 0) // 1000
        dur = (item.get("duration_ms") or 0) // 1000
        return (
            f"{item.get('name','?')} — {artists}\n"
            f"album: {item.get('album',{}).get('name','?')}\n"
            f"progress: {prog}s / {dur}s\n"
            f"device: {(out.get('device') or {}).get('name','?')}\n"
            f"uri: {item.get('uri','?')}"
        )

    async def play(
        self,
        spotify_uri: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Resume playback on the active device, or start playing a specific URI
        (track, album, artist, playlist).
        :param spotify_uri: Optional spotify:track:..., spotify:album:..., etc.
        :return: Confirmation.
        """
        body: dict = {}
        if spotify_uri:
            if spotify_uri.startswith("spotify:track:"):
                body["uris"] = [spotify_uri]
            else:
                body["context_uri"] = spotify_uri
        out = await self._api("PUT", "/me/player/play", user=True, json_body=body or None)
        return json.dumps(out)

    async def pause(self, __user__: Optional[dict] = None) -> str:
        """
        Pause playback on the active device.
        :return: Confirmation.
        """
        return json.dumps(await self._api("PUT", "/me/player/pause", user=True))

    async def next_track(self, __user__: Optional[dict] = None) -> str:
        """
        Skip to the next track.
        :return: Confirmation.
        """
        return json.dumps(await self._api("POST", "/me/player/next", user=True))

    async def previous_track(self, __user__: Optional[dict] = None) -> str:
        """
        Skip to the previous track.
        :return: Confirmation.
        """
        return json.dumps(await self._api("POST", "/me/player/previous", user=True))

    async def queue_track(self, spotify_uri: str, __user__: Optional[dict] = None) -> str:
        """
        Add a track URI to the user's playback queue.
        :param spotify_uri: spotify:track:... URI.
        :return: Confirmation.
        """
        return json.dumps(await self._api(
            "POST", "/me/player/queue", user=True,
            params={"uri": spotify_uri},
        ))

    async def set_volume(self, percent: int, __user__: Optional[dict] = None) -> str:
        """
        Set Spotify Connect device volume (0-100).
        :param percent: Target volume 0-100.
        :return: Confirmation.
        """
        if not 0 <= percent <= 100:
            return f"out of range: {percent}"
        return json.dumps(await self._api(
            "PUT", "/me/player/volume", user=True, params={"volume_percent": percent},
        ))

    # ── Playlists ─────────────────────────────────────────────────────────

    async def my_playlists(
        self,
        limit: int = 50,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        List the user's own and followed playlists.
        :param limit: 1-50.
        :return: Playlist id, name, track count.
        """
        out = await self._api("GET", "/me/playlists", user=True, params={"limit": limit})
        if isinstance(out, dict) and "error" in out:
            return f"{out['error']}: {out['body']}"
        rows = [f"{p['id']:<22}  {p['name']:<40}  tracks={p.get('tracks',{}).get('total','?')}"
                for p in out.get("items", [])]
        return "\n".join(rows) if rows else "(no playlists)"

    async def add_to_playlist(
        self,
        playlist_id: str,
        track_uris: list[str],
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Append tracks to a playlist.
        :param playlist_id: 22-char playlist id (not URI).
        :param track_uris: List of spotify:track:... URIs.
        :return: Confirmation snapshot id from Spotify.
        """
        out = await self._api(
            "POST", f"/playlists/{playlist_id}/tracks", user=True,
            json_body={"uris": track_uris},
        )
        return json.dumps(out)
