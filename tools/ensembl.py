"""
title: Ensembl — Genomics, Gene Sequences & Variants
author: local-ai-stack
description: Query the Ensembl genome database for 250+ species. Look up genes by symbol or ID, retrieve DNA and protein sequences, explore genetic variants (SNPs), find orthologs across species, get gene expression data, and map genomic coordinates. Essential for bioinformatics, genetics research, and molecular biology. No API key required.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from typing import Callable, Any, Optional
from pydantic import BaseModel, Field

BASE = "https://rest.ensembl.org"
HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}


class Tools:
    class Valves(BaseModel):
        DEFAULT_SPECIES: str = Field(
            default="human",
            description="Default species for queries (e.g. 'human', 'mouse', 'zebrafish', 'fly', 'worm', 'rat')",
        )

    def __init__(self):
        self.valves = self.Valves()

    async def lookup_gene(
        self,
        gene: str,
        species: str = "",
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Look up a gene by symbol or Ensembl ID to get its genomic coordinates, biotype, transcripts, and description.
        :param gene: Gene symbol (e.g. 'BRCA1', 'TP53', 'EGFR', 'ACE2', 'APOE') or Ensembl ID (e.g. 'ENSG00000012048')
        :param species: Species name (e.g. 'human', 'mouse', 'rat', 'zebrafish', 'fly') — default from settings
        :return: Gene coordinates, description, transcript count, biotype, and associated phenotypes
        """
        species = species or self.valves.DEFAULT_SPECIES

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Looking up gene {gene} in Ensembl...", "done": False}})

        try:
            async with httpx.AsyncClient(timeout=15, headers=HEADERS) as client:
                # Try by symbol first
                if not gene.startswith("ENS"):
                    xref_resp = await client.get(
                        f"{BASE}/xrefs/symbol/{species}/{gene}",
                        params={"content-type": "application/json"},
                    )
                    xrefs = xref_resp.json() if xref_resp.status_code == 200 else []
                    gene_ids = [x["id"] for x in xrefs if x.get("type") == "gene"]
                    if not gene_ids:
                        # Fall back to search
                        search_resp = await client.get(
                            f"{BASE}/lookup/symbol/{species}/{gene}",
                            params={"content-type": "application/json", "expand": 1},
                        )
                        if search_resp.status_code == 200:
                            data = search_resp.json()
                            gene_ids = [data.get("id", "")]
                    ensembl_id = gene_ids[0] if gene_ids else gene
                else:
                    ensembl_id = gene

                # Get full gene details
                resp = await client.get(
                    f"{BASE}/lookup/id/{ensembl_id}",
                    params={"content-type": "application/json", "expand": 1},
                )
                if resp.status_code == 404:
                    return f"Gene '{gene}' not found in Ensembl for species '{species}'."
                resp.raise_for_status()
                data = resp.json()

        except Exception as e:
            return f"Ensembl gene lookup error: {str(e)}"

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        gene_id = data.get("id", "")
        display_name = data.get("display_name", gene)
        description = data.get("description", "")
        biotype = data.get("biotype", "")
        chrom = data.get("seq_region_name", "")
        start = data.get("start", "")
        end = data.get("end", "")
        strand = "+" if data.get("strand", 1) == 1 else "-"
        assembly = data.get("assembly_name", "")
        version = data.get("version", "")
        source = data.get("source", "")
        transcripts = data.get("Transcript", [])
        n_transcripts = len(transcripts) if transcripts else 0
        canonical = next((t for t in transcripts if t.get("is_canonical")), None) if transcripts else None

        lines = [f"## Ensembl Gene: {display_name} ({gene_id})\n"]
        lines.append(f"**Description:** {description.split('[')[0].strip()}")
        lines.append(f"**Biotype:** {biotype} | **Source:** {source}")
        lines.append(f"**Location:** Chromosome {chrom}:{start:,}-{end:,} ({strand}) | Assembly: {assembly}")
        lines.append(f"**Gene length:** {(end-start):,} bp" if isinstance(start, int) and isinstance(end, int) else "")
        lines.append(f"**Ensembl version:** {gene_id}.{version}")
        lines.append(f"**Transcripts:** {n_transcripts}")

        if canonical:
            can_id = canonical.get("id", "")
            can_len = canonical.get("length", "")
            can_biotype = canonical.get("biotype", "")
            exons = canonical.get("Exon", [])
            n_exons = len(exons)
            lines.append(f"\n### Canonical Transcript: {can_id}")
            lines.append(f"- Biotype: {can_biotype}")
            lines.append(f"- Length: {can_len:,} bp" if can_len else "")
            lines.append(f"- Exons: {n_exons}")

        lines.append(f"\n[View on Ensembl](https://www.ensembl.org/{species.replace(' ','_').capitalize()}/Gene/Summary?g={gene_id})")
        return "\n".join(l for l in lines if l)

    async def get_sequence(
        self,
        gene_id: str,
        sequence_type: str = "cdna",
        species: str = "",
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Retrieve the DNA or protein sequence for a gene or transcript from Ensembl.
        :param gene_id: Ensembl gene ID (ENSG...), transcript ID (ENST...), or gene symbol (BRCA1, TP53)
        :param sequence_type: Sequence type: 'genomic' (DNA), 'cdna' (coding RNA), 'cds' (coding sequence only), 'protein' (amino acids)
        :param species: Species for symbol lookups (default from settings)
        :return: Sequence in FASTA format with length and GC content (for DNA)
        """
        species = species or self.valves.DEFAULT_SPECIES

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Fetching {sequence_type} sequence for {gene_id}...", "done": False}})

        # Resolve symbol to Ensembl ID if needed
        ensembl_id = gene_id
        if not gene_id.startswith("ENS"):
            try:
                async with httpx.AsyncClient(timeout=15, headers=HEADERS) as client:
                    resp = await client.get(
                        f"{BASE}/lookup/symbol/{species}/{gene_id}",
                        params={"content-type": "application/json"},
                    )
                    if resp.status_code == 200:
                        ensembl_id = resp.json().get("id", gene_id)
            except Exception:
                pass

        # For genes, use the canonical transcript
        endpoint = ensembl_id
        try:
            async with httpx.AsyncClient(timeout=20, headers=HEADERS) as client:
                resp = await client.get(
                    f"{BASE}/sequence/id/{endpoint}",
                    params={"content-type": "application/json", "type": sequence_type, "mask_feature": 1},
                )
                if resp.status_code == 404:
                    return f"No {sequence_type} sequence found for '{gene_id}'."
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            return f"Ensembl sequence error: {str(e)}"

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        seq = data.get("seq", "")
        seq_id = data.get("id", "")
        molecule = data.get("molecule", sequence_type)
        desc = data.get("desc", "")

        if not seq:
            return f"No sequence returned for {gene_id}."

        seq_len = len(seq)

        lines = [f"## {sequence_type.upper()} Sequence: {gene_id}\n"]
        lines.append(f"**Ensembl ID:** {seq_id} | **Molecule:** {molecule} | **Length:** {seq_len:,} bp/aa")
        if desc:
            lines.append(f"**Description:** {desc}")

        if sequence_type in ("genomic", "cdna", "cds") and seq:
            gc = (seq.count("G") + seq.count("C")) / len(seq) * 100 if seq else 0
            lines.append(f"**GC Content:** {gc:.1f}%")

        # Show first 200 characters
        display = seq[:200]
        lines.append(f"\n**Sequence (first 200/{seq_len:,}):**\n```\n>{seq_id} | {molecule}\n{display}{'...' if len(seq) > 200 else ''}\n```")

        return "\n".join(lines)

    async def get_variants(
        self,
        gene: str,
        species: str = "",
        consequence: str = "",
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get genetic variants (SNPs, indels) in a gene from the Ensembl Variation database.
        :param gene: Gene symbol or Ensembl ID (e.g. 'CFTR', 'HBB', 'LDLR', 'APOE')
        :param species: Species (default: human)
        :param consequence: Filter by variant consequence (e.g. 'missense_variant', 'stop_gained', 'synonymous_variant', 'frameshift_variant')
        :return: Variants with rsIDs, position, alleles, consequence type, and clinical significance
        """
        species = species or self.valves.DEFAULT_SPECIES

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Fetching variants for {gene}...", "done": False}})

        # Get Ensembl ID first
        ensembl_id = gene
        if not gene.startswith("ENS"):
            try:
                async with httpx.AsyncClient(timeout=15, headers=HEADERS) as client:
                    resp = await client.get(f"{BASE}/lookup/symbol/{species}/{gene}", params={"content-type": "application/json"})
                    if resp.status_code == 200:
                        ensembl_id = resp.json().get("id", gene)
            except Exception:
                pass

        params = {"content-type": "application/json"}
        if consequence:
            params["consequence_type"] = consequence

        try:
            async with httpx.AsyncClient(timeout=20, headers=HEADERS) as client:
                resp = await client.get(
                    f"{BASE}/overlap/id/{ensembl_id}",
                    params={**params, "feature": "variation"},
                )
                if resp.status_code == 404:
                    return f"No variant data found for '{gene}'."
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            return f"Ensembl variants error: {str(e)}"

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        if not data:
            return f"No variants found for '{gene}'" + (f" with consequence '{consequence}'" if consequence else "")

        lines = [f"## Variants in {gene} ({ensembl_id})\n"]
        lines.append(f"**Total variants:** {len(data)}")
        if consequence:
            lines.append(f"**Filter:** {consequence}\n")

        # Summarize by consequence type
        conseq_counts = {}
        for v in data:
            for c in (v.get("consequence_type", "unknown").split(",") if v.get("consequence_type") else ["unknown"]):
                c = c.strip()
                conseq_counts[c] = conseq_counts.get(c, 0) + 1

        lines.append("\n### Consequence Types")
        lines.append("| Consequence | Count |")
        lines.append("|------------|-------|")
        for c, cnt in sorted(conseq_counts.items(), key=lambda x: -x[1])[:10]:
            lines.append(f"| {c} | {cnt:,} |")

        # Show sample variants
        lines.append("\n### Sample Variants (first 20)\n")
        lines.append("| rsID | Position | Alleles | Consequence |")
        lines.append("|------|----------|---------|------------|")
        for v in data[:20]:
            rs_id = v.get("id", "")
            start = v.get("start", "")
            end = v.get("end", "")
            alleles = v.get("alleles", [])
            allele_str = "/".join(alleles[:4]) if alleles else "—"
            conseq = v.get("consequence_type", "")

            rs_link = f"[{rs_id}](https://www.ensembl.org/Homo_sapiens/Variation/Summary?v={rs_id})" if rs_id.startswith("rs") else rs_id
            lines.append(f"| {rs_link} | {start} | {allele_str} | {conseq} |")

        return "\n".join(lines)

    async def get_orthologs(
        self,
        gene: str,
        target_species: str = "",
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Find orthologs (evolutionary equivalents) of a gene across different species.
        :param gene: Gene symbol or Ensembl ID (e.g. 'TP53', 'EGFR', 'FOXP2', 'BRCA1')
        :param target_species: Filter to a specific target species (e.g. 'mouse', 'zebrafish', 'chimpanzee') or blank for all
        :return: Ortholog genes in other species with Ensembl IDs, similarity scores, and type (1:1, 1:many)
        """
        source_species = self.valves.DEFAULT_SPECIES

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Finding orthologs for {gene}...", "done": False}})

        ensembl_id = gene
        if not gene.startswith("ENS"):
            try:
                async with httpx.AsyncClient(timeout=15, headers=HEADERS) as client:
                    resp = await client.get(f"{BASE}/lookup/symbol/{source_species}/{gene}", params={"content-type": "application/json"})
                    if resp.status_code == 200:
                        ensembl_id = resp.json().get("id", gene)
            except Exception:
                pass

        params = {"content-type": "application/json", "type": "orthologues"}
        if target_species:
            params["target_species"] = target_species

        try:
            async with httpx.AsyncClient(timeout=20, headers=HEADERS) as client:
                resp = await client.get(f"{BASE}/homology/id/{ensembl_id}", params=params)
                if resp.status_code == 404:
                    return f"No ortholog data found for '{gene}'."
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            return f"Ensembl ortholog error: {str(e)}"

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        homologies = data.get("data", [{}])[0].get("homologies", []) if data.get("data") else []

        if not homologies:
            return f"No orthologs found for '{gene}'."

        lines = [f"## Orthologs of {gene} ({ensembl_id})\n"]
        lines.append(f"**Found {len(homologies)} orthologous genes**\n")
        lines.append("| Target Species | Gene ID | Gene Name | Similarity (%) | Type |")
        lines.append("|---------------|---------|-----------|----------------|------|")

        for h in homologies[:30]:
            target = h.get("target", {})
            t_species = target.get("species", "").replace("_", " ").title()
            t_id = target.get("id", "")
            t_name = target.get("display_label", "")
            similarity = h.get("target", {}).get("perc_id", "")
            h_type = h.get("type", "")

            similarity_str = f"{similarity:.1f}%" if similarity else "—"
            type_label = h_type.replace("_", " ")
            lines.append(f"| *{t_species}* | {t_id} | {t_name} | {similarity_str} | {type_label} |")

        return "\n".join(lines)
