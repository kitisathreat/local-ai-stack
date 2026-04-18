"""
title: OSV.dev — Open Source Vulnerabilities
author: local-ai-stack
description: Query the OSV (Open Source Vulnerabilities) database. Covers PyPI, npm, Go, Maven, crates.io, RubyGems, Packagist, Nuget, Linux distros, Android, Hex, Pub. Defensive use. No API key required.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from pydantic import BaseModel, Field
from typing import Optional


BASE = "https://api.osv.dev/v1"


class Tools:
    class Valves(BaseModel):
        pass

    def __init__(self):
        self.valves = self.Valves()

    async def package(
        self,
        name: str,
        ecosystem: str = "PyPI",
        version: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        List known vulnerabilities for a package (optionally at a specific version).
        :param name: Package name (e.g. "requests", "lodash")
        :param ecosystem: PyPI, npm, Go, Maven, crates.io, RubyGems, Packagist, NuGet, Hex, Pub, Android, Alpine, Debian, Ubuntu
        :param version: Optional exact version string
        :return: Vulnerabilities with IDs, severity, and summary
        """
        payload = {"package": {"name": name, "ecosystem": ecosystem}}
        if version:
            payload["version"] = version
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(f"{BASE}/query", json=payload)
                r.raise_for_status()
                data = r.json()
            vulns = data.get("vulns", [])
            if not vulns:
                return f"No known vulnerabilities for {name} ({ecosystem}" + (f" @ {version})" if version else ")")
            lines = [f"## OSV: {name} ({ecosystem}" + (f" @ {version})" if version else ")") + f" — {len(vulns)} vulns\n"]
            for v in vulns:
                vid = v.get("id", "")
                summary = v.get("summary", "") or (v.get("details", "") or "")[:200]
                aliases = ", ".join(v.get("aliases", []))
                sev_list = v.get("severity", [])
                sev = ", ".join(f"{s.get('type','')} {s.get('score','')}" for s in sev_list)
                published = v.get("published", "")[:10]
                lines.append(f"**{vid}** ({published})")
                if aliases:
                    lines.append(f"   aliases: {aliases}")
                if sev:
                    lines.append(f"   severity: {sev}")
                lines.append(f"   {summary}")
                lines.append(f"   🔗 https://osv.dev/vulnerability/{vid}\n")
            return "\n".join(lines)
        except Exception as e:
            return f"OSV error: {e}"

    async def vuln(self, vuln_id: str, __user__: Optional[dict] = None) -> str:
        """
        Fetch a single OSV vulnerability record.
        :param vuln_id: OSV ID (e.g. "GHSA-xxxx", "PYSEC-2023-1", "CVE-2024-...")
        :return: Full record with affected ranges and references
        """
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(f"{BASE}/vulns/{vuln_id}")
                if r.status_code == 404:
                    return f"Not found: {vuln_id}"
                r.raise_for_status()
                v = r.json()
            aliases = ", ".join(v.get("aliases", []))
            summary = v.get("summary", "")
            details = v.get("details", "")
            affected = v.get("affected", [])
            lines = [f"## {v.get('id','')}"]
            if aliases:
                lines.append(f"aliases: {aliases}")
            lines.append(f"Published: {v.get('published','')[:10]}   Modified: {v.get('modified','')[:10]}")
            if summary:
                lines.append(f"\n**Summary:** {summary}")
            if details:
                lines.append(f"\n{details[:800]}")
            if affected:
                lines.append("\n**Affected:**")
                for a in affected[:6]:
                    pkg = a.get("package", {})
                    ranges = ", ".join(
                        f"{e.get('introduced','')}→{e.get('fixed','—')}"
                        for r_ in a.get("ranges", [])
                        for e in r_.get("events", [])
                    )
                    lines.append(f"- {pkg.get('ecosystem','')}/{pkg.get('name','')}: {ranges}")
            refs = [r["url"] for r in v.get("references", [])][:5]
            if refs:
                lines.append("\n**References:**")
                for u in refs:
                    lines.append(f"- {u}")
            return "\n".join(lines)
        except Exception as e:
            return f"OSV error: {e}"
