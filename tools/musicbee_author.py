"""
title: MusicBee Author — Playlists + Vibe-Based Streaming
author: local-ai-stack
description: Build .m3u8 playlists from a list of audio files (or a whole directory) and stream them in MusicBee. Supports random shuffle, weighted random by file mtime ("recent first"), simple tag-based filtering (genre/year/artist substring) when mutagen is installed, and chained "search → playlist → stream" flows. Pair with media_library to organize the library first, then this tool to author the listening session.
required_open_webui_version: 0.4.0
requirements: mutagen
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

import os
import random
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


_AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".wv", ".alac", ".aiff"}


class Tools:
    class Valves(BaseModel):
        MUSICBEE_EXE: str = Field(
            default=r"C:\Program Files (x86)\MusicBee\MusicBee.exe",
            description="Path to MusicBee.exe — used to auto-stream a generated playlist.",
        )
        PLAYLIST_DIR: str = Field(
            default=str(Path.home() / "Music" / "Playlists"),
            description="Where to save generated .m3u8 playlist files.",
        )
        MUSIC_LIBRARY: str = Field(
            default=str(Path.home() / "Library" / "Music"),
            description="Default music library to scan when a directory isn't supplied.",
        )

    def __init__(self):
        self.valves = self.Valves()

    # ── Helpers ───────────────────────────────────────────────────────────

    def _which(self) -> str:
        exe = self.valves.MUSICBEE_EXE
        if Path(exe).exists():
            return exe
        located = shutil.which(exe) or shutil.which("MusicBee")
        if not located:
            raise FileNotFoundError(f"MusicBee binary not found: {exe}")
        return located

    def _scan_audio(self, root: Path, recursive: bool = True) -> list[Path]:
        if not root.exists():
            return []
        if root.is_file():
            return [root] if root.suffix.lower() in _AUDIO_EXTS else []
        if recursive:
            return [p for p in root.rglob("*")
                    if p.is_file() and p.suffix.lower() in _AUDIO_EXTS]
        return [p for p in root.iterdir()
                if p.is_file() and p.suffix.lower() in _AUDIO_EXTS]

    def _read_tags(self, p: Path) -> dict:
        try:
            import mutagen
        except ImportError:
            return {}
        try:
            f = mutagen.File(p, easy=True)
            return dict(f.tags or {}) if f else {}
        except Exception:
            return {}

    def _spawn(self, args: list[str]) -> int:
        kwargs: dict = {"close_fds": True}
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x00000008 | 0x00000200
        else:
            kwargs["start_new_session"] = True
        return subprocess.Popen([self._which(), *args], **kwargs).pid

    def _write_m3u8(self, name: str, tracks: list[Path]) -> Path:
        out_dir = Path(self.valves.PLAYLIST_DIR).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{name}.m3u8"
        lines = ["#EXTM3U"]
        for t in tracks:
            tags = self._read_tags(t)
            artist = (tags.get("artist") or [""])[0] if isinstance(tags.get("artist"), list) else tags.get("artist", "")
            title = (tags.get("title") or [""])[0] if isinstance(tags.get("title"), list) else tags.get("title", "")
            label = f"{artist} - {title}".strip(" -") or t.stem
            # Duration not computed (would require parsing every file).
            lines.append(f"#EXTINF:-1,{label}")
            lines.append(str(t))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    # ── Authoring ─────────────────────────────────────────────────────────

    def create_playlist_from_files(
        self,
        name: str,
        tracks: list[str],
        stream: bool = True,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Build a playlist from an explicit list of audio file paths.
        :param name: Playlist name (also the filename, .m3u8 added automatically).
        :param tracks: Absolute paths to audio files in playback order.
        :param stream: When True, opens the playlist in MusicBee immediately.
        :return: Path to the playlist + (if streaming) the MusicBee PID.
        """
        paths = [Path(t).expanduser().resolve() for t in tracks]
        missing = [p for p in paths if not p.exists()]
        if missing:
            return f"Refused: {len(missing)} files don't exist (e.g. {missing[0]})"
        path = self._write_m3u8(name, paths)
        out = f"playlist -> {path} ({len(paths)} tracks)"
        if stream:
            pid = self._spawn([str(path)])
            out += f"\nstreaming in MusicBee (pid={pid})"
        return out

    def create_playlist_from_directory(
        self,
        name: str,
        directory: str = "",
        recursive: bool = True,
        shuffle: bool = False,
        limit: int = 0,
        artist_contains: str = "",
        genre_contains: str = "",
        year_min: int = 0,
        year_max: int = 0,
        stream: bool = True,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Walk a directory (defaults to MUSIC_LIBRARY), apply optional tag
        filters, and write an .m3u8. Filters are AND-ed.
        :param name: Playlist name.
        :param directory: Source directory. Empty = MUSIC_LIBRARY.
        :param recursive: Walk subdirectories.
        :param shuffle: Randomize track order.
        :param limit: Cap the playlist length. 0 = unlimited.
        :param artist_contains: Case-insensitive artist substring filter.
        :param genre_contains: Case-insensitive genre substring filter.
        :param year_min: Minimum year (inclusive). 0 = no lower bound.
        :param year_max: Maximum year (inclusive). 0 = no upper bound.
        :param stream: Open in MusicBee after creation.
        :return: Confirmation with track count and path.
        """
        src = Path(directory or self.valves.MUSIC_LIBRARY).expanduser().resolve()
        files = self._scan_audio(src, recursive=recursive)
        if not files:
            return f"(no audio under {src})"

        if any([artist_contains, genre_contains, year_min, year_max]):
            kept: list[Path] = []
            for p in files:
                tags = self._read_tags(p)
                first = lambda k: (tags.get(k) or [""])[0] if isinstance(tags.get(k), list) else (tags.get(k) or "")
                if artist_contains and artist_contains.lower() not in str(first("artist")).lower():
                    continue
                if genre_contains and genre_contains.lower() not in str(first("genre")).lower():
                    continue
                yr = str(first("date") or first("year"))[:4]
                try:
                    y = int(yr)
                    if year_min and y < year_min: continue
                    if year_max and y > year_max: continue
                except (TypeError, ValueError):
                    if year_min or year_max:
                        continue
                kept.append(p)
            files = kept

        if shuffle:
            random.shuffle(files)
        if limit > 0:
            files = files[:limit]
        if not files:
            return "(no tracks survived the filter)"

        path = self._write_m3u8(name, files)
        out = f"playlist -> {path}\n  {len(files)} tracks (from {src})"
        if stream:
            pid = self._spawn([str(path)])
            out += f"\n  streaming in MusicBee (pid={pid})"
        return out

    def shuffle_library(
        self,
        name: str = "shuffle",
        directory: str = "",
        limit: int = 100,
        stream: bool = True,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Quick "shuffle the library" call — convenience wrapper around
        create_playlist_from_directory with shuffle=True.
        :param name: Playlist name. Default "shuffle".
        :param directory: Source directory. Empty = MUSIC_LIBRARY.
        :param limit: How many tracks to pick. Default 100.
        :param stream: Open in MusicBee.
        :return: Confirmation.
        """
        return self.create_playlist_from_directory(
            name=name, directory=directory, recursive=True,
            shuffle=True, limit=limit, stream=stream,
        )

    def list_playlists(self, __user__: Optional[dict] = None) -> str:
        """
        List previously generated .m3u8 playlists in PLAYLIST_DIR.
        :return: Newline-delimited paths.
        """
        d = Path(self.valves.PLAYLIST_DIR).expanduser()
        if not d.exists():
            return f"(no playlist dir yet) {d}"
        rows = [str(p) for p in sorted(d.glob("*.m3u8"))]
        return "\n".join(rows) if rows else "(no playlists)"

    def stream_playlist(self, playlist_path: str, __user__: Optional[dict] = None) -> str:
        """
        Open an existing .m3u8 (or .m3u / .pls) in MusicBee.
        :param playlist_path: Path to the playlist file.
        :return: PID of the MusicBee process.
        """
        p = Path(playlist_path).expanduser().resolve()
        if not p.exists():
            return f"Not found: {p}"
        return f"streaming -> MusicBee (pid={self._spawn([str(p)])})"
