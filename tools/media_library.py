"""
title: Media Library — Organize Music, Books, Films & TV From Downloads
author: local-ai-stack
description: Universal organizer for downloaded media. Reads ID3/Vorbis/FLAC/MP4 tags via mutagen for audio, parses EPUB/PDF metadata for books, and pattern-matches "Show.S01E02.title" / "Title.YEAR" filenames for films and TV. Moves (or copies) files into structured library folders: `Music/<Artist>/<Album>/<NN> <Title>.<ext>`, `Books/<Author>/<Title>.<ext>`, `Films/<Title> (<Year>)/`, `TV/<Show>/Season <NN>/<Show> S<NN>E<NN> <Title>.<ext>`. Metadata can be enriched via the musicbrainz / tmdb / omdb tools when filename parsing isn't enough. Pairs with soulseek / qobuz_dl / free_music / annas_archive / qbittorrent to close the search → download → organize loop.
required_open_webui_version: 0.4.0
requirements: mutagen
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

import re
import shutil
import zipfile
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field


_AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".wv", ".alac", ".aiff", ".ape"}
_BOOK_EXTS  = {".epub", ".pdf", ".mobi", ".azw3", ".djvu", ".cbz", ".cbr", ".fb2", ".txt"}
_VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".webm", ".m4v", ".ts"}

# Patterns for film / TV filename parsing.
_TV_RE = re.compile(
    r"(?P<show>.+?)[\.\s_-]+S(?P<season>\d{1,2})E(?P<episode>\d{1,2})"
    r"(?:[\.\s_-]+(?P<title>.+?))?"
    r"[\.\s_-]+(?:\d{3,4}p|x26[45]|h26[45]|hdtv|web|webrip|bluray|dvdrip|10bit|hevc|aac|dts|ac3|atmos|hdr|dl|sdr).*",
    re.IGNORECASE,
)
_FILM_RE = re.compile(
    r"(?P<title>.+?)[\.\s_-]+(?P<year>(?:19|20)\d{2})"
    r"(?:[\.\s_-]+.*)?",
)
_INVALID_FS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_name(s: str, fallback: str = "Unknown") -> str:
    s = (s or "").strip().rstrip(". ")
    s = _INVALID_FS_CHARS.sub("_", s)
    return s or fallback


def _human_size(n: int) -> str:
    n = float(n or 0); u = ["B", "KB", "MB", "GB", "TB"]; i = 0
    while n >= 1024 and i < len(u) - 1: n /= 1024; i += 1
    return f"{n:.2f} {u[i]}"


class Tools:
    class Valves(BaseModel):
        LIBRARY_ROOT: str = Field(
            default=str(Path.home() / "Library"),
            description="Root folder under which all organized media lives.",
        )
        MUSIC_SUBDIR:    str = Field(default="Music")
        BOOKS_SUBDIR:    str = Field(default="Books")
        FILMS_SUBDIR:    str = Field(default="Films")
        TV_SUBDIR:       str = Field(default="TV")
        AUDIOBOOKS_SUBDIR: str = Field(default="Audiobooks")
        DRY_RUN_DEFAULT: bool = Field(
            default=False,
            description="When True, methods log what they'd do without moving files.",
        )

    def __init__(self):
        self.valves = self.Valves()

    # ── Helpers ───────────────────────────────────────────────────────────

    def _root(self, *parts: str) -> Path:
        return Path(self.valves.LIBRARY_ROOT, *parts).expanduser()

    def _files_under(self, source: Path, exts: set[str]) -> list[Path]:
        if source.is_file():
            return [source] if source.suffix.lower() in exts else []
        return [p for p in source.rglob("*") if p.is_file() and p.suffix.lower() in exts]

    def _move_or_copy(self, src: Path, dst: Path, copy: bool, dry_run: bool) -> str:
        dst.parent.mkdir(parents=True, exist_ok=True)
        verb = "would copy" if dry_run and copy else \
               "would move" if dry_run else \
               "copied" if copy else "moved"
        if not dry_run:
            if copy:
                shutil.copy2(src, dst)
            else:
                shutil.move(str(src), str(dst))
        return f"{verb}  {src.name}  ->  {dst}"

    # ── Audio / Music ─────────────────────────────────────────────────────

    def _read_audio_tags(self, path: Path) -> dict[str, Any]:
        try:
            import mutagen
        except ImportError:
            return {}
        try:
            f = mutagen.File(path, easy=True)
            if f is None:
                return {}
        except Exception:
            return {}
        tags = dict(f.tags or {})
        first = lambda k: (tags.get(k) or [""])[0] if isinstance(tags.get(k), list) else (tags.get(k) or "")
        return {
            "artist":      first("albumartist") or first("artist") or "",
            "album":       first("album") or "",
            "title":       first("title") or path.stem,
            "tracknumber": first("tracknumber") or "",
            "date":        first("date") or "",
            "genre":       first("genre") or "",
        }

    def organize_audio(
        self,
        source: str,
        copy: bool = False,
        dry_run: bool = False,
        as_audiobooks: bool = False,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Walk a source directory (or single file), read each audio file's tags,
        and move/copy it into the canonical library layout
        `<LIBRARY_ROOT>/<Music or Audiobooks>/<Artist>/<Album>/<NN> <Title>.<ext>`.
        :param source: Directory or file containing freshly-downloaded audio.
        :param copy: When True, copy instead of move.
        :param dry_run: Plan only — print would-be moves without touching the disk.
        :param as_audiobooks: When True, route under the Audiobooks subdir instead of Music.
        :return: Per-file action log.
        """
        src = Path(source).expanduser().resolve()
        if not src.exists():
            return f"Not found: {src}"
        files = self._files_under(src, _AUDIO_EXTS)
        if not files:
            return f"(no audio files under {src})"
        sub = self.valves.AUDIOBOOKS_SUBDIR if as_audiobooks else self.valves.MUSIC_SUBDIR
        dry_run = dry_run or self.valves.DRY_RUN_DEFAULT

        log: list[str] = []
        for p in files:
            tags = self._read_audio_tags(p)
            artist = _safe_name(tags.get("artist") or "Unknown Artist")
            album = _safe_name(tags.get("album") or "Unknown Album")
            title = _safe_name(tags.get("title") or p.stem)
            tn = (tags.get("tracknumber") or "").split("/")[0]
            try:
                tn = f"{int(tn):02d} "
            except (ValueError, TypeError):
                tn = ""
            dest = self._root(sub, artist, album, f"{tn}{title}{p.suffix.lower()}")
            log.append(self._move_or_copy(p, dest, copy, dry_run))
        return f"{len(files)} audio files -> {self._root(sub)}\n" + "\n".join(log)

    # ── Books ────────────────────────────────────────────────────────────

    def _read_epub_meta(self, path: Path) -> dict[str, str]:
        try:
            with zipfile.ZipFile(path) as z:
                opf = next((n for n in z.namelist() if n.endswith(".opf")), None)
                if not opf:
                    return {}
                xml = z.read(opf).decode("utf-8", errors="ignore")
        except Exception:
            return {}
        author = re.search(r"<dc:creator[^>]*>([^<]+)</dc:creator>", xml)
        title = re.search(r"<dc:title[^>]*>([^<]+)</dc:title>", xml)
        return {
            "author": author.group(1).strip() if author else "",
            "title":  title.group(1).strip() if title else "",
        }

    def _read_pdf_meta(self, path: Path) -> dict[str, str]:
        # Very light-touch PDF info-dict scan; full parsing requires PyPDF2.
        try:
            data = path.read_bytes()[:200_000]
            author = re.search(rb"/Author\s*\(([^)]+)\)", data)
            title = re.search(rb"/Title\s*\(([^)]+)\)", data)
            return {
                "author": author.group(1).decode("latin-1", "ignore").strip() if author else "",
                "title":  title.group(1).decode("latin-1", "ignore").strip() if title else "",
            }
        except Exception:
            return {}

    def organize_books(
        self,
        source: str,
        copy: bool = False,
        dry_run: bool = False,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Walk a source directory and organize ebooks into
        `<LIBRARY_ROOT>/Books/<Author>/<Title>.<ext>`. EPUB metadata is read
        from the .opf inside the archive; PDF metadata is sniffed from the
        info-dict; everything else falls back to the filename stem.
        :param source: Directory or file containing freshly-downloaded books.
        :param copy: Copy instead of move.
        :param dry_run: Plan only.
        :return: Per-file log.
        """
        src = Path(source).expanduser().resolve()
        if not src.exists():
            return f"Not found: {src}"
        files = self._files_under(src, _BOOK_EXTS)
        if not files:
            return f"(no book files under {src})"
        dry_run = dry_run or self.valves.DRY_RUN_DEFAULT
        log: list[str] = []
        for p in files:
            meta: dict[str, str]
            if p.suffix.lower() == ".epub":
                meta = self._read_epub_meta(p)
            elif p.suffix.lower() == ".pdf":
                meta = self._read_pdf_meta(p)
            else:
                meta = {}
            author = _safe_name(meta.get("author") or "Unknown Author")
            title = _safe_name(meta.get("title") or p.stem)
            dest = self._root(self.valves.BOOKS_SUBDIR, author, f"{title}{p.suffix.lower()}")
            log.append(self._move_or_copy(p, dest, copy, dry_run))
        return f"{len(files)} book files -> {self._root(self.valves.BOOKS_SUBDIR)}\n" + "\n".join(log)

    # ── Films ────────────────────────────────────────────────────────────

    def organize_films(
        self,
        source: str,
        copy: bool = False,
        dry_run: bool = False,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Walk a source directory and organize films into
        `<LIBRARY_ROOT>/Films/<Title> (<Year>)/<Title> (<Year>).<ext>`.
        Files matching a TV pattern (S<NN>E<NN>) are skipped — use
        organize_tv() for those.
        :param source: Directory or file with film downloads.
        :param copy: Copy instead of move.
        :param dry_run: Plan only.
        :return: Per-file log.
        """
        src = Path(source).expanduser().resolve()
        if not src.exists():
            return f"Not found: {src}"
        files = self._files_under(src, _VIDEO_EXTS)
        if not files:
            return f"(no video files under {src})"
        dry_run = dry_run or self.valves.DRY_RUN_DEFAULT
        log: list[str] = []
        skipped = 0
        for p in files:
            if _TV_RE.match(p.stem):
                skipped += 1
                continue
            m = _FILM_RE.match(p.stem.replace("_", " ").replace(".", " "))
            if m:
                title = _safe_name(re.sub(r"\s+", " ", m.group("title")).strip())
                year = m.group("year")
                folder = f"{title} ({year})"
                fname = f"{title} ({year}){p.suffix.lower()}"
            else:
                title = _safe_name(p.stem)
                folder = title
                fname = f"{title}{p.suffix.lower()}"
            dest = self._root(self.valves.FILMS_SUBDIR, folder, fname)
            log.append(self._move_or_copy(p, dest, copy, dry_run))
        msg = f"{len(files) - skipped} film files -> {self._root(self.valves.FILMS_SUBDIR)}"
        if skipped:
            msg += f"  ({skipped} TV episodes skipped — call organize_tv)"
        return msg + "\n" + "\n".join(log)

    # ── TV ───────────────────────────────────────────────────────────────

    def organize_tv(
        self,
        source: str,
        copy: bool = False,
        dry_run: bool = False,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Walk a source directory and organize TV episodes into
        `<LIBRARY_ROOT>/TV/<Show>/Season <NN>/<Show> S<NN>E<NN> <Title>.<ext>`.
        :param source: Directory or file with TV downloads.
        :param copy: Copy instead of move.
        :param dry_run: Plan only.
        :return: Per-file log.
        """
        src = Path(source).expanduser().resolve()
        if not src.exists():
            return f"Not found: {src}"
        files = self._files_under(src, _VIDEO_EXTS)
        if not files:
            return f"(no video files under {src})"
        dry_run = dry_run or self.valves.DRY_RUN_DEFAULT
        log: list[str] = []
        unmatched = 0
        for p in files:
            stem = p.stem.replace("_", " ").replace(".", " ")
            m = _TV_RE.match(stem)
            if not m:
                unmatched += 1
                continue
            show = _safe_name(re.sub(r"\s+", " ", m.group("show")).strip())
            season = int(m.group("season"))
            ep = int(m.group("episode"))
            title = m.group("title") or ""
            title = _safe_name(re.sub(r"\s+", " ", title).strip())
            base = f"{show} S{season:02d}E{ep:02d}"
            fname = (f"{base} {title}{p.suffix.lower()}".strip()
                     if title else f"{base}{p.suffix.lower()}")
            dest = self._root(
                self.valves.TV_SUBDIR, show, f"Season {season:02d}", fname,
            )
            log.append(self._move_or_copy(p, dest, copy, dry_run))
        msg = f"{len(files) - unmatched} TV files -> {self._root(self.valves.TV_SUBDIR)}"
        if unmatched:
            msg += f"  ({unmatched} files didn't match S<NN>E<NN> — try organize_films)"
        return msg + "\n" + "\n".join(log)

    # ── Auto / mixed ──────────────────────────────────────────────────────

    def organize_auto(
        self,
        source: str,
        copy: bool = False,
        dry_run: bool = False,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Sort a mixed directory by file extension and dispatch to the right
        organizer (audio, books, films, TV). The film/TV split is decided
        by the "S<NN>E<NN>" pattern in the filename.
        :param source: Directory of mixed downloads.
        :param copy: Copy instead of move.
        :param dry_run: Plan only.
        :return: Combined log.
        """
        src = Path(source).expanduser().resolve()
        if not src.exists():
            return f"Not found: {src}"
        results: list[str] = []
        if any(p.suffix.lower() in _AUDIO_EXTS for p in src.rglob("*")):
            results.append("── audio ──")
            results.append(self.organize_audio(source, copy=copy, dry_run=dry_run))
        if any(p.suffix.lower() in _BOOK_EXTS for p in src.rglob("*")):
            results.append("── books ──")
            results.append(self.organize_books(source, copy=copy, dry_run=dry_run))
        if any(p.suffix.lower() in _VIDEO_EXTS for p in src.rglob("*")):
            results.append("── tv ──")
            results.append(self.organize_tv(source, copy=copy, dry_run=dry_run))
            results.append("── films ──")
            results.append(self.organize_films(source, copy=copy, dry_run=dry_run))
        return "\n".join(results) if results else "(nothing to organize)"

    # ── Inspection ────────────────────────────────────────────────────────

    def library_summary(self, __user__: Optional[dict] = None) -> str:
        """
        Print counts and total size per library subfolder.
        :return: Human-readable summary.
        """
        rows = []
        for sub in (self.valves.MUSIC_SUBDIR, self.valves.BOOKS_SUBDIR,
                    self.valves.FILMS_SUBDIR, self.valves.TV_SUBDIR,
                    self.valves.AUDIOBOOKS_SUBDIR):
            d = self._root(sub)
            if not d.exists():
                rows.append(f"{sub:<12} (missing) {d}")
                continue
            files = [p for p in d.rglob("*") if p.is_file()]
            size = sum(p.stat().st_size for p in files if p.exists())
            rows.append(f"{sub:<12} files={len(files):>5}  size={_human_size(size):>10}  {d}")
        return "\n".join(rows)
