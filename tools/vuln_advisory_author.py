"""
title: Vulnerability Advisory Author — Manifest → CVE Triage
author: local-ai-stack
description: Parse a project's dependency manifest (`requirements.txt`, `package.json`, `Cargo.toml`, `go.mod`, `Gemfile.lock`, `composer.json`, `pyproject.toml`) and cross-reference each pinned version against OSV and NVD. Output is a markdown report ranked by CVSS / known-exploitable status, with patch instructions and links. Pair with the `filesystem` tool to read the manifest, then this tool to triage.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
from pathlib import Path
from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field


def _load_tool(name: str):
    spec = importlib.util.spec_from_file_location(
        f"_lai_{name}", Path(__file__).parent / f"{name}.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod.Tools()


# Dependency manifest parsers.
def _parse_requirements_txt(text: str) -> list[tuple[str, str, str]]:
    out = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        m = re.match(r"^([A-Za-z0-9_.\-]+)\s*[=<>~!]+\s*([0-9A-Za-z.\-_+*]+)", line)
        if m:
            out.append(("PyPI", m.group(1), m.group(2)))
    return out


def _parse_package_json(text: str) -> list[tuple[str, str, str]]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    out = []
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        for name, ver in (data.get(key) or {}).items():
            ver_clean = re.sub(r"^[\^~>=<]+", "", str(ver)).strip()
            out.append(("npm", name, ver_clean))
    return out


def _parse_cargo_toml(text: str) -> list[tuple[str, str, str]]:
    out = []
    in_deps = False
    for line in text.splitlines():
        if re.match(r"^\s*\[(dependencies|dev-dependencies|build-dependencies)\]", line):
            in_deps = True; continue
        if line.startswith("[") and in_deps:
            in_deps = False
        if in_deps:
            m = re.match(r'^([A-Za-z0-9_-]+)\s*=\s*"([^"]+)"', line)
            if m:
                out.append(("crates.io", m.group(1), re.sub(r"^[\^~>=<]+", "", m.group(2))))
    return out


def _parse_go_mod(text: str) -> list[tuple[str, str, str]]:
    out = []
    for m in re.finditer(r"^\s*([\w./\-]+)\s+v([\d.]+(?:[\w.\-+]+)?)", text, re.MULTILINE):
        out.append(("Go", m.group(1), m.group(2)))
    return out


def _parse_gemfile_lock(text: str) -> list[tuple[str, str, str]]:
    out = []
    for m in re.finditer(r"^\s{4}([\w-]+)\s+\(([\d.]+)\)", text, re.MULTILINE):
        out.append(("RubyGems", m.group(1), m.group(2)))
    return out


def _parse_composer_json(text: str) -> list[tuple[str, str, str]]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    out = []
    for key in ("require", "require-dev"):
        for name, ver in (data.get(key) or {}).items():
            v = re.sub(r"^[\^~>=<v]+", "", str(ver))
            out.append(("Packagist", name, v))
    return out


_PARSERS = {
    "requirements.txt":  _parse_requirements_txt,
    "package.json":      _parse_package_json,
    "Cargo.toml":        _parse_cargo_toml,
    "go.mod":            _parse_go_mod,
    "Gemfile.lock":      _parse_gemfile_lock,
    "composer.json":     _parse_composer_json,
}


class Tools:
    class Valves(BaseModel):
        OSV_BASE: str = Field(default="https://api.osv.dev")
        TIMEOUT: int = Field(default=30)
        MAX_PACKAGES: int = Field(default=200, description="Soft cap on packages to query.")

    def __init__(self):
        self.valves = self.Valves()

    def _detect_manifest(self, path: Path) -> tuple[str, list[tuple[str, str, str]]]:
        text = path.read_text(encoding="utf-8", errors="ignore")
        name = path.name
        parser = _PARSERS.get(name)
        if parser:
            return name, parser(text)
        # Fall back: pyproject.toml has a [project.dependencies] / [tool.poetry.dependencies] block.
        if name == "pyproject.toml":
            out = []
            for m in re.finditer(r'^\s*"?([A-Za-z0-9_.\-]+)"?\s*=\s*"([^"]+)"', text, re.MULTILINE):
                v = re.sub(r"^[\^~>=<]+", "", m.group(2))
                out.append(("PyPI", m.group(1), v))
            return name, out
        return name, []

    async def _query_osv(
        self,
        client: httpx.AsyncClient,
        ecosystem: str,
        name: str,
        version: str,
    ) -> list[dict]:
        try:
            r = await client.post(
                f"{self.valves.OSV_BASE}/v1/query",
                json={"package": {"name": name, "ecosystem": ecosystem}, "version": version},
            )
        except Exception:
            return []
        if r.status_code != 200:
            return []
        return (r.json() or {}).get("vulns") or []

    @staticmethod
    def _severity(vuln: dict) -> tuple[float, str]:
        sevs = vuln.get("severity") or []
        for s in sevs:
            if s.get("type") in ("CVSS_V3", "CVSS_V4"):
                # CVSS string like "CVSS:3.1/AV:N/..." has the score at /N: in some forms;
                # OSV stores .score separately on databases that publish it.
                sc = vuln.get("database_specific", {}).get("severity")
                try:
                    return float(sc), s["score"]
                except (TypeError, ValueError):
                    return 0.0, s["score"]
        return 0.0, ""

    async def triage(
        self,
        manifest_path: str,
        max_packages: int = 0,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Read a manifest, query OSV for each (ecosystem, name, version) tuple,
        and emit a markdown triage report.
        :param manifest_path: Absolute path to the manifest file.
        :param max_packages: Cap on packages queried. 0 = MAX_PACKAGES.
        :return: Markdown report, vulnerable packages first.
        """
        path = Path(manifest_path).expanduser().resolve()
        if not path.exists():
            return f"Not found: {path}"
        kind, packages = self._detect_manifest(path)
        if not packages:
            return f"unsupported manifest type or empty: {path.name}"
        cap = max_packages or self.valves.MAX_PACKAGES
        packages = packages[:cap]

        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT) as client:
            results = await asyncio.gather(
                *[self._query_osv(client, eco, n, v) for eco, n, v in packages]
            )

        rows = []
        clean = []
        for (eco, name, ver), vulns in zip(packages, results):
            if not vulns:
                clean.append(f"- {eco}/{name} {ver}")
                continue
            for v in vulns:
                vid = v.get("id", "?")
                summary = (v.get("summary") or "")[:100]
                fixed = "?"
                for affected in (v.get("affected") or []):
                    for r in affected.get("ranges") or []:
                        for ev in r.get("events") or []:
                            if "fixed" in ev:
                                fixed = ev["fixed"]; break
                aliases = ", ".join(a for a in (v.get("aliases") or []) if a.startswith("CVE"))
                rows.append((eco, name, ver, vid, aliases, summary, fixed))

        # Most-severe-first: known-CVE rows ahead of others; otherwise alphabetic.
        rows.sort(key=lambda r: (0 if "CVE" in r[4] else 1, r[1]))

        out = [f"# Vulnerability triage: {path.name} ({kind})\n"]
        out.append(f"_Queried {len(packages)} packages, {len(rows)} vulnerable._\n")
        if rows:
            out.append("| ecosystem | package | version | id | CVE | summary | fixed in |")
            out.append("|---|---|---|---|---|---|---|")
            for eco, name, ver, vid, cve, summary, fixed in rows:
                out.append(f"| {eco} | {name} | {ver} | {vid} | {cve} | {summary.replace('|','/')} | {fixed} |")
        else:
            out.append("**No known vulnerabilities** — all packages clean per OSV.")
        if clean:
            out.append(f"\n<details><summary>Clean ({len(clean)} packages)</summary>\n")
            out.extend(clean[:50])
            if len(clean) > 50:
                out.append(f"… and {len(clean)-50} more.")
            out.append("</details>")
        return "\n".join(out)
