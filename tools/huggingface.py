"""
title: Hugging Face — Hub Search, Docs, Papers, Spaces
author: local-ai-stack
description: Hit the Hugging Face Hub — search models / datasets / spaces, fetch repo details and READMEs, search docs, and look up papers via the Hub paper index. Most endpoints are public; HF_TOKEN unlocks private repos and gated models.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel, Field


_API = "https://huggingface.co/api"
_HUB = "https://huggingface.co"


class Tools:
    class Valves(BaseModel):
        HF_TOKEN: str = Field(
            default="",
            description=(
                "Optional Hugging Face access token "
                "(https://huggingface.co/settings/tokens). Required only for "
                "private / gated repos and to lift the anonymous rate limit."
            ),
        )
        TIMEOUT_SEC: int = Field(default=20, description="Per-request timeout.")

    def __init__(self) -> None:
        self.valves = self.Valves()

    def _headers(self) -> dict[str, str]:
        h = {"User-Agent": "local-ai-stack/1.0", "Accept": "application/json"}
        if self.valves.HF_TOKEN:
            h["Authorization"] = f"Bearer {self.valves.HF_TOKEN}"
        return h

    async def _get(self, url: str, params: dict | None = None, raw: bool = False):
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT_SEC, follow_redirects=True) as c:
            r = await c.get(url, headers=self._headers(), params=params)
        if r.status_code >= 400:
            raise RuntimeError(f"HF GET {url} -> {r.status_code}: {r.text[:300]}")
        return r.text if raw else r.json()

    async def search_models(
        self,
        query: str = "",
        task: str = "",
        author: str = "",
        sort: str = "downloads",
        limit: int = 10,
    ) -> str:
        """Search models on the Hub.

        :param query: Free-text query against repo names and tags.
        :param task: Optional pipeline tag (e.g. "text-generation", "image-classification").
        :param author: Optional org / user filter (e.g. "meta-llama").
        :param sort: "downloads" | "likes" | "modified" | "trending_score".
        :param limit: Max results.
        """
        params: dict[str, Any] = {"sort": sort, "limit": min(max(int(limit), 1), 50)}
        if query: params["search"] = query
        if task:  params["pipeline_tag"] = task
        if author: params["author"] = author
        body = await self._get(f"{_API}/models", params=params)
        return _format_models(body)

    async def search_datasets(
        self,
        query: str = "",
        sort: str = "downloads",
        limit: int = 10,
    ) -> str:
        """Search datasets on the Hub.

        :param query: Free-text query.
        :param sort: "downloads" | "likes" | "modified".
        :param limit: Max results.
        """
        params: dict[str, Any] = {"sort": sort, "limit": min(max(int(limit), 1), 50)}
        if query: params["search"] = query
        body = await self._get(f"{_API}/datasets", params=params)
        return _format_datasets(body)

    async def search_spaces(
        self,
        query: str = "",
        sdk: str = "",
        limit: int = 10,
    ) -> str:
        """Search Spaces (interactive demos).

        :param query: Free-text query.
        :param sdk: Optional SDK filter ("gradio" | "streamlit" | "static" | "docker").
        :param limit: Max results.
        """
        params: dict[str, Any] = {"limit": min(max(int(limit), 1), 50)}
        if query: params["search"] = query
        if sdk:   params["sdk"] = sdk
        body = await self._get(f"{_API}/spaces", params=params)
        return _format_spaces(body)

    async def repo_details(self, repo_id: str, repo_type: str = "model") -> str:
        """Get the full metadata for a single repo.

        :param repo_id: Like "meta-llama/Llama-3.1-8B-Instruct".
        :param repo_type: "model" | "dataset" | "space".
        """
        if repo_type not in {"model", "dataset", "space"}:
            raise ValueError("repo_type must be 'model', 'dataset', or 'space'.")
        bucket = {"model": "models", "dataset": "datasets", "space": "spaces"}[repo_type]
        body = await self._get(f"{_API}/{bucket}/{repo_id}")
        return _format_repo_detail(body, repo_type)

    async def fetch_readme(self, repo_id: str, repo_type: str = "model", revision: str = "main") -> str:
        """Fetch the README.md of a repo as raw markdown.

        :param repo_id: Repo id.
        :param repo_type: "model" | "dataset" | "space".
        :param revision: Branch / tag / commit. Default "main".
        """
        if repo_type not in {"model", "dataset", "space"}:
            raise ValueError("repo_type must be 'model', 'dataset', or 'space'.")
        bucket = {"model": "", "dataset": "datasets/", "space": "spaces/"}[repo_type]
        url = f"{_HUB}/{bucket}{repo_id}/raw/{revision}/README.md"
        return await self._get(url, raw=True)

    async def search_docs(self, query: str, limit: int = 5) -> str:
        """Search Hugging Face documentation pages.

        :param query: Free-text query.
        :param limit: Max results.
        """
        body = await self._get(
            f"{_HUB}/api/docs/search",
            params={"q": query, "limit": min(max(int(limit), 1), 25)},
        )
        hits = body.get("hits", body) if isinstance(body, dict) else body
        if not hits:
            return "No doc matches."
        rows = []
        for h in hits[:limit]:
            title = h.get("title") or h.get("heading") or "(untitled)"
            url = h.get("url") or h.get("link") or ""
            snippet = (h.get("snippet") or h.get("text") or "")[:200]
            rows.append(f"- {title}\n  {url}\n  {snippet}")
        return "\n".join(rows)

    async def search_papers(self, query: str, limit: int = 10) -> str:
        """Search papers indexed on huggingface.co/papers.

        :param query: Free-text query.
        :param limit: Max results.
        """
        body = await self._get(
            f"{_HUB}/api/papers/search",
            params={"q": query},
        )
        if not isinstance(body, list):
            return "No papers."
        rows = []
        for p in body[:limit]:
            paper = p.get("paper") or p
            arxiv = paper.get("id") or paper.get("arxiv_id") or ""
            title = paper.get("title", "(untitled)")
            rows.append(f"- arxiv:{arxiv}  {title}\n  https://huggingface.co/papers/{arxiv}")
        return "\n".join(rows) if rows else "No papers."

    async def whoami(self) -> str:
        """Return the authenticated user's profile (or an unauth notice)."""
        if not self.valves.HF_TOKEN:
            return "No HF_TOKEN configured — calling anonymously."
        body = await self._get(f"{_API}/whoami-v2")
        name = body.get("name") or body.get("fullname") or "?"
        orgs = ", ".join(o.get("name", "") for o in (body.get("orgs") or []))
        return f"User: {name}\nOrgs: {orgs or '(none)'}"


