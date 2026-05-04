"""
title: Cloudflare Developer Platform
author: local-ai-stack
description: Manage Cloudflare developer-platform resources via the v4 REST API — Workers, D1 databases (incl. queries), KV namespaces, R2 buckets, Hyperdrive configs — plus the Cloudflare docs search. Auth via an API token (https://dash.cloudflare.com/profile/api-tokens) scoped to the resources you intend to read/write.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field


_API = "https://api.cloudflare.com/client/v4"
_DOCS_SEARCH = "https://developers.cloudflare.com/api/search"


class Tools:
    class Valves(BaseModel):
        API_TOKEN: str = Field(
            default="",
            description=(
                "Cloudflare API token. Create at "
                "https://dash.cloudflare.com/profile/api-tokens with the "
                "scopes for the resources you'll touch (Workers Scripts:Read/Edit, "
                "D1:Read/Edit, KV:Edit, R2:Edit, Hyperdrive:Edit, etc.)."
            ),
        )
        ACCOUNT_ID: str = Field(
            default="",
            description="Default account_id, used when callers omit it.",
        )
        TIMEOUT_SEC: int = Field(default=30, description="Per-request timeout.")

    def __init__(self) -> None:
        self.valves = self.Valves()

    def _headers(self) -> dict[str, str]:
        if not self.valves.API_TOKEN:
            raise PermissionError("Cloudflare API_TOKEN is not set on the tool's Valves.")
        return {
            "Authorization": f"Bearer {self.valves.API_TOKEN}",
            "Content-Type": "application/json",
            "User-Agent": "local-ai-stack/1.0",
        }

    def _resolve_account(self, account_id: str | None) -> str:
        aid = (account_id or self.valves.ACCOUNT_ID or "").strip()
        if not aid:
            raise ValueError("account_id is required (or set ACCOUNT_ID in Valves).")
        return aid

    async def _request(
        self, method: str, path: str, *,
        json: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT_SEC) as c:
            r = await c.request(method, f"{_API}{path}", headers=self._headers(), params=params, json=json)
        if r.status_code >= 400:
            raise RuntimeError(f"Cloudflare {method} {path} -> {r.status_code}: {r.text[:300]}")
        body = r.json() if r.content else {}
        if isinstance(body, dict) and body.get("success") is False:
            errs = "; ".join(e.get("message", "") for e in (body.get("errors") or []))
            raise RuntimeError(f"Cloudflare API error: {errs or body}")
        return body

    # ── Account & Workers ───────────────────────────────────────────────────

    async def list_accounts(self) -> str:
        """List accounts the API token can see."""
        body = await self._request("GET", "/accounts")
        accounts = body.get("result", [])
        if not accounts:
            return "No accounts visible."
        return "\n".join(f"- {a.get('id')}  {a.get('name')}" for a in accounts)

    async def list_workers(self, account_id: str = "") -> str:
        """List Workers scripts in the account."""
        aid = self._resolve_account(account_id)
        body = await self._request("GET", f"/accounts/{aid}/workers/scripts")
        scripts = body.get("result", [])
        if not scripts:
            return f"No Workers in {aid}."
        return "\n".join(
            f"- {s.get('id')}  modified={s.get('modified_on','')}"
            for s in scripts
        )

    async def get_worker_code(self, name: str, account_id: str = "") -> str:
        """Download the source of a Workers script.

        :param name: Worker script name.
        :param account_id: Falls back to ACCOUNT_ID.
        """
        aid = self._resolve_account(account_id)
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT_SEC) as c:
            r = await c.get(
                f"{_API}/accounts/{aid}/workers/scripts/{name}",
                headers={**self._headers(), "Accept": "application/javascript"},
            )
        if r.status_code >= 400:
            raise RuntimeError(f"Cloudflare GET worker -> {r.status_code}: {r.text[:300]}")
        return r.text

    # ── D1 ──────────────────────────────────────────────────────────────────

    async def list_d1_databases(self, account_id: str = "") -> str:
        """List D1 databases in the account."""
        aid = self._resolve_account(account_id)
        body = await self._request("GET", f"/accounts/{aid}/d1/database")
        dbs = body.get("result", [])
        if not dbs:
            return f"No D1 databases in {aid}."
        return "\n".join(
            f"- {d.get('uuid')}  {d.get('name')}  size={d.get('file_size','?')}"
            for d in dbs
        )

    async def create_d1_database(self, name: str, account_id: str = "") -> str:
        """Create a new D1 database.

        :param name: Database name.
        :param account_id: Falls back to ACCOUNT_ID.
        """
        aid = self._resolve_account(account_id)
        body = await self._request(
            "POST", f"/accounts/{aid}/d1/database",
            json={"name": name},
        )
        db = body.get("result", {})
        return f"Created D1 {db.get('uuid')}  {db.get('name')}"

    async def delete_d1_database(self, database_id: str, account_id: str = "") -> str:
        """Delete a D1 database.

        :param database_id: D1 database UUID.
        :param account_id: Falls back to ACCOUNT_ID.
        """
        aid = self._resolve_account(account_id)
        await self._request("DELETE", f"/accounts/{aid}/d1/database/{database_id}")
        return f"Deleted D1 {database_id}."

    async def query_d1(
        self,
        database_id: str,
        sql: str,
        params: Optional[list[Any]] = None,
        account_id: str = "",
    ) -> str:
        """Run a SQL statement against a D1 database.

        :param database_id: D1 UUID.
        :param sql: SQL string. Use `?` for parameter binding.
        :param params: Positional bind values.
        :param account_id: Falls back to ACCOUNT_ID.
        """
        aid = self._resolve_account(account_id)
        body = await self._request(
            "POST", f"/accounts/{aid}/d1/database/{database_id}/query",
            json={"sql": sql, "params": params or []},
        )
        results = body.get("result") or []
        out = []
        for r in results:
            success = r.get("success")
            meta = r.get("meta", {})
            rows = r.get("results") or []
            out.append(
                f"success={success}  rows={len(rows)}  duration_ms={meta.get('duration', 0):.2f}"
            )
            for row in rows[:25]:
                out.append(f"  {row}")
            if len(rows) > 25:
                out.append(f"  … +{len(rows) - 25} more rows")
        return "\n".join(out) or "No statements."

    # ── KV ──────────────────────────────────────────────────────────────────

    async def list_kv_namespaces(self, account_id: str = "") -> str:
        """List KV namespaces in the account."""
        aid = self._resolve_account(account_id)
        body = await self._request("GET", f"/accounts/{aid}/storage/kv/namespaces")
        ns = body.get("result", [])
        if not ns:
            return f"No KV namespaces in {aid}."
        return "\n".join(f"- {n.get('id')}  {n.get('title')}" for n in ns)

    async def create_kv_namespace(self, title: str, account_id: str = "") -> str:
        """Create a KV namespace.

        :param title: Namespace title.
        :param account_id: Falls back to ACCOUNT_ID.
        """
        aid = self._resolve_account(account_id)
        body = await self._request(
            "POST", f"/accounts/{aid}/storage/kv/namespaces",
            json={"title": title},
        )
        ns = body.get("result", {})
        return f"Created KV namespace {ns.get('id')}  {ns.get('title')}"

    async def delete_kv_namespace(self, namespace_id: str, account_id: str = "") -> str:
        """Delete a KV namespace.

        :param namespace_id: KV namespace id.
        :param account_id: Falls back to ACCOUNT_ID.
        """
        aid = self._resolve_account(account_id)
        await self._request("DELETE", f"/accounts/{aid}/storage/kv/namespaces/{namespace_id}")
        return f"Deleted KV namespace {namespace_id}."

    # ── R2 ──────────────────────────────────────────────────────────────────

    async def list_r2_buckets(self, account_id: str = "") -> str:
        """List R2 buckets in the account."""
        aid = self._resolve_account(account_id)
        body = await self._request("GET", f"/accounts/{aid}/r2/buckets")
        buckets = (body.get("result") or {}).get("buckets") or body.get("result") or []
        if not buckets:
            return f"No R2 buckets in {aid}."
        return "\n".join(
            f"- {b.get('name')}  location={b.get('location','?')}  created={b.get('creation_date','?')}"
            for b in buckets
        )

    async def create_r2_bucket(self, name: str, location: str = "", account_id: str = "") -> str:
        """Create an R2 bucket.

        :param name: Bucket name (DNS-safe, lowercase, ≤63 chars).
        :param location: Optional jurisdiction (e.g. "wnam", "enam", "weur").
        :param account_id: Falls back to ACCOUNT_ID.
        """
        aid = self._resolve_account(account_id)
        payload: dict[str, Any] = {"name": name}
        if location:
            payload["locationHint"] = location
        body = await self._request("POST", f"/accounts/{aid}/r2/buckets", json=payload)
        b = body.get("result", {})
        return f"Created R2 bucket {b.get('name')}  location={b.get('location','?')}"

    async def delete_r2_bucket(self, name: str, account_id: str = "") -> str:
        """Delete an R2 bucket. Must be empty.

        :param name: Bucket name.
        :param account_id: Falls back to ACCOUNT_ID.
        """
        aid = self._resolve_account(account_id)
        await self._request("DELETE", f"/accounts/{aid}/r2/buckets/{name}")
        return f"Deleted R2 bucket {name}."

    # ── Hyperdrive ─────────────────────────────────────────────────────────

    async def list_hyperdrive_configs(self, account_id: str = "") -> str:
        """List Hyperdrive configs."""
        aid = self._resolve_account(account_id)
        body = await self._request("GET", f"/accounts/{aid}/hyperdrive/configs")
        configs = body.get("result", [])
        if not configs:
            return f"No Hyperdrive configs in {aid}."
        return "\n".join(
            f"- {c.get('id')}  {c.get('name')}  origin={c.get('origin',{}).get('host','?')}"
            for c in configs
        )

    # ── Docs search ────────────────────────────────────────────────────────

    async def search_docs(self, query: str, limit: int = 5) -> str:
        """Search the Cloudflare developer documentation.

        :param query: Free-text query.
        :param limit: Max hits to return.
        """
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT_SEC) as c:
            r = await c.get(_DOCS_SEARCH, params={"q": query})
        if r.status_code >= 400:
            raise RuntimeError(f"Cloudflare docs -> {r.status_code}: {r.text[:300]}")
        body = r.json()
        hits = body if isinstance(body, list) else body.get("hits", [])
        if not hits:
            return "No doc matches."
        rows = []
        for h in hits[:limit]:
            url = h.get("url") or ""
            title = h.get("title", "(untitled)")
            snippet = (h.get("description") or h.get("snippet") or "")[:240]
            rows.append(f"- {title}\n  {url}\n  {snippet}")
        return "\n".join(rows)
