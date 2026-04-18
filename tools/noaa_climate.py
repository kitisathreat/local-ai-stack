"""
title: NOAA Climate Data — Historical Weather & Climate Records
author: local-ai-stack
description: Access NOAA's Climate Data Online (CDO) database — the world's largest archive of atmospheric, coastal, geophysical, and oceanic data. Retrieve historical temperature records, precipitation, snowfall, and extreme weather events from 100,000+ stations worldwide dating back to 1763. Free API token at ncei.noaa.gov/cdo-web/token.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from datetime import datetime, timedelta
from typing import Callable, Any, Optional
from pydantic import BaseModel, Field

BASE = "https://www.ncdc.noaa.gov/cdo-web/api/v2"

DATASETS = {
    "GHCND": "Global Historical Climatology Network Daily — daily temp, precip, snow",
    "GSOM": "Global Summary of the Month — monthly aggregated climate summaries",
    "GSOY": "Global Summary of the Year — annual climate summaries",
    "NORMAL_DLY": "U.S. Climate Normals Daily — 30-year averages (1991-2020)",
    "NORMAL_MLY": "U.S. Climate Normals Monthly — 30-year averages",
    "PRECIP_HLY": "Precipitation Hourly — hourly US precipitation data",
}

DATATYPES = {
    "TMAX": "Maximum Temperature (°C × 10)",
    "TMIN": "Minimum Temperature (°C × 10)",
    "TAVG": "Average Temperature (°C × 10)",
    "PRCP": "Precipitation (mm × 10)",
    "SNOW": "Snowfall (mm)",
    "SNWD": "Snow Depth (mm)",
    "AWND": "Average Wind Speed (m/s × 10)",
    "WT01": "Fog, Ice Fog, or Freezing Fog",
    "WT08": "Smoke or Haze",
    "WT16": "Rain (may include freezing rain, drizzle, and freezing drizzle)",
}

# Well-known station IDs
STATION_ALIASES = {
    "new_york": "GHCND:USW00094728",
    "los_angeles": "GHCND:USW00023174",
    "chicago": "GHCND:USW00094846",
    "london": "GHCND:UKW00003772",
    "paris": "GHCND:FRM00007156",
    "tokyo": "GHCND:JA000047662",
    "sydney": "GHCND:ASN00066062",
    "toronto": "GHCND:CA006158350",
    "miami": "GHCND:USW00092811",
    "seattle": "GHCND:USW00024233",
    "denver": "GHCND:USW00003017",
    "houston": "GHCND:USW00012960",
}


class Tools:
    class Valves(BaseModel):
        NOAA_TOKEN: str = Field(
            default="",
            description="NOAA CDO API token — free at https://www.ncdc.noaa.gov/cdo-web/token",
        )

    def __init__(self):
        self.valves = self.Valves()

    def _check_token(self) -> Optional[str]:
        if not self.valves.NOAA_TOKEN:
            return (
                "NOAA API token required.\n"
                "Request your free token at: https://www.ncdc.noaa.gov/cdo-web/token\n"
                "It will be emailed to you instantly.\n"
                "Add it in Open WebUI > Tools > NOAA Climate Data > NOAA_TOKEN"
            )
        return None

    def _headers(self) -> dict:
        return {"token": self.valves.NOAA_TOKEN}

    async def get_station_data(
        self,
        station: str,
        start_date: str = "",
        end_date: str = "",
        datatypes: str = "TMAX,TMIN,PRCP",
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get historical climate observations from a NOAA weather station.
        :param station: Station ID (e.g. 'GHCND:USW00094728') or alias ('new_york', 'london', 'tokyo', 'paris', 'sydney', 'chicago', 'miami', 'seattle', 'denver')
        :param start_date: Start date YYYY-MM-DD (default: 30 days ago)
        :param end_date: End date YYYY-MM-DD (default: today — max range: 1 year per query)
        :param datatypes: Comma-separated data types to retrieve: TMAX, TMIN, TAVG, PRCP, SNOW, SNWD, AWND
        :return: Climate observations table with converted units
        """
        err = self._check_token()
        if err:
            return err

        station_id = STATION_ALIASES.get(station.lower().replace(" ", "_"), station)

        if not start_date:
            start_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        if not end_date:
            end_date = datetime.now().strftime("%Y-%m-%d")

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Fetching climate data for {station}...", "done": False}})

        try:
            async with httpx.AsyncClient(timeout=20, headers=self._headers()) as client:
                resp = await client.get(
                    f"{BASE}/data",
                    params={
                        "datasetid": "GHCND",
                        "stationid": station_id,
                        "startdate": start_date,
                        "enddate": end_date,
                        "datatypeid": datatypes,
                        "limit": 1000,
                        "units": "metric",
                    },
                )
                if resp.status_code == 400:
                    return f"Invalid station ID or date range. Try an alias like 'new_york', 'london', or 'tokyo'."
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            return f"NOAA API error: {str(e)}"

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        results = data.get("results", [])
        if not results:
            return f"No data found for station {station_id} from {start_date} to {end_date}."

        # Pivot data by date
        by_date = {}
        for r in results:
            date = r["date"][:10]
            dtype = r["datatype"]
            val = r["value"]
            by_date.setdefault(date, {})[dtype] = val

        dtype_list = list({r["datatype"] for r in results})
        dtype_list_sorted = sorted(dtype_list)

        lines = [f"## NOAA Climate Data — Station: {station_id}\n"]
        lines.append(f"**Period:** {start_date} to {end_date} | **Dataset:** GHCND\n")

        # Unit conversions helper
        def fmt_val(dtype, raw_val):
            v = raw_val
            if dtype in ("TMAX", "TMIN", "TAVG"):
                return f"{v/10:.1f}°C"
            elif dtype in ("PRCP", "SNOW", "SNWD"):
                return f"{v/10:.1f}mm"
            elif dtype == "AWND":
                return f"{v/10:.1f}m/s"
            else:
                return str(v)

        header = "| Date | " + " | ".join(dtype_list_sorted) + " |"
        sep = "|------|" + "------|" * len(dtype_list_sorted)
        lines.append(header)
        lines.append(sep)

        for date in sorted(by_date.keys()):
            row = by_date[date]
            vals = " | ".join(fmt_val(dt, row[dt]) if dt in row else "—" for dt in dtype_list_sorted)
            lines.append(f"| {date} | {vals} |")

        # Monthly stats
        if len(by_date) >= 7:
            lines.append("\n### Summary Statistics\n")
            for dtype in dtype_list_sorted:
                all_vals = [by_date[d][dtype] / 10 for d in by_date if dtype in by_date[d]]
                if all_vals and dtype in ("TMAX", "TMIN", "TAVG"):
                    lines.append(f"**{dtype}:** Min={min(all_vals):.1f}°C, Max={max(all_vals):.1f}°C, Avg={sum(all_vals)/len(all_vals):.1f}°C")
                elif all_vals and dtype == "PRCP":
                    lines.append(f"**Precipitation Total:** {sum(all_vals):.1f}mm over {len(all_vals)} days")
                elif all_vals and dtype == "SNOW":
                    lines.append(f"**Snowfall Total:** {sum(all_vals):.1f}mm over {len(all_vals)} days")

        return "\n".join(lines)

    async def find_stations(
        self,
        location: str = "",
        country_code: str = "",
        limit: int = 15,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Find NOAA weather stations by location name or country code to get their station IDs.
        :param location: Location name to search (e.g. 'Boston', 'Berlin', 'Cape Town')
        :param country_code: ISO2 country code (e.g. 'US', 'DE', 'ZA', 'AU')
        :param limit: Maximum number of stations to return
        :return: Station IDs, names, locations, and data availability dates
        """
        err = self._check_token()
        if err:
            return err

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Finding stations near {location or country_code}...", "done": False}})

        params = {"datasetid": "GHCND", "limit": min(limit, 50)}
        if location:
            params["name"] = location
        if country_code:
            params["locationid"] = f"FIPS:{country_code.upper()}"

        try:
            async with httpx.AsyncClient(timeout=15, headers=self._headers()) as client:
                resp = await client.get(f"{BASE}/stations", params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            return f"NOAA station search error: {str(e)}"

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        stations = data.get("results", [])
        if not stations:
            return f"No stations found for '{location or country_code}'."

        lines = [f"## NOAA Stations: {location or country_code}\n"]
        lines.append("| Station ID | Name | Latitude | Longitude | Data From | Data To |")
        lines.append("|-----------|------|---------|-----------|----------|--------|")
        for s in stations:
            sid = s.get("id", "")
            name = s.get("name", "")[:40]
            lat = s.get("latitude", "")
            lon = s.get("longitude", "")
            mindate = s.get("mindate", "")
            maxdate = s.get("maxdate", "")
            lines.append(f"| `{sid}` | {name} | {lat} | {lon} | {mindate} | {maxdate} |")

        lines.append(f"\nUse any Station ID with `get_station_data(station='GHCND:...')`")
        return "\n".join(lines)

    async def get_climate_normals(
        self,
        station: str,
        month: int = 0,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get 30-year US climate normals (1991-2020) for a station — average temperature and precipitation by month.
        :param station: Station ID or alias (new_york, chicago, miami, seattle, denver, los_angeles, houston)
        :param month: Specific month number 1-12 (0 = all months)
        :return: Monthly averages for temperature and precipitation
        """
        err = self._check_token()
        if err:
            return err

        station_id = STATION_ALIASES.get(station.lower().replace(" ", "_"), station)

        params = {
            "datasetid": "NORMAL_MLY",
            "stationid": station_id,
            "datatypeid": "MLY-TMAX-NORMAL,MLY-TMIN-NORMAL,MLY-PRCP-NORMAL",
            "startdate": "2010-01-01",
            "enddate": "2010-12-31",
            "limit": 100,
            "units": "metric",
        }

        try:
            async with httpx.AsyncClient(timeout=15, headers=self._headers()) as client:
                resp = await client.get(f"{BASE}/data", params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            return f"Climate normals error: {str(e)}"

        results = data.get("results", [])
        if not results:
            return f"No climate normals data for {station_id}. Note: Normals are only available for US stations."

        by_month = {}
        for r in results:
            month_num = int(r["date"][5:7])
            dtype = r["datatype"]
            val = r["value"]
            by_month.setdefault(month_num, {})[dtype] = val

        MONTH_NAMES = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

        lines = [f"## 30-Year Climate Normals (1991-2020): {station_id}\n"]
        lines.append("| Month | Avg Max (°C) | Avg Min (°C) | Avg Precip (mm) |")
        lines.append("|-------|------------|------------|----------------|")

        months_to_show = [month] if month else range(1, 13)
        for m in months_to_show:
            if m in by_month:
                row = by_month[m]
                tmax = f"{row.get('MLY-TMAX-NORMAL', 0)/10:.1f}°C" if row.get("MLY-TMAX-NORMAL") else "—"
                tmin = f"{row.get('MLY-TMIN-NORMAL', 0)/10:.1f}°C" if row.get("MLY-TMIN-NORMAL") else "—"
                prcp = f"{row.get('MLY-PRCP-NORMAL', 0)/100:.1f}mm" if row.get("MLY-PRCP-NORMAL") else "—"
                lines.append(f"| {MONTH_NAMES[m]} | {tmax} | {tmin} | {prcp} |")

        lines.append("\nSource: NOAA 1991-2020 US Climate Normals")
        return "\n".join(lines)

    def list_station_aliases(self, __user__: Optional[dict] = None) -> str:
        """
        List pre-configured city station ID aliases for quick access.
        :return: City names and their NOAA station IDs
        """
        lines = ["## NOAA Station Aliases\n"]
        lines.append("| Alias | Station ID |")
        lines.append("|-------|-----------|")
        for alias, sid in STATION_ALIASES.items():
            lines.append(f"| `{alias}` | `{sid}` |")
        lines.append("\nUse any alias with `get_station_data(station='new_york')`")
        return "\n".join(lines)
