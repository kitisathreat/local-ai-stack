"""
title: ChEMBL — Bioactive Molecules & Drug Targets
author: local-ai-stack
description: Search ChEMBL's 2.3M+ bioactive molecules, 15,000+ targets, and 20M+ activity measurements. Includes approved drugs, clinical candidates, and research compounds. EMBL-EBI. No API key required.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from pydantic import BaseModel, Field
from typing import Optional


BASE = "https://www.ebi.ac.uk/chembl/api/data"


class Tools:
    class Valves(BaseModel):
        LIMIT: int = Field(default=10, description="Max results")

    def __init__(self):
        self.valves = self.Valves()

    async def molecule(self, name: str, __user__: Optional[dict] = None) -> str:
        """
        Look up a molecule/drug by name.
        :param name: Trade or generic name (e.g. "aspirin", "imatinib", "pembrolizumab")
        :return: Molecule entries with ChEMBL ID, max phase, MW, and indication
        """
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(
                    f"{BASE}/molecule/search.json",
                    params={"q": name, "limit": self.valves.LIMIT},
                )
                r.raise_for_status()
                data = r.json()
            mols = data.get("molecules", [])
            if not mols:
                return f"No ChEMBL molecule found for: {name}"
            lines = [f"## ChEMBL: {name}\n"]
            for m in mols:
                cid = m.get("molecule_chembl_id", "")
                pref = m.get("pref_name", "") or ""
                phase = m.get("max_phase", "")
                mtype = m.get("molecule_type", "")
                props = m.get("molecule_properties", {}) or {}
                mw = props.get("full_mwt", "")
                alogp = props.get("alogp", "")
                smiles = (m.get("molecule_structures") or {}).get("canonical_smiles", "")
                lines.append(f"**{pref or cid}**  [{cid}]")
                lines.append(f"   Type: {mtype}   Max phase: {phase}   MW: {mw}   AlogP: {alogp}")
                if smiles:
                    lines.append(f"   SMILES: `{smiles[:120]}`")
                lines.append(f"   🔗 https://www.ebi.ac.uk/chembl/compound_report_card/{cid}/\n")
            return "\n".join(lines)
        except Exception as e:
            return f"ChEMBL error: {e}"

    async def target(self, name: str, __user__: Optional[dict] = None) -> str:
        """
        Look up drug targets (proteins, cells, organisms).
        :param name: Target name (e.g. "EGFR", "cyclooxygenase-2", "SARS-CoV-2 spike")
        :return: Target records with ChEMBL ID, type, organism, and components
        """
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(
                    f"{BASE}/target/search.json",
                    params={"q": name, "limit": self.valves.LIMIT},
                )
                r.raise_for_status()
                data = r.json()
            ts = data.get("targets", [])
            if not ts:
                return f"No ChEMBL target for: {name}"
            lines = [f"## ChEMBL Targets: {name}\n"]
            for t in ts:
                tid = t.get("target_chembl_id", "")
                pref = t.get("pref_name", "")
                typ = t.get("target_type", "")
                org = t.get("organism", "")
                lines.append(f"**{pref}**  [{tid}]  ({typ}, {org})")
                comps = t.get("target_components", [])
                for c in comps[:2]:
                    uniprot = (c.get("accession") or "")
                    if uniprot:
                        lines.append(f"   UniProt: {uniprot}")
                lines.append(f"   🔗 https://www.ebi.ac.uk/chembl/target_report_card/{tid}/\n")
            return "\n".join(lines)
        except Exception as e:
            return f"ChEMBL error: {e}"
