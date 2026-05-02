"""
title: Plan Tracker — Durable Cross-Session Todo List
author: local-ai-stack
description: A small persistent task list the model can read at the start of every turn and update as it works. Different from in-conversation TodoWrite (which lives in the chat history): plan_tracker survives session boundaries so a long-running build (a KiCad design, a multi-paper review, a DAW project) can be resumed cleanly. Backed by SQLite under data/.
required_open_webui_version: 0.4.0
requirements:
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_plans_project ON plans(project);
        CREATE INDEX IF NOT EXISTS idx_plans_status ON plans(status);
    """)
    return conn


_VALID_STATUS = {"pending", "in_progress", "blocked", "completed", "cancelled"}


class Tools:
    class Valves(BaseModel):
        DB_PATH: str = Field(
            default=str(Path("data") / "plan_tracker.sqlite"),
            description="SQLite file (relative to backend cwd or absolute).",
        )

    def __init__(self):
        self.valves = self.Valves()

    def _conn(self) -> sqlite3.Connection:
        return _connect(Path(self.valves.DB_PATH))

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    # ── CRUD ──────────────────────────────────────────────────────────────

    def add(
        self,
        content: str,
        project: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Add a new task.
        :param content: Imperative task description.
        :param project: Optional project slug to scope the task.
        :return: Confirmation with task id.
        """
        now = self._now()
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO plans (project, content, status, created_at, updated_at) "
                "VALUES (?, ?, 'pending', ?, ?)",
                (project, content, now, now),
            )
            return f"+ task id={cur.lastrowid}  project={project or '-'}  status=pending"

    def update_status(
        self,
        task_id: int,
        status: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Change a task's status.
        :param task_id: id from `list`.
        :param status: pending, in_progress, blocked, completed, cancelled.
        :return: Confirmation.
        """
        if status not in _VALID_STATUS:
            return f"invalid status {status!r}. Try: {sorted(_VALID_STATUS)}"
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE plans SET status = ?, updated_at = ? WHERE id = ?",
                (status, self._now(), task_id),
            )
            return f"updated {cur.rowcount} row(s) -> {status}"

    def edit(
        self,
        task_id: int,
        content: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Rewrite a task's content.
        :param task_id: id to edit.
        :param content: New content text.
        :return: Confirmation.
        """
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE plans SET content = ?, updated_at = ? WHERE id = ?",
                (content, self._now(), task_id),
            )
            return f"updated {cur.rowcount} row(s)"

    def delete(self, task_id: int, __user__: Optional[dict] = None) -> str:
        """
        Delete a task. Prefer `update_status` to mark it cancelled.
        :param task_id: id to delete.
        :return: Confirmation.
        """
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM plans WHERE id = ?", (task_id,))
            return f"deleted {cur.rowcount} row(s)"

    def list(
        self,
        project: str = "",
        status: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        List tasks, newest first. Filter by project and/or status.
        :param project: Optional project slug.
        :param status: Optional status filter.
        :return: Formatted table.
        """
        sql = "SELECT id, project, status, content, updated_at FROM plans WHERE 1=1"
        args: list[Any] = []
        if project:
            sql += " AND project = ?"; args.append(project)
        if status:
            sql += " AND status = ?";  args.append(status)
        sql += " ORDER BY id DESC LIMIT 200"
        with self._conn() as conn:
            rows = conn.execute(sql, args).fetchall()
        if not rows:
            return "(no tasks)"
        out = []
        for tid, proj, st, content, upd in rows:
            mark = {"pending": " ", "in_progress": "→", "blocked": "!",
                    "completed": "✓", "cancelled": "x"}.get(st, "?")
            out.append(f"[{tid:>4}] [{mark}]  {proj or '-':<16}  {content[:90]}  ({upd})")
        return "\n".join(out)

    def briefing(
        self,
        project: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Compact "where am I?" briefing — open + in-progress tasks plus the
        last 3 completed ones. Designed to be the model's first call on
        resuming a project.
        :param project: Optional project filter.
        :return: Multi-section markdown.
        """
        sql_open = (
            "SELECT id, status, content FROM plans "
            "WHERE status IN ('pending','in_progress','blocked')"
        )
        sql_done = (
            "SELECT id, status, content, updated_at FROM plans "
            "WHERE status = 'completed'"
        )
        args = []
        if project:
            sql_open += " AND project = ?"; sql_done += " AND project = ?"; args.append(project)
        sql_open += " ORDER BY status DESC, id ASC"
        sql_done += " ORDER BY id DESC LIMIT 3"
        with self._conn() as conn:
            opens = conn.execute(sql_open, args).fetchall()
            dones = conn.execute(sql_done, args).fetchall()
        out = [f"# Briefing: {project or '(all projects)'}\n"]
        if opens:
            out.append("## Open")
            for tid, st, content in opens:
                out.append(f"- [{st}] [{tid}] {content}")
        else:
            out.append("(no open tasks)")
        out.append("\n## Recently completed")
        if dones:
            for tid, st, content, upd in dones:
                out.append(f"- [{tid}] {content}  ({upd})")
        else:
            out.append("(none)")
        return "\n".join(out)
