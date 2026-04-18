"""
title: NASA Open APIs — APOD, NEO, Mars Rover & Earth
author: local-ai-stack
description: Access NASA's free Open APIs. Astronomy Picture of the Day (APOD), Near Earth Object (asteroid) tracking, Mars Rover photos (Curiosity, Perseverance, Opportunity), Earth satellite imagery, and NASA technology patent search. Free API key required — get one instantly at api.nasa.gov.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from datetime import datetime, timedelta
from typing import Callable, Any, Optional
from pydantic import BaseModel, Field

BASE = "https://api.nasa.gov"


class Tools:
    class Valves(BaseModel):
        NASA_API_KEY: str = Field(
            default="DEMO_KEY",
            description="NASA API key — free at https://api.nasa.gov (DEMO_KEY works but is rate-limited to 30/hr)",
        )

    def __init__(self):
        self.valves = self.Valves()

    async def astronomy_picture_of_the_day(
        self,
        date: str = "",
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get NASA's Astronomy Picture of the Day with full explanation by a professional astronomer.
        :param date: Date in YYYY-MM-DD format (leave blank for today). Can go back to 1995-06-16.
        :return: Title, date, explanation, image URL, and media type
        """
        params = {"api_key": self.valves.NASA_API_KEY}
        if date:
            params["date"] = date

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Fetching NASA APOD...", "done": False}})

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(f"{BASE}/planetary/apod", params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            return f"NASA APOD error: {str(e)}"

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        title = data.get("title", "")
        date_str = data.get("date", "")
        explanation = data.get("explanation", "")
        url = data.get("url", "")
        hdurl = data.get("hdurl", url)
        media = data.get("media_type", "image")
        copyright_ = data.get("copyright", "NASA")

        lines = [f"## NASA Astronomy Picture of the Day\n"]
        lines.append(f"### {title}")
        lines.append(f"**Date:** {date_str} | **Credit:** {copyright_}\n")
        lines.append(explanation)
        lines.append(f"\n**{'Image' if media == 'image' else 'Video'}:** {url}")
        if hdurl and hdurl != url:
            lines.append(f"**HD:** {hdurl}")

        return "\n".join(lines)

    async def near_earth_objects(
        self,
        start_date: str = "",
        days: int = 3,
        min_magnitude: float = 0,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get Near Earth Objects (asteroids and comets) approaching Earth from NASA's NeoWs database.
        :param start_date: Start date YYYY-MM-DD (default: today)
        :param days: Number of days to look ahead (1-7, default 3)
        :param min_magnitude: Filter by minimum estimated diameter in km (0 = all sizes)
        :return: List of NEOs with close approach dates, distances, sizes, and hazard status
        """
        if not start_date:
            start_date = datetime.now().strftime("%Y-%m-%d")

        days = max(1, min(7, days))
        end_date = (datetime.strptime(start_date, "%Y-%m-%d") + timedelta(days=days)).strftime("%Y-%m-%d")

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Fetching Near Earth Objects {start_date} to {end_date}...", "done": False}})

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(
                    f"{BASE}/neo/rest/v1/feed",
                    params={"start_date": start_date, "end_date": end_date, "api_key": self.valves.NASA_API_KEY},
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            return f"NEO API error: {str(e)}"

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        total = data.get("element_count", 0)
        neo_dates = data.get("near_earth_objects", {})

        lines = [f"## Near Earth Objects: {start_date} to {end_date}\n"]
        lines.append(f"Total objects tracked: **{total}**\n")

        hazardous_count = 0
        all_neos = []
        for date_key, neos in sorted(neo_dates.items()):
            for neo in neos:
                approach = neo.get("close_approach_data", [{}])[0]
                diam_min = neo.get("estimated_diameter", {}).get("kilometers", {}).get("estimated_diameter_min", 0)
                diam_max = neo.get("estimated_diameter", {}).get("kilometers", {}).get("estimated_diameter_max", 0)
                if diam_max < min_magnitude:
                    continue
                hazardous = neo.get("is_potentially_hazardous_asteroid", False)
                if hazardous:
                    hazardous_count += 1
                all_neos.append({
                    "date": date_key,
                    "name": neo.get("name", ""),
                    "id": neo.get("id", ""),
                    "diam_min": diam_min,
                    "diam_max": diam_max,
                    "velocity_km_s": float(approach.get("relative_velocity", {}).get("kilometers_per_second", 0)),
                    "miss_km": float(approach.get("miss_distance", {}).get("kilometers", 0)),
                    "miss_lunar": float(approach.get("miss_distance", {}).get("lunar", 0)),
                    "hazardous": hazardous,
                })

        all_neos.sort(key=lambda x: x["miss_km"])

        lines.append(f"⚠️ **Potentially hazardous: {hazardous_count}**\n")
        lines.append("| Date | Name | Size (km) | Miss Distance | Velocity | Hazardous |")
        lines.append("|------|------|-----------|---------------|----------|-----------|")

        for neo in all_neos[:25]:
            hazard = "⚠️ YES" if neo["hazardous"] else "No"
            diam = f"{neo['diam_min']:.3f} – {neo['diam_max']:.3f}"
            miss = f"{neo['miss_km']:,.0f} km ({neo['miss_lunar']:.1f} lunar)"
            vel = f"{neo['velocity_km_s']:.2f} km/s"
            lines.append(f"| {neo['date']} | {neo['name']} | {diam} | {miss} | {vel} | {hazard} |")

        return "\n".join(lines)

    async def mars_rover_photos(
        self,
        rover: str = "curiosity",
        sol: int = 1000,
        camera: str = "",
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get photos taken by NASA Mars rovers (Curiosity, Perseverance, Opportunity) on a given Martian sol.
        :param rover: Rover name: 'curiosity', 'perseverance', or 'opportunity'
        :param sol: Martian sol (day) since landing (e.g. 1000, 3000, 100). Curiosity has 4000+ sols.
        :param camera: Camera abbreviation (FHAZ, RHAZ, MAST, CHEMCAM, MAHLI, MARDI, NAVCAM, PANCAM, MINITES) or blank for all
        :return: List of photos with URLs, camera, and Earth date
        """
        rover = rover.lower().strip()
        valid_rovers = ["curiosity", "perseverance", "opportunity", "spirit"]
        if rover not in valid_rovers:
            return f"Invalid rover. Choose from: {', '.join(valid_rovers)}"

        params = {"sol": sol, "api_key": self.valves.NASA_API_KEY, "page": 1}
        if camera:
            params["camera"] = camera.upper()

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Fetching {rover.title()} photos from sol {sol}...", "done": False}})

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{BASE}/mars-photos/api/v1/rovers/{rover}/photos",
                    params=params,
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            return f"Mars Rover API error: {str(e)}"

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        photos = data.get("photos", [])
        if not photos:
            return (
                f"No photos found for {rover.title()} on sol {sol}"
                + (f" with camera {camera}" if camera else "")
                + f". Try a different sol."
            )

        rover_info = photos[0].get("rover", {})
        earth_date = photos[0].get("earth_date", "")
        status = rover_info.get("status", "")
        total_photos = rover_info.get("total_photos", "")

        lines = [f"## {rover.title()} Mars Rover Photos — Sol {sol}\n"]
        lines.append(f"**Earth Date:** {earth_date} | **Rover Status:** {status} | **Total Mission Photos:** {total_photos:,}\n")
        lines.append(f"Found **{len(photos)}** photo(s) on sol {sol}{' (' + camera + ' camera)' if camera else ''}\n")

        # Group by camera
        by_camera = {}
        for p in photos:
            cam = p.get("camera", {}).get("full_name", "Unknown")
            by_camera.setdefault(cam, []).append(p.get("img_src", ""))

        for cam, urls in by_camera.items():
            lines.append(f"**{cam}** ({len(urls)} photos):")
            for url in urls[:3]:
                lines.append(f"- {url}")
            if len(urls) > 3:
                lines.append(f"  *(+{len(urls)-3} more)*")
            lines.append("")

        return "\n".join(lines)

    async def exoplanet_search(
        self,
        query: str = "",
        min_mass: float = 0,
        discovery_method: str = "",
        limit: int = 20,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search the NASA Exoplanet Archive for confirmed exoplanets. Filter by name, discovery method, or mass.
        :param query: Planet or star name to search (e.g. 'Kepler-452', 'TRAPPIST', 'hot Jupiter')
        :param min_mass: Minimum planet mass in Jupiter masses (0 = no filter, e.g. 0.1 for super-Earths)
        :param discovery_method: Discovery method filter (Transit, Radial Velocity, Imaging, Microlensing, Astrometry)
        :param limit: Number of results (max 50)
        :return: Exoplanet data including mass, radius, orbital period, equilibrium temperature, and discovery details
        """
        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Querying NASA Exoplanet Archive...", "done": False}})

        # NASA Exoplanet Archive TAP service - no API key needed
        where_clauses = ["pl_controv_flag=0"]  # confirmed only
        if query:
            where_clauses.append(f"(pl_name LIKE '%{query}%' OR hostname LIKE '%{query}%')")
        if min_mass > 0:
            where_clauses.append(f"pl_bmassj>{min_mass}")
        if discovery_method:
            where_clauses.append(f"discoverymethod='{discovery_method}'")

        where = " AND ".join(where_clauses)
        adql = (
            f"SELECT TOP {min(limit,50)} pl_name,hostname,discoverymethod,disc_year,"
            f"pl_orbper,pl_bmassj,pl_radj,pl_eqt,sy_dist,pl_nnotes "
            f"FROM pscomppars WHERE {where} ORDER BY disc_year DESC"
        )

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(
                    "https://exoplanetarchive.ipac.caltech.edu/TAP/sync",
                    params={"query": adql, "format": "json"},
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            return f"Exoplanet Archive error: {str(e)}"

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        if not data:
            return f"No exoplanets found matching your criteria."

        lines = [f"## NASA Exoplanet Archive — {len(data)} results\n"]
        lines.append("| Planet | Host Star | Method | Year | Period (days) | Mass (Mj) | Radius (Rj) | Temp (K) | Distance (pc) |")
        lines.append("|--------|-----------|--------|------|--------------|-----------|-------------|----------|--------------|")

        for p in data:
            name = p.get("pl_name", "")
            host = p.get("hostname", "")
            method = p.get("discoverymethod", "")[:15]
            year = p.get("disc_year", "")
            period = f"{float(p['pl_orbper']):.2f}" if p.get("pl_orbper") else "—"
            mass = f"{float(p['pl_bmassj']):.3f}" if p.get("pl_bmassj") else "—"
            radius = f"{float(p['pl_radj']):.3f}" if p.get("pl_radj") else "—"
            temp = f"{int(p['pl_eqt'])}" if p.get("pl_eqt") else "—"
            dist = f"{float(p['sy_dist']):.1f}" if p.get("sy_dist") else "—"
            lines.append(f"| **{name}** | {host} | {method} | {year} | {period} | {mass} | {radius} | {temp} | {dist} |")

        lines.append(f"\nSource: NASA Exoplanet Archive (exoplanetarchive.ipac.caltech.edu)")
        return "\n".join(lines)
