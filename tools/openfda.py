"""
title: OpenFDA — Drug, Food & Device Database
author: local-ai-stack
description: Query the FDA's open database for drug approvals, adverse events, recalls, drug labels, and food safety alerts. Free, no API key required.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import os
import httpx
from pydantic import BaseModel, Field
from typing import Callable, Any, Optional


FDA_API = "https://api.fda.gov"


class Tools:
    class Valves(BaseModel):
        FDA_API_KEY: str = Field(
            default_factory=lambda: os.environ.get("FDA_API_KEY", ""),
            description="Optional FDA API key for 240 req/min instead of 40 req/min (free at https://open.fda.gov/apis/authentication/)",
        )
        MAX_RESULTS: int = Field(default=5, description="Maximum results to return")

    def __init__(self):
        self.valves = self.Valves()

    def _params(self, extras: dict) -> dict:
        p = {"limit": self.valves.MAX_RESULTS, **extras}
        if self.valves.FDA_API_KEY:
            p["api_key"] = self.valves.FDA_API_KEY
        return p

    async def search_drug_labels(
        self,
        drug_name: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search FDA drug labels for prescribing information, indications, warnings, and dosing.
        :param drug_name: Drug brand or generic name (e.g. "ibuprofen", "Ozempic", "metformin")
        :return: Official FDA label info — indications, warnings, dosage, and contraindications
        """
        if __event_emitter__:
            await __event_emitter__(
                {"type": "status", "data": {"description": f"Searching FDA drug labels: {drug_name}", "done": False}}
            )

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{FDA_API}/drug/label.json",
                    params=self._params({"search": f'openfda.brand_name:"{drug_name}"+openfda.generic_name:"{drug_name}"'})
                )
                if resp.status_code == 404:
                    # Try broader search
                    resp = await client.get(
                        f"{FDA_API}/drug/label.json",
                        params=self._params({"search": drug_name})
                    )
                resp.raise_for_status()
                data = resp.json()

            results = data.get("results", [])
            if not results:
                return f"No FDA drug label found for: {drug_name}"

            r = results[0]
            openfda = r.get("openfda", {})
            brand = ", ".join(openfda.get("brand_name", [drug_name])[:3])
            generic = ", ".join(openfda.get("generic_name", [])[:2])
            manufacturer = ", ".join(openfda.get("manufacturer_name", [])[:2])
            route = ", ".join(openfda.get("route", []))
            substance = ", ".join(openfda.get("substance_name", [])[:3])

            def first(key):
                val = r.get(key, [])
                return val[0][:500] if val else ""

            indications = first("indications_and_usage")
            warnings = first("warnings")
            dosage = first("dosage_and_administration")
            contraindications = first("contraindications")

            if __event_emitter__:
                await __event_emitter__(
                    {"type": "status", "data": {"description": "FDA label retrieved", "done": True}}
                )

            lines = [f"## FDA Drug Label: {brand}\n"]
            lines.append(f"- **Generic name:** {generic}")
            lines.append(f"- **Manufacturer:** {manufacturer}")
            lines.append(f"- **Route:** {route}")
            lines.append(f"- **Active substance:** {substance}\n")
            if indications:
                lines.append(f"**Indications & Usage:**\n{indications.strip()}...\n")
            if warnings:
                lines.append(f"**Warnings:**\n{warnings.strip()[:400]}...\n")
            if dosage:
                lines.append(f"**Dosage:**\n{dosage.strip()[:300]}...\n")
            if contraindications:
                lines.append(f"**Contraindications:**\n{contraindications.strip()[:300]}...")

            lines.append(f"\n⚠️ Always consult a healthcare professional. This is official labeling, not medical advice.")
            return "\n".join(lines)

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return f"Drug not found in FDA database: {drug_name}"
            return f"OpenFDA error: HTTP {e.response.status_code}"
        except Exception as e:
            return f"Drug label error: {str(e)}"

    async def search_adverse_events(
        self,
        drug_name: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search FDA adverse event reports (FAERS) for a drug — reported side effects and reactions.
        :param drug_name: Drug name to look up adverse events for
        :return: Top reported adverse reactions with case counts
        """
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{FDA_API}/drug/event.json",
                    params=self._params({
                        "search": f"patient.drug.medicinalproduct:{drug_name}",
                        "count": "patient.reaction.reactionmeddrapt.exact",
                        "limit": 10,
                    })
                )
                resp.raise_for_status()
                data = resp.json()

            results = data.get("results", [])
            if not results:
                return f"No adverse event reports found for: {drug_name}"

            lines = [f"## FDA Adverse Events (FAERS): {drug_name}\n"]
            lines.append("Top reported reactions:\n")
            for r in results[:10]:
                term = r.get("term", "Unknown")
                count = r.get("count", 0)
                lines.append(f"- {term}: {count:,} reports")

            lines.append(f"\n⚠️ FAERS reports are voluntary and don't establish causality.")
            return "\n".join(lines)

        except Exception as e:
            return f"Adverse events error: {str(e)}"

    async def search_recalls(
        self,
        query: str,
        category: str = "food",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search FDA enforcement actions and product recalls.
        :param query: Product name or company to search recalls for
        :param category: Product category: 'food', 'drug', 'device'
        :return: Recent recalls with reason, classification, and affected products
        """
        endpoint_map = {"food": "food/enforcement", "drug": "drug/enforcement", "device": "device/enforcement"}
        endpoint = endpoint_map.get(category.lower(), "food/enforcement")

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{FDA_API}/{endpoint}.json",
                    params=self._params({"search": query, "sort": "recall_initiation_date:desc"})
                )
                resp.raise_for_status()
                data = resp.json()

            results = data.get("results", [])
            if not results:
                return f"No {category} recalls found for: {query}"

            lines = [f"## FDA Recalls ({category.title()}): {query}\n"]
            for r in results:
                product = r.get("product_description", "Unknown product")[:100]
                reason = r.get("reason_for_recall", "")[:200]
                classification = r.get("classification", "")
                date = r.get("recall_initiation_date", "")
                company = r.get("recalling_firm", "")
                status = r.get("status", "")

                lines.append(f"**{product}**")
                lines.append(f"   Class: {classification} | Date: {date} | Status: {status}")
                lines.append(f"   Firm: {company}")
                lines.append(f"   Reason: {reason}...")
                lines.append("")

            return "\n".join(lines)

        except Exception as e:
            return f"FDA recalls error: {str(e)}"
