"""
title: RCSB PDB — Protein Data Bank
author: local-ai-stack
description: Search 200,000+ experimentally-determined 3D macromolecular structures (proteins, nucleic acids, complexes) from the Protein Data Bank. Includes X-ray, NMR, cryo-EM structures. No API key required.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from pydantic import BaseModel, Field
from typing import Optional


SEARCH = "https://search.rcsb.org/rcsbsearch/v2/query"
DATA = "https://data.rcsb.org/rest/v1/core"


class Tools:
    class Valves(BaseModel):
        LIMIT: int = Field(default=8, description="Max PDB IDs to return")

    def __init__(self):
        self.valves = self.Valves()

    async def search(
        self,
        query: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Full-text search the PDB for structures.
        :param query: Keywords (e.g. "hemoglobin", "SARS-CoV-2 spike", "green fluorescent protein")
        :return: Matching PDB IDs with title and resolution
        """
        body = {
            "query": {
                "type": "terminal",
                "service": "full_text",
                "parameters": {"value": query},
            },
            "return_type": "entry",
            "request_options": {"paginate": {"start": 0, "rows": self.valves.LIMIT}},
        }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(SEARCH, json=body)
                r.raise_for_status()
                data = r.json()
                ids = [h["identifier"] for h in data.get("result_set", [])]
                if not ids:
                    return f"No PDB structures for: {query}"
                total = data.get("total_count", len(ids))
                lines = [f"## RCSB PDB: {query} ({total:,} matches)\n"]
                for pid in ids:
                    d = await client.get(f"{DATA}/entry/{pid}")
                    if d.status_code != 200:
                        lines.append(f"- **{pid}** (details unavailable)")
                        continue
                    e = d.json()
                    title = (e.get("struct") or {}).get("title", "")
                    res = (e.get("rcsb_entry_info") or {}).get("resolution_combined", [""])[0]
                    method = (e.get("exptl") or [{}])[0].get("method", "")
                    year = (e.get("rcsb_accession_info") or {}).get("initial_release_date", "")[:4]
                    lines.append(f"**{pid}** — {title}")
                    lines.append(f"   {method}, resolution {res} Å, released {year}")
                    lines.append(f"   🔗 https://www.rcsb.org/structure/{pid}\n")
                return "\n".join(lines)
        except Exception as e:
            return f"PDB error: {e}"

    async def entry(self, pdb_id: str, __user__: Optional[dict] = None) -> str:
        """
        Look up a PDB entry's full details.
        :param pdb_id: 4-character PDB ID (e.g. "1CRN", "6VXX")
        :return: Full metadata: method, resolution, polymers, ligands, citation
        """
        pdb_id = pdb_id.strip().upper()
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(f"{DATA}/entry/{pdb_id}")
                if r.status_code == 404:
                    return f"PDB entry not found: {pdb_id}"
                r.raise_for_status()
                e = r.json()
            title = (e.get("struct") or {}).get("title", "")
            method = (e.get("exptl") or [{}])[0].get("method", "")
            res = (e.get("rcsb_entry_info") or {}).get("resolution_combined", [""])[0]
            mw = (e.get("rcsb_entry_info") or {}).get("molecular_weight", "")
            atoms = (e.get("rcsb_entry_info") or {}).get("deposited_atom_count", 0)
            polymers = (e.get("rcsb_entry_info") or {}).get("polymer_composition", "")
            citation = (e.get("citation") or [{}])[0]
            cite_title = citation.get("title", "")
            journal = citation.get("rcsb_journal_abbrev", "")
            year = citation.get("year", "")
            doi = citation.get("pdbx_database_id_doi", "")
            out = [f"## PDB {pdb_id}: {title}"]
            out.append(f"**Method:** {method}   **Resolution:** {res} Å")
            out.append(f"**MW:** {mw}   **Atoms:** {atoms:,}   **Composition:** {polymers}")
            if cite_title:
                out.append(f"\n**Citation:** {cite_title}  ({journal} {year})")
                if doi:
                    out.append(f"   DOI: {doi}")
            out.append(f"\n🔗 https://www.rcsb.org/structure/{pdb_id}")
            return "\n".join(out)
        except Exception as e:
            return f"PDB error: {e}"
