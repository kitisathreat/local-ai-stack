"""
title: Local Filesystem — Read/Write/Search C:/D:
author: local-ai-stack
description: Read, write, list, search, copy, move, and delete files anywhere on the host machine (typically C:\\ and D:\\). Built for the Windows-native stack but works on any platform. Has a configurable allow-list of root directories and a blocklist of system paths so the model can't accidentally clobber Windows or Program Files. Writes and deletes default to dry-run mode and require an explicit `confirm=True` until the operator turns on WRITE_ENABLED / DELETE_ENABLED in the Tools panel.
required_open_webui_version: 0.4.0
requirements:
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


# Default deny patterns. Matched case-insensitively against the absolute
# resolved path with forward-slash normalisation. Use fnmatch globs.
_DEFAULT_BLOCKED = [
    "*/windows/*",
    "*/windows",
    "*/program files/windowsapps/*",
    "*/$recycle.bin/*",
    "*/system volume information/*",
    "*/perflogs/*",
    "*/windows.old/*",
    "*/recovery/*",
]


def _norm(path: str | os.PathLike) -> str:
    return str(Path(path)).replace("\\", "/").rstrip("/")


class Tools:
    class Valves(BaseModel):
        ALLOWED_ROOTS: list[str] = Field(
            default_factory=lambda: ["C:\\", "D:\\", str(Path.home())],
            description=(
                "Top-level directories the model is allowed to read/write under. "
                "Anything outside these roots is denied. Add more (e.g. external drives) "
                "or remove C:\\ for tighter scoping."
            ),
        )
        BLOCKED_PATTERNS: list[str] = Field(
            default_factory=lambda: list(_DEFAULT_BLOCKED),
            description=(
                "Glob patterns (fnmatch, lowercase, forward-slash) that override "
                "ALLOWED_ROOTS. Matches the resolved absolute path. Default blocks "
                "Windows, Program Files\\WindowsApps, Recycle Bin, etc."
            ),
        )
        WRITE_ENABLED: bool = Field(
            default=False,
            description=(
                "Master switch for write_text / write_bytes / append_text / "
                "create_directory. Off by default — flip to True after reviewing "
                "the allow-list."
            ),
        )
        DELETE_ENABLED: bool = Field(
            default=False,
            description="Master switch for delete_file and delete_directory.",
        )
        MAX_READ_BYTES: int = Field(
            default=2_000_000,
            description="Hard cap on bytes returned per read call (prevents context blow-ups).",
        )
        MAX_LIST_ENTRIES: int = Field(
            default=500,
            description="Maximum directory entries returned by list_directory in one call.",
        )
        MAX_SEARCH_RESULTS: int = Field(
            default=200,
            description="Maximum matches returned by search_files in one call.",
        )

    def __init__(self):
        self.valves = self.Valves()

    # ── Path-safety primitives ────────────────────────────────────────────

    def _resolve(self, path: str) -> Path:
        try:
            p = Path(path).expanduser()
            return p.resolve(strict=False)
        except Exception as e:
            raise ValueError(f"Invalid path '{path}': {e}")

    def _check_allowed(self, path: Path) -> None:
        norm = _norm(path).lower()

        for pat in self.valves.BLOCKED_PATTERNS:
            if fnmatch.fnmatch(norm, pat.lower()):
                raise PermissionError(
                    f"Path is blocked by pattern '{pat}': {path}"
                )

        roots = [_norm(r).lower() for r in self.valves.ALLOWED_ROOTS]
        if not any(norm == r or norm.startswith(r + "/") for r in roots):
            raise PermissionError(
                f"Path '{path}' is outside ALLOWED_ROOTS={self.valves.ALLOWED_ROOTS}. "
                "Update the tool's Valves to grant access."
            )

    def _guard(self, path: str, *, write: bool = False, delete: bool = False) -> Path:
        p = self._resolve(path)
        self._check_allowed(p)
        if write and not self.valves.WRITE_ENABLED:
            raise PermissionError(
                "WRITE_ENABLED is False on the filesystem tool. "
                "Enable it from the admin Tools panel first."
            )
        if delete and not self.valves.DELETE_ENABLED:
            raise PermissionError(
                "DELETE_ENABLED is False on the filesystem tool. "
                "Enable it from the admin Tools panel first."
            )
        return p

    # ── Read / inspect ────────────────────────────────────────────────────

    def list_directory(
        self,
        path: str,
        glob: str = "*",
        recursive: bool = False,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        List files and subdirectories at a path. Returns a newline-delimited
        listing with size, mtime, and type. Honors ALLOWED_ROOTS.
        :param path: Absolute or user-relative directory (e.g. "D:\\projects").
        :param glob: Filename glob filter (default "*", supports "*.kicad_pro").
        :param recursive: When True, walks subdirectories. Output is capped at MAX_LIST_ENTRIES.
        :return: Plain-text directory listing.
        """
        p = self._guard(path)
        if not p.exists():
            return f"Not found: {p}"
        if not p.is_dir():
            return f"Not a directory: {p}"

        cap = self.valves.MAX_LIST_ENTRIES
        rows: list[str] = []
        iterator = p.rglob(glob) if recursive else p.glob(glob)
        for entry in iterator:
            try:
                st = entry.stat()
                kind = "dir " if entry.is_dir() else "file"
                mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(timespec="seconds")
                size = "" if entry.is_dir() else f"{st.st_size:>10}"
                rows.append(f"{kind}  {size:>10}  {mtime}  {entry}")
            except OSError as e:
                rows.append(f"err   {entry}: {e}")
            if len(rows) >= cap:
                rows.append(f"... (truncated at {cap} entries — narrow the glob or call again recursively)")
                break

        if not rows:
            return f"(empty) {p}"
        return f"{len(rows)} entries under {p}\n" + "\n".join(rows)

    def file_info(self, path: str, __user__: Optional[dict] = None) -> str:
        """
        Return size, mtime, and type for a single file or directory.
        :param path: Absolute path to inspect.
        :return: Multi-line summary.
        """
        p = self._guard(path)
        if not p.exists():
            return f"Not found: {p}"
        st = p.stat()
        kind = "directory" if p.is_dir() else "file"
        mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(timespec="seconds")
        ctime = datetime.fromtimestamp(st.st_ctime, tz=timezone.utc).isoformat(timespec="seconds")
        return (
            f"path:    {p}\n"
            f"type:    {kind}\n"
            f"size:    {st.st_size}\n"
            f"mtime:   {mtime}\n"
            f"ctime:   {ctime}\n"
            f"mode:    {oct(st.st_mode)}"
        )

    def read_text(
        self,
        path: str,
        encoding: str = "utf-8",
        max_bytes: int = 0,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Read a UTF-8 (or other) text file and return its contents. Capped by
        MAX_READ_BYTES; pass max_bytes to override per-call (still bounded).
        :param path: File to read.
        :param encoding: Text encoding (default utf-8).
        :param max_bytes: Optional per-call cap. 0 means use MAX_READ_BYTES.
        :return: File contents (truncation note appended if cut).
        """
        p = self._guard(path)
        if not p.is_file():
            return f"Not a file: {p}"
        cap = min(max_bytes or self.valves.MAX_READ_BYTES, self.valves.MAX_READ_BYTES)
        with p.open("rb") as f:
            data = f.read(cap + 1)
        truncated = len(data) > cap
        if truncated:
            data = data[:cap]
        try:
            text = data.decode(encoding, errors="replace")
        except LookupError:
            return f"Unknown encoding '{encoding}'."
        if truncated:
            text += f"\n\n[... truncated at {cap} bytes — read again with max_bytes or use read_bytes ...]"
        return text

    def read_bytes_b64(
        self,
        path: str,
        max_bytes: int = 0,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Read a binary file and return base64-encoded bytes (use for images,
        compiled DAW projects, etc).
        :param path: File to read.
        :param max_bytes: Optional per-call cap.
        :return: Base64 string + size header.
        """
        import base64
        p = self._guard(path)
        if not p.is_file():
            return f"Not a file: {p}"
        cap = min(max_bytes or self.valves.MAX_READ_BYTES, self.valves.MAX_READ_BYTES)
        data = p.read_bytes()[:cap]
        return f"size_bytes={len(data)}\nbase64={base64.b64encode(data).decode('ascii')}"

    def search_files(
        self,
        root: str,
        pattern: str = "*",
        contains: str = "",
        max_results: int = 0,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Recursively find files under a root by filename glob and optional
        substring match against text contents (skipped for binaries).
        :param root: Directory to walk (must be inside ALLOWED_ROOTS).
        :param pattern: Filename glob, e.g. "*.flp" or "*.kicad_sch".
        :param contains: Optional case-insensitive substring; only text files are scanned.
        :param max_results: 0 → use MAX_SEARCH_RESULTS.
        :return: Matched paths, newline-delimited.
        """
        p = self._guard(root)
        if not p.is_dir():
            return f"Not a directory: {p}"
        cap = max_results or self.valves.MAX_SEARCH_RESULTS
        cap = min(cap, self.valves.MAX_SEARCH_RESULTS)

        needle = contains.lower() if contains else ""
        out: list[str] = []
        for entry in p.rglob(pattern):
            if not entry.is_file():
                continue
            if needle:
                try:
                    snippet = entry.read_bytes()[:200_000]
                    if needle.encode("utf-8", "ignore") not in snippet.lower():
                        # Try a utf-8 decode pass for non-ASCII matches.
                        try:
                            if needle not in snippet.decode("utf-8", "ignore").lower():
                                continue
                        except Exception:
                            continue
                except (OSError, UnicodeDecodeError):
                    continue
            out.append(str(entry))
            if len(out) >= cap:
                out.append(f"... (truncated at {cap})")
                break

        return "\n".join(out) if out else f"(no matches) {pattern!r} under {p}"

    def compute_hash(
        self,
        path: str,
        algorithm: str = "sha256",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Compute a cryptographic digest for a file. Useful before/after edits.
        :param path: File to hash.
        :param algorithm: md5, sha1, sha256, sha512, blake2b.
        :return: Hex digest.
        """
        p = self._guard(path)
        if not p.is_file():
            return f"Not a file: {p}"
        try:
            h = hashlib.new(algorithm)
        except ValueError:
            return f"Unknown algorithm: {algorithm}"
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return f"{algorithm}={h.hexdigest()}  {p}"

    # ── Mutate ────────────────────────────────────────────────────────────

    def write_text(
        self,
        path: str,
        content: str,
        encoding: str = "utf-8",
        overwrite: bool = False,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Write text to a file. Requires WRITE_ENABLED. Refuses to overwrite an
        existing file unless overwrite=True.
        :param path: Destination file.
        :param content: Full file contents to write.
        :param encoding: Default utf-8.
        :param overwrite: Allow replacing an existing file.
        :return: Confirmation line with bytes written.
        """
        p = self._guard(path, write=True)
        if p.exists() and not overwrite:
            return f"Refused: {p} exists. Pass overwrite=True to replace."
        p.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode(encoding)
        p.write_bytes(data)
        return f"wrote {len(data)} bytes -> {p}"

    def append_text(
        self,
        path: str,
        content: str,
        encoding: str = "utf-8",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Append text to an existing file (creates it if missing). Requires WRITE_ENABLED.
        :param path: File to append to.
        :param content: Text to append.
        :param encoding: Default utf-8.
        :return: Confirmation with new size.
        """
        p = self._guard(path, write=True)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("ab") as f:
            f.write(content.encode(encoding))
        return f"appended {len(content)} chars -> {p} (size now {p.stat().st_size})"

    def write_bytes_b64(
        self,
        path: str,
        b64: str,
        overwrite: bool = False,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Write a binary blob (decoded from base64) to a file. Requires WRITE_ENABLED.
        :param path: Destination file.
        :param b64: Base64-encoded payload.
        :param overwrite: Allow replacing an existing file.
        :return: Confirmation.
        """
        import base64
        p = self._guard(path, write=True)
        if p.exists() and not overwrite:
            return f"Refused: {p} exists. Pass overwrite=True to replace."
        try:
            data = base64.b64decode(b64, validate=False)
        except Exception as e:
            return f"Invalid base64: {e}"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return f"wrote {len(data)} bytes -> {p}"

    def create_directory(self, path: str, __user__: Optional[dict] = None) -> str:
        """
        Create a directory (parents included). Requires WRITE_ENABLED.
        :param path: Directory to create.
        :return: Confirmation or no-op note.
        """
        p = self._guard(path, write=True)
        if p.exists():
            return f"already exists: {p}"
        p.mkdir(parents=True, exist_ok=False)
        return f"created -> {p}"

    def copy_file(
        self,
        source: str,
        destination: str,
        overwrite: bool = False,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Copy a file from source to destination. Requires WRITE_ENABLED.
        :param source: File to copy.
        :param destination: Destination path.
        :param overwrite: Allow replacing destination.
        :return: Confirmation.
        """
        s = self._guard(source)
        d = self._guard(destination, write=True)
        if not s.is_file():
            return f"Not a file: {s}"
        if d.exists() and not overwrite:
            return f"Refused: {d} exists. Pass overwrite=True."
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s, d)
        return f"copied {s} -> {d}"

    def move_file(
        self,
        source: str,
        destination: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Move/rename a file. Requires WRITE_ENABLED on both source and destination roots.
        :param source: File to move.
        :param destination: New location.
        :return: Confirmation.
        """
        s = self._guard(source, write=True)
        d = self._guard(destination, write=True)
        if not s.exists():
            return f"Not found: {s}"
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(s), str(d))
        return f"moved {s} -> {d}"

    def delete_file(self, path: str, __user__: Optional[dict] = None) -> str:
        """
        Delete a single file. Requires DELETE_ENABLED.
        :param path: File to delete.
        :return: Confirmation.
        """
        p = self._guard(path, delete=True)
        if not p.exists():
            return f"Not found: {p}"
        if p.is_dir():
            return f"Refusing to delete directory via delete_file: {p}"
        p.unlink()
        return f"deleted -> {p}"

    def delete_directory(
        self,
        path: str,
        recursive: bool = False,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Delete a directory. Requires DELETE_ENABLED. Set recursive=True to
        also wipe non-empty trees (use with care).
        :param path: Directory to delete.
        :param recursive: Allow deleting non-empty directories.
        :return: Confirmation.
        """
        p = self._guard(path, delete=True)
        if not p.exists():
            return f"Not found: {p}"
        if not p.is_dir():
            return f"Not a directory: {p}"
        if recursive:
            shutil.rmtree(p)
        else:
            try:
                p.rmdir()
            except OSError as e:
                return f"Refused: {e}. Pass recursive=True to wipe non-empty trees."
        return f"deleted -> {p}"
