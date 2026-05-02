"""
title: Project Workspace — Per-Project Durable Memory
author: local-ai-stack
description: Keyed durable memory for long-running design / research projects. Different from `memory_tool` (which is per-user, free-form facts), this scopes notes by project name (e.g. "stm32-clock-board", "tower-render-v2", "lit-review-on-attention") so a model can resume context across sessions without re-fetching everything. Backed by a SQLite file under data/ — survives restarts. Supports tagged entries, search, and snapshot export.
required_open_webui_version: 0.4.0
requirements:
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project TEXT NOT NULL,
            tag TEXT NOT NULL DEFAULT '',
            body TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_notes_project ON notes(project);
        CREATE INDEX IF NOT EXISTS idx_notes_project_tag ON notes(project, tag);
    """)
    return conn


class Tools:
    class Valves(BaseModel):
        DB_PATH: str = Field(
            default=str(Path("data") / "project_workspace.sqlite"),
            description="SQLite file (relative to backend cwd or absolute).",
        )

    def __init__(self):
        self.valves = self.Valves()

    def _conn(self) -> sqlite3.Connection:
        return _connect(Path(self.valves.DB_PATH))

    # ── CRUD ──────────────────────────────────────────────────────────────

    def add_note(
        self,
        project: str,
        body: str,
        tag: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Append a note to a project workspace.
        :param project: Project slug (e.g. "stm32-clock", "tower-render").
        :param body: Free-form text. Markdown is fine.
        :param tag: Optional tag for filtering (e.g. "decision", "bom", "todo").
        :return: Confirmation with note id.
        """
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO notes (project, tag, body, created_at) VALUES (?, ?, ?, ?)",
                (project, tag, body, datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
            return f"+ note id={cur.lastrowid}  project={project}  tag={tag or '-'}"

    def list_notes(
        self,
        project: str,
        tag: str = "",
        limit: int = 50,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        List notes for a project, newest first.
        :param project: Project slug.
        :param tag: Optional tag filter.
        :param limit: Max rows to return.
        :return: One note per row with id, tag, timestamp, body preview.
        """
        with self._conn() as conn:
            if tag:
                rows = conn.execute(
                    "SELECT id, tag, created_at, body FROM notes "
                    "WHERE project = ? AND tag = ? ORDER BY id DESC LIMIT ?",
                    (project, tag, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, tag, created_at, body FROM notes "
                    "WHERE project = ? ORDER BY id DESC LIMIT ?",
                    (project, limit),
                ).fetchall()
        if not rows:
            return f"(no notes for project={project} tag={tag or '*'})"
        out = []
        for nid, t, ts, body in rows:
            preview = (body or "").replace("\n", " ")[:120]
            out.append(f"[{nid:>5}] {ts}  tag={t or '-':<10}  {preview}")
        return "\n".join(out)

    def search(
        self,
        project: str,
        query: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Substring search inside a project's notes.
        :param project: Project slug.
        :param query: Case-insensitive substring.
        :return: Matching note rows.
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, tag, created_at, body FROM notes "
                "WHERE project = ? AND lower(body) LIKE ? "
                "ORDER BY id DESC LIMIT 50",
                (project, f"%{query.lower()}%"),
            ).fetchall()
        if not rows:
            return f"(no matches for {query!r} in {project})"
        return "\n".join(
            f"[{nid:>5}] {ts}  {tag or '-'}  {body[:200].replace(chr(10), ' ')}"
            for nid, tag, ts, body in rows
        )

    def get_note(self, note_id: int, __user__: Optional[dict] = None) -> str:
        """
        Return the full body of a single note by id.
        :param note_id: id from list_notes / search.
        :return: Note body or "not found".
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT project, tag, created_at, body FROM notes WHERE id = ?",
                (note_id,),
            ).fetchone()
        if not row:
            return f"note {note_id} not found"
        proj, tag, ts, body = row
        return f"# id={note_id}  project={proj}  tag={tag or '-'}  {ts}\n\n{body}"

    def delete_note(self, note_id: int, __user__: Optional[dict] = None) -> str:
        """
        Delete a single note by id.
        :param note_id: id to delete.
        :return: Confirmation.
        """
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
            return f"deleted {cur.rowcount} row(s)"

    def list_projects(self, __user__: Optional[dict] = None) -> str:
        """
        List every distinct project slug with note counts and last-modified
        timestamps.
        :return: One row per project.
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT project, COUNT(*), MAX(created_at) FROM notes "
                "GROUP BY project ORDER BY MAX(created_at) DESC"
            ).fetchall()
        if not rows:
            return "(no projects yet)"
        return "\n".join(f"{p:<32}  notes={n:<4}  last={ts}" for p, n, ts in rows)

    def export(
        self,
        project: str,
        output_path: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Export every note in a project as a markdown file with sections
        ordered chronologically.
        :param project: Project slug.
        :param output_path: Where to write the .md file.
        :return: Confirmation.
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, tag, created_at, body FROM notes WHERE project = ? ORDER BY id ASC",
                (project,),
            ).fetchall()
        path = Path(output_path).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        out = [f"# Project: {project}\n"]
        for nid, tag, ts, body in rows:
            out.append(f"\n## [{nid}] {ts}  tag={tag or '-'}\n\n{body}\n")
        path.write_text("\n".join(out), encoding="utf-8")
        return f"wrote {len(rows)} notes -> {path}"
