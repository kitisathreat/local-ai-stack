"""
title: TVMaze — Free TV Schedule, Episodes & Show Metadata
author: local-ai-stack
description: TVMaze is a free, no-key TV database covering shows, networks, schedules, episodes, and cast. Better than TMDB for "is the next episode out yet?" — schedule endpoints publish exact air dates with timezone. This tool exposes show search, full episode list, current/upcoming schedule, episode-by-number lookup, and cast.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import httpx
from pydantic import BaseModel, Field


_UA = "local-ai-stack/1.0 tvmaze"
TVMAZE = "https://api.tvmaze.com"


def _strip_html(s: str, n: int = 1000) -> str:
    import re
    s = (s or "").replace("&nbsp;", " ").replace("&amp;", "&")
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:n]


class Tools:
    class Valves(BaseModel):
        MAX_RESULTS: int = Field(default=10, description="Max search results.")
        TIMEOUT: int = Field(default=15, description="HTTP timeout, seconds.")
        SCHEDULE_COUNTRY: str = Field(default="US", description="ISO country code for /schedule.")

    def __init__(self):
        self.valves = self.Valves()

    async def _get(self, path: str, params: Optional[dict] = None):
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT) as c:
            r = await c.get(f"{TVMAZE}{path}", params=params or {}, headers={"User-Agent": _UA})
            if r.status_code == 404:
                return None
            if r.status_code >= 400:
                return {"_err": f"tvmaze {r.status_code}: {r.text[:200]}"}
            try:
                return r.json()
            except Exception:
                return {"_err": "tvmaze returned non-JSON"}

    # ── Search ────────────────────────────────────────────────────────────

    async def search_shows(
        self,
        query: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search TV shows by title.
        :param query: Show title.
        :return: Markdown list with id, network, premiered, status, page URL.
        """
        data = await self._get("/search/shows", {"q": query})
        if isinstance(data, dict) and data.get("_err"):
            return f"TVMaze error: {data['_err']}"
        if not data:
            return f"No TVMaze shows for: {query}"
        out = [f"## TVMaze: {query}\n"]
        for it in data[: self.valves.MAX_RESULTS]:
            s = it.get("show", {})
            net = (s.get("network") or s.get("webChannel") or {}).get("name", "—")
            sched = s.get("schedule", {})
            air = f"{', '.join(sched.get('days', []) or ['—'])} {sched.get('time', '')}".strip()
            out.append(
                f"**{s.get('name', '—')}**  ({s.get('premiered', '—')[:4]})  _{s.get('status', '?')}_\n"
                f"   id: {s.get('id')}  ·  network: {net}  ·  airs: {air or '—'}\n"
                f"   genres: {', '.join(s.get('genres', []) or ['—'])}\n"
                f"   {s.get('url', '')}\n"
            )
        return "\n".join(out)

    # ── Show details ─────────────────────────────────────────────────────

    async def show_info(
        self,
        show_id: int,
        include_cast: bool = False,
        include_episodes: bool = False,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Detailed info for a TVMaze show id.
        :param show_id: TVMaze show id.
        :param include_cast: Embed cast list.
        :param include_episodes: Embed full episode list.
        :return: Markdown summary.
        """
        embeds = []
        if include_cast:
            embeds.append("cast")
        if include_episodes:
            embeds.append("episodes")
        params = {}
        if embeds:
            params["embed[]"] = embeds
        data = await self._get(f"/shows/{int(show_id)}", params)
        if isinstance(data, dict) and data.get("_err"):
            return f"TVMaze error: {data['_err']}"
        if not data:
            return f"Show {show_id} not found"

        out = [
            f"## {data.get('name', '—')}  ({data.get('premiered', '—')[:4]})",
            f"status: {data.get('status', '?')}  ·  type: {data.get('type', '?')}",
            f"network: {(data.get('network') or data.get('webChannel') or {}).get('name', '—')}",
            f"genres: {', '.join(data.get('genres', []) or ['—'])}",
            f"runtime: {data.get('runtime') or data.get('averageRuntime') or '?'} min",
            f"page: {data.get('url', '—')}",
            "",
            f"### Summary\n{_strip_html(data.get('summary', ''), 2000)}",
        ]
        embedded = data.get("_embedded") or {}
        if include_cast and embedded.get("cast"):
            out.append("\n### Cast")
            for c in embedded["cast"][:30]:
                p = (c.get("person") or {}).get("name", "—")
                ch = (c.get("character") or {}).get("name", "—")
                out.append(f"  - {p} as {ch}")
        if include_episodes and embedded.get("episodes"):
            out.append(f"\n### Episodes ({len(embedded['episodes'])})")
            for ep in embedded["episodes"][:200]:
                out.append(
                    f"  S{ep.get('season', '?'):02d}E{ep.get('number', '?'):02d}  "
                    f"({ep.get('airdate', '—')})  {ep.get('name', '—')}"
                )
        return "\n".join(out)

    # ── Episode lookup ───────────────────────────────────────────────────

    async def episode(
        self,
        show_id: int,
        season: int,
        number: int,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Look up a single episode by season + number.
        :param show_id: TVMaze show id.
        :param season: Season number.
        :param number: Episode number within the season.
        :return: Title, airdate, runtime, summary, page URL.
        """
        data = await self._get(
            f"/shows/{int(show_id)}/episodebynumber",
            {"season": int(season), "number": int(number)},
        )
        if isinstance(data, dict) and data.get("_err"):
            return f"TVMaze error: {data['_err']}"
        if not data:
            return f"Episode S{season:02d}E{number:02d} not found for show {show_id}"
        return (
            f"## S{data.get('season', season):02d}E{data.get('number', number):02d} — {data.get('name', '—')}\n"
            f"airdate: {data.get('airdate', '—')}  ·  runtime: {data.get('runtime', '?')} min\n"
            f"page: {data.get('url', '')}\n\n"
            f"{_strip_html(data.get('summary', ''), 2000)}"
        )

    async def next_episode(
        self,
        show_id: int,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        When does the next episode air? Returns "no upcoming" if the show has
        ended or none is scheduled.
        :param show_id: TVMaze show id.
        :return: Markdown with airdate, episode title, time-until-air.
        """
        data = await self._get(f"/shows/{int(show_id)}", {"embed": "nextepisode"})
        if isinstance(data, dict) and data.get("_err"):
            return f"TVMaze error: {data['_err']}"
        if not data:
            return f"Show {show_id} not found"
        nxt = (data.get("_embedded") or {}).get("nextepisode")
        if not nxt:
            return f"No upcoming episode listed for {data.get('name', show_id)} (status: {data.get('status', '?')})"

        airstamp = nxt.get("airstamp")
        delta = "?"
        try:
            t = datetime.fromisoformat((airstamp or "").replace("Z", "+00:00"))
            secs = int((t - datetime.now(timezone.utc)).total_seconds())
            if secs > 0:
                d, rem = divmod(secs, 86400)
                h, _ = divmod(rem, 3600)
                delta = f"{d}d {h}h"
            else:
                delta = "aired"
        except Exception:
            pass

        return (
            f"## Next episode: {data.get('name', '—')}\n"
            f"S{nxt.get('season', '?'):02d}E{nxt.get('number', '?'):02d} — {nxt.get('name', '—')}\n"
            f"airs: {airstamp or nxt.get('airdate', '—')}  ·  in: {delta}\n"
            f"page: {nxt.get('url', '—')}"
        )

    # ── Schedule ─────────────────────────────────────────────────────────

    async def schedule_today(
        self,
        country: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        TV schedule for today in a given country.
        :param country: ISO country code; defaults to valves.SCHEDULE_COUNTRY.
        :return: Markdown grouped by network.
        """
        country = country or self.valves.SCHEDULE_COUNTRY
        data = await self._get("/schedule", {"country": country})
        if isinstance(data, dict) and data.get("_err"):
            return f"TVMaze error: {data['_err']}"
        if not data:
            return f"No TV schedule for {country} today."
        groups: dict[str, list[dict]] = {}
        for ep in data:
            net = (ep.get("show") or {}).get("network", {}).get("name") or "—"
            groups.setdefault(net, []).append(ep)
        out = [f"## TV schedule today — {country}\n"]
        for net, eps in sorted(groups.items(), key=lambda kv: kv[0]):
            out.append(f"### {net}")
            for ep in eps[:30]:
                show = (ep.get("show") or {}).get("name", "—")
                out.append(
                    f"  {ep.get('airtime', '?')} — **{show}** "
                    f"S{ep.get('season', '?'):02d}E{ep.get('number', '?'):02d}: "
                    f"{ep.get('name', '—')}"
                )
            out.append("")
        return "\n".join(out)

    # ── External-id lookup ──────────────────────────────────────────────

    async def lookup_imdb(
        self,
        imdb_id: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Resolve an IMDb id (tt-prefixed) to a TVMaze show.
        :param imdb_id: IMDb id, e.g. tt0944947.
        :return: Show summary.
        """
        data = await self._get("/lookup/shows", {"imdb": imdb_id})
        if isinstance(data, dict) and data.get("_err"):
            return f"TVMaze error: {data['_err']}"
        if not data:
            return f"No TVMaze show for IMDb id {imdb_id}"
        return (
            f"## {data.get('name', '—')}  ({data.get('premiered', '—')[:4]})\n"
            f"id: {data.get('id')}  ·  status: {data.get('status', '?')}\n"
            f"page: {data.get('url', '—')}"
        )
