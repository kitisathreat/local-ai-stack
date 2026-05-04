"""
title: Canva — Connect API
author: local-ai-stack
description: Talk to the Canva Connect API — list/search designs, list folders, fetch design metadata, export a design (PDF/PNG/JPG), upload assets, and create a new design from a brand template. Auth via OAuth 2.0 with PKCE; this tool accepts a long-lived access token (refreshed externally) plus an optional refresh_token for in-process renewal.
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


_API = "https://api.canva.com/rest/v1"
_TOKEN = "https://api.canva.com/rest/v1/oauth/token"


class Tools:
    class Valves(BaseModel):
        ACCESS_TOKEN: str = Field(
            default="",
            description=(
                "Canva Connect access token. Mint via the OAuth 2.0 + PKCE "
                "flow at https://www.canva.dev/docs/connect/authentication/. "
                "Combine with REFRESH_TOKEN + CLIENT_ID + CLIENT_SECRET for "
                "automatic in-process renewal."
            ),
        )
        REFRESH_TOKEN: str = Field(
            default="",
            description="Optional refresh token used to mint fresh access tokens automatically.",
        )
        CLIENT_ID: str = Field(default="", description="Canva integration client_id.")
        CLIENT_SECRET: str = Field(default="", description="Canva integration client_secret.")
        TIMEOUT_SEC: int = Field(default=30, description="Per-request timeout.")

    def __init__(self) -> None:
        self.valves = self.Valves()
        self._token_expiry: float = 0.0

    async def _ensure_token(self) -> str:
        if not self.valves.ACCESS_TOKEN:
            raise PermissionError("Canva ACCESS_TOKEN is not set.")
        # Cheap refresh: if we have a refresh token + client creds and the
        # cached expiry is close, renew silently.
        if (
            self.valves.REFRESH_TOKEN and self.valves.CLIENT_ID and self.valves.CLIENT_SECRET
            and self._token_expiry and self._token_expiry - 60 < time.time()
        ):
            await self._refresh()
        return self.valves.ACCESS_TOKEN

    async def _refresh(self) -> None:
        basic = base64.b64encode(
            f"{self.valves.CLIENT_ID}:{self.valves.CLIENT_SECRET}".encode()
        ).decode()
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT_SEC) as c:
            r = await c.post(
                _TOKEN,
                headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
                data={"grant_type": "refresh_token", "refresh_token": self.valves.REFRESH_TOKEN},
            )
        if r.status_code >= 400:
            raise RuntimeError(f"Canva refresh failed: {r.status_code} {r.text[:200]}")
        body = r.json()
        self.valves.ACCESS_TOKEN = body["access_token"]
        if body.get("refresh_token"):
            self.valves.REFRESH_TOKEN = body["refresh_token"]
        self._token_expiry = time.time() + int(body.get("expires_in", 3600))

    async def _headers(self) -> dict[str, str]:
        token = await self._ensure_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "local-ai-stack/1.0",
        }

    async def _request(
        self, method: str, path: str, *,
        params: dict | None = None,
        json: dict | None = None,
    ) -> dict:
        headers = await self._headers()
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT_SEC) as c:
            r = await c.request(method, f"{_API}{path}", headers=headers, params=params, json=json)
        if r.status_code >= 400:
            raise RuntimeError(f"Canva {method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}

    async def list_designs(
        self,
        query: str = "",
        ownership: str = "any",
        sort: str = "modified_descending",
        limit: int = 25,
    ) -> str:
        """List or search the user's designs.

        :param query: Optional free-text search.
        :param ownership: "any" | "owned" | "shared".
        :param sort: "relevance" | "modified_descending" | "modified_ascending" | "title_descending" | "title_ascending".
        :param limit: Page size cap.
        """
        params: dict[str, Any] = {"ownership": ownership, "sort_by": sort, "page_size": min(max(int(limit), 1), 100)}
        if query: params["query"] = query
        body = await self._request("GET", "/designs", params=params)
        return _format_designs(body)

    async def get_design(self, design_id: str) -> str:
        """Fetch metadata for a single design.

        :param design_id: Canva design id (DAxxxxxx).
        """
        body = await self._request("GET", f"/designs/{design_id}")
        d = body.get("design", body)
        return _format_design_detail(d)

    async def create_design(
        self,
        title: str,
        design_type: str = "presentation",
        width: int = 0,
        height: int = 0,
    ) -> str:
        """Create a blank design. design_type accepts a preset name or "custom"
        with explicit width/height (pixels).

        :param title: Design title.
        :param design_type: Preset like "presentation", "instagram-post-square",
            "youtube-thumbnail", or "custom".
        :param width: Required when design_type == "custom".
        :param height: Required when design_type == "custom".
        """
        if design_type == "custom":
            if width <= 0 or height <= 0:
                raise ValueError("width and height must be > 0 when design_type='custom'.")
            spec = {"type": "custom", "width": int(width), "height": int(height)}
        else:
            spec = {"type": "preset", "name": design_type}
        body = await self._request(
            "POST", "/designs",
            json={"title": title, "design_type": spec},
        )
        d = body.get("design", body)
        return f"Created design {d.get('id', '?')}  {d.get('title', '')}  url={d.get('urls', {}).get('edit_url', '')}"

    async def export_design(
        self,
        design_id: str,
        format: str = "pdf",
        page_range: str = "",
    ) -> str:
        """Kick off a design export. Returns the export job id and current
        status; poll via :py:meth:`get_export` until it's `success`.

        :param design_id: Design id.
        :param format: "pdf" | "png" | "jpg" | "gif" | "mp4" | "pptx".
        :param page_range: Optional page filter, e.g. "1-3,5".
        """
        if format not in {"pdf", "png", "jpg", "gif", "mp4", "pptx"}:
            raise ValueError("format must be pdf, png, jpg, gif, mp4, or pptx.")
        payload: dict[str, Any] = {"design_id": design_id, "format": {"type": format}}
        if page_range:
            payload["format"]["pages"] = page_range
        body = await self._request("POST", "/exports", json=payload)
        job = body.get("job", body)
        return f"Export job {job.get('id')}  status={job.get('status')}"

    async def get_export(self, job_id: str) -> str:
        """Poll the status of an export job.

        :param job_id: Export job id.
        """
        body = await self._request("GET", f"/exports/{job_id}")
        job = body.get("job", body)
        urls = job.get("urls") or []
        out = [f"job {job.get('id')}  status={job.get('status')}"]
        for u in urls:
            out.append(f"  {u}")
        return "\n".join(out)

    async def list_folders(self, query: str = "", limit: int = 25) -> str:
        """List or search folders the user owns or has access to.

        :param query: Free-text search.
        :param limit: Page size.
        """
        params: dict[str, Any] = {"page_size": min(max(int(limit), 1), 100)}
        if query: params["query"] = query
        body = await self._request("GET", "/folders/search", params=params)
        items = body.get("items") or body.get("folders") or []
        if not items:
            return "No folders."
        return "\n".join(f"- {f.get('id')}  {f.get('name')}" for f in items)

    async def upload_asset(self, name: str, file_url: str, mime_type: str = "image/png") -> str:
        """Upload an asset by URL. The Canva backend fetches the URL and
        stores the bytes — no need to stream them through this tool.

        :param name: Display name for the asset.
        :param file_url: Public HTTP(S) URL the asset can be fetched from.
        :param mime_type: MIME type, e.g. "image/png".
        """
        body = await self._request(
            "POST", "/asset-uploads/url",
            json={"name": name, "url": file_url, "content_type": mime_type},
        )
        upload = body.get("asset_upload", body)
        return f"Asset upload {upload.get('id')}  status={upload.get('status')}"


def _format_designs(body: dict) -> str:
    items = body.get("items", body.get("designs", []))
    if not items:
        return "No designs."
    out = []
    for d in items:
        url = (d.get("urls") or {}).get("edit_url", "")
        out.append(f"- {d.get('id')}  {d.get('title') or '(untitled)'}\n    {url}")
    return "\n".join(out)


def _format_design_detail(d: dict) -> str:
    out = [f"# {d.get('title') or '(untitled)'}  ({d.get('id')})"]
    out.append(f"created: {d.get('created_at', '?')}")
    out.append(f"updated: {d.get('updated_at', '?')}")
    pages = d.get("page_count")
    if pages is not None:
        out.append(f"pages: {pages}")
    urls = d.get("urls", {})
    if urls.get("edit_url"):
        out.append(f"edit: {urls['edit_url']}")
    if urls.get("view_url"):
        out.append(f"view: {urls['view_url']}")
    thumb = (d.get("thumbnail") or {}).get("url")
    if thumb:
        out.append(f"thumbnail: {thumb}")
    return "\n".join(out)
