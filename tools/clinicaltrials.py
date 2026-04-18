"""
title: ClinicalTrials.gov
author: local-ai-stack
description: Search 480,000+ clinical trials worldwide via the NIH ClinicalTrials.gov API. Find active trials, eligibility criteria, sponsors, and results for any condition or treatment.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from pydantic import BaseModel, Field
from typing import Callable, Any, Optional


CT_API = "https://clinicaltrials.gov/api/v2"


class Tools:
    class Valves(BaseModel):
        MAX_RESULTS: int = Field(default=5, description="Maximum trials to return")

    def __init__(self):
        self.valves = self.Valves()

    def _fmt_trial(self, study: dict) -> str:
        proto = study.get("protocolSection", {})
        ident = proto.get("identificationModule", {})
        status = proto.get("statusModule", {})
        desc = proto.get("descriptionModule", {})
        design = proto.get("designModule", {})
        eligibility = proto.get("eligibilityModule", {})
        contacts = proto.get("contactsLocationsModule", {})
        sponsor = proto.get("sponsorCollaboratorsModule", {})

        nct_id = ident.get("nctId", "")
        title = ident.get("briefTitle", "No title")
        overall_status = status.get("overallStatus", "Unknown")
        phase = " / ".join(design.get("phases", [])) or "N/A"
        brief_summary = desc.get("briefSummary", "")[:300]
        sponsor_name = sponsor.get("leadSponsor", {}).get("name", "")
        enrollment = design.get("enrollmentInfo", {}).get("count", "?")
        start = status.get("startDateStruct", {}).get("date", "?")
        completion = status.get("completionDateStruct", {}).get("date", "?")
        min_age = eligibility.get("minimumAge", "")
        max_age = eligibility.get("maximumAge", "")
        sex = eligibility.get("sex", "")
        url = f"https://clinicaltrials.gov/study/{nct_id}" if nct_id else ""

        status_emoji = {"RECRUITING": "🟢", "COMPLETED": "✅", "ACTIVE_NOT_RECRUITING": "🔵",
                        "NOT_YET_RECRUITING": "⏳", "TERMINATED": "🔴", "WITHDRAWN": "⚫"}.get(overall_status, "⬜")

        lines = [f"**{title}**"]
        lines.append(f"   {status_emoji} {overall_status} | Phase: {phase} | Enrollment: {enrollment}")
        lines.append(f"   Sponsor: {sponsor_name}")
        lines.append(f"   Dates: {start} → {completion}")
        if min_age or max_age:
            lines.append(f"   Eligibility: Ages {min_age}–{max_age}, {sex}")
        if brief_summary:
            lines.append(f"   {brief_summary.strip()}...")
        lines.append(f"   🔗 {url}")
        return "\n".join(lines)

    async def search_trials(
        self,
        condition: str,
        status: str = "RECRUITING",
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search ClinicalTrials.gov for trials by medical condition, disease, or intervention.
        :param condition: Medical condition or drug (e.g. "type 2 diabetes", "mRNA vaccine", "Alzheimer's disease")
        :param status: Trial status filter: RECRUITING, COMPLETED, ACTIVE_NOT_RECRUITING, or ALL
        :return: Matching trials with phase, sponsor, eligibility, and links
        """
        if __event_emitter__:
            await __event_emitter__(
                {"type": "status", "data": {"description": f"Searching ClinicalTrials.gov: {condition}", "done": False}}
            )

        params = {
            "query.cond": condition,
            "pageSize": self.valves.MAX_RESULTS,
            "format": "json",
            "fields": "protocolSection",
        }
        if status.upper() != "ALL":
            params["filter.overallStatus"] = status.upper()

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(f"{CT_API}/studies", params=params)
                resp.raise_for_status()
                data = resp.json()

            studies = data.get("studies", [])
            total = data.get("totalCount", 0)

            if not studies:
                return f"No clinical trials found for: {condition} (status: {status})"

            if __event_emitter__:
                await __event_emitter__(
                    {"type": "status", "data": {"description": f"Found {total:,} trials", "done": True}}
                )

            lines = [f"## ClinicalTrials.gov: {condition}\n({total:,} total | showing {len(studies)})\n"]
            for s in studies:
                lines.append(self._fmt_trial(s))
                lines.append("")

            return "\n".join(lines)

        except Exception as e:
            return f"ClinicalTrials.gov error: {str(e)}"

    async def get_trial(
        self,
        nct_id: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get full details for a specific clinical trial by its NCT identifier.
        :param nct_id: The NCT ID (e.g. "NCT04368728")
        :return: Complete trial details including eligibility criteria and outcome measures
        """
        nct_id = nct_id.strip().upper()
        if not nct_id.startswith("NCT"):
            nct_id = f"NCT{nct_id}"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{CT_API}/studies/{nct_id}",
                    params={"format": "json"},
                )
                resp.raise_for_status()
                data = resp.json()

            base = self._fmt_trial(data)
            proto = data.get("protocolSection", {})
            outcomes = proto.get("outcomesModule", {})
            primary = [o.get("measure", "") for o in outcomes.get("primaryOutcomes", [])[:3]]
            criteria = proto.get("eligibilityModule", {}).get("eligibilityCriteria", "")[:600]

            result = base
            if primary:
                result += f"\n\n**Primary Outcomes:**\n" + "\n".join(f"- {p}" for p in primary)
            if criteria:
                result += f"\n\n**Eligibility Criteria (excerpt):**\n{criteria.strip()}..."
            return result

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return f"Trial not found: {nct_id}"
            return f"ClinicalTrials.gov error: HTTP {e.response.status_code}"
        except Exception as e:
            return f"Trial lookup error: {str(e)}"
