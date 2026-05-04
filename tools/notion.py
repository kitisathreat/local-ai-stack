"""
title: Notion — Search, Pages, Databases, Comments
author: local-ai-stack
description: Talk to Notion's REST API. Search pages and databases, fetch a page or database, query a database with filters, append blocks to a page, create new pages or comments. Auth via an Internal Integration Secret (https://www.notion.so/profile/integrations) — invite the integration to each page/db you want it to see.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field


_API = "https://api.notion.com/v1"
_NOTION_VERSION = "2022-06-28"


class Tools:
    class Valves(BaseModel):
        ACCESS_TOKEN: str = Field(
            default="",
            description=(
                "Notion Internal Integration Secret. Create at "
                "https://www.notion.so/profile/integrations and connect the "
                "integration to each page or database you want this tool to "
                "read. The secret starts with `secret_` or `ntn_`."
            ),
        )
        TIMEOUT_SEC: int = Field(default=20, description="Per-request timeout.")

    def __init__(self) -> None:
        self.valves = self.Valves()

    def _headers(self) -> dict[str, str]:
        if not self.valves.ACCESS_TOKEN:
            raise PermissionError("Notion ACCESS_TOKEN is not set on the tool's Valves.")
        return {
            "Authorization": f"Bearer {self.valves.ACCESS_TOKEN}",
            "Notion-Version": _NOTION_VERSION,
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
            raise RuntimeError(f"Notion {method} {path} -> {r.status_code}: {r.text[:300]}")
        if r.status_code == 204 or not r.content:
            return {}
        return r.json()

    async def search(
        self,
        query: str,
        filter_type: str = "",
        page_size: int = 10,
    ) -> str:
        """Search across the workspaces the integration can see.

        :param query: Free-text search.
        :param filter_type: "page" | "database" to scope the result type.
        :param page_size: 1-100.
        """
        body: dict[str, Any] = {"query": query, "page_size": min(max(int(page_size), 1), 100)}
        if filter_type in {"page", "database"}:
            body["filter"] = {"property": "object", "value": filter_type}
        out = await self._request("POST", "/search", json=body)
        return _format_search(out)

    async def get_page(self, page_id: str) -> str:
        """Fetch a page's metadata + all top-level blocks (paragraphs, headings,
        list items, code, callouts) flattened into plain text.

        :param page_id: Notion page id (UUID, with or without hyphens).
        """
        page = await self._request("GET", f"/pages/{page_id}")
        blocks = await self._request("GET", f"/blocks/{page_id}/children", params={"page_size": 100})
        title = _page_title(page)
        body = _format_blocks(blocks.get("results", []))
        return f"# {title}\n\n{body}\n\n(URL: {page.get('url','')})"

    async def fetch_url(self, url: str) -> str:
        """Resolve a notion.so URL or page id and return the page's contents.
        Convenience wrapper over get_page that strips the URL parameters.

        :param url: A notion.so/... URL or a bare page id.
        """
        cleaned = url.rsplit("/", 1)[-1].split("?", 1)[0]
        # Notion page slugs end with the 32-char id (no hyphens).
        candidate = cleaned[-32:] if len(cleaned) >= 32 else cleaned
        return await self.get_page(candidate)

    async def get_database(self, database_id: str) -> str:
        """Fetch a database's schema (title, properties, types).

        :param database_id: Database id.
        """
        db = await self._request("GET", f"/databases/{database_id}")
        return _format_database(db)

    async def query_database(
        self,
        database_id: str,
        filter_json: Optional[dict] = None,
        sorts: Optional[list[dict]] = None,
        page_size: int = 25,
    ) -> str:
        """Query a database. ``filter_json`` and ``sorts`` follow the Notion
        REST shape — see https://developers.notion.com/reference/post-database-query.

        :param database_id: Database id.
        :param filter_json: A Notion filter object (or null).
        :param sorts: A list of sort objects.
        :param page_size: 1-100.
        """
        body: dict[str, Any] = {"page_size": min(max(int(page_size), 1), 100)}
        if filter_json:
            body["filter"] = filter_json
        if sorts:
            body["sorts"] = sorts
        out = await self._request("POST", f"/databases/{database_id}/query", json=body)
        results = out.get("results", [])
        if not results:
            return "No rows."
        rows = []
        for p in results:
            title = _page_title(p)
            rows.append(f"- {p.get('id')}  {title}")
        return "\n".join(rows)

    async def create_page(
        self,
        parent_id: str,
        title: str,
        body_markdown: str = "",
        parent_type: str = "page_id",
    ) -> str:
        """Create a child page under a parent page or database.

        :param parent_id: Parent page id or database id.
        :param title: Page title.
        :param body_markdown: Optional markdown-style body. Each line becomes
            a paragraph; lines starting with `# `/`## `/`### ` become headings;
            lines starting with `- ` become bulleted list items.
        :param parent_type: "page_id" or "database_id".
        """
        if parent_type not in {"page_id", "database_id"}:
            raise ValueError("parent_type must be 'page_id' or 'database_id'.")
        children = _markdown_to_blocks(body_markdown) if body_markdown else []
        if parent_type == "database_id":
            payload = {
                "parent": {"database_id": parent_id},
                "properties": {"Name": {"title": [{"text": {"content": title}}]}},
                "children": children,
            }
        else:
            payload = {
                "parent": {"page_id": parent_id},
                "properties": {"title": {"title": [{"text": {"content": title}}]}},
                "children": children,
            }
        out = await self._request("POST", "/pages", json=payload)
        return f"Created page {out.get('id')}  ({out.get('url','')})"

    async def append_blocks(self, page_id: str, body_markdown: str) -> str:
        """Append markdown-derived blocks to an existing page.

        :param page_id: Page id.
        :param body_markdown: Markdown to convert (see create_page).
        """
        children = _markdown_to_blocks(body_markdown)
        if not children:
            return "Nothing to append."
        out = await self._request("PATCH", f"/blocks/{page_id}/children", json={"children": children})
        return f"Appended {len(out.get('results', []))} blocks to {page_id}."

    async def get_comments(self, block_id: str, page_size: int = 25) -> str:
        """List comments on a block or page.

        :param block_id: Block or page id.
        :param page_size: 1-100.
        """
        out = await self._request(
            "GET", "/comments",
            params={"block_id": block_id, "page_size": min(max(int(page_size), 1), 100)},
        )
        results = out.get("results", [])
        if not results:
            return "No comments."
        return "\n".join(
            f"- [{c.get('created_time')}] {_rich(c.get('rich_text', []))}"
            for c in results
        )

    async def create_comment(self, page_id: str, text: str) -> str:
        """Add a top-level comment to a page.

        :param page_id: Page id.
        :param text: Plain-text comment body.
        """
        out = await self._request(
            "POST", "/comments",
            json={
                "parent": {"page_id": page_id},
                "rich_text": [{"text": {"content": text}}],
            },
        )
        return f"Posted comment {out.get('id', '?')} on {page_id}."


# ── Formatting helpers ────────────────────────────────────────────────────────


def _rich(parts: list[dict]) -> str:
    return "".join((p.get("plain_text") or "") for p in (parts or []))


def _page_title(page: dict) -> str:
    props = page.get("properties") or {}
    # Pages-as-database-rows expose the title as the property whose type=='title'.
    for prop in props.values():
        if prop.get("type") == "title":
            return _rich(prop.get("title", [])) or "(untitled)"
    return _rich(props.get("title", {}).get("title", [])) or "(untitled)"


def _format_search(body: dict) -> str:
    results = body.get("results", [])
    if not results:
        return "No matches."
    out = []
    for r in results:
        kind = r.get("object")
        title = _page_title(r) if kind == "page" else _rich(r.get("title", []))
        out.append(f"- [{kind}] {r.get('id')}  {title or '(untitled)'}  {r.get('url','')}")
    return "\n".join(out)


def _format_database(db: dict) -> str:
    title = _rich(db.get("title", []))
    props = db.get("properties", {})
    out = [f"# {title}  ({db.get('id')})", f"URL: {db.get('url','')}", "", "Properties:"]
    for name, p in props.items():
        out.append(f"- {name!r}  type={p.get('type')}")
    return "\n".join(out)


def _format_blocks(blocks: list[dict]) -> str:
    lines: list[str] = []
    for b in blocks:
        t = b.get("type")
        data = b.get(t) or {}
        rt = _rich(data.get("rich_text", []))
        if t == "paragraph":
            lines.append(rt)
        elif t == "heading_1":
            lines.append(f"# {rt}")
        elif t == "heading_2":
            lines.append(f"## {rt}")
        elif t == "heading_3":
            lines.append(f"### {rt}")
        elif t == "bulleted_list_item":
            lines.append(f"- {rt}")
        elif t == "numbered_list_item":
            lines.append(f"1. {rt}")
        elif t == "to_do":
            check = "x" if data.get("checked") else " "
            lines.append(f"- [{check}] {rt}")
        elif t == "callout":
            lines.append(f"> {rt}")
        elif t == "code":
            lang = data.get("language", "")
            lines.append(f"```{lang}\n{rt}\n```")
        elif t == "quote":
            lines.append(f"> {rt}")
        else:
            if rt:
                lines.append(rt)
    return "\n\n".join(line for line in lines if line)


def _markdown_to_blocks(md: str) -> list[dict]:
    out: list[dict] = []
    for raw in md.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        if line.startswith("### "):
            out.append(_block("heading_3", line[4:]))
        elif line.startswith("## "):
            out.append(_block("heading_2", line[3:]))
        elif line.startswith("# "):
            out.append(_block("heading_1", line[2:]))
        elif line.startswith("- [ ] "):
            out.append(_block("to_do", line[6:], extra={"checked": False}))
        elif line.startswith("- [x] "):
            out.append(_block("to_do", line[6:], extra={"checked": True}))
        elif line.startswith("- "):
            out.append(_block("bulleted_list_item", line[2:]))
        elif line.startswith("> "):
            out.append(_block("quote", line[2:]))
        else:
            out.append(_block("paragraph", line))
    return out


def _block(kind: str, text: str, extra: dict | None = None) -> dict:
    inner = {"rich_text": [{"text": {"content": text}}]}
    if extra:
        inner.update(extra)
    return {"object": "block", "type": kind, kind: inner}
