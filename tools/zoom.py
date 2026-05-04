"""
title: Zoom — Meetings, Recordings, Users
author: local-ai-stack
description: Talk to Zoom's REST API v2. List + create + update + delete meetings, list past-meeting recordings, fetch transcripts (when cloud-recording captions are on), look up users. Auth via Server-to-Server OAuth (https://developers.zoom.us/docs/internal-apps/s2s-oauth/) — supply account_id, client_id, and client_secret.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

import base64
import time
from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field


_API = "https://api.zoom.us/v2"
_TOKEN = "https://zoom.us/oauth/token"


class Tools:
    class Valves(BaseModel):
        ACCOUNT_ID: str = Field(
            default="",
            description="Zoom account_id from your Server-to-Server OAuth app.",
        )
        CLIENT_ID: str = Field(default="", description="S2S OAuth client_id.")
        CLIENT_SECRET: str = Field(default="", description="S2S OAuth client_secret.")
        DEFAULT_USER: str = Field(
            default="me",
            description="Zoom user id ('me' uses the OAuth account holder).",
        )
        TIMEOUT_SEC: int = Field(default=20, description="Per-request timeout.")

    def __init__(self) -> None:
        self.valves = self.Valves()
        self._token: tuple[str, float] | None = None

    async def _ensure_token(self) -> str:
        if self._token and self._token[1] - 60 > time.time():
            return self._token[0]
        if not (self.valves.ACCOUNT_ID and self.valves.CLIENT_ID and self.valves.CLIENT_SECRET):
            raise PermissionError("Zoom requires ACCOUNT_ID + CLIENT_ID + CLIENT_SECRET.")
        basic = base64.b64encode(
            f"{self.valves.CLIENT_ID}:{self.valves.CLIENT_SECRET}".encode()
        ).decode()
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT_SEC) as c:
            r = await c.post(
                _TOKEN,
                headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
                params={"grant_type": "account_credentials", "account_id": self.valves.ACCOUNT_ID},
            )
        if r.status_code >= 400:
            raise RuntimeError(f"Zoom token failed: {r.status_code} {r.text[:200]}")
        body = r.json()
        self._token = (body["access_token"], time.time() + int(body.get("expires_in", 3600)))
        return self._token[0]

    async def _request(self, method: str, path: str, **kw: Any) -> dict:
        token = await self._ensure_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "local-ai-stack/1.0",
        }
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT_SEC) as c:
            r = await c.request(method, f"{_API}{path}", headers=headers, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"Zoom {method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}

    # ── Meetings ───────────────────────────────────────────────────────────

    async def list_meetings(
        self,
        user_id: str = "",
        scope: str = "scheduled",
        page_size: int = 30,
    ) -> str:
        """List a user's meetings.

        :param user_id: Falls back to DEFAULT_USER.
        :param scope: "scheduled" | "live" | "upcoming" | "previous_meetings".
        :param page_size: 1-300.
        """
        uid = user_id or self.valves.DEFAULT_USER
        body = await self._request(
            "GET", f"/users/{uid}/meetings",
            params={"type": scope, "page_size": min(max(int(page_size), 1), 300)},
        )
        meetings = body.get("meetings", [])
        if not meetings:
            return "No meetings."
        return "\n".join(
            f"- {m.get('id')}  {m.get('topic','(no topic)')}  start={m.get('start_time','?')}  duration={m.get('duration','?')}min"
            for m in meetings
        )

    async def create_meeting(
        self,
        topic: str,
        start_time: str = "",
        duration_minutes: int = 30,
        timezone: str = "UTC",
        user_id: str = "",
        password: str = "",
        agenda: str = "",
    ) -> str:
        """Schedule a meeting.

        :param topic: Meeting topic.
        :param start_time: ISO datetime in the chosen timezone (omit for instant meeting).
        :param duration_minutes: Duration.
        :param timezone: IANA tz.
        :param user_id: Falls back to DEFAULT_USER.
        :param password: Optional join password.
        :param agenda: Optional agenda body.
        """
        uid = user_id or self.valves.DEFAULT_USER
        payload: dict[str, Any] = {
            "topic": topic,
            "type": 2 if start_time else 1,  # 2=scheduled, 1=instant
            "duration": int(duration_minutes),
            "timezone": timezone,
            "settings": {"join_before_host": True, "waiting_room": True, "auto_recording": "none"},
        }
        if start_time: payload["start_time"] = start_time
        if password: payload["password"] = password
        if agenda: payload["agenda"] = agenda
        body = await self._request("POST", f"/users/{uid}/meetings", json=payload)
        return (
            f"Created meeting {body.get('id')}\n"
            f"  topic: {body.get('topic')}\n"
            f"  join: {body.get('join_url')}\n"
            f"  start: {body.get('start_time','(instant)')}\n"
            f"  password: {body.get('password','(none)')}"
        )

    async def get_meeting(self, meeting_id: str) -> str:
        """Fetch a meeting's full settings + URLs.

        :param meeting_id: Meeting id (numeric).
        """
        body = await self._request("GET", f"/meetings/{meeting_id}")
        out = [
            f"# {body.get('topic')}  ({body.get('id')})",
            f"start: {body.get('start_time')}",
            f"duration: {body.get('duration')}min",
            f"timezone: {body.get('timezone')}",
            f"join: {body.get('join_url')}",
        ]
        if body.get("agenda"): out.append(f"\nagenda:\n{body['agenda']}")
        return "\n".join(out)

    async def update_meeting(
        self,
        meeting_id: str,
        topic: str = "",
        start_time: str = "",
        duration_minutes: int = 0,
        agenda: str = "",
    ) -> str:
        """Patch-update a meeting.

        :param meeting_id: Meeting id.
        :param topic: New topic.
        :param start_time: New ISO start.
        :param duration_minutes: New duration.
        :param agenda: New agenda.
        """
        patch: dict[str, Any] = {}
        if topic: patch["topic"] = topic
        if start_time: patch["start_time"] = start_time
        if duration_minutes: patch["duration"] = int(duration_minutes)
        if agenda: patch["agenda"] = agenda
        if not patch:
            return "Nothing to update."
        await self._request("PATCH", f"/meetings/{meeting_id}", json=patch)
        return f"Updated meeting {meeting_id}."

    async def delete_meeting(self, meeting_id: str) -> str:
        """Delete a meeting.

        :param meeting_id: Meeting id.
        """
        await self._request("DELETE", f"/meetings/{meeting_id}")
        return f"Deleted meeting {meeting_id}."

    # ── Recordings ─────────────────────────────────────────────────────────

    async def list_recordings(
        self,
        user_id: str = "",
        from_date: str = "",
        to_date: str = "",
        page_size: int = 30,
    ) -> str:
        """List cloud recordings.

        :param user_id: Falls back to DEFAULT_USER.
        :param from_date: ISO date "YYYY-MM-DD" (default: 30 days ago).
        :param to_date: ISO date.
        :param page_size: 1-300.
        """
        import datetime as _dt
        uid = user_id or self.valves.DEFAULT_USER
        params: dict[str, Any] = {"page_size": min(max(int(page_size), 1), 300)}
        if from_date: params["from"] = from_date
        if to_date:   params["to"] = to_date
        if "from" not in params:
            params["from"] = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=30)).date().isoformat()
        body = await self._request("GET", f"/users/{uid}/recordings", params=params)
        meetings = body.get("meetings", [])
        if not meetings:
            return "No recordings."
        out = []
        for m in meetings:
            files = m.get("recording_files") or []
            out.append(f"- {m.get('id')}  {m.get('topic')}  {m.get('start_time')}  files={len(files)}")
            for f in files[:5]:
                out.append(f"    [{f.get('file_type')}] {f.get('download_url','')}")
        return "\n".join(out)

    async def get_meeting_transcript(self, meeting_id: str) -> str:
        """Fetch the auto-generated VTT transcript for a recorded meeting.

        :param meeting_id: Meeting id (or UUID for past instances).
        """
        body = await self._request("GET", f"/meetings/{meeting_id}/recordings")
        files = body.get("recording_files") or []
        vtt = next((f for f in files if f.get("file_type") == "TRANSCRIPT"), None)
        if not vtt:
            return f"No transcript available for {meeting_id}."
        url = vtt.get("download_url", "")
        # Use the existing token to fetch the binary content.
        token = await self._ensure_token()
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT_SEC) as c:
            r = await c.get(url, headers={"Authorization": f"Bearer {token}"})
        if r.status_code >= 400:
            return f"Transcript download -> {r.status_code}"
        return r.text

    # ── Users ──────────────────────────────────────────────────────────────

    async def get_user(self, user_id: str = "") -> str:
        """Fetch a user's profile."""
        uid = user_id or self.valves.DEFAULT_USER
        body = await self._request("GET", f"/users/{uid}")
        return (
            f"{body.get('first_name','')} {body.get('last_name','')}  "
            f"({body.get('email','?')})  type={body.get('type','?')}"
        )

    async def list_users(self, page_size: int = 30) -> str:
        """List users in the account.

        :param page_size: 1-300.
        """
        body = await self._request("GET", "/users", params={"page_size": min(max(int(page_size), 1), 300)})
        users = body.get("users", [])
        if not users:
            return "No users."
        return "\n".join(
            f"- {u.get('id')}  {u.get('email')}  ({u.get('first_name','')} {u.get('last_name','')})"
            for u in users
        )
