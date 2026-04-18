"""
title: GitHub Search
author: local-ai-stack
description: Search GitHub repositories, issues, code, and users. Useful for finding code examples, open-source libraries, and development resources.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import os
import httpx
from pydantic import BaseModel, Field
from typing import Callable, Any, Optional


GITHUB_API = "https://api.github.com"


class Tools:
    class Valves(BaseModel):
        GITHUB_TOKEN: str = Field(
            default_factory=lambda: os.environ.get("GITHUB_TOKEN", ""),
            description="Optional GitHub Personal Access Token for higher rate limits (60/hr without, 5000/hr with)",
        )
        MAX_RESULTS: int = Field(default=5, description="Maximum results to return")

    def __init__(self):
        self.valves = self.Valves()

    def _headers(self) -> dict:
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "local-ai-stack/1.0",
        }
        if self.valves.GITHUB_TOKEN:
            headers["Authorization"] = f"Bearer {self.valves.GITHUB_TOKEN}"
        return headers

    async def search_repositories(
        self,
        query: str,
        language: str = "",
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search GitHub for repositories matching a query.
        :param query: Search terms (e.g. "local AI stack docker", "python web scraper")
        :param language: Optional programming language filter (e.g. "python", "javascript", "go")
        :return: List of matching repositories with stars, description, and URL
        """
        q = query
        if language:
            q += f" language:{language}"

        if __event_emitter__:
            await __event_emitter__(
                {"type": "status", "data": {"description": f"Searching GitHub: {query}", "done": False}}
            )

        try:
            params = {"q": q, "sort": "stars", "order": "desc", "per_page": self.valves.MAX_RESULTS}
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{GITHUB_API}/search/repositories",
                    params=params,
                    headers=self._headers(),
                )
                resp.raise_for_status()
                data = resp.json()

            items = data.get("items", [])
            if not items:
                return f"No GitHub repositories found for: {query}"

            lines = [f"## GitHub Repositories: {query}\n"]
            for r in items:
                stars = r.get("stargazers_count", 0)
                lang = r.get("language") or "N/A"
                desc = r.get("description") or "No description"
                lines.append(f"**{r['full_name']}** ⭐ {stars:,} | {lang}")
                lines.append(f"   {desc}")
                lines.append(f"   {r['html_url']}\n")

            if __event_emitter__:
                await __event_emitter__(
                    {"type": "status", "data": {"description": f"Found {len(items)} repositories", "done": True}}
                )

            return "\n".join(lines)

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 403:
                return "GitHub rate limit exceeded. Add a GITHUB_TOKEN in tool settings for higher limits."
            return f"GitHub API error: {e.response.status_code}"
        except Exception as e:
            return f"GitHub search error: {str(e)}"

    async def search_code(
        self,
        query: str,
        language: str = "",
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search GitHub for code snippets matching a query.
        :param query: Code search terms (e.g. "docker compose ollama open-webui")
        :param language: Optional language filter (e.g. "python", "yaml", "typescript")
        :return: Matching code files with links
        """
        q = query
        if language:
            q += f" language:{language}"

        try:
            params = {"q": q, "per_page": self.valves.MAX_RESULTS}
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{GITHUB_API}/search/code",
                    params=params,
                    headers=self._headers(),
                )
                resp.raise_for_status()
                data = resp.json()

            items = data.get("items", [])
            if not items:
                return f"No code found on GitHub for: {query}"

            lines = [f"## GitHub Code Search: {query}\n"]
            for item in items:
                repo = item.get("repository", {}).get("full_name", "unknown")
                path = item.get("path", "")
                url = item.get("html_url", "")
                lines.append(f"**{repo}** — `{path}`")
                lines.append(f"   {url}\n")

            return "\n".join(lines)

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 403:
                return "GitHub rate limit exceeded. Add a GITHUB_TOKEN in tool settings."
            return f"GitHub API error: {e.response.status_code}"
        except Exception as e:
            return f"GitHub code search error: {str(e)}"

    async def get_repository_info(
        self,
        repo: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get detailed information about a specific GitHub repository.
        :param repo: Repository in 'owner/name' format (e.g. "open-webui/open-webui")
        :return: Repository details including description, stats, and README link
        """
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{GITHUB_API}/repos/{repo}",
                    headers=self._headers(),
                )
                resp.raise_for_status()
                r = resp.json()

            topics = ", ".join(r.get("topics", [])) or "None"
            return (
                f"## GitHub: {r['full_name']}\n"
                f"- **Description:** {r.get('description', 'N/A')}\n"
                f"- **Stars:** {r.get('stargazers_count', 0):,}\n"
                f"- **Forks:** {r.get('forks_count', 0):,}\n"
                f"- **Language:** {r.get('language', 'N/A')}\n"
                f"- **License:** {r.get('license', {}).get('name', 'N/A') if r.get('license') else 'None'}\n"
                f"- **Topics:** {topics}\n"
                f"- **Last Updated:** {r.get('updated_at', 'N/A')}\n"
                f"- **Open Issues:** {r.get('open_issues_count', 0)}\n"
                f"- **URL:** {r.get('html_url', '')}\n"
                f"- **Clone:** `git clone {r.get('clone_url', '')}`"
            )

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return f"Repository not found: {repo}"
            return f"GitHub API error: {e.response.status_code}"
        except Exception as e:
            return f"Error fetching repository: {str(e)}"
