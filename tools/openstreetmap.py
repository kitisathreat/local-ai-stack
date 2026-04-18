"""
title: OpenStreetMap — Geocoding, Reverse Geocoding & POI Search
author: local-ai-stack
description: Free geocoding via Nominatim (OSM). Convert addresses to coordinates, reverse-geocode lat/lng to places, and search for points of interest. No API key required.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from pydantic import BaseModel, Field
from typing import Optional


NOMINATIM = "https://nominatim.openstreetmap.org"
OVERPASS = "https://overpass-api.de/api/interpreter"
UA = "local-ai-stack/1.0 (openstreetmap tool; contact: admin@localhost)"


class Tools:
    class Valves(BaseModel):
        MAX_RESULTS: int = Field(default=5, description="Max geocoding results")
        EMAIL: str = Field(default="", description="Optional email for Nominatim usage tracking")

    def __init__(self):
        self.valves = self.Valves()

    def _headers(self) -> dict:
        h = {"User-Agent": UA, "Accept-Language": "en"}
        return h

    async def geocode(
        self,
        query: str,
        country: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Convert an address or place name to latitude/longitude.
        :param query: Address, landmark or place (e.g. "1600 Pennsylvania Ave, DC", "Mount Fuji")
        :param country: Optional ISO country code filter (e.g. "US", "JP")
        :return: Coordinates, display name, OSM type/ID, and bounding box
        """
        params = {"q": query, "format": "jsonv2", "limit": self.valves.MAX_RESULTS, "addressdetails": 1}
        if country:
            params["countrycodes"] = country.lower()
        if self.valves.EMAIL:
            params["email"] = self.valves.EMAIL
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(f"{NOMINATIM}/search", params=params, headers=self._headers())
                r.raise_for_status()
                results = r.json()
            if not results:
                return f"No geocoding match for: {query}"
            lines = [f"## Geocoding: {query}\n"]
            for res in results:
                lat = res.get("lat")
                lon = res.get("lon")
                name = res.get("display_name", "")
                cls = res.get("class", "")
                typ = res.get("type", "")
                osm_id = f"{res.get('osm_type','')} {res.get('osm_id','')}"
                lines.append(f"**{name}**")
                lines.append(f"   {lat}, {lon}   [{cls}/{typ}]   ({osm_id})")
                lines.append(f"   🔗 https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=15/{lat}/{lon}\n")
            return "\n".join(lines)
        except Exception as e:
            return f"Nominatim error: {e}"

    async def reverse(
        self,
        lat: float,
        lon: float,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Reverse-geocode coordinates to an address.
        :param lat: Latitude in decimal degrees
        :param lon: Longitude in decimal degrees
        :return: Nearest address with country, state, city, road, etc.
        """
        params = {"lat": lat, "lon": lon, "format": "jsonv2", "addressdetails": 1, "zoom": 18}
        if self.valves.EMAIL:
            params["email"] = self.valves.EMAIL
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(f"{NOMINATIM}/reverse", params=params, headers=self._headers())
                r.raise_for_status()
                data = r.json()
            if "error" in data:
                return f"Reverse geocoding failed: {data['error']}"
            name = data.get("display_name", "")
            addr = data.get("address", {})
            lines = [f"## Reverse: {lat}, {lon}\n", name]
            for k in ["road", "suburb", "city", "town", "village", "state", "postcode", "country"]:
                if addr.get(k):
                    lines.append(f"**{k.title()}:** {addr[k]}")
            return "\n".join(lines)
        except Exception as e:
            return f"Nominatim error: {e}"

    async def nearby_pois(
        self,
        lat: float,
        lon: float,
        amenity: str = "restaurant",
        radius_m: int = 500,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        List OpenStreetMap points of interest near a coordinate via Overpass API.
        :param lat: Latitude
        :param lon: Longitude
        :param amenity: OSM amenity tag (restaurant, cafe, hospital, school, pharmacy, bank, atm, fuel, ...)
        :param radius_m: Search radius in meters (default 500)
        :return: Nearby POIs with names and distance
        """
        query = f"""
        [out:json][timeout:25];
        (
          node["amenity"="{amenity}"](around:{radius_m},{lat},{lon});
          way["amenity"="{amenity}"](around:{radius_m},{lat},{lon});
        );
        out center tags 25;
        """
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(OVERPASS, data={"data": query}, headers={"User-Agent": UA})
                r.raise_for_status()
                data = r.json()
            els = data.get("elements", [])
            if not els:
                return f"No {amenity} within {radius_m} m of {lat},{lon}"
            lines = [f"## {amenity.title()} within {radius_m} m of {lat},{lon}\n"]
            for e in els[:25]:
                tags = e.get("tags", {})
                name = tags.get("name", "(unnamed)")
                addr_parts = [tags.get(k, "") for k in ["addr:housenumber", "addr:street", "addr:city"] if tags.get(k)]
                addr = " ".join(addr_parts)
                el_lat = e.get("lat") or e.get("center", {}).get("lat")
                el_lon = e.get("lon") or e.get("center", {}).get("lon")
                lines.append(f"- **{name}**" + (f" — {addr}" if addr else "") + (f"  ({el_lat},{el_lon})" if el_lat else ""))
            return "\n".join(lines)
        except Exception as e:
            return f"Overpass error: {e}"
