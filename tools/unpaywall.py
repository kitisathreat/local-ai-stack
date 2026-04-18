"""
title: Unpaywall — Free Paper Finder
author: local-ai-stack
description: Find free, legal, open-access versions of any academic paper by DOI using Unpaywall. No more paywalls — finds PDFs from institutional repositories, PubMed Central, ArXiv, and author pages.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from pydantic import BaseModel, Field
from typing import Callable, Any, Optional


UNPAYWALL_API = "https://api.unpaywall.org/v2"


class Tools:
    class Valves(BaseModel):
        CONTACT_EMAIL: str = Field(
            default="local@localhost",
            description="Your email (required by Unpaywall API — used for rate limiting identification)",
        )

    def __init__(self):
        self.valves = self.Valves()

    async def find_free_paper(
        self,
        doi: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Find a free, legal PDF of an academic paper using its DOI.
        :param doi: Paper DOI with or without prefix (e.g. "10.1038/nature12373" or "https://doi.org/10.1038/nature12373")
        :return: Free access links and open-access status
        """
        doi = doi.strip()
        doi = doi.replace("https://doi.org/", "").replace("http://doi.org/", "")

        if __event_emitter__:
            await __event_emitter__(
                {"type": "status", "data": {"description": f"Checking Unpaywall for DOI: {doi}", "done": False}}
            )

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{UNPAYWALL_API}/{doi}",
                    params={"email": self.valves.CONTACT_EMAIL},
                )
                if resp.status_code == 404:
                    return f"DOI not found in Unpaywall: {doi}\nVerify the DOI is correct."
                resp.raise_for_status()
                data = resp.json()

            title = data.get("title", "Unknown title")
            year = data.get("year", "?")
            journal = data.get("journal_name", "")
            is_oa = data.get("is_oa", False)
            oa_status = data.get("oa_status", "unknown")
            doi_url = data.get("doi_url", f"https://doi.org/{doi}")

            # Best OA location
            best = data.get("best_oa_location")
            all_locs = data.get("oa_locations", [])

            if __event_emitter__:
                await __event_emitter__(
                    {"type": "status", "data": {
                        "description": "Open access" if is_oa else "No free version found",
                        "done": True
                    }}
                )

            lines = [f"## Unpaywall: {title}\n"]
            lines.append(f"- **Year:** {year} | **Journal:** {journal}")
            lines.append(f"- **DOI:** {doi_url}")
            lines.append(f"- **Open Access:** {'✅ Yes' if is_oa else '❌ No'} ({oa_status})\n")

            if is_oa and best:
                url = best.get("url_for_pdf") or best.get("url") or ""
                host_type = best.get("host_type", "")
                version = best.get("version", "")
                license_ = best.get("license") or ""
                lines.append(f"**Best free version:**")
                lines.append(f"- Source: {host_type} | Version: {version} | License: {license_}")
                lines.append(f"- 📄 **{url}**\n")

                if len(all_locs) > 1:
                    lines.append(f"**All {len(all_locs)} free locations:**")
                    for loc in all_locs[:5]:
                        u = loc.get("url_for_pdf") or loc.get("url") or ""
                        h = loc.get("host_type", "")
                        lines.append(f"  - [{h}] {u}")
            else:
                lines.append("❌ No free version found via Unpaywall.")
                lines.append(f"You can try:")
                lines.append(f"- Semantic Scholar (may have author copy): search for the title")
                lines.append(f"- Email the corresponding author directly")
                lines.append(f"- Request via Interlibrary Loan at your institution")

            return "\n".join(lines)

        except httpx.ConnectError:
            return "Cannot reach Unpaywall API. Check internet connection."
        except Exception as e:
            return f"Unpaywall error: {str(e)}"

    async def batch_check(
        self,
        dois: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Check multiple DOIs for open access availability at once.
        :param dois: Newline or comma-separated list of DOIs (up to 5)
        :return: Table of papers with open access status and links
        """
        import re
        doi_list = [d.strip().replace("https://doi.org/", "") for d in re.split(r"[,\n]", dois) if d.strip()][:5]
        if not doi_list:
            return "No valid DOIs provided."

        lines = [f"## Unpaywall Batch Check ({len(doi_list)} DOIs)\n"]
        for doi in doi_list:
            result = await self.find_free_paper(doi, None, None)
            lines.append(result)
            lines.append("---")

        return "\n".join(lines)
