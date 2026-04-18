"""
title: Weather (Open-Meteo)
author: local-ai-stack
description: Get real-time weather and 7-day forecasts using the free Open-Meteo API. No API key required.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from pydantic import BaseModel, Field
from typing import Callable, Any, Optional


class Tools:
    class Valves(BaseModel):
        GEOCODING_URL: str = Field(
            default="https://geocoding-api.open-meteo.com/v1/search",
            description="Open-Meteo geocoding API URL",
        )
        WEATHER_URL: str = Field(
            default="https://api.open-meteo.com/v1/forecast",
            description="Open-Meteo forecast API URL",
        )
        UNITS: str = Field(
            default="fahrenheit",
            description="Temperature units: 'celsius' or 'fahrenheit'",
        )

    def __init__(self):
        self.valves = self.Valves()

    async def _geocode(self, location: str) -> Optional[dict]:
        params = {"name": location, "count": 1, "language": "en", "format": "json"}
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(self.valves.GEOCODING_URL, params=params)
            data = resp.json()
        results = data.get("results", [])
        return results[0] if results else None

    def _wmo_description(self, code: int) -> str:
        codes = {
            0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
            45: "Foggy", 48: "Icy fog", 51: "Light drizzle", 53: "Drizzle",
            55: "Heavy drizzle", 61: "Light rain", 63: "Rain", 65: "Heavy rain",
            71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
            80: "Light showers", 81: "Showers", 82: "Heavy showers",
            85: "Snow showers", 86: "Heavy snow showers",
            95: "Thunderstorm", 96: "Thunderstorm with hail", 99: "Heavy thunderstorm",
        }
        return codes.get(code, f"Weather code {code}")

    async def get_current_weather(
        self,
        location: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get the current weather conditions for any city or location.
        :param location: City name or location (e.g. "New York", "London UK", "Tokyo")
        :return: Current temperature, conditions, humidity, wind speed, and UV index
        """
        if __event_emitter__:
            await __event_emitter__(
                {"type": "status", "data": {"description": f"Fetching weather for {location}", "done": False}}
            )

        geo = await self._geocode(location)
        if not geo:
            return f"Location not found: {location}"

        lat, lon = geo["latitude"], geo["longitude"]
        name = geo.get("name", location)
        country = geo.get("country", "")

        params = {
            "latitude": lat, "longitude": lon,
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,wind_speed_10m,weather_code,uv_index,precipitation",
            "temperature_unit": self.valves.UNITS,
            "wind_speed_unit": "mph",
            "timezone": "auto",
        }

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(self.valves.WEATHER_URL, params=params)
            data = resp.json()

        c = data.get("current", {})
        unit = "°F" if self.valves.UNITS == "fahrenheit" else "°C"
        condition = self._wmo_description(c.get("weather_code", 0))

        if __event_emitter__:
            await __event_emitter__(
                {"type": "status", "data": {"description": "Weather retrieved", "done": True}}
            )

        return (
            f"## Current Weather: {name}, {country}\n"
            f"- **Condition:** {condition}\n"
            f"- **Temperature:** {c.get('temperature_2m')}{unit} "
            f"(feels like {c.get('apparent_temperature')}{unit})\n"
            f"- **Humidity:** {c.get('relative_humidity_2m')}%\n"
            f"- **Wind:** {c.get('wind_speed_10m')} mph\n"
            f"- **UV Index:** {c.get('uv_index', 'N/A')}\n"
            f"- **Precipitation:** {c.get('precipitation', 0)} mm"
        )

    async def get_forecast(
        self,
        location: str,
        days: int = 7,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get a multi-day weather forecast for any city.
        :param location: City name or location
        :param days: Number of forecast days (1–16, default 7)
        :return: Daily forecast with high/low temps and conditions
        """
        days = max(1, min(16, days))

        geo = await self._geocode(location)
        if not geo:
            return f"Location not found: {location}"

        lat, lon = geo["latitude"], geo["longitude"]
        name = geo.get("name", location)
        country = geo.get("country", "")

        params = {
            "latitude": lat, "longitude": lon,
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,wind_speed_10m_max",
            "temperature_unit": self.valves.UNITS,
            "wind_speed_unit": "mph",
            "timezone": "auto",
            "forecast_days": days,
        }

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(self.valves.WEATHER_URL, params=params)
            data = resp.json()

        daily = data.get("daily", {})
        dates = daily.get("time", [])
        codes = daily.get("weather_code", [])
        highs = daily.get("temperature_2m_max", [])
        lows = daily.get("temperature_2m_min", [])
        precip = daily.get("precipitation_sum", [])
        unit = "°F" if self.valves.UNITS == "fahrenheit" else "°C"

        lines = [f"## {days}-Day Forecast: {name}, {country}\n"]
        for i in range(len(dates)):
            cond = self._wmo_description(codes[i] if i < len(codes) else 0)
            hi = highs[i] if i < len(highs) else "?"
            lo = lows[i] if i < len(lows) else "?"
            rain = precip[i] if i < len(precip) else 0
            lines.append(
                f"**{dates[i]}** — {cond} | High: {hi}{unit} / Low: {lo}{unit} | Rain: {rain}mm"
            )

        return "\n".join(lines)
