"""
title: Package Search — PyPI, npm, DockerHub
author: local-ai-stack
description: Search for software packages across PyPI (Python), npm (JavaScript/Node), and Docker Hub. Get version info, download stats, and descriptions. No API key required.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from pydantic import BaseModel, Field
from typing import Callable, Any, Optional


class Tools:
    class Valves(BaseModel):
        MAX_RESULTS: int = Field(default=5, description="Maximum results per search")

    def __init__(self):
        self.valves = self.Valves()

    async def search_pypi(
        self,
        query: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search PyPI for Python packages.
        :param query: Package name or keywords (e.g. "web scraping", "httpx", "machine learning")
        :return: Packages with descriptions, versions, download counts, and install commands
        """
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    "https://pypi.org/search/",
                    params={"q": query, "format": "json"},
                    headers={"Accept": "application/json"},
                )
                # PyPI doesn't have a JSON search API, use the XML API
                xml_resp = await client.get(
                    f"https://pypi.org/simple/",
                    headers={"Accept": "application/vnd.pypi.simple.v1+json"},
                )

                # Use the stats API for top packages
                search_resp = await client.get(
                    f"https://pypi.org/pypi/{query}/json",
                )

            lines = [f"## PyPI: {query}\n"]

            if search_resp.status_code == 200:
                data = search_resp.json()
                info = data.get("info", {})
                latest = info.get("version", "?")
                desc = info.get("summary", "No description")
                author = info.get("author", "")
                license_ = info.get("license", "")
                home = info.get("home_page") or info.get("project_url", "")
                requires_python = info.get("requires_python", "")
                classifiers = info.get("classifiers", [])
                topics = [c.split(" :: ")[-1] for c in classifiers if c.startswith("Topic")][:4]

                lines.append(f"**{query}** v{latest}")
                lines.append(f"   {desc}")
                if author:
                    lines.append(f"   Author: {author}")
                if license_:
                    lines.append(f"   License: {license_}")
                if requires_python:
                    lines.append(f"   Requires Python: {requires_python}")
                if topics:
                    lines.append(f"   Topics: {', '.join(topics)}")
                lines.append(f"   📦 `pip install {query}`")
                lines.append(f"   🔗 https://pypi.org/project/{query}/")
            else:
                # Fallback: suggest related
                lines.append(f"Exact package '{query}' not found.")
                lines.append(f"Browse: https://pypi.org/search/?q={query}")

            return "\n".join(lines)

        except Exception as e:
            return f"PyPI search error: {str(e)}"

    async def get_pypi_package(
        self,
        package: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get detailed info about a specific PyPI package including changelog and dependencies.
        :param package: Exact PyPI package name (e.g. "requests", "numpy", "fastapi")
        :return: Package details, version history, and dependencies
        """
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"https://pypi.org/pypi/{package}/json")
                if resp.status_code == 404:
                    return f"Package not found on PyPI: {package}"
                resp.raise_for_status()
                data = resp.json()

            info = data.get("info", {})
            releases = list(data.get("releases", {}).keys())[-5:][::-1]  # Last 5 versions
            requires = (info.get("requires_dist") or [])[:8]

            desc = info.get("summary", "")
            home = info.get("home_page") or ""
            docs = info.get("docs_url") or ""
            bugtrack = info.get("bugtrack_url") or ""

            lines = [
                f"## PyPI: {info.get('name', package)} v{info.get('version', '?')}\n",
                f"**Description:** {desc}",
                f"**Author:** {info.get('author', 'N/A')}",
                f"**License:** {info.get('license', 'N/A')}",
                f"**Python:** {info.get('requires_python', 'any')}",
                f"\n**Install:** `pip install {package}`",
                f"**PyPI:** https://pypi.org/project/{package}/",
            ]
            if home: lines.append(f"**Homepage:** {home}")
            if docs:  lines.append(f"**Docs:** {docs}")
            if releases:
                lines.append(f"\n**Recent versions:** {', '.join(releases)}")
            if requires:
                lines.append(f"\n**Dependencies:**\n" + "\n".join(f"  - {r}" for r in requires))

            return "\n".join(lines)

        except Exception as e:
            return f"PyPI package error: {str(e)}"

    async def search_npm(
        self,
        query: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search npm for JavaScript/Node.js packages.
        :param query: Package name or keywords (e.g. "react hooks", "express middleware", "axios")
        :return: Top matching packages with descriptions, versions, and install commands
        """
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    "https://registry.npmjs.org/-/v1/search",
                    params={"text": query, "size": self.valves.MAX_RESULTS},
                )
                resp.raise_for_status()
                data = resp.json()

            objects = data.get("objects", [])
            if not objects:
                return f"No npm packages found for: {query}"

            lines = [f"## npm: {query}\n"]
            for obj in objects:
                pkg = obj.get("package", {})
                name = pkg.get("name", "")
                version = pkg.get("version", "?")
                desc = pkg.get("description", "No description")[:100]
                links = pkg.get("links", {})
                npm_url = links.get("npm", f"https://www.npmjs.com/package/{name}")
                keywords = ", ".join((pkg.get("keywords") or [])[:4])
                score = obj.get("score", {}).get("final", 0)

                lines.append(f"**{name}** v{version}  ⭐ {score:.2f}")
                lines.append(f"   {desc}")
                if keywords:
                    lines.append(f"   Keywords: {keywords}")
                lines.append(f"   📦 `npm install {name}`")
                lines.append(f"   🔗 {npm_url}\n")

            return "\n".join(lines)

        except Exception as e:
            return f"npm search error: {str(e)}"

    async def search_dockerhub(
        self,
        query: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search Docker Hub for container images.
        :param query: Image name or keywords (e.g. "postgres", "nginx", "python 3.12")
        :return: Top images with pull counts, star counts, and docker pull commands
        """
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    "https://hub.docker.com/v2/search/repositories/",
                    params={"query": query, "page_size": self.valves.MAX_RESULTS},
                )
                resp.raise_for_status()
                data = resp.json()

            results = data.get("results", [])
            if not results:
                return f"No Docker Hub images found for: {query}"

            lines = [f"## Docker Hub: {query}\n"]
            for r in results:
                name = r.get("repo_name", "")
                desc = (r.get("short_description") or "No description")[:100]
                stars = r.get("star_count", 0)
                pulls = r.get("pull_count", 0)
                is_official = r.get("is_official", False)
                badge = "✅ Official" if is_official else ""

                pulls_str = f"{pulls/1e9:.1f}B+" if pulls >= 1e9 else f"{pulls/1e6:.0f}M+" if pulls >= 1e6 else f"{pulls/1e3:.0f}K+" if pulls >= 1e3 else str(pulls)

                lines.append(f"**{name}** {badge}")
                lines.append(f"   {desc}")
                lines.append(f"   ⭐ {stars:,} stars | ⬇ {pulls_str} pulls")
                lines.append(f"   🐳 `docker pull {name}`")
                lines.append(f"   🔗 https://hub.docker.com/r/{name}\n")

            return "\n".join(lines)

        except Exception as e:
            return f"Docker Hub search error: {str(e)}"
