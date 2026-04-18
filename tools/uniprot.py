"""
title: UniProt — Protein Sequences, Functions & Interactions
author: local-ai-stack
description: Query the UniProt protein database — the world's largest and most comprehensive protein knowledge base with 250M+ sequences. Search by protein name, gene, organism, or function. Get full protein details including amino acid sequence, domain structure, post-translational modifications, disease associations, 3D structures (PDB), and biological pathways. No API key required.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from typing import Callable, Any, Optional
from pydantic import BaseModel, Field

BASE = "https://rest.uniprot.org/uniprotkb"


class Tools:
    class Valves(BaseModel):
        DEFAULT_ORGANISM: str = Field(
            default="Homo sapiens",
            description="Default organism for protein searches (e.g. 'Homo sapiens', 'Mus musculus', 'E. coli')",
        )

    def __init__(self):
        self.valves = self.Valves()

    async def search_proteins(
        self,
        query: str,
        organism: str = "",
        reviewed_only: bool = True,
        limit: int = 10,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search UniProt for proteins by name, gene, function, disease, or keyword.
        :param query: Search query (e.g. 'insulin', 'BRCA2', 'p53 tumor suppressor', 'COVID spike protein', 'ATP synthase')
        :param organism: Filter by organism (e.g. 'Homo sapiens', 'mouse', 'E. coli') — default from settings
        :param reviewed_only: If True, return only Swiss-Prot manually reviewed entries (higher quality)
        :param limit: Number of results (max 25)
        :return: Protein accessions, names, genes, organisms, and function summaries
        """
        organism = organism or self.valves.DEFAULT_ORGANISM

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Searching UniProt for '{query}'...", "done": False}})

        # Build query
        parts = [query]
        if organism:
            parts.append(f"organism_name:{organism}")
        if reviewed_only:
            parts.append("reviewed:true")
        q = " AND ".join(parts)

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{BASE}/search",
                    params={
                        "query": q,
                        "format": "json",
                        "size": min(limit, 25),
                        "fields": "accession,protein_name,gene_names,organism_name,cc_function,length,ft_active_site,cc_disease,xref_pdb",
                    },
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            return f"UniProt search error: {str(e)}"

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        results = data.get("results", [])
        total = data.get("total", {}).get("hits", len(results))

        if not results:
            if reviewed_only:
                return f"No reviewed proteins found for '{query}' in {organism}. Try setting reviewed_only=false."
            return f"No proteins found for '{query}'."

        lines = [f"## UniProt Search: '{query}'\n"]
        lines.append(f"**Organism:** {organism} | **Source:** {'Swiss-Prot (reviewed)' if reviewed_only else 'UniProtKB'} | **Total hits:** {total}\n")

        for entry in results:
            acc = entry.get("primaryAccession", "")
            prot_names = entry.get("proteinDescription", {})
            rec_name = prot_names.get("recommendedName", {})
            full_name = rec_name.get("fullName", {}).get("value", "") if rec_name else ""
            if not full_name:
                sub_names = prot_names.get("submittedNames", [{}])
                full_name = (sub_names[0].get("fullName", {}).get("value", "") if sub_names else "") or acc

            genes = entry.get("genes", [])
            gene_name = genes[0].get("geneName", {}).get("value", "") if genes else ""

            organism_name = entry.get("organism", {}).get("scientificName", "")
            length = entry.get("sequence", {}).get("length", "")

            # Function comment
            comments = entry.get("comments", [])
            function_text = ""
            disease_text = ""
            for c in comments:
                if c.get("commentType") == "FUNCTION":
                    texts = c.get("texts", [{}])
                    function_text = texts[0].get("value", "")[:200] if texts else ""
                elif c.get("commentType") == "DISEASE":
                    disease = c.get("disease", {})
                    disease_text = disease.get("diseaseId", "")

            # PDB structures
            xrefs = entry.get("uniProtKBCrossReferences", [])
            pdb_ids = [x.get("id", "") for x in xrefs if x.get("database") == "PDB"][:3]

            uniprot_url = f"https://www.uniprot.org/uniprotkb/{acc}"

            lines.append(f"### [{acc}]({uniprot_url}) — {full_name}")
            meta = []
            if gene_name:
                meta.append(f"**Gene:** {gene_name}")
            if organism_name:
                meta.append(f"**Organism:** *{organism_name}*")
            if length:
                meta.append(f"**Length:** {length} aa")
            if meta:
                lines.append(" | ".join(meta))
            if function_text:
                lines.append(f"_{function_text}..._" if len(function_text) == 200 else f"_{function_text}_")
            if disease_text:
                lines.append(f"⚕️ **Disease:** {disease_text}")
            if pdb_ids:
                lines.append(f"🧬 **3D Structures (PDB):** {', '.join(pdb_ids)}")
            lines.append("")

        return "\n".join(lines)

    async def get_protein(
        self,
        accession: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get full details for a specific protein by UniProt accession number.
        :param accession: UniProt accession (e.g. 'P04637' for human p53, 'P01308' for insulin, 'P68871' for hemoglobin beta, 'Q9BYF1' for ACE2)
        :return: Full protein profile including sequence, domains, PTMs, diseases, interactions, and structures
        """
        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Fetching UniProt entry {accession}...", "done": False}})

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(f"{BASE}/{accession}", params={"format": "json"})
                if resp.status_code == 404:
                    return f"UniProt accession '{accession}' not found. Example valid accessions: P04637 (p53), P01308 (insulin), Q9BYF1 (ACE2)."
                resp.raise_for_status()
                entry = resp.json()
        except Exception as e:
            return f"UniProt fetch error: {str(e)}"

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        acc = entry.get("primaryAccession", "")
        prot_desc = entry.get("proteinDescription", {})
        rec_name = prot_desc.get("recommendedName", {})
        full_name = rec_name.get("fullName", {}).get("value", acc) if rec_name else acc
        alt_names = [n.get("fullName", {}).get("value", "") for n in prot_desc.get("alternativeNames", [])][:3]
        short_names = rec_name.get("shortNames", [{}]) if rec_name else []
        short_name = short_names[0].get("value", "") if short_names else ""

        genes = entry.get("genes", [])
        gene_name = genes[0].get("geneName", {}).get("value", "") if genes else ""
        gene_syns = [s.get("value", "") for g in genes for s in g.get("synonyms", [])][:5]

        organism = entry.get("organism", {})
        org_name = organism.get("scientificName", "")
        org_common = organism.get("commonName", "")
        taxonomy = organism.get("lineage", [])

        sequence = entry.get("sequence", {})
        seq_len = sequence.get("length", 0)
        seq_mass = sequence.get("molWeight", 0)
        seq_value = sequence.get("value", "")

        # Comments
        comments = entry.get("comments", [])
        function_text = ""
        catalytic = []
        diseases = []
        subcell = []
        interactions = []
        pathway_text = ""

        for c in comments:
            ct = c.get("commentType", "")
            if ct == "FUNCTION":
                texts = c.get("texts", [{}])
                function_text = texts[0].get("value", "") if texts else ""
            elif ct == "CATALYTIC ACTIVITY":
                reaction = c.get("reaction", {})
                catalytic.append(reaction.get("name", ""))
            elif ct == "DISEASE":
                d = c.get("disease", {})
                diseases.append(f"{d.get('diseaseId', '')} — {d.get('description', {}).get('value', '')[:100]}")
            elif ct == "SUBCELLULAR LOCATION":
                for loc in c.get("subcellularLocations", []):
                    loc_name = loc.get("location", {}).get("value", "")
                    if loc_name:
                        subcell.append(loc_name)
            elif ct == "INTERACTION":
                for interact in c.get("interactions", [])[:5]:
                    partner = interact.get("interactantTwo", {}).get("uniProtKBAccession", "")
                    if partner:
                        interactions.append(partner)
            elif ct == "PATHWAY":
                texts = c.get("texts", [{}])
                pathway_text = texts[0].get("value", "") if texts else ""

        # Features
        features = entry.get("features", [])
        domains = [f.get("description", "") for f in features if f.get("type", "") in ("Domain", "Region")][:8]
        ptms = [f.get("description", "") for f in features if f.get("type", "") in ("Modified residue", "Glycosylation", "Disulfide bond")][:6]
        active_sites = [str(f.get("location", {}).get("start", {}).get("value", "")) for f in features if f.get("type") == "Active site"][:5]

        # Cross-references
        xrefs = entry.get("uniProtKBCrossReferences", [])
        pdb_ids = [x.get("id") for x in xrefs if x.get("database") == "PDB"][:8]
        omim_ids = [x.get("id") for x in xrefs if x.get("database") == "MIM"][:3]
        go_terms = [
            f"{x.get('id')} ({[p.get('value') for p in x.get('properties', []) if p.get('key') == 'GoTerm'][0].split(':')[1] if [p for p in x.get('properties', []) if p.get('key') == 'GoTerm'] else ''})"
            for x in xrefs if x.get("database") == "GO"
        ][:8]

        lines = [f"## UniProt: {full_name}\n"]
        lines.append(f"**Accession:** [{acc}](https://www.uniprot.org/uniprotkb/{acc}) | **Gene:** {gene_name}")
        if short_name:
            lines.append(f"**Short Name:** {short_name}")
        if alt_names:
            lines.append(f"**Also known as:** {', '.join(alt_names)}")
        lines.append(f"**Organism:** *{org_name}*" + (f" ({org_common})" if org_common else ""))
        if taxonomy:
            lines.append(f"**Lineage:** {' > '.join(taxonomy[-5:])}")
        lines.append(f"\n**Sequence:** {seq_len} amino acids | MW: {seq_mass/1000:.1f} kDa")

        if function_text:
            lines.append(f"\n### Function\n{function_text[:500]}")

        if subcell:
            lines.append(f"\n### Subcellular Location\n{', '.join(set(subcell))}")

        if catalytic:
            lines.append(f"\n### Catalytic Activity\n" + "\n".join(f"- {c}" for c in catalytic if c))

        if domains:
            lines.append(f"\n### Domains & Regions\n" + "\n".join(f"- {d}" for d in domains if d))

        if active_sites:
            lines.append(f"\n### Active Site Positions\nResidues: {', '.join(active_sites)}")

        if ptms:
            lines.append(f"\n### Post-Translational Modifications\n" + "\n".join(f"- {p}" for p in ptms if p))

        if diseases:
            lines.append(f"\n### Disease Associations\n" + "\n".join(f"- {d}" for d in diseases if d))

        if interactions:
            lines.append(f"\n### Known Interactions\n" + ", ".join(interactions))

        if pdb_ids:
            lines.append(f"\n### 3D Structures (PDB)\n" + ", ".join(f"[{p}](https://www.rcsb.org/structure/{p})" for p in pdb_ids))

        if go_terms:
            lines.append(f"\n### Gene Ontology\n" + "\n".join(f"- {t}" for t in go_terms if t.strip("() ")))

        if pathway_text:
            lines.append(f"\n### Pathways\n{pathway_text[:300]}")

        if seq_value:
            lines.append(f"\n### Sequence (first 60 aa)\n```\n{seq_value[:60]}{'...' if len(seq_value) > 60 else ''}\n```")

        return "\n".join(lines)

    async def get_gene_proteins(
        self,
        gene: str,
        organism: str = "Homo sapiens",
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get all proteins encoded by a specific gene in an organism.
        :param gene: Gene symbol (e.g. 'TP53', 'BRCA1', 'EGFR', 'ACE2', 'APOE', 'TNF')
        :param organism: Organism name (default: Homo sapiens)
        :return: All protein isoforms and variants for this gene
        """
        return await self.search_proteins(
            query=f"gene:{gene}",
            organism=organism,
            reviewed_only=True,
            limit=10,
            __event_emitter__=__event_emitter__,
            __user__=__user__,
        )
