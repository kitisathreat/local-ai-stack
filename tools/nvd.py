"""
title: NVD — National Vulnerability Database (CVEs)
author: local-ai-stack
description: Search the NIST National Vulnerability Database for CVE records. CVSS scores, affected products (CPE), references, and exploitability. Defensive/informational use. Free API key optional but strongly recommended.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from pydantic import BaseModel, Field
from typing import Optional


BASE = "https://services.nvd.nist.gov/rest/json/cves/2.0"


class Tools:
    class Valves(BaseModel):
        API_KEY: str = Field(default="", description="NVD API key (free; raises limit from 5→50 req/30s) at https://nvd.nist.gov/developers/request-an-api-key")
        RESULTS_PER_PAGE: int = Field(default=10, description="Max CVEs per request")

    def __init__(self):
        self.valves = self.Valves()

    def _headers(self):
        h = {"User-Agent": "local-ai-stack/1.0"}
        if self.valves.API_KEY:
            h["apiKey"] = self.valves.API_KEY
        return h

    async def cve(self, cve_id: str, __user__: Optional[dict] = None) -> str:
        """
        Look up a specific CVE.
        :param cve_id: CVE identifier (e.g. "CVE-2024-3094")
        :return: Description, CVSS, affected products, references
        """
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(BASE, params={"cveId": cve_id.strip()}, headers=self._headers())
                r.raise_for_status()
                data = r.json()
            items = data.get("vulnerabilities", [])
            if not items:
                return f"CVE not found: {cve_id}"
            cv = items[0].get("cve", {})
            desc = next((d.get("value", "") for d in cv.get("descriptions", []) if d.get("lang") == "en"), "")
            metrics = cv.get("metrics", {})
            cvss = ""
            for key in ["cvssMetricV31", "cvssMetricV30", "cvssMetricV2"]:
                if metrics.get(key):
                    m0 = metrics[key][0]
                    cd = m0.get("cvssData", {})
                    cvss = f"{cd.get('baseSeverity','?')} / {cd.get('baseScore','?')}  ({cd.get('vectorString','')})"
                    break
            refs = [r["url"] for r in cv.get("references", [])][:5]
            config = cv.get("configurations", [])
            products = []
            for c in config:
                for n in c.get("nodes", []):
                    for m in n.get("cpeMatch", [])[:5]:
                        if m.get("vulnerable"):
                            products.append(m.get("criteria", ""))
            lines = [f"## {cve_id}"]
            lines.append(f"**Published:** {cv.get('published','')}   **Modified:** {cv.get('lastModified','')}")
            if cvss:
                lines.append(f"**CVSS:** {cvss}")
            lines.append(f"\n{desc}")
            if products:
                lines.append("\n**Affected:**")
                for p in products[:8]:
                    lines.append(f"- {p}")
            if refs:
                lines.append("\n**References:**")
                for u in refs:
                    lines.append(f"- {u}")
            return "\n".join(lines)
        except Exception as e:
            return f"NVD error: {e}"

    async def search(
        self,
        keyword: str = "",
        cpe: str = "",
        last_days: int = 30,
        min_score: float = 0.0,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search CVEs by keyword or CPE, optionally recent-only or above a CVSS threshold.
        :param keyword: Free-text keyword (e.g. "openssl heap overflow")
        :param cpe: Optional CPE 2.3 string (e.g. "cpe:2.3:a:openssl:openssl:*:*:*:*:*:*:*:*")
        :param last_days: Limit to CVEs modified within N days (default 30)
        :param min_score: Optional CVSS base score minimum (0–10)
        :return: Ranked CVEs with severity and summary
        """
        import datetime as dt
        params = {"resultsPerPage": self.valves.RESULTS_PER_PAGE}
        if keyword: params["keywordSearch"] = keyword
        if cpe: params["cpeName"] = cpe
        if last_days:
            end = dt.datetime.utcnow()
            start = end - dt.timedelta(days=last_days)
            params["lastModStartDate"] = start.strftime("%Y-%m-%dT00:00:00.000")
            params["lastModEndDate"] = end.strftime("%Y-%m-%dT23:59:59.999")
        if min_score:
            params["cvssV3Severity"] = "HIGH" if min_score >= 7 else "MEDIUM"
        try:
            async with httpx.AsyncClient(timeout=45) as client:
                r = await client.get(BASE, params=params, headers=self._headers())
                r.raise_for_status()
                data = r.json()
            items = data.get("vulnerabilities", [])
            total = data.get("totalResults", 0)
            if not items:
                return f"No CVEs for {keyword or cpe}"
            lines = [f"## NVD Search: {keyword or cpe} ({total} matches, showing {len(items)})\n"]
            for v in items:
                cv = v.get("cve", {})
                cid = cv.get("id", "")
                desc = next((d.get("value", "") for d in cv.get("descriptions", []) if d.get("lang") == "en"), "")
                metrics = cv.get("metrics", {})
                cvss = ""
                for key in ["cvssMetricV31", "cvssMetricV30"]:
                    if metrics.get(key):
                        cd = metrics[key][0].get("cvssData", {})
                        cvss = f"{cd.get('baseSeverity','?')} {cd.get('baseScore','?')}"
                        break
                lines.append(f"**{cid}** — {cvss}")
                lines.append(f"   {desc[:240]}")
                lines.append("")
            return "\n".join(lines)
        except Exception as e:
            return f"NVD error: {e}"
