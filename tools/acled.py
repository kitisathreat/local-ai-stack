"""
title: ACLED — Armed Conflict Location & Event Data
author: local-ai-stack
description: Access the Armed Conflict Location & Event Data (ACLED) database — the world's most comprehensive real-time dataset on political violence and protest events. Covers 200+ countries and territories since 1997. Analyze conflict trends, event types (battles, explosions, protests, riots), actor groups, fatalities, and geographic distribution. Free API key at acleddata.com.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from datetime import datetime, timedelta
from typing import Callable, Any, Optional
from pydantic import BaseModel, Field

BASE = "https://api.acleddata.com/acled/read"

EVENT_TYPES = [
    "Battles", "Violence against civilians", "Explosions/Remote violence",
    "Protests", "Riots", "Strategic developments",
]

REGIONS = {
    "western_africa": 1, "middle_africa": 2, "eastern_africa": 3,
    "southern_africa": 4, "northern_africa": 5, "south_asia": 7,
    "southeast_asia": 9, "middle_east": 11, "europe": 12,
    "caucasus_central_asia": 13, "east_asia": 14, "north_america": 15,
    "south_america": 16, "central_america_caribbean": 17, "oceania": 18,
}


class Tools:
    class Valves(BaseModel):
        ACLED_API_KEY: str = Field(
            default="",
            description="ACLED API key — free at https://developer.acleddata.com",
        )
        ACLED_EMAIL: str = Field(
            default="",
            description="Email address registered with ACLED (required alongside API key)",
        )

    def __init__(self):
        self.valves = self.Valves()

    def _check_credentials(self) -> Optional[str]:
        if not self.valves.ACLED_API_KEY or not self.valves.ACLED_EMAIL:
            return (
                "ACLED API key and email required.\n"
                "1. Register free at: https://developer.acleddata.com\n"
                "2. Add your key in Open WebUI > Tools > ACLED > ACLED_API_KEY\n"
                "3. Add your email in Open WebUI > Tools > ACLED > ACLED_EMAIL"
            )
        return None

    def _base_params(self) -> dict:
        return {
            "key": self.valves.ACLED_API_KEY,
            "email": self.valves.ACLED_EMAIL,
        }

    async def search_events(
        self,
        country: str = "",
        event_type: str = "",
        start_date: str = "",
        end_date: str = "",
        actor: str = "",
        limit: int = 25,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search for conflict events (battles, protests, explosions, etc.) by country, actor, type, and date range.
        :param country: Country name (e.g. 'Ukraine', 'Sudan', 'Myanmar', 'Mexico') — leave blank for global
        :param event_type: Filter by type: 'Battles', 'Protests', 'Riots', 'Explosions/Remote violence', 'Violence against civilians', 'Strategic developments'
        :param start_date: Start date YYYY-MM-DD (default: 30 days ago)
        :param end_date: End date YYYY-MM-DD (default: today)
        :param actor: Filter by actor name (e.g. 'ISIS', 'Wagner Group', 'military forces')
        :param limit: Number of events to return (max 50)
        :return: Event list with location, type, actors, fatalities, and notes
        """
        err = self._check_credentials()
        if err:
            return err

        if not start_date:
            start_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        if not end_date:
            end_date = datetime.now().strftime("%Y-%m-%d")

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Querying ACLED conflict data...", "done": False}})

        params = {
            **self._base_params(),
            "event_date": f"{start_date}|{end_date}",
            "event_date_where": "BETWEEN",
            "limit": min(limit, 50),
            "fields": "event_date|country|admin1|location|event_type|sub_event_type|actor1|actor2|fatalities|notes|source",
            "order": "event_date",
        }
        if country:
            params["country"] = country
        if event_type:
            params["event_type"] = event_type
        if actor:
            params["actor1"] = actor

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(BASE, params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            return f"ACLED API error: {str(e)}"

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        if not data.get("success"):
            msg = data.get("message", "Unknown error")
            return f"ACLED error: {msg}"

        events = data.get("data", [])
        count = data.get("count", len(events))

        geo_filter = f" in {country}" if country else ""
        type_filter = f" ({event_type})" if event_type else ""
        lines = [f"## ACLED Conflict Events{geo_filter}{type_filter}\n"]
        lines.append(f"**Period:** {start_date} to {end_date} | **Results:** {count} events shown\n")

        if not events:
            lines.append("No events found for these filters.")
            return "\n".join(lines)

        total_fatalities = sum(int(e.get("fatalities", 0) or 0) for e in events)
        lines.append(f"**Total Fatalities in Results:** {total_fatalities:,}\n")

        # Event type breakdown
        type_counts = {}
        for e in events:
            et = e.get("event_type", "Unknown")
            type_counts[et] = type_counts.get(et, 0) + 1

        lines.append("**Event Types:**")
        for et, cnt in sorted(type_counts.items(), key=lambda x: -x[1]):
            lines.append(f"- {et}: {cnt}")
        lines.append("")

        lines.append("| Date | Country | Location | Event Type | Actor 1 | Fatalities |")
        lines.append("|------|---------|----------|-----------|---------|-----------|")
        for e in events:
            date = e.get("event_date", "")
            ctry = e.get("country", "")
            loc = e.get("location", "")
            admin = e.get("admin1", "")
            etype = e.get("event_type", "")
            actor1 = (e.get("actor1") or "Unknown")[:30]
            fatalities = e.get("fatalities", 0) or 0
            location_full = f"{loc}, {admin}" if admin else loc
            lines.append(f"| {date} | {ctry} | {location_full} | {etype} | {actor1} | {fatalities} |")

        # Show notes for top 3 deadliest events
        deadliest = sorted(events, key=lambda x: int(x.get("fatalities", 0) or 0), reverse=True)[:3]
        if any(int(e.get("fatalities", 0) or 0) > 0 for e in deadliest):
            lines.append("\n### Deadliest Events\n")
            for e in deadliest:
                if int(e.get("fatalities", 0) or 0) > 0:
                    lines.append(f"**{e.get('event_date')} — {e.get('location')}, {e.get('country')}** ({e.get('fatalities')} fatalities)")
                    notes = (e.get("notes") or "")[:200]
                    if notes:
                        lines.append(f"_{notes}_")
                    lines.append("")

        return "\n".join(lines)

    async def get_conflict_summary(
        self,
        country: str,
        year: int = 0,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get a statistical summary of conflict activity in a country: total events, fatalities, and breakdown by event type.
        :param country: Country name (e.g. 'Nigeria', 'Ukraine', 'Colombia', 'Ethiopia')
        :param year: Year to analyze (e.g. 2023, 2022 — default: current year)
        :return: Event and fatality totals by type, top locations, and key actors
        """
        err = self._check_credentials()
        if err:
            return err

        if not year:
            year = datetime.now().year
        start = f"{year}-01-01"
        end = f"{year}-12-31"

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Analyzing {country} conflict data for {year}...", "done": False}})

        params = {
            **self._base_params(),
            "country": country,
            "event_date": f"{start}|{end}",
            "event_date_where": "BETWEEN",
            "limit": 500,
            "fields": "event_date|event_type|fatalities|location|admin1|actor1",
        }

        try:
            async with httpx.AsyncClient(timeout=25) as client:
                resp = await client.get(BASE, params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            return f"ACLED error: {str(e)}"

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        events = data.get("data", [])
        if not events:
            return f"No ACLED data found for {country} in {year}."

        total_events = len(events)
        total_fatalities = sum(int(e.get("fatalities", 0) or 0) for e in events)

        # By type
        type_stats = {}
        for e in events:
            et = e.get("event_type", "Unknown")
            fat = int(e.get("fatalities", 0) or 0)
            if et not in type_stats:
                type_stats[et] = {"count": 0, "fatalities": 0}
            type_stats[et]["count"] += 1
            type_stats[et]["fatalities"] += fat

        # Top locations
        loc_counts = {}
        for e in events:
            loc = e.get("location", "Unknown")
            fat = int(e.get("fatalities", 0) or 0)
            loc_counts[loc] = loc_counts.get(loc, 0) + fat
        top_locs = sorted(loc_counts.items(), key=lambda x: -x[1])[:10]

        # Top actors
        actor_counts = {}
        for e in events:
            a = e.get("actor1", "Unknown") or "Unknown"
            actor_counts[a] = actor_counts.get(a, 0) + 1
        top_actors = sorted(actor_counts.items(), key=lambda x: -x[1])[:10]

        lines = [f"## ACLED Conflict Summary: {country} ({year})\n"]
        lines.append(f"**Total Events:** {total_events:,} | **Total Fatalities:** {total_fatalities:,}\n")

        lines.append("### Events & Fatalities by Type\n")
        lines.append("| Event Type | Events | Fatalities | Fatality Rate |")
        lines.append("|-----------|--------|-----------|--------------|")
        for et, stats in sorted(type_stats.items(), key=lambda x: -x[1]["count"]):
            rate = stats["fatalities"] / stats["count"] if stats["count"] > 0 else 0
            lines.append(f"| {et} | {stats['count']:,} | {stats['fatalities']:,} | {rate:.2f}/event |")

        lines.append("\n### Top Locations by Fatalities\n")
        lines.append("| Location | Fatalities |")
        lines.append("|----------|-----------|")
        for loc, fat in top_locs:
            lines.append(f"| {loc} | {fat:,} |")

        lines.append("\n### Most Active Actors\n")
        lines.append("| Actor | Events Involved |")
        lines.append("|-------|----------------|")
        for actor, cnt in top_actors:
            lines.append(f"| {actor[:50]} | {cnt:,} |")

        return "\n".join(lines)

    def list_event_types(self, __user__: Optional[dict] = None) -> str:
        """
        List all ACLED event types and sub-event types with descriptions.
        :return: Categorized list of conflict event types
        """
        categories = {
            "Battles": ["Armed clash", "Government regains territory", "Non-state actor overtakes territory"],
            "Explosions/Remote violence": ["Air/drone strike", "Suicide bomb", "Shelling/artillery/missile attack", "Remote explosive/landmine/IED", "Grenade", "Chemical weapon", "Car bomb"],
            "Violence against civilians": ["Attack", "Sexual violence", "Abduction/forced disappearance", "Torture"],
            "Protests": ["Peaceful protest", "Protest with intervention", "Excessive force against protesters"],
            "Riots": ["Mob violence", "Violent demonstration"],
            "Strategic developments": ["Agreement", "Arrests", "Change to group/activity", "Disrupted weapons use", "Headquarters/base established", "Looting/property destruction", "Non-violent transfer of territory"],
        }
        lines = ["## ACLED Event Types\n"]
        for etype, subtypes in categories.items():
            lines.append(f"**{etype}:**")
            for st in subtypes:
                lines.append(f"  - {st}")
            lines.append("")
        return "\n".join(lines)
