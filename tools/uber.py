"""
title: Uber — Price Estimates, Eats Search, Trip History
author: local-ai-stack
description: Talk to Uber's public APIs. The Rides API is restricted to Uber-approved partners — for general use this tool covers price + ETA estimates from the public widget endpoints, an Uber Eats restaurant search via the public storefront API, and (when a server-token is present) Eats menu lookups. Auth is two-tier: server tokens for guest endpoints; OAuth refresh-token for user-scoped endpoints (history, current trip).
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

import time
from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field


_API = "https://api.uber.com"
_TOKEN = "https://login.uber.com/oauth/v2/token"


class Tools:
    class Valves(BaseModel):
        SERVER_TOKEN: str = Field(
            default="",
            description=(
                "Uber server-token (https://developer.uber.com). Used for "
                "anonymous price-estimate and product-listing endpoints."
            ),
        )
        CLIENT_ID: str = Field(default="", description="OAuth client_id (for user-scoped endpoints).")
        CLIENT_SECRET: str = Field(default="", description="OAuth client_secret.")
        REFRESH_TOKEN: str = Field(
            default="",
            description=(
                "Long-lived refresh token (scopes: history / profile / "
                "request as approved by Uber)."
            ),
        )
        DEFAULT_LANGUAGE: str = Field(default="en", description="Accept-Language for storefront responses.")
        TIMEOUT_SEC: int = Field(default=20, description="Per-request timeout.")

    def __init__(self) -> None:
        self.valves = self.Valves()
        self._user_token: tuple[str, float] | None = None

    # ── Auth ────────────────────────────────────────────────────────────────

    async def _refresh_user_token(self) -> str:
        if self._user_token and self._user_token[1] - 60 > time.time():
            return self._user_token[0]
        if not (self.valves.CLIENT_ID and self.valves.CLIENT_SECRET and self.valves.REFRESH_TOKEN):
            raise PermissionError(
                "User-scoped Uber call requires CLIENT_ID + CLIENT_SECRET + REFRESH_TOKEN."
            )
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT_SEC) as c:
            r = await c.post(
                _TOKEN,
                data={
                    "client_id": self.valves.CLIENT_ID,
                    "client_secret": self.valves.CLIENT_SECRET,
                    "refresh_token": self.valves.REFRESH_TOKEN,
                    "grant_type": "refresh_token",
                },
            )
        if r.status_code >= 400:
            raise RuntimeError(f"Uber token refresh failed: {r.status_code} {r.text[:200]}")
        body = r.json()
        token = body["access_token"]
        self._user_token = (token, time.time() + int(body.get("expires_in", 1800)))
        return token

    async def _server_request(self, method: str, path: str, **kw: Any) -> dict:
        if not self.valves.SERVER_TOKEN:
            raise PermissionError("SERVER_TOKEN required for anonymous Uber endpoints.")
        headers = {
            "Authorization": f"Token {self.valves.SERVER_TOKEN}",
            "Accept-Language": self.valves.DEFAULT_LANGUAGE,
            "Content-Type": "application/json",
            "User-Agent": "local-ai-stack/1.0",
        }
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT_SEC) as c:
            r = await c.request(method, f"{_API}{path}", headers=headers, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"Uber {method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}

    async def _user_request(self, method: str, path: str, **kw: Any) -> dict:
        token = await self._refresh_user_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept-Language": self.valves.DEFAULT_LANGUAGE,
            "Content-Type": "application/json",
            "User-Agent": "local-ai-stack/1.0",
        }
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT_SEC) as c:
            r = await c.request(method, f"{_API}{path}", headers=headers, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"Uber {method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}

    # ── Rides: estimates + products ────────────────────────────────────────

    async def list_products(self, lat: float, lng: float) -> str:
        """Available ride product types at a coordinate.

        :param lat: Latitude.
        :param lng: Longitude.
        """
        body = await self._server_request(
            "GET", "/v1.2/products",
            params={"latitude": lat, "longitude": lng},
        )
        items = body.get("products", [])
        if not items:
            return "No products at that location."
        return "\n".join(
            f"- {p.get('product_id')}  {p.get('display_name')}  capacity={p.get('capacity','?')}"
            for p in items
        )

    async def price_estimate(
        self,
        start_lat: float,
        start_lng: float,
        end_lat: float,
        end_lng: float,
    ) -> str:
        """Get price + duration estimates between two coordinates.

        :param start_lat: Pickup latitude.
        :param start_lng: Pickup longitude.
        :param end_lat: Dropoff latitude.
        :param end_lng: Dropoff longitude.
        """
        body = await self._server_request(
            "GET", "/v1.2/estimates/price",
            params={
                "start_latitude": start_lat,
                "start_longitude": start_lng,
                "end_latitude": end_lat,
                "end_longitude": end_lng,
            },
        )
        prices = body.get("prices", [])
        if not prices:
            return "No price estimates."
        return "\n".join(
            f"- {p.get('display_name')}  {p.get('estimate')}  duration={p.get('duration')}s  distance={p.get('distance')}mi"
            for p in prices
        )

    async def time_estimate(self, lat: float, lng: float, product_id: str = "") -> str:
        """Estimate ETA for a pickup coordinate.

        :param lat: Pickup latitude.
        :param lng: Pickup longitude.
        :param product_id: Optional product to scope to.
        """
        params: dict[str, Any] = {"start_latitude": lat, "start_longitude": lng}
        if product_id: params["product_id"] = product_id
        body = await self._server_request("GET", "/v1.2/estimates/time", params=params)
        items = body.get("times", [])
        if not items:
            return "No ETAs available."
        return "\n".join(
            f"- {t.get('display_name')}  ETA {t.get('estimate', '?')}s"
            for t in items
        )

    # ── User-scoped endpoints ──────────────────────────────────────────────

    async def trip_history(self, limit: int = 10, offset: int = 0) -> str:
        """List the authenticated user's recent trips.

        :param limit: 1-50.
        :param offset: Pagination offset.
        """
        body = await self._user_request(
            "GET", "/v1.2/history",
            params={"limit": min(max(int(limit), 1), 50), "offset": int(offset)},
        )
        items = body.get("history", [])
        if not items:
            return "No trips."
        out = []
        for t in items:
            start = t.get("start_city", {}).get("display_name", "?")
            distance = t.get("distance", "?")
            out.append(f"- {t.get('request_id')}  {start}  {distance}mi  status={t.get('status')}")
        return "\n".join(out)

    async def me(self) -> str:
        """Authenticated user's profile."""
        body = await self._user_request("GET", "/v1.2/me")
        first = body.get("first_name", "")
        last = body.get("last_name", "")
        return f"{first} {last}  ({body.get('email','?')})  rating={body.get('rider_rating','?')}"

    # ── Eats search ────────────────────────────────────────────────────────

    async def eats_search(
        self,
        latitude: float,
        longitude: float,
        query: str = "",
        limit: int = 10,
    ) -> str:
        """Search Uber Eats restaurants near a coordinate. Uses the public
        storefront. SERVER_TOKEN improves rate limits; not strictly required.

        :param latitude: Lat.
        :param longitude: Lng.
        :param query: Free-text search.
        :param limit: Max results.
        """
        # The storefront API is consumer-facing JSON. Endpoint shape is
        # stable enough for read-only search.
        url = "https://www.ubereats.com/api/getFeedV1"
        payload = {
            "userQuery": query,
            "userQueryParameters": {},
            "cacheKey": "",
            "feedSessionId": "",
            "feedType": "GLOBAL_SEARCH" if query else "MAIN",
        }
        cookies = {"uev2.loc": f'%7B"address"%3A"%22%2C"latitude"%3A{latitude}%2C"longitude"%3A{longitude}%7D'}
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "x-csrf-token": "x",
            "User-Agent": "Mozilla/5.0 local-ai-stack/1.0",
        }
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT_SEC, cookies=cookies) as c:
            r = await c.post(url, headers=headers, json=payload)
        if r.status_code >= 400:
            return f"Uber Eats public search returned {r.status_code}; for production use Uber's partner API."
        try:
            body = r.json()
        except ValueError:
            return "Unparseable Eats storefront response (rate-limited or geo-blocked)."
        feed = (body.get("data") or {}).get("feedItems") or []
        results = []
        for item in feed:
            store = (item.get("store") or {}).get("title", "")
            slug = (item.get("store") or {}).get("slug", "")
            eta = (item.get("store") or {}).get("etaRange", {}).get("min", "?")
            if store:
                results.append(f"- {store}  ETA {eta}min  https://ubereats.com/store/{slug}")
            if len(results) >= limit:
                break
        return "\n".join(results) if results else "No restaurants found."
