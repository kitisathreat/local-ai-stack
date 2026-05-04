"""Shared Google OAuth 2.0 token-refresh helper.

Underscore-prefixed so :py:func:`build_registry` skips it — this module
exposes no `class Tools`, just helpers used by gmail / google_calendar /
google_drive at call time.

Contract: callers stash their own client_id, client_secret, and the
long-lived refresh_token in the per-tool Valves. We exchange those for
a short-lived access token, cache it in-process, and refresh
transparently when within 60 s of expiry.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import httpx


GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


@dataclass
class GoogleAuth:
    client_id: str
    client_secret: str
    refresh_token: str
    _access_token: str = ""
    _expiry: float = 0.0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def access_token(self) -> str:
        if self._access_token and self._expiry - 60 > time.time():
            return self._access_token
        async with self._lock:
            # Re-check inside the lock to coalesce concurrent refreshes.
            if self._access_token and self._expiry - 60 > time.time():
                return self._access_token
            await self._refresh()
        return self._access_token

    async def _refresh(self) -> None:
        if not (self.client_id and self.client_secret and self.refresh_token):
            raise PermissionError(
                "Google OAuth not configured: set CLIENT_ID, CLIENT_SECRET and "
                "REFRESH_TOKEN on the tool's Valves. Mint the refresh token "
                "via the OAuth installed-app flow at "
                "https://console.cloud.google.com/apis/credentials with the "
                "scopes the tool needs."
            )
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                GOOGLE_TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "refresh_token": self.refresh_token,
                    "grant_type": "refresh_token",
                },
            )
        if r.status_code >= 400:
            raise RuntimeError(f"Google token refresh failed: {r.status_code} {r.text[:300]}")
        body = r.json()
        self._access_token = body["access_token"]
        self._expiry = time.time() + int(body.get("expires_in", 3600))

    async def headers(self, extra: dict | None = None) -> dict[str, str]:
        token = await self.access_token()
        h = {
            "Authorization": f"Bearer {token}",
            "User-Agent": "local-ai-stack/1.0",
        }
        if extra:
            h.update(extra)
        return h


async def google_request(
    auth: GoogleAuth,
    method: str,
    url: str,
    *,
    params: dict | None = None,
    json_body: dict | None = None,
    data: bytes | None = None,
    timeout: int = 30,
    accept: str = "application/json",
) -> httpx.Response:
    headers = await auth.headers({"Accept": accept})
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    async with httpx.AsyncClient(timeout=timeout) as c:
        return await c.request(
            method, url,
            headers=headers,
            params=params,
            json=json_body,
            content=data,
        )
