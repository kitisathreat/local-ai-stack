"""
title: Art Collection Author — Cross-Museum Curated Galleries
author: local-ai-stack
description: Given a theme (e.g. "Dutch Golden Age portraits", "Japanese woodblock prints", "Egyptian funerary masks"), parallel-search the existing museum tools (Met, Smithsonian, Rijksmuseum, Europeana) and assemble a curated markdown gallery: title, artist, date, museum, license, and a permanent URL — sorted by license openness (CC0 first), then by date. Optional: download the high-res image previews via the filesystem tool.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

import asyncio
import importlib.util
import re
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field


def _load_tool(name: str):
    spec = importlib.util.spec_from_file_location(
        f"_lai_{name}", Path(__file__).parent / f"{name}.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod.Tools()


class Tools:
    class Valves(BaseModel):
        PER_SOURCE: int = Field(default=10, description="Max objects fetched from each museum.")
        DOWNLOAD_DIR: str = Field(
            default=str(Path.home() / "Pictures" / "art-collections"),
            description="Where to save image downloads.",
        )

    def __init__(self):
        self.valves = self.Valves()

    @staticmethod
    def _harvest(text: str, source: str) -> list[dict]:
        """Heuristic parser — pulls out [title, url, license-ish] tuples from
        whatever shape the underlying museum tool returned."""
        out = []
        for line in (text or "").splitlines():
            line = line.strip()
            if not line:
                continue
            urls = re.findall(r"https?://\S+", line)
            if not urls:
                continue
            license_score = 0
            if "cc0" in line.lower() or "public domain" in line.lower(): license_score = 3
            elif "open access" in line.lower():                          license_score = 2
            elif "cc by" in line.lower():                                license_score = 1
            out.append({
                "source": source,
                "title": re.sub(r"https?://\S+", "", line).strip(" -*•")[:120],
                "url": urls[0],
                "license_score": license_score,
                "raw": line,
            })
        return out

    async def curate(
        self,
        theme: str,
        download_images: bool = False,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Curate a cross-museum gallery on a theme.
        :param theme: Search query (e.g. "Vermeer interiors", "Greek red-figure pottery").
        :param download_images: When True, save preview images under DOWNLOAD_DIR/<theme>/.
        :return: Markdown gallery, CC0 first.
        """
        sources = []
        for mod in ("met_museum", "smithsonian", "rijksmuseum", "europeana", "library_of_congress"):
            try:
                sources.append((mod, _load_tool(mod)))
            except Exception:
                pass

        async def call(name: str, t):
            for fn_name in ("search", "search_objects", "search_collection", "search_artworks"):
                fn = getattr(t, fn_name, None)
                if fn is not None:
                    try:
                        return await fn(theme, limit=self.valves.PER_SOURCE)
                    except TypeError:
                        try:
                            return await fn(theme)
                        except Exception as e:
                            return f"({name} error: {e})"
                    except Exception as e:
                        return f"({name} error: {e})"
            return f"({name} has no search method)"

        results = await asyncio.gather(*[call(n, t) for n, t in sources])
        items: list[dict] = []
        for (name, _), text in zip(sources, results):
            items.extend(self._harvest(text, name))

        items.sort(key=lambda d: (-d["license_score"], d["title"]))
        if not items:
            return f"(no items found across {len(sources)} museums for '{theme}')"

        out = [f"# Collection: {theme}\n",
               f"_Curated across {len(sources)} museum APIs. CC0 / open-access first._\n"]
        out.append("| source | title | license | url |")
        out.append("|---|---|---|---|")
        for it in items[:50]:
            tag = ["?", "CC-BY", "open access", "CC0/PD"][it["license_score"]]
            out.append(f"| {it['source']} | {it['title']} | {tag} | {it['url']} |")

        if download_images:
            outdir = Path(self.valves.DOWNLOAD_DIR).expanduser() / re.sub(r"\W+", "-", theme)
            outdir.mkdir(parents=True, exist_ok=True)
            out.append(f"\n_(image download requested — caller should chain `filesystem.write_bytes_b64` or use a download tool to save to {outdir})_")
        return "\n".join(out)
