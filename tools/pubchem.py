"""
title: PubChem — Chemical Compound Database
author: local-ai-stack
description: Search PubChem — NCBI's database of 115M+ chemical compounds. Get molecular formulas, structures, physical properties, safety data, and bioactivity for any chemical.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from pydantic import BaseModel, Field
from typing import Callable, Any, Optional


PUBCHEM_API = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"


class Tools:
    class Valves(BaseModel):
        pass

    def __init__(self):
        self.valves = self.Valves()

    async def get_compound(
        self,
        name: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Look up a chemical compound by name, formula, CAS number, or common name.
        :param name: Chemical name, formula, or CAS number (e.g. "caffeine", "C8H10N4O2", "aspirin", "58-08-2")
        :return: Molecular formula, weight, IUPAC name, physical properties, and safety info
        """
        if __event_emitter__:
            await __event_emitter__(
                {"type": "status", "data": {"description": f"Looking up compound: {name}", "done": False}}
            )

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                # Get CID first
                cid_resp = await client.get(
                    f"{PUBCHEM_API}/compound/name/{name}/cids/JSON",
                )
                if cid_resp.status_code == 404:
                    return f"Compound not found in PubChem: {name}"
                cid_resp.raise_for_status()
                cids = cid_resp.json().get("IdentifierList", {}).get("CID", [])
                if not cids:
                    return f"No PubChem CID found for: {name}"
                cid = cids[0]

                # Get properties
                props_resp = await client.get(
                    f"{PUBCHEM_API}/compound/cid/{cid}/property/MolecularFormula,MolecularWeight,IUPACName,CanonicalSMILES,InChIKey/JSON",
                )
                props_resp.raise_for_status()
                props = props_resp.json().get("PropertyTable", {}).get("Properties", [{}])[0]

                # Get synonyms
                syn_resp = await client.get(
                    f"{PUBCHEM_API}/compound/cid/{cid}/synonyms/JSON",
                )
                synonyms = []
                if syn_resp.status_code == 200:
                    synonyms = syn_resp.json().get("InformationList", {}).get("Information", [{}])[0].get("Synonym", [])[:6]

            if __event_emitter__:
                await __event_emitter__(
                    {"type": "status", "data": {"description": "Compound data retrieved", "done": True}}
                )

            formula = props.get("MolecularFormula", "N/A")
            weight = props.get("MolecularWeight", "N/A")
            iupac = props.get("IUPACName", "N/A")
            smiles = props.get("CanonicalSMILES", "")
            inchikey = props.get("InChIKey", "")
            pubchem_url = f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}"

            lines = [f"## PubChem: {name.title()}\n"]
            lines.append(f"- **CID:** {cid}")
            lines.append(f"- **Molecular Formula:** {formula}")
            lines.append(f"- **Molecular Weight:** {weight} g/mol")
            lines.append(f"- **IUPAC Name:** {iupac}")
            if inchikey:
                lines.append(f"- **InChIKey:** {inchikey}")
            if smiles:
                lines.append(f"- **SMILES:** `{smiles}`")
            if synonyms:
                lines.append(f"- **Also known as:** {', '.join(synonyms[:5])}")
            lines.append(f"- **PubChem:** {pubchem_url}")

            return "\n".join(lines)

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return f"Compound not found: {name}"
            return f"PubChem error: HTTP {e.response.status_code}"
        except Exception as e:
            return f"PubChem error: {str(e)}"

    async def search_compounds(
        self,
        query: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search PubChem for compounds matching a query — returns a list of matching chemicals.
        :param query: Search terms (e.g. "antidepressant", "omega-3", "beta blocker")
        :return: List of matching compounds with molecular formulas and weights
        """
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/{query}/cids/JSON",
                    params={"name_type": "word"},
                )
                if resp.status_code == 404:
                    return f"No compounds found for: {query}"
                resp.raise_for_status()
                cids = resp.json().get("IdentifierList", {}).get("CID", [])[:8]

                if not cids:
                    return f"No PubChem results for: {query}"

                # Batch property fetch
                props_resp = await client.get(
                    f"{PUBCHEM_API}/compound/cid/{','.join(str(c) for c in cids)}/property/MolecularFormula,MolecularWeight,IUPACName/JSON",
                )
                props_resp.raise_for_status()
                props = props_resp.json().get("PropertyTable", {}).get("Properties", [])

            lines = [f"## PubChem Search: {query}\n"]
            for p in props:
                cid = p.get("CID", "")
                formula = p.get("MolecularFormula", "?")
                weight = p.get("MolecularWeight", "?")
                iupac = p.get("IUPACName", "")[:60]
                url = f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}"
                lines.append(f"**CID {cid}** — {iupac}")
                lines.append(f"   Formula: {formula} | MW: {weight} g/mol | {url}\n")

            return "\n".join(lines)

        except Exception as e:
            return f"PubChem search error: {str(e)}"

    async def get_safety_data(
        self,
        name: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get GHS safety classifications and hazard information for a chemical compound.
        :param name: Chemical name (e.g. "sodium hydroxide", "ethanol", "benzene")
        :return: GHS hazard codes, signal words, and safety precautions
        """
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                cid_resp = await client.get(f"{PUBCHEM_API}/compound/name/{name}/cids/JSON")
                if cid_resp.status_code == 404:
                    return f"Compound not found: {name}"
                cid = cid_resp.json().get("IdentifierList", {}).get("CID", [None])[0]
                if not cid:
                    return f"No CID found for: {name}"

                ghs_resp = await client.get(
                    f"https://pubchem.ncbi.nlm.nih.gov/rest/pug_view/data/compound/{cid}/JSON",
                    params={"heading": "GHS+Classification"},
                )
                ghs_resp.raise_for_status()
                data = ghs_resp.json()

            # Navigate GHS data
            sections = data.get("Record", {}).get("Section", [])
            lines = [f"## GHS Safety Data: {name}\n"]
            found = False
            for section in sections:
                if "GHS" in section.get("TOCHeading", "") or "Safety" in section.get("TOCHeading", ""):
                    for sub in section.get("Section", []):
                        heading = sub.get("TOCHeading", "")
                        info = sub.get("Information", [])
                        if info:
                            val = info[0].get("Value", {}).get("StringWithMarkup", [{}])[0].get("String", "")
                            if val:
                                lines.append(f"**{heading}:** {val}")
                                found = True

            if not found:
                lines.append("No GHS classification data available for this compound.")

            lines.append(f"\n🔗 Full data: https://pubchem.ncbi.nlm.nih.gov/compound/{cid}#section=GHS-Classification")
            return "\n".join(lines)

        except Exception as e:
            return f"PubChem safety error: {str(e)}"
