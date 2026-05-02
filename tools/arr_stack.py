"""
title: *arr Stack — Sonarr / Radarr / Lidarr / Bazarr / Prowlarr Control
author: local-ai-stack
description: Talk to the standard self-hosted *arr media-automation stack via their REST APIs. Sonarr (TV) and Radarr (movies) handle indexing, monitoring, queueing, and downloader handoff; Lidarr does the same for music; Bazarr fetches subtitles for everything; Prowlarr is the upstream indexer manager. This tool exposes search and add operations, queue inspection, and a unified library lookup. Each app has its own URL + API key; setting one is enough — methods that need a missing app will say so.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field


_UA = "local-ai-stack/1.0 arr-stack"


def _strip(url: str) -> str:
    return (url or "").rstrip("/")


class Tools:
    class Valves(BaseModel):
        SONARR_URL: str = Field(default="", description="Sonarr base URL, e.g. http://localhost:8989")
        SONARR_API_KEY: str = Field(default="", description="Sonarr API key (Settings → General).")
        RADARR_URL: str = Field(default="", description="Radarr base URL, e.g. http://localhost:7878")
        RADARR_API_KEY: str = Field(default="", description="Radarr API key.")
        LIDARR_URL: str = Field(default="", description="Lidarr base URL, e.g. http://localhost:8686")
        LIDARR_API_KEY: str = Field(default="", description="Lidarr API key.")
        BAZARR_URL: str = Field(default="", description="Bazarr base URL, e.g. http://localhost:6767")
        BAZARR_API_KEY: str = Field(default="", description="Bazarr API key.")
        PROWLARR_URL: str = Field(default="", description="Prowlarr base URL, e.g. http://localhost:9696")
        PROWLARR_API_KEY: str = Field(default="", description="Prowlarr API key.")
        QUALITY_PROFILE_ID: int = Field(
            default=1,
            description="Default quality profile id when adding new series/movies/artists. Inspect /qualityprofile to find ids.",
        )
        ROOT_FOLDER: str = Field(
            default="",
            description="Default root folder path when adding new media. Must match what the *arr instance has configured.",
        )
        TIMEOUT: int = Field(default=20, description="HTTP timeout, seconds.")

    def __init__(self):
        self.valves = self.Valves()

    # ── HTTP helpers ──────────────────────────────────────────────────────

    def _cfg(self, app: str) -> tuple[str, str]:
        m = {
            "sonarr":  (self.valves.SONARR_URL,  self.valves.SONARR_API_KEY),
            "radarr":  (self.valves.RADARR_URL,  self.valves.RADARR_API_KEY),
            "lidarr":  (self.valves.LIDARR_URL,  self.valves.LIDARR_API_KEY),
            "bazarr":  (self.valves.BAZARR_URL,  self.valves.BAZARR_API_KEY),
            "prowlarr":(self.valves.PROWLARR_URL,self.valves.PROWLARR_API_KEY),
        }
        url, key = m.get(app.lower(), ("", ""))
        return _strip(url), key

    async def _get(self, app: str, path: str, params: Optional[dict] = None) -> Any:
        base, key = self._cfg(app)
        if not base or not key:
            return {"_err": f"{app} URL or API key not configured."}
        params = dict(params or {})
        # Sonarr/Radarr/Lidarr/Prowlarr accept either header or query param.
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT) as c:
            try:
                r = await c.get(
                    f"{base}{path}",
                    params=params,
                    headers={"X-Api-Key": key, "User-Agent": _UA, "Accept": "application/json"},
                )
            except Exception as e:
                return {"_err": f"{e}"}
        if r.status_code >= 400:
            return {"_err": f"{app} {r.status_code}: {r.text[:200]}"}
        try:
            return r.json()
        except Exception:
            return {"_err": f"{app} returned non-JSON"}

    async def _post(self, app: str, path: str, json_body: dict) -> Any:
        base, key = self._cfg(app)
        if not base or not key:
            return {"_err": f"{app} URL or API key not configured."}
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT) as c:
            try:
                r = await c.post(
                    f"{base}{path}",
                    json=json_body,
                    headers={"X-Api-Key": key, "User-Agent": _UA, "Accept": "application/json", "Content-Type": "application/json"},
                )
            except Exception as e:
                return {"_err": f"{e}"}
        if r.status_code >= 400:
            return {"_err": f"{app} {r.status_code}: {r.text[:300]}"}
        try:
            return r.json()
        except Exception:
            return {"_err": f"{app} returned non-JSON"}

    # ── Library overview ─────────────────────────────────────────────────

    async def system_status(self, __user__: Optional[dict] = None) -> str:
        """
        Probe every configured *arr app for /system/status.
        :return: Markdown table of app, version, branch, uptime.
        """
        rows = []
        for app in ("sonarr", "radarr", "lidarr", "bazarr", "prowlarr"):
            base, key = self._cfg(app)
            if not base or not key:
                rows.append(f"  {app:<9} —  not configured")
                continue
            data = await self._get(app, "/api/v3/system/status")
            if isinstance(data, dict) and data.get("_err"):
                rows.append(f"  {app:<9} ❌ {data['_err']}")
            else:
                ver = data.get("version", "?")
                branch = data.get("branch", "?")
                rows.append(f"  {app:<9} ✅ v{ver} ({branch})")
        return "## *arr stack status\n" + "\n".join(rows)

    # ── Sonarr (TV) ──────────────────────────────────────────────────────

    async def sonarr_search_series(self, query: str, __user__: Optional[dict] = None) -> str:
        """
        Search TVDB via Sonarr for new series to add.
        :param query: Show title.
        :return: Markdown list with TVDB ids and titles.
        """
        data = await self._get("sonarr", "/api/v3/series/lookup", {"term": query})
        if isinstance(data, dict) and data.get("_err"):
            return f"Sonarr: {data['_err']}"
        if not data:
            return f"Sonarr found no shows for: {query}"
        out = [f"## Sonarr lookup: {query}\n"]
        for s in data[:10]:
            out.append(
                f"**{s.get('title')}**  ({s.get('year', '—')})\n"
                f"   tvdbId: {s.get('tvdbId')}  ·  imdb: {s.get('imdbId', '—')}  ·  status: {s.get('status', '?')}\n"
                f"   {s.get('overview', '')[:300]}\n"
            )
        return "\n".join(out)

    async def sonarr_add_series(
        self,
        tvdb_id: int,
        monitor: str = "all",
        season_folder: bool = True,
        search_now: bool = True,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Add a series to Sonarr. Uses valves.QUALITY_PROFILE_ID + valves.ROOT_FOLDER.
        :param tvdb_id: TVDB id from `sonarr_search_series`.
        :param monitor: "all", "future", "missing", "existing", "pilot", "firstSeason", "latestSeason", "none".
        :param season_folder: Use season folders.
        :param search_now: Trigger a search after adding.
        :return: Status.
        """
        if not self.valves.ROOT_FOLDER:
            return "Sonarr add blocked: set ROOT_FOLDER in this tool's Valves first."
        # Re-fetch the lookup payload so we hand Sonarr its own series shape.
        lookup = await self._get("sonarr", "/api/v3/series/lookup", {"term": f"tvdb:{int(tvdb_id)}"})
        if isinstance(lookup, dict) and lookup.get("_err"):
            return f"Sonarr lookup failed: {lookup['_err']}"
        if not lookup:
            return f"Sonarr could not find tvdbId {tvdb_id}"
        body = lookup[0]
        body.update({
            "monitored": True,
            "rootFolderPath": self.valves.ROOT_FOLDER,
            "qualityProfileId": self.valves.QUALITY_PROFILE_ID,
            "seasonFolder": season_folder,
            "addOptions": {"monitor": monitor, "searchForMissingEpisodes": bool(search_now)},
        })
        res = await self._post("sonarr", "/api/v3/series", body)
        if isinstance(res, dict) and res.get("_err"):
            return f"Sonarr add failed: {res['_err']}"
        return f"Added '{res.get('title')}' to Sonarr (id {res.get('id')}). Search triggered: {search_now}."

    async def sonarr_queue(self, __user__: Optional[dict] = None) -> str:
        """
        Show the current Sonarr download queue.
        """
        data = await self._get("sonarr", "/api/v3/queue", {"pageSize": 50})
        if isinstance(data, dict) and data.get("_err"):
            return f"Sonarr: {data['_err']}"
        records = data.get("records") or []
        if not records:
            return "Sonarr queue is empty."
        out = [f"## Sonarr queue ({len(records)})\n"]
        for r in records:
            pct = r.get("size") and r.get("sizeleft") is not None
            progress = (1 - (r["sizeleft"] / r["size"])) * 100 if pct and r["size"] else 0
            out.append(f"- {r.get('title', '?')}  ·  {r.get('status', '?')}  ·  {progress:.1f}%")
        return "\n".join(out)

    # ── Radarr (movies) ──────────────────────────────────────────────────

    async def radarr_search_movie(self, query: str, __user__: Optional[dict] = None) -> str:
        """
        Search TMDB via Radarr for movies to add.
        :param query: Movie title (or "tmdb:NNN" / "imdb:tt…").
        :return: Markdown list with tmdbId, year, runtime.
        """
        data = await self._get("radarr", "/api/v3/movie/lookup", {"term": query})
        if isinstance(data, dict) and data.get("_err"):
            return f"Radarr: {data['_err']}"
        if not data:
            return f"Radarr found no movies for: {query}"
        out = [f"## Radarr lookup: {query}\n"]
        for m in data[:10]:
            out.append(
                f"**{m.get('title')}**  ({m.get('year', '—')})\n"
                f"   tmdbId: {m.get('tmdbId')}  ·  imdb: {m.get('imdbId', '—')}  ·  runtime: {m.get('runtime', '?')}m\n"
                f"   {m.get('overview', '')[:300]}\n"
            )
        return "\n".join(out)

    async def radarr_add_movie(
        self,
        tmdb_id: int,
        search_now: bool = True,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Add a movie to Radarr.
        :param tmdb_id: TMDB id.
        :param search_now: Trigger a search after adding.
        :return: Status.
        """
        if not self.valves.ROOT_FOLDER:
            return "Radarr add blocked: set ROOT_FOLDER in this tool's Valves first."
        lookup = await self._get("radarr", "/api/v3/movie/lookup", {"term": f"tmdb:{int(tmdb_id)}"})
        if isinstance(lookup, dict) and lookup.get("_err"):
            return f"Radarr lookup failed: {lookup['_err']}"
        if not lookup:
            return f"Radarr could not find tmdbId {tmdb_id}"
        body = lookup[0]
        body.update({
            "monitored": True,
            "rootFolderPath": self.valves.ROOT_FOLDER,
            "qualityProfileId": self.valves.QUALITY_PROFILE_ID,
            "minimumAvailability": "released",
            "addOptions": {"searchForMovie": bool(search_now)},
        })
        res = await self._post("radarr", "/api/v3/movie", body)
        if isinstance(res, dict) and res.get("_err"):
            return f"Radarr add failed: {res['_err']}"
        return f"Added '{res.get('title')}' to Radarr (id {res.get('id')}). Search: {search_now}."

    async def radarr_queue(self, __user__: Optional[dict] = None) -> str:
        """
        Show the Radarr download queue.
        """
        data = await self._get("radarr", "/api/v3/queue", {"pageSize": 50})
        if isinstance(data, dict) and data.get("_err"):
            return f"Radarr: {data['_err']}"
        records = data.get("records") or []
        if not records:
            return "Radarr queue is empty."
        out = [f"## Radarr queue ({len(records)})\n"]
        for r in records:
            out.append(f"- {r.get('title', '?')}  ·  {r.get('status', '?')}")
        return "\n".join(out)

    # ── Lidarr (music) ───────────────────────────────────────────────────

    async def lidarr_search_artist(self, query: str, __user__: Optional[dict] = None) -> str:
        """
        Search MusicBrainz via Lidarr for artists to monitor.
        :param query: Artist name.
        :return: Markdown list with foreignArtistId.
        """
        data = await self._get("lidarr", "/api/v1/artist/lookup", {"term": query})
        if isinstance(data, dict) and data.get("_err"):
            return f"Lidarr: {data['_err']}"
        if not data:
            return f"Lidarr found no artists for: {query}"
        out = [f"## Lidarr lookup: {query}\n"]
        for a in data[:10]:
            out.append(
                f"**{a.get('artistName')}**\n"
                f"   foreignArtistId (MBID): {a.get('foreignArtistId')}  ·  status: {a.get('status', '?')}\n"
                f"   {a.get('overview', '')[:300]}\n"
            )
        return "\n".join(out)

    async def lidarr_add_artist(
        self,
        foreign_artist_id: str,
        monitor_new: bool = True,
        search_now: bool = True,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Add an artist to Lidarr.
        :param foreign_artist_id: MusicBrainz artist UUID.
        :param monitor_new: Monitor newly-released albums.
        :param search_now: Trigger a search.
        :return: Status.
        """
        if not self.valves.ROOT_FOLDER:
            return "Lidarr add blocked: set ROOT_FOLDER in this tool's Valves first."
        lookup = await self._get("lidarr", "/api/v1/artist/lookup", {"term": f"lidarr:{foreign_artist_id}"})
        if isinstance(lookup, dict) and lookup.get("_err"):
            return f"Lidarr lookup failed: {lookup['_err']}"
        if not lookup:
            return f"Lidarr could not find MBID {foreign_artist_id}"
        body = lookup[0]
        body.update({
            "monitored": True,
            "rootFolderPath": self.valves.ROOT_FOLDER,
            "qualityProfileId": self.valves.QUALITY_PROFILE_ID,
            "metadataProfileId": 1,
            "addOptions": {"monitor": "all" if monitor_new else "none", "searchForMissingAlbums": bool(search_now)},
        })
        res = await self._post("lidarr", "/api/v1/artist", body)
        if isinstance(res, dict) and res.get("_err"):
            return f"Lidarr add failed: {res['_err']}"
        return f"Added '{res.get('artistName')}' to Lidarr (id {res.get('id')})."

    # ── Prowlarr (indexer search) ────────────────────────────────────────

    async def prowlarr_search(
        self,
        query: str,
        category: int = 0,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search every configured Prowlarr indexer in one shot. Useful when
        Sonarr/Radarr say "no results" — Prowlarr's pooled view is wider.
        :param query: Free-text query.
        :param category: Newznab category id (e.g. 2000 = movies, 5000 = TV, 3000 = audio). 0 = all.
        :return: Top results across all indexers.
        """
        params: dict[str, Any] = {"query": query, "type": "search", "limit": 50}
        if category:
            params["categories"] = category
        data = await self._get("prowlarr", "/api/v1/search", params)
        if isinstance(data, dict) and data.get("_err"):
            return f"Prowlarr: {data['_err']}"
        if not data:
            return f"Prowlarr: no results for {query}"
        # Sort by seeders desc, then size desc.
        data.sort(key=lambda r: (r.get("seeders", 0) or 0, r.get("size", 0) or 0), reverse=True)
        out = [f"## Prowlarr: {query}\n"]
        for r in data[:20]:
            size = r.get("size") or 0
            seeds = r.get("seeders", 0)
            out.append(
                f"**{r.get('title', '—')}**  [{r.get('indexer', '?')}]\n"
                f"   {seeds} seeders  ·  {size/1e9:.2f} GB  ·  cat: {r.get('categories', '?')}\n"
                f"   {r.get('downloadUrl') or r.get('infoUrl') or ''}\n"
            )
        return "\n".join(out)

    # ── Bazarr (subtitles) ───────────────────────────────────────────────

    async def bazarr_missing_subtitles(self, __user__: Optional[dict] = None) -> str:
        """
        List items Bazarr currently has missing subtitles for.
        """
        data = await self._get("bazarr", "/api/episodes/wanted")
        if isinstance(data, dict) and data.get("_err"):
            return f"Bazarr: {data['_err']}"
        items = (data or {}).get("data") or []
        if not items:
            return "Bazarr: no missing subtitles."
        out = [f"## Bazarr missing subtitles ({len(items)})\n"]
        for it in items[:50]:
            out.append(f"- {it.get('seriesTitle', '?')} S{it.get('season', '?'):02d}E{it.get('episode', '?'):02d}: {it.get('episodeTitle', '?')}  ·  missing: {it.get('missing_subtitles', [])}")
        return "\n".join(out)
