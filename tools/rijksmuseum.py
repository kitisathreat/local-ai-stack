"""
title: Rijksmuseum — Dutch Golden Age & Beyond
author: local-ai-stack
description: Search the Rijksmuseum's 700,000+ collection: Rembrandt, Vermeer, Van Gogh, Asian art, applied arts, photography. High-resolution CC0 images. Free API key required (instant signup).
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from pydantic import BaseModel, Field
from typing import Optional


BASE = "https://www.rijksmuseum.nl/api/en/collection"


class Tools:
    class Valves(BaseModel):
        API_KEY: str = Field(default="", description="Rijksmuseum API key (free at https://data.rijksmuseum.nl)")
        MAX_RESULTS: int = Field(default=8, description="Max results")

    def __init__(self):
        self.valves = self.Valves()

    async def search(
        self,
        query: str,
        maker: str = "",
        type: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search the Rijksmuseum collection.
        :param query: Keywords (e.g. "self portrait", "night watch", "tulip")
        :param maker: Optional artist (e.g. "Rembrandt van Rijn", "Johannes Vermeer")
        :param type: Optional object type (e.g. "painting", "drawing", "photograph")
        :return: Matching works with title, maker, date, and image
        """
        if not self.valves.API_KEY:
            return "Set RIJKSMUSEUM API_KEY valve (free at https://data.rijksmuseum.nl)."
        params = {"key": self.valves.API_KEY, "q": query, "ps": self.valves.MAX_RESULTS, "imgonly": "true"}
        if maker:
            params["involvedMaker"] = maker
        if type:
            params["type"] = type
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(BASE, params=params)
                r.raise_for_status()
                data = r.json()
            arts = data.get("artObjects", [])
            total = data.get("count", 0)
            if not arts:
                return f"No Rijksmuseum works for: {query}"
            lines = [f"## Rijksmuseum: {query} ({total:,} matches)\n"]
            for a in arts:
                title = a.get("title", "")
                principal_maker = a.get("principalOrFirstMaker", "")
                long_title = a.get("longTitle", "")
                img = (a.get("webImage") or {}).get("url", "")
                link = a.get("links", {}).get("web", "")
                lines.append(f"**{title}**")
                lines.append(f"   {principal_maker}")
                if long_title and long_title != title:
                    lines.append(f"   _{long_title}_")
                if img:
                    lines.append(f"   ![img]({img})")
                if link:
                    lines.append(f"   🔗 {link}\n")
            return "\n".join(lines)
        except Exception as e:
            return f"Rijksmuseum error: {e}"

    async def object(self, object_number: str, __user__: Optional[dict] = None) -> str:
        """
        Look up a Rijksmuseum object by its object number (e.g. "SK-C-5" for The Night Watch).
        :param object_number: Object number
        :return: Full record with description, dimensions, materials, colors
        """
        if not self.valves.API_KEY:
            return "Set RIJKSMUSEUM API_KEY valve."
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(f"{BASE}/{object_number}", params={"key": self.valves.API_KEY})
                r.raise_for_status()
                data = r.json()
            o = data.get("artObject", {})
            if not o:
                return f"Not found: {object_number}"
            lines = [f"## {o.get('title', '')}"]
            lines.append(f"**Maker:** {o.get('principalOrFirstMaker', '')}")
            lines.append(f"**Dated:** {o.get('dating', {}).get('presentingDate', '')}")
            desc = o.get("description") or o.get("plaqueDescriptionEnglish") or ""
            if desc:
                lines.append(f"\n{desc[:800]}")
            dims = "; ".join(f"{d.get('type','')} {d.get('value','')} {d.get('unit','')}" for d in o.get("dimensions", []))
            if dims:
                lines.append(f"\n**Dimensions:** {dims}")
            img = (o.get("webImage") or {}).get("url", "")
            if img:
                lines.append(f"\n![img]({img})")
            return "\n".join(lines)
        except Exception as e:
            return f"Rijksmuseum error: {e}"
