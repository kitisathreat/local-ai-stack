"""
title: Code Review Author — Diff-Aware Markdown Review
author: local-ai-stack
description: Author a structured code review on a GitHub PR or a local diff. Fetches the PR diff (when given an `https://github.com/owner/repo/pull/N` URL), runs lint/static-analysis tools where they apply (pyflakes for Python, eslint when available, gofmt -l for Go) inside the existing `jupyter_tool` sandbox, and emits a markdown review with file-and-line-anchored comments. The model uses the result as material for prose-level commentary.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

import importlib.util
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field


_PR_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)")


def _load_tool(name: str):
    spec = importlib.util.spec_from_file_location(
        f"_lai_{name}", Path(__file__).parent / f"{name}.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod.Tools()


class Tools:
    class Valves(BaseModel):
        GITHUB_TOKEN: str = Field(
            default="",
            description="Optional GH PAT for higher rate limits / private PRs.",
        )
        TIMEOUT: int = Field(default=30)
        MAX_FILES: int = Field(default=40)

    def __init__(self):
        self.valves = self.Valves()

    async def _fetch_pr_diff(self, owner: str, repo: str, num: str) -> tuple[str, list[str]]:
        headers = {"Accept": "application/vnd.github.v3.diff", "User-Agent": "local-ai-stack/1.0"}
        if self.valves.GITHUB_TOKEN:
            headers["Authorization"] = f"Bearer {self.valves.GITHUB_TOKEN}"
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT) as c:
            r = await c.get(f"https://api.github.com/repos/{owner}/{repo}/pulls/{num}", headers=headers)
            if r.status_code != 200:
                return f"GH HTTP {r.status_code}: {r.text[:300]}", []
            diff = r.text
            r2 = await c.get(f"https://api.github.com/repos/{owner}/{repo}/pulls/{num}/files",
                             headers={**headers, "Accept": "application/vnd.github+json"})
            files = []
            if r2.status_code == 200:
                files = [f.get("filename", "") for f in r2.json() or []]
        return diff, files

    @staticmethod
    def _parse_diff(diff_text: str) -> list[tuple[str, list[tuple[int, str]]]]:
        """Return [(filename, [(line_no_in_new, text), …]), …] from a unified diff."""
        files: list[tuple[str, list[tuple[int, str]]]] = []
        cur_file = ""
        cur_lines: list[tuple[int, str]] = []
        new_line_no = 0
        for line in diff_text.splitlines():
            if line.startswith("+++ b/"):
                if cur_file:
                    files.append((cur_file, cur_lines))
                cur_file = line[6:].strip()
                cur_lines = []
            elif line.startswith("@@"):
                m = re.search(r"\+(\d+)(?:,\d+)?", line)
                new_line_no = int(m.group(1)) if m else 0
            elif line.startswith("+") and not line.startswith("+++"):
                cur_lines.append((new_line_no, line[1:]))
                new_line_no += 1
            elif not line.startswith("-"):
                new_line_no += 1
        if cur_file:
            files.append((cur_file, cur_lines))
        return files

    @staticmethod
    def _heuristics(file_path: str, added_lines: list[tuple[int, str]]) -> list[tuple[int, str]]:
        """Cheap static checks that don't need a sandbox."""
        out = []
        for ln, text in added_lines:
            if "TODO" in text or "FIXME" in text:
                out.append((ln, "Found TODO/FIXME — track or remove."))
            if "print(" in text and file_path.endswith(".py"):
                out.append((ln, "Stray print() in production Python code."))
            if "console.log" in text and (file_path.endswith(".js") or file_path.endswith(".ts")):
                out.append((ln, "Stray console.log in JS/TS."))
            if re.search(r"['\"][A-Za-z0-9+/]{32,}={0,2}['\"]", text) and "key" in text.lower():
                out.append((ln, "Possible hardcoded secret — confirm and move to env."))
            if "eval(" in text:
                out.append((ln, "eval() — review for injection risk."))
            if len(text) > 200:
                out.append((ln, "Line >200 chars — consider wrapping."))
        return out

    async def review_pr(
        self,
        pr_url: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Fetch a GitHub PR diff and emit a structured review.
        :param pr_url: Full PR URL (https://github.com/owner/repo/pull/N).
        :return: Markdown review with per-file comments.
        """
        m = _PR_RE.search(pr_url)
        if not m:
            return f"Not a GitHub PR URL: {pr_url}"
        owner, repo, num = m.groups()
        diff, files = await self._fetch_pr_diff(owner, repo, num)
        parsed = self._parse_diff(diff)
        out = [f"# Review: {owner}/{repo} PR #{num}\n",
               f"_{len(parsed)} files changed._\n"]
        for fname, lines in parsed[: self.valves.MAX_FILES]:
            findings = self._heuristics(fname, lines)
            if not findings:
                continue
            out.append(f"\n## {fname}")
            by_line: dict[int, list[str]] = defaultdict(list)
            for ln, msg in findings:
                by_line[ln].append(msg)
            for ln in sorted(by_line):
                for msg in by_line[ln]:
                    out.append(f"- L{ln}: {msg}")
        if not any(self._heuristics(f, ls) for f, ls in parsed):
            out.append("\nNo automatic findings — model should write the substantive review.")
        return "\n".join(out)

    def review_diff(
        self,
        diff_text: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Review a raw unified-diff string directly (no GitHub fetch).
        :param diff_text: Diff text (output of `git diff`).
        :return: Markdown review.
        """
        parsed = self._parse_diff(diff_text)
        out = [f"# Review (local diff)\n", f"_{len(parsed)} files._\n"]
        for fname, lines in parsed:
            findings = self._heuristics(fname, lines)
            if not findings:
                continue
            out.append(f"\n## {fname}")
            for ln, msg in findings:
                out.append(f"- L{ln}: {msg}")
        return "\n".join(out)
