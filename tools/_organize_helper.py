"""Lazy bridge to the media_library tool.

Files starting with `_` are skipped by the tool registry so this module
isn't surfaced to the model directly. Per-tool helpers (soulseek,
qobuz_dl, free_music, annas_archive, qbittorrent) call into here from
their own `download_and_organize` methods to chain a fresh download
into the universal organizer without needing a circular import.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


_CACHE: dict[str, Any] = {}


def _media_library() -> Any:
    """Return a singleton media_library Tools() instance, lazy-loaded."""
    inst = _CACHE.get("media_library")
    if inst is not None:
        return inst
    here = Path(__file__).parent
    spec = importlib.util.spec_from_file_location(
        "_lai_media_library_inline", here / "media_library.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("media_library.py not found beside this helper")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_lai_media_library_inline"] = mod
    spec.loader.exec_module(mod)
    inst = mod.Tools()
    _CACHE["media_library"] = inst
    return inst


def organize(path: str, kind: str = "auto", copy: bool = False, dry_run: bool = False) -> str:
    """Run the named organizer on `path`.

    `kind` is one of: audio, books, films, tv, audiobooks, auto.
    """
    ml = _media_library()
    if kind == "audio":      return ml.organize_audio(path, copy=copy, dry_run=dry_run)
    if kind == "audiobooks": return ml.organize_audio(path, copy=copy, dry_run=dry_run, as_audiobooks=True)
    if kind == "books":      return ml.organize_books(path, copy=copy, dry_run=dry_run)
    if kind == "films":      return ml.organize_films(path, copy=copy, dry_run=dry_run)
    if kind == "tv":         return ml.organize_tv(path, copy=copy, dry_run=dry_run)
    if kind == "auto":       return ml.organize_auto(path, copy=copy, dry_run=dry_run)
    return f"unknown organize kind: {kind}"
