"""
title: USGS — Earthquakes, Geology & Water Resources
author: local-ai-stack
description: Real-time and historical data from the US Geological Survey. Track earthquakes worldwide (magnitude, depth, shake map), query stream gauges and water levels, access geological hazard data, and look up USGS scientific publications. No API key required.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from datetime import datetime, timedelta
from typing import Callable, Any, Optional
from pydantic import BaseModel, Field

EARTHQUAKE_BASE = "https://earthquake.usgs.gov/fdsnws/event/1"
WATER_BASE = "https://waterservices.usgs.gov/nwis"


class Tools:
    class Valves(BaseModel):
        pass

    def __init__(self):
        self.valves = self.Valves()

    async def get_earthquakes(
        self,
        min_magnitude: float = 4.0,
        start_date: str = "",
        end_date: str = "",
        location: str = "",
        radius_km: float = 0,
        lat: float = 0,
        lon: float = 0,
        limit: int = 25,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get recent earthquakes from the USGS real-time earthquake catalog. Filter by magnitude, date, and location.
        :param min_magnitude: Minimum earthquake magnitude (e.g. 4.0 for significant, 6.0 for major, 7.0 for great)
        :param start_date: Start date YYYY-MM-DD (default: 7 days ago)
        :param end_date: End date YYYY-MM-DD (default: now)
        :param location: Place name for context (not used for filtering — use lat/lon + radius instead)
        :param radius_km: If set, filter to events within this radius of lat/lon center
        :param lat: Latitude of center point for radius search (e.g. 35.68 for Tokyo, 34.05 for Los Angeles)
        :param lon: Longitude of center point for radius search (e.g. 139.69 for Tokyo, -118.24 for LA)
        :param limit: Maximum number of earthquakes to return
        :return: Earthquake list with magnitude, location, depth, coordinates, and time
        """
        if not start_date:
            start_date = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        if not end_date:
            end_date = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Fetching M{min_magnitude}+ earthquakes...", "done": False}})

        params = {
            "format": "geojson",
            "starttime": start_date,
            "endtime": end_date,
            "minmagnitude": min_magnitude,
            "limit": min(limit, 200),
            "orderby": "time",
        }
        if lat and lon and radius_km:
            params["latitude"] = lat
            params["longitude"] = lon
            params["maxradiuskm"] = radius_km

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(f"{EARTHQUAKE_BASE}/query", params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            return f"USGS earthquake error: {str(e)}"

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        features = data.get("features", [])
        meta = data.get("metadata", {})
        count = meta.get("count", len(features))

        geo_str = f" within {radius_km}km of ({lat},{lon})" if radius_km else ""
        lines = [f"## USGS Earthquakes M{min_magnitude}+{geo_str}\n"]
        lines.append(f"**Period:** {start_date} to {end_date}")
        lines.append(f"**Events found:** {count}\n")

        if not features:
            lines.append("No earthquakes found matching these criteria.")
            return "\n".join(lines)

        # Stats
        magnitudes = [f["properties"]["mag"] for f in features if f["properties"]["mag"]]
        if magnitudes:
            lines.append(f"**Largest:** M{max(magnitudes):.1f} | **Average:** M{sum(magnitudes)/len(magnitudes):.1f}\n")

        lines.append("| Time (UTC) | Magnitude | Location | Depth (km) | Coordinates |")
        lines.append("|------------|-----------|----------|-----------|------------|")

        for feat in features:
            props = feat.get("properties", {})
            geom = feat.get("geometry", {})
            coords = geom.get("coordinates", [None, None, None])

            mag = props.get("mag", 0) or 0
            place = props.get("place", "Unknown location")[:60]
            depth = coords[2] if coords[2] is not None else 0
            eq_lat = coords[1] if coords[1] is not None else 0
            eq_lon = coords[0] if coords[0] is not None else 0
            time_ms = props.get("time", 0)
            time_str = datetime.utcfromtimestamp(time_ms / 1000).strftime("%Y-%m-%d %H:%M") if time_ms else ""
            url = props.get("url", "")

            # Magnitude emoji
            mag_emoji = "🟥" if mag >= 7 else ("🟠" if mag >= 6 else ("🟡" if mag >= 5 else "⚪"))

            lines.append(
                f"| {time_str} | {mag_emoji} **{mag:.1f}** | {place} | {depth:.1f} | {eq_lat:.3f}, {eq_lon:.3f} |"
            )

        # Detail for largest
        if features:
            largest = max(features, key=lambda f: f["properties"].get("mag", 0) or 0)
            props = largest.get("properties", {})
            url = props.get("url", "")
            if url:
                lines.append(f"\n**Largest event details:** {url}")

        return "\n".join(lines)

    async def get_water_level(
        self,
        site_number: str = "",
        state: str = "",
        parameter: str = "00060",
        hours: int = 24,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get real-time streamflow or water level data from USGS stream gauges.
        :param site_number: USGS site number (8-digit ID, e.g. '01646500' for Potomac at DC, '11447650' for Sacramento River)
        :param state: US state 2-letter code to list top gauges (e.g. 'CA', 'TX', 'FL') — use instead of site_number
        :param parameter: Data parameter code ('00060' = streamflow in cfs, '00065' = gage height in feet)
        :param hours: Hours of data to retrieve (1-168, default 24)
        :return: Water level/flow measurements with timestamps and trend
        """
        if not site_number and not state:
            return "Provide either a site_number (8-digit USGS gauge ID) or a state code (e.g. 'CA')."

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Fetching USGS water data...", "done": False}})

        hours = max(1, min(168, hours))
        end_time = datetime.now()
        start_time = end_time - timedelta(hours=hours)

        params = {
            "format": "json",
            "parameterCd": parameter,
            "startDT": start_time.strftime("%Y-%m-%dT%H:%M"),
            "endDT": end_time.strftime("%Y-%m-%dT%H:%M"),
        }
        if site_number:
            params["sites"] = site_number
        elif state:
            params["stateCd"] = state.upper()
            params["siteType"] = "ST"  # streams only
            params["siteStatus"] = "active"

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(f"{WATER_BASE}/iv/", params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            return f"USGS water data error: {str(e)}"

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        time_series_list = data.get("value", {}).get("timeSeries", [])
        if not time_series_list:
            return f"No water data found. Check the site number or state code."

        param_names = {"00060": "Streamflow (cfs)", "00065": "Gage Height (ft)"}
        param_name = param_names.get(parameter, f"Parameter {parameter}")

        lines = [f"## USGS Water Data — {param_name}\n"]

        for ts in time_series_list[:5]:  # Show up to 5 gauges
            site_name = ts.get("sourceInfo", {}).get("siteName", "Unknown")
            site_code = ts.get("sourceInfo", {}).get("siteCode", [{}])[0].get("value", "")
            values_list = ts.get("values", [{}])[0].get("value", [])

            valid_values = [(v["dateTime"], float(v["value"])) for v in values_list if v.get("value") and v["value"] != "-999999"]

            lines.append(f"### {site_name} (Site: {site_code})")

            if not valid_values:
                lines.append("No valid readings.\n")
                continue

            lines.append(f"**Period:** {hours} hours | **Readings:** {len(valid_values)}")

            vals = [v for _, v in valid_values]
            latest_ts, latest_val = valid_values[-1]
            lines.append(f"**Current:** {latest_val:,.1f} | **Peak:** {max(vals):,.1f} | **Min:** {min(vals):,.1f}")

            # Show sample (every 4th reading to fit)
            sample = valid_values[::max(1, len(valid_values)//12)]
            lines.append("\n| Time (UTC) | Value |")
            lines.append("|------------|-------|")
            for ts_str, val in sample:
                time_fmt = ts_str[:16].replace("T", " ")
                lines.append(f"| {time_fmt} | {val:,.1f} |")
            lines.append("")

        return "\n".join(lines)

    async def get_earthquake_history(
        self,
        lat: float,
        lon: float,
        radius_km: float = 100,
        min_magnitude: float = 3.0,
        years: int = 10,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get historical earthquake statistics for a region to assess seismic hazard.
        :param lat: Latitude of the region center (e.g. 37.77 for San Francisco, 35.68 for Tokyo)
        :param lon: Longitude of the region center (e.g. -122.42 for San Francisco, 139.69 for Tokyo)
        :param radius_km: Radius of region in km (default 100)
        :param min_magnitude: Minimum magnitude to include (default 3.0)
        :param years: Years of history to analyze (1-100, default 10)
        :return: Earthquake frequency, magnitude distribution, and largest events
        """
        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Analyzing seismic history near ({lat}, {lon})...", "done": False}})

        end_date = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=365 * min(years, 50))).strftime("%Y-%m-%d")

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    f"{EARTHQUAKE_BASE}/query",
                    params={
                        "format": "geojson",
                        "starttime": start_date,
                        "endtime": end_date,
                        "latitude": lat,
                        "longitude": lon,
                        "maxradiuskm": radius_km,
                        "minmagnitude": min_magnitude,
                        "orderby": "magnitude",
                        "limit": 500,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            return f"USGS history error: {str(e)}"

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        features = data.get("features", [])
        total = data.get("metadata", {}).get("count", len(features))

        lines = [f"## Seismic History: {radius_km}km radius of ({lat}, {lon})\n"]
        lines.append(f"**Period:** {start_date} to {end_date} ({years} years)")
        lines.append(f"**Total M{min_magnitude}+ events:** {total}\n")

        if not features:
            lines.append("No significant earthquakes recorded in this area.")
            return "\n".join(lines)

        magnitudes = [f["properties"]["mag"] for f in features if f["properties"]["mag"]]

        # Magnitude distribution
        bins = [(7, 10, "M7+"), (6, 7, "M6–7"), (5, 6, "M5–6"), (4, 5, "M4–5"), (3, 4, "M3–4"), (0, 3, "M<3")]
        lines.append("### Magnitude Distribution\n")
        lines.append("| Range | Count | Rate/Year |")
        lines.append("|-------|-------|-----------|")
        for lo, hi, label in bins:
            count = sum(1 for m in magnitudes if lo <= m < hi)
            rate = count / years
            lines.append(f"| {label} | {count} | {rate:.1f}/yr |")

        # Largest events
        lines.append("\n### Top 10 Largest Events\n")
        lines.append("| Magnitude | Time | Location | Depth (km) |")
        lines.append("|-----------|------|----------|-----------|")
        for feat in features[:10]:
            props = feat["properties"]
            coords = feat.get("geometry", {}).get("coordinates", [0, 0, 0])
            mag = props.get("mag", 0)
            place = (props.get("place") or "Unknown")[:50]
            depth = coords[2] if len(coords) > 2 else 0
            time_ms = props.get("time", 0)
            time_str = datetime.utcfromtimestamp(time_ms / 1000).strftime("%Y-%m-%d") if time_ms else ""
            lines.append(f"| **M{mag:.1f}** | {time_str} | {place} | {depth:.1f} |")

        avg_rate = total / years
        lines.append(f"\n**Annual seismicity rate:** {avg_rate:.1f} events/year (M{min_magnitude}+)")

        return "\n".join(lines)

    async def get_earthquake_count(
        self,
        start_date: str = "",
        end_date: str = "",
        min_magnitude: float = 5.0,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get the total count of earthquakes globally in a date range at a given minimum magnitude.
        :param start_date: Start date YYYY-MM-DD (default: 30 days ago)
        :param end_date: End date YYYY-MM-DD (default: today)
        :param min_magnitude: Minimum magnitude threshold
        :return: Global earthquake count and summary statistics
        """
        if not start_date:
            start_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        if not end_date:
            end_date = datetime.now().strftime("%Y-%m-%d")

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{EARTHQUAKE_BASE}/count",
                    params={"format": "geojson", "starttime": start_date, "endtime": end_date, "minmagnitude": min_magnitude},
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            return f"USGS count error: {str(e)}"

        count = data.get("count", 0)
        days = (datetime.strptime(end_date, "%Y-%m-%d") - datetime.strptime(start_date, "%Y-%m-%d")).days or 1
        rate = count / days

        return (
            f"## Global Earthquake Count: M{min_magnitude}+\n\n"
            f"**Period:** {start_date} to {end_date} ({days} days)\n"
            f"**Total earthquakes:** {count:,}\n"
            f"**Daily rate:** {rate:.1f} earthquakes/day\n\n"
            f"Source: USGS Earthquake Hazards Program (earthquake.usgs.gov)"
        )
