"""
title: Metadata Enricher — MusicBrainz Lookup + Tag Write-Back
author: local-ai-stack
description: Walk an audio directory, identify each file's release/recording via MusicBrainz (free public API; no key needed) using its existing tag fingerprint, and write back canonical artist / album / title / track / date / genre / MBID tags via mutagen. Useful right after a download to fill in missing or sloppy tags before media_library re-organizes the files.
required_open_webui_version: 0.4.0
requirements: httpx, mutagen
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field


_AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav"}
_MB_BASE = "https://musicbrainz.org/ws/2"


class Tools:
    class Valves(BaseModel):
        UA: str = Field(
            default="local-ai-stack/1.0 (https://github.com/kitisathreat/local-ai-stack)",
            description="MusicBrainz requires a contact User-Agent.",
        )
        MIN_SCORE: int = Field(
            default=70,
            description="Minimum MusicBrainz match score 0-100 to accept (lower = looser match).",
        )
        TIMEOUT: int = Field(default=15)

    def __init__(self):
        self.valves = self.Valves()

    def _read_existing(self, p: Path) -> dict[str, str]:
        try:
            import mutagen
        except ImportError:
            return {}
        try:
            f = mutagen.File(p, easy=True)
            tags = dict(f.tags or {}) if f else {}
        except Exception:
            return {}
        first = lambda k: (tags.get(k) or [""])[0] if isinstance(tags.get(k), list) else (tags.get(k) or "")
        return {
            "artist": first("artist"),
            "album": first("album"),
            "title": first("title"),
            "tracknumber": first("tracknumber"),
            "date": first("date"),
        }

    async def _mb_search(self, client: httpx.AsyncClient, artist: str, title: str) -> dict | None:
        q_parts = []
        if title:  q_parts.append(f'recording:"{title}"')
        if artist: q_parts.append(f'artist:"{artist}"')
        if not q_parts:
            return None
        params = {"query": " AND ".join(q_parts), "fmt": "json", "limit": 5}
        try:
            r = await client.get(f"{_MB_BASE}/recording", params=params,
                                 headers={"User-Agent": self.valves.UA})
        except Exception:
            return None
        if r.status_code != 200:
            return None
        recs = (r.json() or {}).get("recordings") or []
        # Pick highest-score recording with at least one release.
        best = None
        for r2 in recs:
            score = int(r2.get("score") or 0)
            if score < self.valves.MIN_SCORE:
                continue
            if r2.get("releases"):
                if best is None or score > int(best.get("score") or 0):
                    best = r2
        return best

    def _write_tags(self, p: Path, fields: dict[str, str]) -> str:
        try:
            import mutagen
        except ImportError:
            return "mutagen not installed"
        try:
            f = mutagen.File(p, easy=True)
            if f is None:
                return "unsupported file format"
            for k, v in fields.items():
                if v:
                    f[k] = v
            f.save()
            return "ok"
        except Exception as e:
            return f"write error: {e}"

    async def enrich(
        self,
        directory: str,
        recursive: bool = True,
        dry_run: bool = False,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Walk a directory, look up each audio file on MusicBrainz, write back
        canonical tags (artist / album / title / tracknumber / date / mbid).
        :param directory: Audio directory to enrich.
        :param recursive: Walk subdirectories.
        :param dry_run: Plan only — don't write tags.
        :return: Per-file action log.
        """
        d = Path(directory).expanduser().resolve()
        if not d.exists():
            return f"Not found: {d}"
        files = (
            [p for p in d.rglob("*") if p.is_file() and p.suffix.lower() in _AUDIO_EXTS]
            if recursive else
            [p for p in d.iterdir() if p.is_file() and p.suffix.lower() in _AUDIO_EXTS]
        )
        if not files:
            return f"(no audio under {d})"
        log = []
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT) as client:
            for p in files:
                cur = self._read_existing(p)
                rec = await self._mb_search(client, cur.get("artist", ""), cur.get("title", ""))
                if not rec:
                    log.append(f"-- no match  {p.name}")
                    continue
                rel = (rec.get("releases") or [{}])[0]
                fields = {
                    "title":       rec.get("title", ""),
                    "artist":      ", ".join(a["name"] for a in rec.get("artist-credit") or [] if isinstance(a, dict) and "name" in a),
                    "album":       rel.get("title", ""),
                    "tracknumber": "",
                    "date":        rel.get("date", ""),
                    "musicbrainz_trackid": rec.get("id", ""),
                }
                action = "would-write" if dry_run else self._write_tags(p, fields)
                log.append(
                    f"{action:<11} {p.name}  ←  {fields.get('artist','?')} / "
                    f"{fields.get('album','?')} / {fields.get('title','?')} ({fields.get('date','?')})"
                )
        return f"{len(files)} files\n" + "\n".join(log)
