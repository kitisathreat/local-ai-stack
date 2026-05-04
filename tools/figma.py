"""
title: Figma — Files, Frames, Comments, Components
author: local-ai-stack
description: Read Figma files via the REST API — file structure, specific node JSON, image renders (PNG/SVG/PDF), comments, projects, team styles. Auth via a Personal Access Token (https://www.figma.com/developers/api#access-tokens). The tool covers the read-only and comment-write surface; full editing requires the Plugin API which is not network-accessible.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field


_API = "https://api.figma.com/v1"


class Tools:
    class Valves(BaseModel):
        ACCESS_TOKEN: str = Field(
            default="",
            description=(
                "Figma Personal Access Token from "
                "https://www.figma.com/developers/api#access-tokens. The "
                "scopes you select on the token determine what the tool can "
                "read or write."
            ),
        )
        TIMEOUT_SEC: int = Field(default=20, description="Per-request timeout.")

    def __init__(self) -> None:
        self.valves = self.Valves()

    def _headers(self) -> dict[str, str]:
        if not self.valves.ACCESS_TOKEN:
            raise PermissionError("Figma ACCESS_TOKEN is not set.")
        return {
            "X-Figma-Token": self.valves.ACCESS_TOKEN,
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
            raise RuntimeError(f"Figma {method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}

    @staticmethod
    def _file_key(file_or_url: str) -> str:
        """Accept either a bare key (`abc123`) or any figma.com/(file|design|board)/<key>/... URL."""
        s = file_or_url.strip()
        if "figma.com/" not in s:
            return s
        for marker in ("/file/", "/design/", "/board/", "/proto/"):
            if marker in s:
                tail = s.split(marker, 1)[1]
                return tail.split("/", 1)[0].split("?", 1)[0]
        return s

    async def get_file(self, file_or_url: str, depth: int = 2) -> str:
        """Fetch a file's structure (pages and frames). Returns a tree
        flattened for human reading.

        :param file_or_url: File key or any figma.com URL.
        :param depth: How deep into the document tree to descend (1 = pages only).
        """
        key = self._file_key(file_or_url)
        body = await self._request("GET", f"/files/{key}", params={"depth": int(depth)})
        return _format_file(body)

    async def get_nodes(self, file_or_url: str, node_ids: list[str]) -> str:
        """Fetch the JSON for specific nodes within a file.

        :param file_or_url: File key or URL.
        :param node_ids: One or more node ids (e.g. "12:34").
        """
        if not node_ids:
            raise ValueError("node_ids must contain at least one id.")
        key = self._file_key(file_or_url)
        body = await self._request(
            "GET", f"/files/{key}/nodes",
            params={"ids": ",".join(node_ids)},
        )
        nodes = body.get("nodes", {})
        out = []
        for nid, payload in nodes.items():
            doc = (payload or {}).get("document") or {}
            out.append(f"# Node {nid}: {doc.get('name', '?')}  type={doc.get('type', '?')}")
            out.append(_format_node(doc, indent=0, max_depth=2))
        return "\n".join(out) or "No nodes returned."

    async def render_images(
        self,
        file_or_url: str,
        node_ids: list[str],
        format: str = "png",
        scale: float = 1.0,
    ) -> str:
        """Generate signed image URLs for one or more nodes.

        :param file_or_url: File key or URL.
        :param node_ids: Node ids to render.
        :param format: "png" | "jpg" | "svg" | "pdf".
        :param scale: Render scale (0.01-4.0).
        """
        if format not in {"png", "jpg", "svg", "pdf"}:
            raise ValueError("format must be one of png, jpg, svg, pdf.")
        key = self._file_key(file_or_url)
        body = await self._request(
            "GET", f"/images/{key}",
            params={
                "ids": ",".join(node_ids),
                "format": format,
                "scale": max(0.01, min(float(scale), 4.0)),
            },
        )
        images = body.get("images", {})
        if not images:
            return "No images rendered."
        return "\n".join(f"- {nid}: {url or '(failed)'}" for nid, url in images.items())

    async def list_comments(self, file_or_url: str) -> str:
        """List comments on a file.

        :param file_or_url: File key or URL.
        """
        key = self._file_key(file_or_url)
        body = await self._request("GET", f"/files/{key}/comments")
        comments = body.get("comments", [])
        if not comments:
            return "No comments."
        out = []
        for c in comments:
            user = (c.get("user") or {}).get("handle", "?")
            msg = c.get("message", "")
            out.append(f"- [{c.get('created_at')}] @{user} {c.get('id')}: {msg}")
        return "\n".join(out)

    async def post_comment(
        self,
        file_or_url: str,
        message: str,
        node_id: str = "",
        x: float = 0.0,
        y: float = 0.0,
    ) -> str:
        """Post a comment on a file. Optionally pinned to a node coordinate.

        :param file_or_url: File key or URL.
        :param message: Comment body.
        :param node_id: Optional node id to anchor the comment to.
        :param x: X coordinate within the node (when node_id is set).
        :param y: Y coordinate within the node.
        """
        key = self._file_key(file_or_url)
        payload: dict[str, Any] = {"message": message}
        if node_id:
            payload["client_meta"] = {"node_id": node_id, "node_offset": {"x": x, "y": y}}
        body = await self._request("POST", f"/files/{key}/comments", json=payload)
        return f"Posted comment {body.get('id', '?')} on {key}."

    async def get_team_projects(self, team_id: str) -> str:
        """List projects belonging to a team.

        :param team_id: Figma team id (visible in the team URL).
        """
        body = await self._request("GET", f"/teams/{team_id}/projects")
        projects = body.get("projects", [])
        if not projects:
            return "No projects."
        return "\n".join(f"- {p.get('id')}  {p.get('name')}" for p in projects)

    async def get_project_files(self, project_id: str) -> str:
        """List files in a project.

        :param project_id: Project id.
        """
        body = await self._request("GET", f"/projects/{project_id}/files")
        files = body.get("files", [])
        if not files:
            return "No files."
        return "\n".join(
            f"- {f.get('key')}  {f.get('name')}  modified={f.get('last_modified','')}"
            for f in files
        )

    async def get_components(self, file_or_url: str) -> str:
        """List local components published from a file.

        :param file_or_url: File key or URL.
        """
        key = self._file_key(file_or_url)
        body = await self._request("GET", f"/files/{key}/components")
        comps = (body.get("meta") or {}).get("components") or body.get("components") or []
        if not comps:
            return "No components."
        return "\n".join(
            f"- {c.get('node_id')}  {c.get('name')}  description={(c.get('description') or '')[:120]}"
            for c in comps
        )


def _format_file(body: dict) -> str:
    name = body.get("name", "(untitled)")
    last = body.get("lastModified", "")
    out = [f"# {name}", f"last_modified: {last}", ""]
    doc = body.get("document") or {}
    for page in doc.get("children", []) or []:
        out.append(f"## {page.get('name')}  ({page.get('id')})")
        for child in (page.get("children") or [])[:50]:
            out.append(f"  - {child.get('id')}  {child.get('type')}  {child.get('name')}")
    return "\n".join(out)


def _format_node(node: dict, indent: int, max_depth: int) -> str:
    pad = "  " * indent
    line = f"{pad}- {node.get('id')}  {node.get('type')}  {node.get('name', '')}"
    if max_depth <= 0:
        return line
    children = node.get("children") or []
    if not children:
        return line
    sub = "\n".join(_format_node(c, indent + 1, max_depth - 1) for c in children[:30])
    if len(children) > 30:
        sub += f"\n{pad}  … +{len(children) - 30} more"
    return f"{line}\n{sub}"
