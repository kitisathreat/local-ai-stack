"""
title: Google Calendar — Events, Calendars, Free/Busy
author: local-ai-stack
description: List calendars, list / search / create / update / delete events, and run free/busy queries via the Google Calendar API. OAuth refresh-token pattern shared with the gmail tool.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Optional

from pydantic import BaseModel, Field

from ._google_oauth import GoogleAuth, google_request


_API = "https://www.googleapis.com/calendar/v3"


class Tools:
    class Valves(BaseModel):
        CLIENT_ID: str = Field(default="")
        CLIENT_SECRET: str = Field(default="")
        REFRESH_TOKEN: str = Field(
            default="",
            description=(
                "Refresh token minted with scope "
                "https://www.googleapis.com/auth/calendar (or .events / .readonly "
                "depending on what you need)."
            ),
        )
        DEFAULT_CALENDAR: str = Field(
            default="primary",
            description="Calendar id used when callers omit it. 'primary' = the user's main calendar.",
        )
        DEFAULT_TIMEZONE: str = Field(
            default="UTC",
            description="IANA timezone for naive datetimes (e.g. 'America/New_York').",
        )

    def __init__(self) -> None:
        self.valves = self.Valves()
        self._auth: GoogleAuth | None = None

    def _ensure_auth(self) -> GoogleAuth:
        if self._auth is None or self._auth.refresh_token != self.valves.REFRESH_TOKEN:
            self._auth = GoogleAuth(
                client_id=self.valves.CLIENT_ID,
                client_secret=self.valves.CLIENT_SECRET,
                refresh_token=self.valves.REFRESH_TOKEN,
            )
        return self._auth

    async def _request(self, method: str, path: str, **kw: Any) -> dict:
        r = await google_request(self._ensure_auth(), method, f"{_API}{path}", **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"Calendar {method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}

    async def list_calendars(self) -> str:
        """List the calendars the user has access to."""
        body = await self._request("GET", "/users/me/calendarList")
        items = body.get("items", [])
        if not items:
            return "No calendars."
        return "\n".join(
            f"- {c.get('id')}  {c.get('summary')}  primary={bool(c.get('primary'))}"
            for c in items
        )

    async def list_events(
        self,
        calendar_id: str = "",
        time_min: str = "",
        time_max: str = "",
        query: str = "",
        max_results: int = 25,
    ) -> str:
        """List events in a window, optionally filtered by free-text.

        :param calendar_id: Calendar id; defaults to DEFAULT_CALENDAR.
        :param time_min: ISO datetime lower bound (defaults to "now").
        :param time_max: ISO datetime upper bound (defaults to "+7d").
        :param query: Optional free-text q=.
        :param max_results: 1-250.
        """
        cid = calendar_id or self.valves.DEFAULT_CALENDAR
        params: dict[str, Any] = {
            "timeMin": time_min or _now_iso(),
            "timeMax": time_max or _plus_days_iso(7),
            "maxResults": min(max(int(max_results), 1), 250),
            "singleEvents": "true",
            "orderBy": "startTime",
        }
        if query: params["q"] = query
        body = await self._request("GET", f"/calendars/{cid}/events", params=params)
        events = body.get("items", [])
        if not events:
            return "No events in window."
        return "\n".join(_format_event_line(e) for e in events)

    async def get_event(self, event_id: str, calendar_id: str = "") -> str:
        """Fetch one event with full detail.

        :param event_id: Event id.
        :param calendar_id: Defaults to DEFAULT_CALENDAR.
        """
        cid = calendar_id or self.valves.DEFAULT_CALENDAR
        body = await self._request("GET", f"/calendars/{cid}/events/{event_id}")
        return _format_event_full(body)

    async def create_event(
        self,
        summary: str,
        start: str,
        end: str,
        calendar_id: str = "",
        description: str = "",
        location: str = "",
        attendees: list[str] = None,
        timezone: str = "",
    ) -> str:
        """Create an event.

        :param summary: Event title.
        :param start: ISO datetime (e.g. "2026-05-04T15:00:00").
        :param end: ISO datetime.
        :param calendar_id: Defaults to DEFAULT_CALENDAR.
        :param description: Optional body.
        :param location: Optional location.
        :param attendees: List of email addresses.
        :param timezone: IANA tz; defaults to DEFAULT_TIMEZONE.
        """
        cid = calendar_id or self.valves.DEFAULT_CALENDAR
        tz = timezone or self.valves.DEFAULT_TIMEZONE
        payload: dict[str, Any] = {
            "summary": summary,
            "description": description,
            "location": location,
            "start": {"dateTime": start, "timeZone": tz},
            "end": {"dateTime": end, "timeZone": tz},
        }
        if attendees:
            payload["attendees"] = [{"email": a} for a in attendees]
        body = await self._request("POST", f"/calendars/{cid}/events", json_body=payload)
        return f"Created event {body.get('id')}  {body.get('htmlLink','')}"

    async def update_event(
        self,
        event_id: str,
        calendar_id: str = "",
        summary: str = "",
        start: str = "",
        end: str = "",
        description: str = "",
        location: str = "",
        timezone: str = "",
    ) -> str:
        """Patch-update an event. Only non-empty fields are written.

        :param event_id: Event id.
        :param calendar_id: Defaults to DEFAULT_CALENDAR.
        :param summary: New title.
        :param start: New ISO datetime.
        :param end: New ISO datetime.
        :param description: New body.
        :param location: New location.
        :param timezone: IANA tz for start/end.
        """
        cid = calendar_id or self.valves.DEFAULT_CALENDAR
        tz = timezone or self.valves.DEFAULT_TIMEZONE
        patch: dict[str, Any] = {}
        if summary: patch["summary"] = summary
        if description: patch["description"] = description
        if location: patch["location"] = location
        if start: patch["start"] = {"dateTime": start, "timeZone": tz}
        if end: patch["end"] = {"dateTime": end, "timeZone": tz}
        if not patch:
            return "Nothing to update."
        body = await self._request("PATCH", f"/calendars/{cid}/events/{event_id}", json_body=patch)
        return f"Updated event {body.get('id')}  {body.get('htmlLink','')}"

    async def delete_event(self, event_id: str, calendar_id: str = "") -> str:
        """Delete an event.

        :param event_id: Event id.
        :param calendar_id: Defaults to DEFAULT_CALENDAR.
        """
        cid = calendar_id or self.valves.DEFAULT_CALENDAR
        await self._request("DELETE", f"/calendars/{cid}/events/{event_id}")
        return f"Deleted event {event_id} from {cid}."

    async def free_busy(
        self,
        calendar_ids: list[str] = None,
        time_min: str = "",
        time_max: str = "",
        timezone: str = "",
    ) -> str:
        """Free/busy for one or more calendars.

        :param calendar_ids: List of calendar ids; defaults to [DEFAULT_CALENDAR].
        :param time_min: ISO lower bound (default: now).
        :param time_max: ISO upper bound (default: +7d).
        :param timezone: IANA tz; defaults to DEFAULT_TIMEZONE.
        """
        ids = calendar_ids or [self.valves.DEFAULT_CALENDAR]
        payload = {
            "timeMin": time_min or _now_iso(),
            "timeMax": time_max or _plus_days_iso(7),
            "timeZone": timezone or self.valves.DEFAULT_TIMEZONE,
            "items": [{"id": c} for c in ids],
        }
        body = await self._request("POST", "/freeBusy", json_body=payload)
        cals = body.get("calendars", {})
        out = []
        for cid, info in cals.items():
            busy = info.get("busy", [])
            out.append(f"# {cid}")
            if not busy:
                out.append("  (free for whole window)")
            else:
                for b in busy:
                    out.append(f"  busy {b.get('start')} → {b.get('end')}")
        return "\n".join(out) or "No data."


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def _plus_days_iso(days: int) -> str:
    return (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=days)).replace(microsecond=0).isoformat()


def _format_event_line(e: dict) -> str:
    start = (e.get("start") or {}).get("dateTime") or (e.get("start") or {}).get("date") or "?"
    end = (e.get("end") or {}).get("dateTime") or (e.get("end") or {}).get("date") or "?"
    return f"- [{start} → {end}] {e.get('summary','(no title)')}  ({e.get('id')})"


def _format_event_full(e: dict) -> str:
    out = [
        f"# {e.get('summary','(no title)')}  ({e.get('id')})",
        f"start: {(e.get('start') or {}).get('dateTime') or (e.get('start') or {}).get('date')}",
        f"end:   {(e.get('end') or {}).get('dateTime') or (e.get('end') or {}).get('date')}",
    ]
    if e.get("location"): out.append(f"location: {e['location']}")
    if e.get("description"): out.append(f"\n{e['description']}")
    attendees = e.get("attendees") or []
    if attendees:
        out.append("\nAttendees:")
        for a in attendees:
            out.append(f"  - {a.get('email')}  ({a.get('responseStatus','?')})")
    if e.get("htmlLink"):
        out.append(f"\nlink: {e['htmlLink']}")
    return "\n".join(out)
