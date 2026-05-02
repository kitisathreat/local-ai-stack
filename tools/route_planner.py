"""
title: Route Planner — Multi-Stop Routing via OSRM / OpenStreetMap
author: local-ai-stack
description: Compute driving / walking / cycling routes between points, optimize multi-stop tours (TSP), get turn-by-turn directions, and return distance + duration. Uses the public OSRM demo server by default; point ROUTING_BASE at a self-hosted OSRM (or Valhalla) instance for production load. Coordinates accept either lon,lat pairs or human place names (resolved through openstreetmap nominatim).
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field


class Tools:
    class Valves(BaseModel):
        ROUTING_BASE: str = Field(
            default="https://router.project-osrm.org",
            description="OSRM endpoint. The public demo enforces fair-use limits; self-host for serious usage.",
        )
        NOMINATIM_BASE: str = Field(
            default="https://nominatim.openstreetmap.org",
            description="Nominatim geocoder endpoint.",
        )
        UA: str = Field(default="local-ai-stack/1.0")
        TIMEOUT: int = Field(default=20)

    def __init__(self):
        self.valves = self.Valves()

    async def _geocode(self, client: httpx.AsyncClient, place: str) -> tuple[float, float] | None:
        # Allow direct "lon,lat" pass-through.
        if "," in place and all(p.replace(".", "").replace("-", "").isdigit()
                                for p in place.split(",")):
            lon, lat = (float(x) for x in place.split(","))
            return lon, lat
        try:
            r = await client.get(
                f"{self.valves.NOMINATIM_BASE}/search",
                params={"q": place, "format": "json", "limit": 1},
                headers={"User-Agent": self.valves.UA},
            )
        except Exception:
            return None
        if r.status_code != 200:
            return None
        results = r.json() or []
        if not results:
            return None
        return float(results[0]["lon"]), float(results[0]["lat"])

    async def _osrm(
        self,
        client: httpx.AsyncClient,
        coords: list[tuple[float, float]],
        profile: str,
        overview: str = "false",
        alternatives: bool = False,
    ) -> dict | None:
        coord_str = ";".join(f"{lon},{lat}" for lon, lat in coords)
        params: dict = {"overview": overview, "alternatives": str(alternatives).lower(),
                        "steps": "true", "annotations": "true"}
        try:
            r = await client.get(
                f"{self.valves.ROUTING_BASE}/route/v1/{profile}/{coord_str}",
                params=params, headers={"User-Agent": self.valves.UA},
            )
        except Exception:
            return None
        if r.status_code != 200:
            return None
        return r.json()

    # ── Public API ────────────────────────────────────────────────────────

    async def route(
        self,
        from_place: str,
        to_place: str,
        profile: str = "driving",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        A→B route with turn-by-turn directions and total distance + duration.
        :param from_place: Place name or "lon,lat".
        :param to_place: Place name or "lon,lat".
        :param profile: driving, walking, cycling (OSRM names: car, foot, bike — we map for you).
        :return: Multi-line summary with steps.
        """
        prof = {"driving": "car", "walking": "foot", "cycling": "bike"}.get(profile, profile)
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT) as c:
            a = await self._geocode(c, from_place)
            b = await self._geocode(c, to_place)
            if not a or not b:
                return f"could not geocode: {from_place if not a else to_place}"
            data = await self._osrm(c, [a, b], prof)
        if not data or not data.get("routes"):
            return "no route found"
        route = data["routes"][0]
        d_km = route["distance"] / 1000
        t_min = route["duration"] / 60
        out = [f"# {from_place} → {to_place} ({profile})",
               f"distance: {d_km:.1f} km   duration: {t_min:.0f} min"]
        legs = route.get("legs", [])
        for leg in legs:
            for step in leg.get("steps", [])[:30]:
                man = step.get("maneuver", {}).get("instruction") or step.get("name") or "(no instruction)"
                d = step.get("distance", 0)
                out.append(f"- {man}  ({d:.0f} m)")
        return "\n".join(out)

    async def tour(
        self,
        stops: list[str],
        profile: str = "driving",
        roundtrip: bool = True,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Optimize a multi-stop tour (TSP) via OSRM's /trip endpoint. The
        first stop is the start; with roundtrip=True the route returns
        to it.
        :param stops: List of place names or "lon,lat" pairs (3+).
        :param profile: driving / walking / cycling.
        :param roundtrip: When True, end at start.
        :return: Optimized order + total distance/duration.
        """
        if len(stops) < 3:
            return "tour requires at least 3 stops; use route() for A→B"
        prof = {"driving": "car", "walking": "foot", "cycling": "bike"}.get(profile, profile)
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT) as c:
            coords: list[tuple[float, float]] = []
            for s in stops:
                geo = await self._geocode(c, s)
                if not geo:
                    return f"could not geocode: {s}"
                coords.append(geo)
            coord_str = ";".join(f"{lon},{lat}" for lon, lat in coords)
            r = await c.get(
                f"{self.valves.ROUTING_BASE}/trip/v1/{prof}/{coord_str}",
                params={"source": "first",
                        "destination": "last" if not roundtrip else "any",
                        "roundtrip": str(roundtrip).lower(),
                        "overview": "false"},
                headers={"User-Agent": self.valves.UA},
            )
        if r.status_code != 200:
            return f"OSRM HTTP {r.status_code}"
        data = r.json() or {}
        trip = (data.get("trips") or [{}])[0]
        wp = data.get("waypoints") or []
        order = [(int(w["waypoint_index"]), int(w["trips_index"]) if "trips_index" in w else 0)
                 for w in wp]
        # Re-sort stops by trip order.
        ordered = sorted(zip(order, stops), key=lambda x: x[0][0])
        out = [
            f"# Optimized tour ({profile}, {'roundtrip' if roundtrip else 'one-way'})",
            f"distance: {trip.get('distance', 0)/1000:.1f} km   duration: {trip.get('duration', 0)/60:.0f} min",
            "",
            "order:",
        ]
        for i, (_, name) in enumerate(ordered):
            out.append(f"  {i+1}. {name}")
        return "\n".join(out)
