"""
title: Airtable — Bases, Tables, Records, Comments
author: local-ai-stack
description: Read and write Airtable bases. List bases / tables, fetch the schema, list / search / filter records, create / update / delete records, post comments. Auth via a Personal Access Token from https://airtable.com/create/tokens with the appropriate scopes (data.records:read/write, schema.bases:read, ...).
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field


_API = "https://api.airtable.com/v0"
_META = "https://api.airtable.com/v0/meta"


class Tools:
    class Valves(BaseModel):
        ACCESS_TOKEN: str = Field(
            default="",
            description=(
                "Airtable Personal Access Token. Generate at "
                "https://airtable.com/create/tokens with scopes "
                "data.records:read, data.records:write, schema.bases:read, "
                "and (optionally) data.recordComments:read/write."
            ),
        )
        DEFAULT_BASE_ID: str = Field(
            default="",
            description="Optional default base ID (appXXXXXXXXXXXXXX) used when callers omit base_id.",
        )
        TIMEOUT_SEC: int = Field(default=20, description="Per-request timeout.")

    def __init__(self) -> None:
        self.valves = self.Valves()

    def _headers(self) -> dict[str, str]:
        if not self.valves.ACCESS_TOKEN:
            raise PermissionError("Airtable ACCESS_TOKEN is not set on the tool's Valves.")
        return {
            "Authorization": f"Bearer {self.valves.ACCESS_TOKEN}",
            "User-Agent": "local-ai-stack/1.0",
            "Content-Type": "application/json",
        }

    def _resolve_base(self, base_id: str | None) -> str:
        bid = (base_id or self.valves.DEFAULT_BASE_ID or "").strip()
        if not bid:
            raise ValueError("base_id is required (or set DEFAULT_BASE_ID on the tool).")
        return bid

    async def _request(
        self, method: str, url: str, *,
        params: dict | None = None,
        json: dict | None = None,
    ) -> dict:
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT_SEC) as c:
            r = await c.request(method, url, headers=self._headers(), params=params, json=json)
        if r.status_code >= 400:
            raise RuntimeError(f"Airtable {method} {url} -> {r.status_code}: {r.text[:300]}")
        if r.status_code == 204 or not r.content:
            return {}
        return r.json()

    async def list_bases(self) -> str:
        """List every base the access token can see. Returns the base id, name,
        and permission level for each.
        """
        body = await self._request("GET", f"{_META}/bases")
        bases = body.get("bases", [])
        if not bases:
            return "No bases visible to this token."
        rows = [
            f"- {b.get('id')}  {b.get('name')}  ({b.get('permissionLevel')})"
            for b in bases
        ]
        return "Bases:\n" + "\n".join(rows)

    async def list_tables(self, base_id: str = "") -> str:
        """List the tables in a base, with their primary field and view names.

        :param base_id: The base id (appXXX...). Falls back to DEFAULT_BASE_ID.
        """
        bid = self._resolve_base(base_id)
        body = await self._request("GET", f"{_META}/bases/{bid}/tables")
        tables = body.get("tables", [])
        if not tables:
            return f"No tables in base {bid}."
        out = []
        for t in tables:
            views = ", ".join(v.get("name", "") for v in t.get("views", []))
            out.append(
                f"- {t.get('id')}  {t.get('name')}  pk={t.get('primaryFieldId')}  views=[{views}]"
            )
        return f"Tables in {bid}:\n" + "\n".join(out)

    async def get_table_schema(self, table_id: str, base_id: str = "") -> str:
        """Detailed schema for a single table — every field with its type and
        (where applicable) the singleSelect / multipleSelects choice IDs.

        :param table_id: The table id (tblXXX...) or its name.
        :param base_id: The base id. Falls back to DEFAULT_BASE_ID.
        """
        bid = self._resolve_base(base_id)
        body = await self._request("GET", f"{_META}/bases/{bid}/tables")
        for t in body.get("tables", []):
            if t.get("id") == table_id or t.get("name") == table_id:
                return _format_schema(t)
        return f"Table {table_id} not found in base {bid}."

    async def list_records(
        self,
        table: str,
        base_id: str = "",
        view: str = "",
        filter_formula: str = "",
        max_records: int = 25,
        fields: Optional[list[str]] = None,
    ) -> str:
        """List records from a table. Supports view filtering, formula filtering,
        and explicit field selection.

        :param table: Table id (tblXXX...) or human-readable name.
        :param base_id: Base id. Falls back to DEFAULT_BASE_ID.
        :param view: Optional view name to scope to.
        :param filter_formula: Airtable formula language (e.g. "Status = 'Active'").
        :param max_records: Page size cap. Hard ceiling 100.
        :param fields: Restrict the return to these fields only.
        """
        bid = self._resolve_base(base_id)
        params: dict[str, Any] = {"maxRecords": min(max(int(max_records), 1), 100)}
        if view:
            params["view"] = view
        if filter_formula:
            params["filterByFormula"] = filter_formula
        for i, f in enumerate(fields or []):
            params[f"fields[{i}]"] = f
        body = await self._request("GET", f"{_API}/{bid}/{table}", params=params)
        return _format_records(body)

    async def search_records(
        self,
        table: str,
        query: str,
        base_id: str = "",
        max_records: int = 25,
    ) -> str:
        """Full-text search across every text-like field in a table.

        :param table: Table id or name.
        :param query: Substring to search for; case-insensitive.
        :param base_id: Falls back to DEFAULT_BASE_ID.
        :param max_records: Max records to return.
        """
        # Airtable doesn't have a global text search — we approximate with a
        # formula that ORs SEARCH() against every field by referencing
        # `RECORD_ID()` plus a stringified-row trick.
        safe = query.replace("'", "\\'")
        formula = f"FIND(LOWER('{safe}'), LOWER(CONCATENATE({{}}))) > 0"
        # Without a schema lookup we can't enumerate fields cheaply, so use
        # the cheaper REGEX_MATCH against the whole record JSON as a fallback.
        formula = f"OR(REGEX_MATCH(LOWER(CONCATENATE(VALUES())), LOWER('{safe}')))"
        return await self.list_records(table, base_id=base_id, filter_formula=formula, max_records=max_records)

    async def get_record(self, table: str, record_id: str, base_id: str = "") -> str:
        """Fetch a single record by id.

        :param table: Table id or name.
        :param record_id: Record id (recXXX...).
        :param base_id: Falls back to DEFAULT_BASE_ID.
        """
        bid = self._resolve_base(base_id)
        body = await self._request("GET", f"{_API}/{bid}/{table}/{record_id}")
        return _format_records({"records": [body]})

    async def create_record(
        self,
        table: str,
        fields: dict[str, Any],
        base_id: str = "",
    ) -> str:
        """Create a new record.

        :param table: Table id or name.
        :param fields: Dict of {field_name: value}.
        :param base_id: Falls back to DEFAULT_BASE_ID.
        """
        bid = self._resolve_base(base_id)
        body = await self._request(
            "POST", f"{_API}/{bid}/{table}",
            json={"records": [{"fields": fields}]},
        )
        return _format_records(body)

    async def update_record(
        self,
        table: str,
        record_id: str,
        fields: dict[str, Any],
        base_id: str = "",
    ) -> str:
        """PATCH-update an existing record. Only the supplied fields are
        modified — others are left intact.

        :param table: Table id or name.
        :param record_id: Record id (recXXX...).
        :param fields: Field deltas to apply.
        :param base_id: Falls back to DEFAULT_BASE_ID.
        """
        bid = self._resolve_base(base_id)
        body = await self._request(
            "PATCH", f"{_API}/{bid}/{table}/{record_id}",
            json={"fields": fields},
        )
        return _format_records({"records": [body]})

    async def delete_record(self, table: str, record_id: str, base_id: str = "") -> str:
        """Delete a record permanently.

        :param table: Table id or name.
        :param record_id: Record id (recXXX...).
        :param base_id: Falls back to DEFAULT_BASE_ID.
        """
        bid = self._resolve_base(base_id)
        await self._request("DELETE", f"{_API}/{bid}/{table}/{record_id}")
        return f"Deleted {record_id} from {bid}/{table}."

    async def list_comments(
        self,
        table: str,
        record_id: str,
        base_id: str = "",
        page_size: int = 25,
    ) -> str:
        """List comments on a record. Requires the data.recordComments:read scope.

        :param table: Table id or name.
        :param record_id: Record id.
        :param base_id: Falls back to DEFAULT_BASE_ID.
        :param page_size: Comments per page.
        """
        bid = self._resolve_base(base_id)
        body = await self._request(
            "GET", f"{_API}/{bid}/{table}/{record_id}/comments",
            params={"pageSize": min(max(int(page_size), 1), 100)},
        )
        comments = body.get("comments", [])
        if not comments:
            return f"No comments on {record_id}."
        return "\n".join(
            f"- [{c.get('createdTime')}] {(c.get('author') or {}).get('email','?')}: {c.get('text','')}"
            for c in comments
        )

    async def post_comment(
        self,
        table: str,
        record_id: str,
        text: str,
        base_id: str = "",
    ) -> str:
        """Post a comment on a record. Requires data.recordComments:write.

        :param table: Table id or name.
        :param record_id: Record id.
        :param text: Comment body. Supports @mentions of base collaborators.
        :param base_id: Falls back to DEFAULT_BASE_ID.
        """
        bid = self._resolve_base(base_id)
        body = await self._request(
            "POST", f"{_API}/{bid}/{table}/{record_id}/comments",
            json={"text": text},
        )
        return f"Posted comment {body.get('id', '?')} on {record_id}."


def _format_records(body: dict) -> str:
    records = body.get("records", [])
    if not records:
        return "No records."
    out = []
    for r in records:
        fields = r.get("fields", {})
        kv = ", ".join(f"{k}={_short(v)}" for k, v in fields.items())
        out.append(f"- {r.get('id')}  {kv}")
    return "\n".join(out)


def _short(v: Any) -> str:
    s = str(v)
    return s if len(s) <= 80 else s[:77] + "..."


def _format_schema(table: dict) -> str:
    out = [f"# {table.get('name')}  ({table.get('id')})"]
    out.append(f"primary field: {table.get('primaryFieldId')}")
    out.append("\nfields:")
    for f in table.get("fields", []):
        line = f"- {f.get('id')}  {f.get('name')!r}  type={f.get('type')}"
        opts = f.get("options") or {}
        choices = opts.get("choices")
        if choices:
            line += " choices=[" + ", ".join(
                f"{c.get('id')}={c.get('name')!r}" for c in choices
            ) + "]"
        out.append(line)
    return "\n".join(out)
