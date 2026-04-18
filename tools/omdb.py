"""
title: OMDb — Open Movie Database (IMDb data)
author: local-ai-stack
description: Look up films and shows using IMDb data via OMDb. Title, year, plot, cast, director, genre, IMDb/Rotten Tomatoes ratings, box office. Free API key (1,000 requests/day).
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import os
import httpx
from pydantic import BaseModel, Field
from typing import Optional


BASE = "https://www.omdbapi.com/"


class Tools:
    class Valves(BaseModel):
        API_KEY: str = Field(default_factory=lambda: os.environ.get("OMDB_API_KEY", ""), description="OMDb key (free at https://www.omdbapi.com/apikey.aspx)")

    def __init__(self):
        self.valves = self.Valves()

    async def title(self, title: str, year: int = 0, __user__: Optional[dict] = None) -> str:
        """
        Look up a single movie/show by title.
        :param title: Exact or close title
        :param year: Optional release year for disambiguation
        :return: Full record with plot, cast, ratings, etc.
        """
        if not self.valves.API_KEY:
            return "Set OMDB API_KEY valve."
        params = {"apikey": self.valves.API_KEY, "t": title, "plot": "short"}
        if year: params["y"] = year
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(BASE, params=params)
                r.raise_for_status()
                d = r.json()
            if d.get("Response") == "False":
                return f"OMDb: {d.get('Error', 'not found')}"
            ratings = "\n".join(f"- {x.get('Source')}: {x.get('Value')}" for x in d.get("Ratings", []))
            poster = d.get("Poster") if d.get("Poster") and d.get("Poster") != "N/A" else ""
            out = [
                f"## {d.get('Title','')} ({d.get('Year','')})",
                f"**Rated:** {d.get('Rated','')}   **Runtime:** {d.get('Runtime','')}   **Genre:** {d.get('Genre','')}",
                f"**Director:** {d.get('Director','')}",
                f"**Writer:** {d.get('Writer','')}",
                f"**Cast:** {d.get('Actors','')}",
                f"\n{d.get('Plot','')}",
                "\n**Ratings:**", ratings,
                f"\n**Box office:** {d.get('BoxOffice','—')}   **Awards:** {d.get('Awards','—')}",
                f"\n🔗 https://www.imdb.com/title/{d.get('imdbID','')}/",
            ]
            if poster:
                out.append(f"\n![poster]({poster})")
            return "\n".join(out)
        except Exception as e:
            return f"OMDb error: {e}"

    async def search(self, query: str, type_: str = "", __user__: Optional[dict] = None) -> str:
        """
        Search movies/shows by keyword.
        :param query: Keywords
        :param type_: Optional "movie", "series", or "episode"
        :return: Top 10 matches with title, year, and IMDb ID
        """
        if not self.valves.API_KEY:
            return "Set OMDB API_KEY valve."
        params = {"apikey": self.valves.API_KEY, "s": query}
        if type_: params["type"] = type_
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(BASE, params=params)
                r.raise_for_status()
                d = r.json()
            if d.get("Response") == "False":
                return f"OMDb: {d.get('Error','no results')}"
            lines = [f"## OMDb search: {query}\n"]
            for x in d.get("Search", [])[:10]:
                lines.append(f"- **{x.get('Title','')}** ({x.get('Year','')}) [{x.get('Type','')}]  imdb:{x.get('imdbID','')}")
            return "\n".join(lines)
        except Exception as e:
            return f"OMDb error: {e}"
