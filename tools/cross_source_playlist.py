"""
title: Cross-Source Playlist — Spotify → Local Library → Free Music Fallback
author: local-ai-stack
description: Author a playlist that gracefully degrades across sources for each track. The model gives a list of "Artist - Title" pairs; the tool tries (1) Spotify (when CLIENT_ID is set) → adds to a Spotify playlist; (2) the local MusicBee library — if the file is on disk, append to an .m3u8; (3) free_music — search FMA / Internet Archive Audio for a CC-licensed match. Returns a per-track resolution log so the model knows which fallback was used.
required_open_webui_version: 0.4.0
requirements: httpx, mutagen
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

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


_AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav"}


class Tools:
    class Valves(BaseModel):
        LOCAL_LIBRARY: str = Field(
            default=str(Path.home() / "Library" / "Music"),
            description="Local MusicBee music root.",
        )
        PLAYLIST_DIR: str = Field(
            default=str(Path.home() / "Music" / "Playlists"),
            description="Where to save .m3u8 files.",
        )
        ENABLE_SPOTIFY: bool = Field(default=True)
        ENABLE_LOCAL: bool = Field(default=True)
        ENABLE_FREE_MUSIC: bool = Field(default=True)

    def __init__(self):
        self.valves = self.Valves()

    def _scan_local(self) -> dict[str, Path]:
        """Index local library by 'artist - title' (lowercased)."""
        idx: dict[str, Path] = {}
        root = Path(self.valves.LOCAL_LIBRARY).expanduser()
        if not root.exists():
            return idx
        try:
            import mutagen
        except ImportError:
            return idx
        for p in root.rglob("*"):
            if not p.is_file() or p.suffix.lower() not in _AUDIO_EXTS:
                continue
            try:
                f = mutagen.File(p, easy=True)
                tags = dict(f.tags or {}) if f else {}
            except Exception:
                continue
            first = lambda k: (tags.get(k) or [""])[0] if isinstance(tags.get(k), list) else (tags.get(k) or "")
            artist = str(first("artist") or "").lower().strip()
            title = str(first("title") or p.stem).lower().strip()
            if artist and title:
                idx[f"{artist} - {title}"] = p
        return idx

    @staticmethod
    def _norm(s: str) -> str:
        return re.sub(r"\s+", " ", s).strip().lower()

    async def author(
        self,
        playlist_name: str,
        tracks: list[str],
        spotify_playlist_id: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Build a cross-source playlist from a list of "Artist - Title" entries.
        :param playlist_name: Used for the local .m3u8 filename + log header.
        :param tracks: List of "Artist - Title" strings.
        :param spotify_playlist_id: Existing Spotify playlist id to append to. Empty = skip Spotify add.
        :return: Per-track resolution log + list of generated artifact paths.
        """
        spotify = None
        free_music = None
        if self.valves.ENABLE_SPOTIFY:
            try: spotify = _load_tool("spotify")
            except Exception: spotify = None
        if self.valves.ENABLE_FREE_MUSIC:
            try: free_music = _load_tool("free_music")
            except Exception: free_music = None
        local_idx = self._scan_local() if self.valves.ENABLE_LOCAL else {}

        resolved_local: list[Path] = []
        spotify_uris: list[str] = []
        free_links: list[str] = []
        log: list[str] = [f"# Cross-source playlist: {playlist_name}\n"]

        for track in tracks:
            label = track.strip()
            log.append(f"\n• {label}")
            key = self._norm(label)

            # 1) Spotify search.
            if spotify is not None:
                try:
                    text = await spotify.search(label, kind="track", limit=1)
                    m = re.search(r"uri=(spotify:track:\S+)", text)
                    if m:
                        spotify_uris.append(m.group(1))
                        log.append(f"  spotify   ✓  {m.group(1)}")
                        if spotify_playlist_id:
                            await spotify.add_to_playlist(spotify_playlist_id, [m.group(1)])
                        continue
                    log.append(f"  spotify   ·  (no match)")
                except Exception as e:
                    log.append(f"  spotify   ·  (error: {e})")

            # 2) Local MusicBee library.
            if local_idx and key in local_idx:
                resolved_local.append(local_idx[key])
                log.append(f"  local     ✓  {local_idx[key]}")
                continue
            if local_idx:
                # Loose match.
                close = next((p for k, p in local_idx.items()
                              if key.split(" - ")[-1] in k), None)
                if close:
                    resolved_local.append(close)
                    log.append(f"  local~    ✓  {close}  (loose match)")
                    continue
                log.append("  local     ·  (no match)")

            # 3) Free Music fallback.
            if free_music is not None:
                try:
                    fma = await free_music.search_fma(label)
                    m = re.search(r"https?://\S+", fma)
                    if m:
                        free_links.append(m.group(0))
                        log.append(f"  fma       ✓  {m.group(0)}")
                        continue
                    ia = await free_music.search_internet_archive(label, only_lossless=False)
                    m = re.search(r"https?://archive\.org\S+", ia)
                    if m:
                        free_links.append(m.group(0))
                        log.append(f"  ia        ✓  {m.group(0)}")
                        continue
                    log.append("  free      ·  (no match)")
                except Exception as e:
                    log.append(f"  free      ·  (error: {e})")

            log.append("  …unresolved")

        # Emit local .m3u8.
        if resolved_local:
            outdir = Path(self.valves.PLAYLIST_DIR).expanduser()
            outdir.mkdir(parents=True, exist_ok=True)
            m3u = outdir / f"{playlist_name}.m3u8"
            m3u.write_text("#EXTM3U\n" + "\n".join(str(p) for p in resolved_local), encoding="utf-8")
            log.append(f"\nwrote local m3u8: {m3u}")
        if spotify_uris:
            log.append(f"\nspotify uris ({len(spotify_uris)}): {spotify_uris[:5]}…")
        if free_links:
            log.append(f"\nfree-music links ({len(free_links)}): {free_links[:5]}…")
        return "\n".join(log)
