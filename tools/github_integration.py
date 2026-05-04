"""
title: GitHub Integration — PRs, Issues, Files, Reviews
author: local-ai-stack
description: Full GitHub Integration via the REST + GraphQL APIs. Read and write pull requests (list / get / create / merge), issues (list / get / comment / close), file contents, branches, releases, workflow runs. Complements the read-only `github_search` tool. Auth via a fine-grained Personal Access Token or a GitHub App installation token.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

import base64
from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field


_API = "https://api.github.com"


class Tools:
    class Valves(BaseModel):
        ACCESS_TOKEN: str = Field(
            default="",
            description=(
                "GitHub Personal Access Token (fine-grained or classic) or "
                "an installation token. Required for any write operation; "
                "recommended for reads to lift the 60/hr anonymous cap."
            ),
        )
        DEFAULT_REPO: str = Field(
            default="",
            description="Optional default 'owner/repo' used when callers omit it.",
        )
        TIMEOUT_SEC: int = Field(default=30, description="Per-request timeout.")

    def __init__(self) -> None:
        self.valves = self.Valves()

    def _headers(self, accept: str = "application/vnd.github+json") -> dict[str, str]:
        h = {
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "local-ai-stack/1.0",
        }
        if self.valves.ACCESS_TOKEN:
            h["Authorization"] = f"Bearer {self.valves.ACCESS_TOKEN}"
        return h

    def _resolve_repo(self, repo: str | None) -> tuple[str, str]:
        r = (repo or self.valves.DEFAULT_REPO or "").strip()
        if "/" not in r:
            raise ValueError("repo must be 'owner/repo' (or set DEFAULT_REPO).")
        owner, name = r.split("/", 1)
        return owner, name

    async def _request(
        self, method: str, path: str, *,
        params: dict | None = None,
        json: dict | None = None,
        accept: str = "application/vnd.github+json",
    ) -> Any:
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT_SEC) as c:
            r = await c.request(method, f"{_API}{path}", headers=self._headers(accept), params=params, json=json)
        if r.status_code >= 400:
            raise RuntimeError(f"GitHub {method} {path} -> {r.status_code}: {r.text[:300]}")
        if r.status_code == 204 or not r.content:
            return {}
        if accept == "application/vnd.github.raw":
            return r.text
        return r.json()

    # ── Pull requests ──────────────────────────────────────────────────────

    async def list_pull_requests(
        self,
        repo: str = "",
        state: str = "open",
        head: str = "",
        base: str = "",
        per_page: int = 30,
    ) -> str:
        """List pull requests.

        :param repo: 'owner/repo'. Falls back to DEFAULT_REPO.
        :param state: "open" | "closed" | "all".
        :param head: Optional `user:branch` filter.
        :param base: Optional base-branch filter.
        :param per_page: 1-100.
        """
        owner, name = self._resolve_repo(repo)
        params: dict[str, Any] = {"state": state, "per_page": min(max(int(per_page), 1), 100)}
        if head: params["head"] = head
        if base: params["base"] = base
        body = await self._request("GET", f"/repos/{owner}/{name}/pulls", params=params)
        if not body:
            return "No PRs."
        return "\n".join(
            f"- #{p.get('number')}  [{p.get('state')}]  {p.get('title')}  by @{(p.get('user') or {}).get('login','?')}"
            for p in body
        )

    async def get_pull_request(self, number: int, repo: str = "") -> str:
        """Get a single PR with diff stats and review state.

        :param number: PR number.
        :param repo: Falls back to DEFAULT_REPO.
        """
        owner, name = self._resolve_repo(repo)
        pr = await self._request("GET", f"/repos/{owner}/{name}/pulls/{number}")
        out = [
            f"# PR #{pr.get('number')}: {pr.get('title')}",
            f"state: {pr.get('state')}  draft: {pr.get('draft')}  merged: {pr.get('merged')}",
            f"author: @{(pr.get('user') or {}).get('login','?')}",
            f"head: {(pr.get('head') or {}).get('ref','?')}  →  base: {(pr.get('base') or {}).get('ref','?')}",
            f"changes: +{pr.get('additions','?')} -{pr.get('deletions','?')} across {pr.get('changed_files','?')} files",
            f"url: {pr.get('html_url')}",
        ]
        if pr.get("body"):
            out.append(f"\n{pr['body'][:1500]}")
        return "\n".join(out)

    async def create_pull_request(
        self,
        title: str,
        head: str,
        base: str,
        body: str = "",
        draft: bool = False,
        repo: str = "",
    ) -> str:
        """Open a PR.

        :param title: PR title.
        :param head: Source branch (or `user:branch` for cross-fork).
        :param base: Target branch (typically `main`).
        :param body: Description.
        :param draft: Open as draft.
        :param repo: Falls back to DEFAULT_REPO.
        """
        owner, name = self._resolve_repo(repo)
        pr = await self._request(
            "POST", f"/repos/{owner}/{name}/pulls",
            json={"title": title, "head": head, "base": base, "body": body, "draft": bool(draft)},
        )
        return f"Opened PR #{pr.get('number')}  {pr.get('html_url')}"

    async def merge_pull_request(
        self,
        number: int,
        method: str = "squash",
        commit_title: str = "",
        commit_message: str = "",
        repo: str = "",
    ) -> str:
        """Merge a PR.

        :param number: PR number.
        :param method: "merge" | "squash" | "rebase".
        :param commit_title: Optional override for the merge commit title.
        :param commit_message: Optional override for the merge commit body.
        :param repo: Falls back to DEFAULT_REPO.
        """
        if method not in {"merge", "squash", "rebase"}:
            raise ValueError("method must be merge/squash/rebase.")
        owner, name = self._resolve_repo(repo)
        payload: dict[str, Any] = {"merge_method": method}
        if commit_title: payload["commit_title"] = commit_title
        if commit_message: payload["commit_message"] = commit_message
        body = await self._request(
            "PUT", f"/repos/{owner}/{name}/pulls/{number}/merge",
            json=payload,
        )
        return f"Merged PR #{number}  sha={body.get('sha','?')}"

    async def add_pr_comment(self, number: int, body: str, repo: str = "") -> str:
        """Post a top-level review comment on a PR.

        :param number: PR number.
        :param body: Markdown body.
        :param repo: Falls back to DEFAULT_REPO.
        """
        owner, name = self._resolve_repo(repo)
        # Top-level PR comments use the issue comments endpoint.
        out = await self._request(
            "POST", f"/repos/{owner}/{name}/issues/{number}/comments",
            json={"body": body},
        )
        return f"Posted comment {out.get('id')}  {out.get('html_url')}"

    # ── Issues ─────────────────────────────────────────────────────────────

    async def list_issues(
        self,
        repo: str = "",
        state: str = "open",
        labels: str = "",
        per_page: int = 30,
    ) -> str:
        """List issues (excluding PRs — GitHub conflates them at the API
        level, so we filter).

        :param repo: Falls back to DEFAULT_REPO.
        :param state: "open" | "closed" | "all".
        :param labels: Comma-separated labels.
        :param per_page: 1-100.
        """
        owner, name = self._resolve_repo(repo)
        params: dict[str, Any] = {"state": state, "per_page": min(max(int(per_page), 1), 100)}
        if labels: params["labels"] = labels
        body = await self._request("GET", f"/repos/{owner}/{name}/issues", params=params)
        rows = []
        for i in body:
            if "pull_request" in i:
                continue  # skip PRs
            rows.append(
                f"- #{i.get('number')}  [{i.get('state')}]  {i.get('title')}  "
                f"by @{(i.get('user') or {}).get('login','?')}"
            )
        return "\n".join(rows) or "No issues."

    async def get_issue(self, number: int, repo: str = "") -> str:
        """Get a single issue.

        :param number: Issue number.
        :param repo: Falls back to DEFAULT_REPO.
        """
        owner, name = self._resolve_repo(repo)
        i = await self._request("GET", f"/repos/{owner}/{name}/issues/{number}")
        out = [
            f"# Issue #{i.get('number')}: {i.get('title')}",
            f"state: {i.get('state')}  by @{(i.get('user') or {}).get('login','?')}",
            f"url: {i.get('html_url')}",
        ]
        if i.get("labels"):
            out.append("labels: " + ", ".join(l.get("name", "") for l in i["labels"]))
        if i.get("body"):
            out.append(f"\n{i['body'][:1500]}")
        return "\n".join(out)

    async def create_issue(
        self,
        title: str,
        body: str = "",
        labels: list[str] = None,
        assignees: list[str] = None,
        repo: str = "",
    ) -> str:
        """Open an issue.

        :param title: Issue title.
        :param body: Markdown body.
        :param labels: Labels.
        :param assignees: GitHub usernames.
        :param repo: Falls back to DEFAULT_REPO.
        """
        owner, name = self._resolve_repo(repo)
        payload: dict[str, Any] = {"title": title, "body": body}
        if labels: payload["labels"] = labels
        if assignees: payload["assignees"] = assignees
        i = await self._request("POST", f"/repos/{owner}/{name}/issues", json=payload)
        return f"Opened issue #{i.get('number')}  {i.get('html_url')}"

    async def comment_on_issue(self, number: int, body: str, repo: str = "") -> str:
        """Post a comment on an issue.

        :param number: Issue number.
        :param body: Markdown body.
        :param repo: Falls back to DEFAULT_REPO.
        """
        owner, name = self._resolve_repo(repo)
        out = await self._request(
            "POST", f"/repos/{owner}/{name}/issues/{number}/comments",
            json={"body": body},
        )
        return f"Commented on #{number}  {out.get('html_url')}"

    async def close_issue(self, number: int, repo: str = "") -> str:
        """Close an issue.

        :param number: Issue number.
        :param repo: Falls back to DEFAULT_REPO.
        """
        owner, name = self._resolve_repo(repo)
        await self._request(
            "PATCH", f"/repos/{owner}/{name}/issues/{number}",
            json={"state": "closed"},
        )
        return f"Closed issue #{number}."

    # ── Files & branches ───────────────────────────────────────────────────

    async def get_file(self, path: str, repo: str = "", ref: str = "") -> str:
        """Read a file from a repo.

        :param path: File path (e.g. "src/main.py").
        :param repo: Falls back to DEFAULT_REPO.
        :param ref: Branch / tag / SHA. Default = repo's default branch.
        """
        owner, name = self._resolve_repo(repo)
        params = {"ref": ref} if ref else None
        # Ask for the raw representation to skip base64 hassle.
        text = await self._request(
            "GET", f"/repos/{owner}/{name}/contents/{path}",
            params=params,
            accept="application/vnd.github.raw",
        )
        if isinstance(text, str):
            return text
        # Fallback for symlinks / submodules
        return str(text)

    async def write_file(
        self,
        path: str,
        content: str,
        message: str,
        branch: str = "",
        repo: str = "",
    ) -> str:
        """Create or update a file on a branch.

        :param path: File path.
        :param content: New file body (text).
        :param message: Commit message.
        :param branch: Target branch (default: repo default).
        :param repo: Falls back to DEFAULT_REPO.
        """
        owner, name = self._resolve_repo(repo)
        # Need the existing sha if the file already exists.
        sha: str | None = None
        try:
            existing = await self._request(
                "GET", f"/repos/{owner}/{name}/contents/{path}",
                params={"ref": branch} if branch else None,
            )
            if isinstance(existing, dict):
                sha = existing.get("sha")
        except RuntimeError:
            pass
        payload: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content.encode()).decode(),
        }
        if branch: payload["branch"] = branch
        if sha:    payload["sha"] = sha
        out = await self._request(
            "PUT", f"/repos/{owner}/{name}/contents/{path}",
            json=payload,
        )
        commit = out.get("commit", {})
        return f"{'Updated' if sha else 'Created'} {path}  commit={commit.get('sha','?')[:7]}"

    async def list_branches(self, repo: str = "", per_page: int = 30) -> str:
        """List branches.

        :param repo: Falls back to DEFAULT_REPO.
        :param per_page: 1-100.
        """
        owner, name = self._resolve_repo(repo)
        body = await self._request(
            "GET", f"/repos/{owner}/{name}/branches",
            params={"per_page": min(max(int(per_page), 1), 100)},
        )
        return "\n".join(
            f"- {b.get('name')}  protected={b.get('protected')}  sha={(b.get('commit') or {}).get('sha','?')[:7]}"
            for b in body
        ) or "No branches."

    async def create_branch(self, branch: str, from_branch: str = "main", repo: str = "") -> str:
        """Create a new branch off another branch.

        :param branch: New branch name.
        :param from_branch: Source branch.
        :param repo: Falls back to DEFAULT_REPO.
        """
        owner, name = self._resolve_repo(repo)
        # Resolve the source SHA first.
        ref = await self._request("GET", f"/repos/{owner}/{name}/git/ref/heads/{from_branch}")
        sha = (ref.get("object") or {}).get("sha")
        if not sha:
            raise RuntimeError(f"Couldn't resolve {from_branch} in {owner}/{name}.")
        await self._request(
            "POST", f"/repos/{owner}/{name}/git/refs",
            json={"ref": f"refs/heads/{branch}", "sha": sha},
        )
        return f"Created branch {branch} from {from_branch}@{sha[:7]}"

    # ── Workflows ──────────────────────────────────────────────────────────

    async def list_workflow_runs(
        self,
        repo: str = "",
        status: str = "",
        branch: str = "",
        per_page: int = 10,
    ) -> str:
        """Recent CI workflow runs.

        :param repo: Falls back to DEFAULT_REPO.
        :param status: "queued" | "in_progress" | "completed" | "" for any.
        :param branch: Optional branch filter.
        :param per_page: 1-100.
        """
        owner, name = self._resolve_repo(repo)
        params: dict[str, Any] = {"per_page": min(max(int(per_page), 1), 100)}
        if status: params["status"] = status
        if branch: params["branch"] = branch
        body = await self._request("GET", f"/repos/{owner}/{name}/actions/runs", params=params)
        runs = body.get("workflow_runs", [])
        if not runs:
            return "No workflow runs."
        return "\n".join(
            f"- {r.get('id')}  {r.get('name','?')}  [{r.get('status','?')}/{r.get('conclusion','?')}]  "
            f"{r.get('head_branch','?')}@{(r.get('head_sha','?'))[:7]}"
            for r in runs
        )