def _format_models(body: list[dict] | dict) -> str:
    items = body if isinstance(body, list) else body.get("models", [])
    if not items:
        return "No models."
    rows = []
    for m in items:
        rid = m.get("modelId") or m.get("id")
        downloads = m.get("downloads", 0)
        likes = m.get("likes", 0)
        task = m.get("pipeline_tag", "")
        rows.append(f"- {rid}  ↓{downloads:,}  ♥{likes:,}  {task}")
    return "\n".join(rows)


def _format_datasets(body: list[dict] | dict) -> str:
    items = body if isinstance(body, list) else body.get("datasets", [])
    if not items:
        return "No datasets."
    rows = []
    for d in items:
        rid = d.get("id")
        downloads = d.get("downloads", 0)
        likes = d.get("likes", 0)
        rows.append(f"- {rid}  ↓{downloads:,}  ♥{likes:,}")
    return "\n".join(rows)


def _format_spaces(body: list[dict] | dict) -> str:
    items = body if isinstance(body, list) else body.get("spaces", [])
    if not items:
        return "No spaces."
    rows = []
    for s in items:
        rid = s.get("id")
        sdk = s.get("sdk", "")
        likes = s.get("likes", 0)
        rows.append(f"- {rid}  sdk={sdk}  ♥{likes:,}")
    return "\n".join(rows)


def _format_repo_detail(body: dict, kind: str) -> str:
    rid = body.get("modelId") or body.get("id") or "(unknown)"
    out = [f"# {rid}  ({kind})"]
    if body.get("pipeline_tag"):
        out.append(f"task: {body['pipeline_tag']}")
    if body.get("downloads") is not None:
        out.append(f"downloads: {body['downloads']:,}")
    if body.get("likes") is not None:
        out.append(f"likes: {body['likes']:,}")
    if body.get("library_name"):
        out.append(f"library: {body['library_name']}")
    tags = body.get("tags") or []
    if tags:
        out.append("tags: " + ", ".join(tags[:25]))
    siblings = body.get("siblings") or []
    if siblings:
        out.append(f"\nfiles ({len(siblings)}):")
        for s in siblings[:20]:
            out.append(f"  - {s.get('rfilename')}")
        if len(siblings) > 20:
            out.append(f"  … +{len(siblings) - 20} more")
    return "\n".join(out)
