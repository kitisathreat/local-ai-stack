"""
title: Library Dedupe — Find Duplicate Music Files
author: local-ai-stack
description: Walk a music library and identify duplicate tracks (same artist + title + album, or same audio fingerprint when chromaprint/fpcalc is available). Ranks copies by quality (FLAC > 320kbps MP3 > 192kbps MP3 > anything else), keeps the best, and moves the rest into a `trash/` subfolder so the operator can review before permanent deletion. Always dry-runs by default.
required_open_webui_version: 0.4.0
requirements: mutagen
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

import shutil
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field


_AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".alac", ".aiff"}


def _human_size(n: int) -> str:
    n = float(n or 0); u = ["B", "KB", "MB", "GB", "TB"]; i = 0
    while n >= 1024 and i < len(u) - 1: n /= 1024; i += 1
    return f"{n:.2f} {u[i]}"


def _quality_rank(p: Path, tags: dict) -> int:
    """Higher = better. FLAC = 1000, MP3 by bitrate, others by extension."""
    ext = p.suffix.lower()
    if ext in (".flac", ".alac", ".wv", ".aiff", ".wav"):
        return 1000 + p.stat().st_size // 1_000_000
    if ext == ".mp3":
        try:
            import mutagen
            f = mutagen.File(p)
            br = int((f.info.bitrate or 0) // 1000) if f and f.info else 0
        except Exception:
            br = 0
        return 100 + br
    if ext == ".m4a":
        return 200
    if ext in (".ogg", ".opus"):
        return 150
    return 50


def _read_tags(p: Path) -> dict:
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
        "artist": str(first("artist") or "").lower().strip(),
        "album":  str(first("album") or "").lower().strip(),
        "title":  str(first("title") or "").lower().strip(),
    }


def _fingerprint(p: Path) -> str | None:
    """Chromaprint AcoustID fingerprint via fpcalc CLI, when available."""
    try:
        out = subprocess.run(
            ["fpcalc", "-length", "60", "-json", str(p)],
            capture_output=True, text=True, timeout=20,
        )
        if out.returncode != 0:
            return None
        import json
        return json.loads(out.stdout).get("fingerprint")
    except FileNotFoundError:
        return None
    except Exception:
        return None


class Tools:
    class Valves(BaseModel):
        TRASH_DIR_NAME: str = Field(
            default="_dedupe_trash",
            description="Subfolder name inside the library where loser copies move (relative to library root).",
        )
        USE_FINGERPRINT: bool = Field(
            default=False,
            description="When True, also compute chromaprint fingerprints (requires fpcalc on PATH).",
        )

    def __init__(self):
        self.valves = self.Valves()

    def find(
        self,
        directory: str,
        recursive: bool = True,
        dry_run: bool = True,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Find duplicate tracks. By default this is a dry-run that only
        prints the plan; pass dry_run=False to actually move losers to
        trash.
        :param directory: Music library root.
        :param recursive: Walk subdirectories.
        :param dry_run: Plan only.
        :return: Per-cluster log of kept vs. moved files.
        """
        d = Path(directory).expanduser().resolve()
        if not d.exists():
            return f"Not found: {d}"
        files = (
            [p for p in d.rglob("*") if p.is_file() and p.suffix.lower() in _AUDIO_EXTS]
            if recursive else
            [p for p in d.iterdir() if p.is_file() and p.suffix.lower() in _AUDIO_EXTS]
        )
        # Bucket by (artist, album, title) tuple.
        buckets: dict[tuple, list[Path]] = defaultdict(list)
        for p in files:
            t = _read_tags(p)
            key = (t.get("artist", ""), t.get("album", ""), t.get("title", "") or p.stem.lower())
            if not (key[0] or key[2]):
                continue   # Skip unidentifiable files
            buckets[key].append(p)

        # Optionally collapse near-duplicates by chromaprint.
        if self.valves.USE_FINGERPRINT:
            fp_buckets: dict[str, list[Path]] = defaultdict(list)
            for paths in buckets.values():
                for p in paths:
                    fp = _fingerprint(p)
                    if fp:
                        fp_buckets[fp[:120]].append(p)
            # Merge fp clusters that share the same prefix.
            for fp, paths in fp_buckets.items():
                if len(paths) > 1:
                    buckets[(f"fp:{fp[:8]}", "", "")] = list(set(paths))

        trash = d / self.valves.TRASH_DIR_NAME
        log = []
        cluster_n = 0
        moved = 0
        kept_total = 0
        for key, paths in buckets.items():
            if len(paths) < 2:
                continue
            cluster_n += 1
            ranked = sorted(paths, key=lambda p: _quality_rank(p, _read_tags(p)), reverse=True)
            keep = ranked[0]
            losers = ranked[1:]
            kept_total += 1
            log.append(f"\n— cluster {cluster_n}: {key}")
            log.append(f"  KEEP   {keep}  ({_human_size(keep.stat().st_size)})")
            for p in losers:
                if dry_run:
                    log.append(f"  TRASH  {p}  ({_human_size(p.stat().st_size)})")
                else:
                    rel = p.relative_to(d) if d in p.parents else p.name
                    dest = trash / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(p), str(dest))
                    log.append(f"  MOVED  {p} -> {dest}")
                    moved += 1
        if cluster_n == 0:
            return "(no duplicate clusters found)"
        head = (
            f"{cluster_n} duplicate clusters, kept {kept_total}, "
            f"{'planned' if dry_run else 'moved'} {moved if not dry_run else sum(len(v)-1 for v in buckets.values() if len(v)>1)}"
            f" loser file(s)"
        )
        return head + "\n" + "\n".join(log)
