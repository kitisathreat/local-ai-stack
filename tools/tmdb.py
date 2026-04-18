"""
title: TMDB — Movies & TV Database
author: local-ai-stack
description: Search 800,000+ movies and 150,000+ TV shows from The Movie Database. Cast, crew, ratings, posters, trailers, genre, reviews. Free API key (register at themoviedb.org).
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from pydantic import BaseModel, Field
from typing import Optional


BASE = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p/w500"


class Tools:
    class Valves(BaseModel):
        API_KEY: str = Field(default="", description="TMDB API key v3 (free at https://www.themoviedb.org/settings/api)")

    def __init__(self):
        self.valves = self.Valves()

    async def search_movie(
        self,
        query: str,
        year: int = 0,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search movies by title.
        :param query: Movie title
        :param year: Optional release year
        :return: Top movies with year, rating, and overview
        """
        if not self.valves.API_KEY:
            return "Set TMDB API_KEY valve."
        params = {"api_key": self.valves.API_KEY, "query": query}
        if year: params["year"] = year
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(f"{BASE}/search/movie", params=params)
                r.raise_for_status()
                data = r.json()
            res = data.get("results", [])
            if not res:
                return f"No movies found: {query}"
            lines = [f"## TMDB Movies: {query}\n"]
            for m in res[:8]:
                t = m.get("title", "")
                ot = m.get("original_title", "")
                rd = m.get("release_date", "")
                rating = m.get("vote_average", 0)
                votes = m.get("vote_count", 0)
                overview = m.get("overview", "")
                poster = m.get("poster_path")
                mid = m.get("id", "")
                lines.append(f"**{t}** ({rd[:4] if rd else '?'}) — ⭐ {rating:.1f} ({votes:,})")
                if ot and ot != t:
                    lines.append(f"   _{ot}_")
                if overview:
                    lines.append(f"   {overview[:250]}")
                if poster:
                    lines.append(f"   ![poster]({IMG}{poster})")
                lines.append(f"   🔗 https://www.themoviedb.org/movie/{mid}\n")
            return "\n".join(lines)
        except Exception as e:
            return f"TMDB error: {e}"

    async def search_tv(self, query: str, __user__: Optional[dict] = None) -> str:
        """
        Search TV shows by title.
        :param query: TV show title
        :return: Top shows with first air date, rating, and overview
        """
        if not self.valves.API_KEY:
            return "Set TMDB API_KEY valve."
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(f"{BASE}/search/tv", params={"api_key": self.valves.API_KEY, "query": query})
                r.raise_for_status()
                res = r.json().get("results", [])
            if not res:
                return f"No TV shows found: {query}"
            lines = [f"## TMDB TV: {query}\n"]
            for s in res[:8]:
                t = s.get("name", "")
                fd = s.get("first_air_date", "")
                rating = s.get("vote_average", 0)
                overview = s.get("overview", "")
                tid = s.get("id", "")
                poster = s.get("poster_path")
                lines.append(f"**{t}** ({fd[:4] if fd else '?'}) — ⭐ {rating:.1f}")
                if overview:
                    lines.append(f"   {overview[:250]}")
                if poster:
                    lines.append(f"   ![poster]({IMG}{poster})")
                lines.append(f"   🔗 https://www.themoviedb.org/tv/{tid}\n")
            return "\n".join(lines)
        except Exception as e:
            return f"TMDB error: {e}"

    async def trending(
        self,
        media_type: str = "all",
        time_window: str = "week",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        What's trending on TMDB right now.
        :param media_type: "all", "movie", "tv", or "person"
        :param time_window: "day" or "week"
        :return: Trending titles
        """
        if not self.valves.API_KEY:
            return "Set TMDB API_KEY valve."
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(
                    f"{BASE}/trending/{media_type}/{time_window}",
                    params={"api_key": self.valves.API_KEY},
                )
                r.raise_for_status()
                res = r.json().get("results", [])
            if not res:
                return "No trending items."
            lines = [f"## TMDB Trending — {media_type}/{time_window}\n"]
            for m in res[:15]:
                t = m.get("title") or m.get("name", "")
                date = m.get("release_date") or m.get("first_air_date", "")
                rating = m.get("vote_average", 0)
                mt = m.get("media_type", media_type)
                lines.append(f"- **{t}** [{mt}] ({date[:4] if date else '?'}) ⭐ {rating:.1f}")
            return "\n".join(lines)
        except Exception as e:
            return f"TMDB error: {e}"
