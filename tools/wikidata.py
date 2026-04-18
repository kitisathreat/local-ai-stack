"""
title: Wikidata — Structured Knowledge Graph
author: local-ai-stack
description: Query Wikidata, the structured knowledge graph behind Wikipedia. Look up entities by name, fetch claims/statements, and run SPARQL queries over 100M+ items. No API key required.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from pydantic import BaseModel, Field
from typing import Callable, Any, Optional


WD_API = "https://www.wikidata.org/w/api.php"
WD_ENTITY = "https://www.wikidata.org/wiki/Special:EntityData"
WD_SPARQL = "https://query.wikidata.org/sparql"
UA = "local-ai-stack/1.0 (wikidata tool)"


class Tools:
    class Valves(BaseModel):
        MAX_RESULTS: int = Field(default=5, description="Maximum search results")
        LANGUAGE: str = Field(default="en", description="Preferred language code")

    def __init__(self):
        self.valves = self.Valves()

    async def search_entity(
        self,
        query: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search Wikidata for entities by name and return their QIDs and descriptions.
        :param query: Name or keyword (e.g. "Marie Curie", "Apple Inc", "Python programming language")
        :return: Top matches with QID, label, description, and wiki link
        """
        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Searching Wikidata: {query}", "done": False}})
        params = {
            "action": "wbsearchentities", "search": query,
            "language": self.valves.LANGUAGE, "format": "json",
            "limit": self.valves.MAX_RESULTS, "type": "item",
        }
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(WD_API, params=params, headers={"User-Agent": UA})
                r.raise_for_status()
                data = r.json()
            hits = data.get("search", [])
            if not hits:
                return f"No Wikidata entities found for: {query}"
            lines = [f"## Wikidata: {query}\n"]
            for h in hits:
                qid = h.get("id", "")
                label = h.get("label", "")
                desc = h.get("description", "")
                url = h.get("concepturi", f"https://www.wikidata.org/wiki/{qid}")
                lines.append(f"**{label}** ({qid})")
                if desc:
                    lines.append(f"   {desc}")
                lines.append(f"   {url}\n")
            return "\n".join(lines)
        except Exception as e:
            return f"Wikidata error: {e}"

    async def get_entity(
        self,
        qid: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Fetch an entity's claims and key properties by its QID.
        :param qid: Wikidata entity ID (e.g. "Q937" for Einstein, "Q76" for Obama)
        :return: Label, description, and selected statements (P-values)
        """
        qid = qid.strip().upper()
        if not qid.startswith("Q"):
            return "Wikidata entity IDs start with 'Q'. Use search_entity first."
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(f"{WD_ENTITY}/{qid}.json", headers={"User-Agent": UA})
                r.raise_for_status()
                data = r.json()
            ent = data.get("entities", {}).get(qid, {})
            lang = self.valves.LANGUAGE
            label = ent.get("labels", {}).get(lang, {}).get("value", qid)
            desc = ent.get("descriptions", {}).get(lang, {}).get("value", "")
            claims = ent.get("claims", {})
            aliases = [a["value"] for a in ent.get("aliases", {}).get(lang, [])][:5]

            lines = [f"## {label} ({qid})"]
            if desc:
                lines.append(f"\n{desc}")
            if aliases:
                lines.append(f"\n**Also known as:** {', '.join(aliases)}")
            lines.append(f"\n**Statements:** {len(claims)} properties")
            shown = 0
            for pid, stmts in claims.items():
                if shown >= 15:
                    break
                for s in stmts[:1]:
                    mv = s.get("mainsnak", {}).get("datavalue", {}).get("value")
                    if mv is None:
                        continue
                    if isinstance(mv, dict):
                        if "id" in mv:
                            val = mv["id"]
                        elif "time" in mv:
                            val = mv["time"].lstrip("+").split("T")[0]
                        elif "amount" in mv:
                            val = f"{mv['amount']} {mv.get('unit', '').rsplit('/', 1)[-1]}"
                        elif "text" in mv:
                            val = mv["text"]
                        else:
                            val = str(mv)[:80]
                    else:
                        val = str(mv)[:80]
                    lines.append(f"- {pid}: {val}")
                    shown += 1
            lines.append(f"\n🔗 https://www.wikidata.org/wiki/{qid}")
            return "\n".join(lines)
        except Exception as e:
            return f"Wikidata error: {e}"

    async def sparql(
        self,
        query: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Run a raw SPARQL query against the Wikidata Query Service.
        :param query: A SPARQL query string (SELECT ?x ... WHERE { ... } LIMIT 20)
        :return: Bindings as a simple table
        """
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(
                    WD_SPARQL,
                    params={"query": query, "format": "json"},
                    headers={"User-Agent": UA, "Accept": "application/sparql-results+json"},
                )
                r.raise_for_status()
                data = r.json()
            vars_ = data.get("head", {}).get("vars", [])
            rows = data.get("results", {}).get("bindings", [])
            if not rows:
                return "SPARQL: no results."
            lines = ["## SPARQL Results\n", "| " + " | ".join(vars_) + " |", "|" + "|".join("---" for _ in vars_) + "|"]
            for row in rows[:50]:
                cells = []
                for v in vars_:
                    cells.append(str(row.get(v, {}).get("value", ""))[:80])
                lines.append("| " + " | ".join(cells) + " |")
            if len(rows) > 50:
                lines.append(f"\n_Truncated to 50 of {len(rows)} rows._")
            return "\n".join(lines)
        except Exception as e:
            return f"SPARQL error: {e}"
