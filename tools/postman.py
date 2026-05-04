"""
title: Postman — Workspaces, Collections, Environments, Mocks
author: local-ai-stack
description: Read and edit Postman workspaces via the Public API — list workspaces, fetch a collection's full JSON, create / duplicate collections, manage environments and mock servers, search OpenAPI specs. Auth via a Postman API key (https://www.postman.com/settings/me/api-keys).
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field


_API = "https://api.getpostman.com"


class Tools:
    class Valves(BaseModel):
        API_KEY: str = Field(
            default="",
            description=(
                "Postman API key. Generate at "
                "https://www.postman.com/settings/me/api-keys."
            ),
        )
        TIMEOUT_SEC: int = Field(default=20, description="Per-request timeout.")

    def __init__(self) -> None:
        self.valves = self.Valves()

    def _headers(self) -> dict[str, str]:
        if not self.valves.API_KEY:
            raise PermissionError("Postman API_KEY is not set.")
        return {
            "X-API-Key": self.valves.API_KEY,
            "Content-Type": "application/json",
            "User-Agent": "local-ai-stack/1.0",
        }

    async def _request(
        self, method: str, path: str, *,
        params: dict | None = None,
        json: dict | None = None,
    ) -> dict:
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT_SEC) as c:
            r = await c.request(method, f"{_API}{path}", headers=self._headers(), params=params, json=json)
        if r.status_code >= 400:
            raise RuntimeError(f"Postman {method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}

    # ── Workspaces ──────────────────────────────────────────────────────────

    async def list_workspaces(self) -> str:
        """List workspaces visible to the API key."""
        body = await self._request("GET", "/workspaces")
        ws = body.get("workspaces", [])
        if not ws:
            return "No workspaces."
        return "\n".join(f"- {w.get('id')}  {w.get('name')}  ({w.get('type')})" for w in ws)

    async def get_workspace(self, workspace_id: str) -> str:
        """Fetch a workspace including the collections, environments, and mocks
        it contains.

        :param workspace_id: Workspace id (UUID).
        """
        body = await self._request("GET", f"/workspaces/{workspace_id}")
        w = body.get("workspace", {})
        out = [f"# {w.get('name')}  ({w.get('id')})", f"type: {w.get('type')}"]
        out.append(f"description: {(w.get('description') or '').strip()[:300]}")
        for label in ("collections", "environments", "mocks", "monitors", "apis"):
            items = w.get(label) or []
            out.append(f"\n{label} ({len(items)}):")
            for i in items[:25]:
                out.append(f"  - {i.get('uid') or i.get('id')}  {i.get('name')}")
            if len(items) > 25:
                out.append(f"  … +{len(items) - 25} more")
        return "\n".join(out)

    async def create_workspace(
        self,
        name: str,
        type: str = "personal",
        description: str = "",
    ) -> str:
        """Create a workspace.

        :param name: Workspace name.
        :param type: "personal" | "team".
        :param description: Optional description.
        """
        body = await self._request(
            "POST", "/workspaces",
            json={"workspace": {"name": name, "type": type, "description": description}},
        )
        w = body.get("workspace", {})
        return f"Created workspace {w.get('id')}  {w.get('name')}"

    # ── Collections ────────────────────────────────────────────────────────

    async def list_collections(self, workspace_id: str = "") -> str:
        """List collections, optionally scoped to a workspace.

        :param workspace_id: Optional workspace id.
        """
        params = {"workspace": workspace_id} if workspace_id else None
        body = await self._request("GET", "/collections", params=params)
        items = body.get("collections", [])
        if not items:
            return "No collections."
        return "\n".join(f"- {c.get('uid') or c.get('id')}  {c.get('name')}" for c in items)

    async def get_collection(self, collection_id: str) -> str:
        """Fetch a collection's full JSON (info + items + folders).

        :param collection_id: Collection id (UID preferred).
        """
        body = await self._request("GET", f"/collections/{collection_id}")
        c = body.get("collection", {})
        info = c.get("info", {})
        out = [f"# {info.get('name')}  ({info.get('_postman_id')})"]
        if info.get("description"):
            out.append((info["description"] if isinstance(info["description"], str) else info["description"].get("content", ""))[:400])
        out.append("\nitems:")
        out.append(_format_items(c.get("item", []), depth=0))
        return "\n".join(out)

    async def create_collection(
        self,
        name: str,
        workspace_id: str = "",
        description: str = "",
    ) -> str:
        """Create an empty collection.

        :param name: Collection name.
        :param workspace_id: Workspace to put it in (defaults to your personal workspace).
        :param description: Optional description.
        """
        params = {"workspace": workspace_id} if workspace_id else None
        payload = {
            "collection": {
                "info": {
                    "name": name,
                    "description": description,
                    "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
                },
                "item": [],
            }
        }
        body = await self._request("POST", "/collections", params=params, json=payload)
        c = body.get("collection", {})
        return f"Created collection {c.get('uid')}  {c.get('name')}"

    async def duplicate_collection(self, collection_id: str, workspace_id: str = "") -> str:
        """Duplicate an existing collection.

        :param collection_id: Source collection id.
        :param workspace_id: Optional destination workspace.
        """
        params = {"workspace": workspace_id} if workspace_id else None
        body = await self._request(
            "POST", f"/collections/{collection_id}/duplicates",
            params=params, json={},
        )
        return f"Duplicated to {body.get('collection', {}).get('uid', '?')}"

    # ── Environments ───────────────────────────────────────────────────────

    async def list_environments(self, workspace_id: str = "") -> str:
        """List environments, optionally scoped to a workspace."""
        params = {"workspace": workspace_id} if workspace_id else None
        body = await self._request("GET", "/environments", params=params)
        items = body.get("environments", [])
        if not items:
            return "No environments."
        return "\n".join(f"- {e.get('uid') or e.get('id')}  {e.get('name')}" for e in items)

    async def create_environment(
        self,
        name: str,
        values: list[dict[str, Any]],
        workspace_id: str = "",
    ) -> str:
        """Create an environment.

        :param name: Environment name.
        :param values: List of {key, value, type=secret|default, enabled} dicts.
        :param workspace_id: Optional workspace.
        """
        params = {"workspace": workspace_id} if workspace_id else None
        body = await self._request(
            "POST", "/environments",
            params=params,
            json={"environment": {"name": name, "values": values}},
        )
        env = body.get("environment", {})
        return f"Created environment {env.get('uid')}  {env.get('name')}"

    # ── Mocks ─────────────────────────────────────────────────────────────

    async def list_mocks(self, workspace_id: str = "") -> str:
        """List mock servers."""
        params = {"workspace": workspace_id} if workspace_id else None
        body = await self._request("GET", "/mocks", params=params)
        items = body.get("mocks", [])
        if not items:
            return "No mocks."
        return "\n".join(
            f"- {m.get('uid') or m.get('id')}  {m.get('name')}  url={m.get('mockUrl', '?')}"
            for m in items
        )

    async def create_mock(
        self,
        collection_id: str,
        name: str = "",
        environment_id: str = "",
        workspace_id: str = "",
    ) -> str:
        """Create a mock server backed by a collection.

        :param collection_id: Collection UID to mock.
        :param name: Optional mock name.
        :param environment_id: Optional environment UID.
        :param workspace_id: Optional workspace.
        """
        params = {"workspace": workspace_id} if workspace_id else None
        payload: dict[str, Any] = {"mock": {"collection": collection_id}}
        if name: payload["mock"]["name"] = name
        if environment_id: payload["mock"]["environment"] = environment_id
        body = await self._request("POST", "/mocks", params=params, json=payload)
        m = body.get("mock", {})
        return f"Created mock {m.get('uid')}  url={m.get('mockUrl', '?')}"


def _format_items(items: list[dict], depth: int = 0) -> str:
    pad = "  " * depth
    out = []
    for it in items:
        if "request" in it:
            req = it["request"]
            method = req.get("method", "?") if isinstance(req, dict) else "?"
            url = req.get("url") if isinstance(req, dict) else req
            url_str = url.get("raw", "") if isinstance(url, dict) else (url or "")
            out.append(f"{pad}- [{method}] {it.get('name')}  {url_str}")
        else:
            # Folder
            out.append(f"{pad}+ {it.get('name')}/")
            sub = it.get("item") or []
            if sub:
                out.append(_format_items(sub, depth + 1))
    return "\n".join(line for line in out if line)
