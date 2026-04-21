"""Per-user preferences: middleware opt-outs + retrieval tunables.

Issue #17 is the middleware opt-outs; #20 is the retrieval tunables
(RAG top_k / min_score / memory cadence). Implemented as one table and
one pair of endpoints so both surfaces share a schema.

Callers:
    `get_for_user(user_id)` — chat path reads this per request to decide
    which middleware to run. Returns defaults if the user has no row.

    `update_for_user(user_id, patch)` — PATCH /preferences handler; merges
    the patch into the row (or INSERT-if-missing).

All fields have defaults so clients can issue a partial patch without
clobbering unrelated knobs.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from . import db


@dataclass
class UserPreferences:
    # Middleware opt-outs (#17). All default True; toggling any off
    # skips the corresponding inject in _single_agent_sse's pipeline.
    inject_datetime: bool = True
    inject_clarification: bool = True
    auto_web_search: bool = True
    inject_memories: bool = True
    inject_rag: bool = True

    # Retrieval tunables (#20). Applied per-request, overriding the
    # module-level defaults in rag.py / memory.py. Clamped to sane bounds
    # so a rogue PATCH can't trigger oversized Qdrant searches.
    rag_top_k: int = 3
    rag_min_score: float = 0.55
    memory_top_k: int = 3
    memory_cadence: int = 5

    def to_dict(self) -> dict[str, Any]:
        return {
            "inject_datetime": self.inject_datetime,
            "inject_clarification": self.inject_clarification,
            "auto_web_search": self.auto_web_search,
            "inject_memories": self.inject_memories,
            "inject_rag": self.inject_rag,
            "rag_top_k": self.rag_top_k,
            "rag_min_score": self.rag_min_score,
            "memory_top_k": self.memory_top_k,
            "memory_cadence": self.memory_cadence,
        }


_BOOL_FIELDS = {
    "inject_datetime", "inject_clarification", "auto_web_search",
    "inject_memories", "inject_rag",
}
_INT_FIELDS = {"rag_top_k", "memory_top_k", "memory_cadence"}
_FLOAT_FIELDS = {"rag_min_score"}


def _row_to_prefs(row) -> UserPreferences:
    return UserPreferences(
        inject_datetime=bool(row["inject_datetime"]),
        inject_clarification=bool(row["inject_clarification"]),
        auto_web_search=bool(row["auto_web_search"]),
        inject_memories=bool(row["inject_memories"]),
        inject_rag=bool(row["inject_rag"]),
        rag_top_k=int(row["rag_top_k"]),
        rag_min_score=float(row["rag_min_score"]),
        memory_top_k=int(row["memory_top_k"]),
        memory_cadence=int(row["memory_cadence"]),
    )


async def get_for_user(user_id: int) -> UserPreferences:
    """Return the user's preferences, falling back to defaults if the row
    doesn't exist yet. Never raises — callers on the hot path are
    expected to trust the default."""
    async with db.get_conn() as c:
        row = await (await c.execute(
            "SELECT inject_datetime, inject_clarification, auto_web_search, "
            "       inject_memories, inject_rag, rag_top_k, rag_min_score, "
            "       memory_top_k, memory_cadence "
            "FROM user_preferences WHERE user_id = ?",
            (user_id,),
        )).fetchone()
    if row is None:
        return UserPreferences()
    return _row_to_prefs(row)


def _clamp_patch(patch: dict[str, Any]) -> dict[str, Any]:
    """Filter out unknown keys and clamp numeric ranges so a rogue PATCH
    can't pass a negative top_k or an out-of-range similarity threshold."""
    out: dict[str, Any] = {}
    for k, v in patch.items():
        if k in _BOOL_FIELDS:
            out[k] = 1 if bool(v) else 0
        elif k in _INT_FIELDS:
            try:
                out[k] = max(1, min(20, int(v)))
            except (TypeError, ValueError):
                continue
        elif k in _FLOAT_FIELDS:
            try:
                out[k] = max(0.0, min(1.0, float(v)))
            except (TypeError, ValueError):
                continue
    return out


async def update_for_user(
    user_id: int, patch: dict[str, Any],
) -> UserPreferences:
    """UPSERT a preferences row. `patch` is a partial dict; unknown keys
    are ignored. Returns the post-update row."""
    clean = _clamp_patch(patch)
    if not clean:
        return await get_for_user(user_id)

    # Ensure row exists, then apply patch. SQLite's ON CONFLICT UPDATE is
    # available (we're on 3.24+) but keeping the two-step logic lets us
    # treat partial patches correctly without listing every column twice.
    now = time.time()
    defaults = UserPreferences().to_dict()
    async with db.get_conn() as c:
        await c.execute(
            "INSERT OR IGNORE INTO user_preferences "
            "(user_id, inject_datetime, inject_clarification, auto_web_search, "
            " inject_memories, inject_rag, rag_top_k, rag_min_score, "
            " memory_top_k, memory_cadence, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id,
                1 if defaults["inject_datetime"] else 0,
                1 if defaults["inject_clarification"] else 0,
                1 if defaults["auto_web_search"] else 0,
                1 if defaults["inject_memories"] else 0,
                1 if defaults["inject_rag"] else 0,
                defaults["rag_top_k"],
                defaults["rag_min_score"],
                defaults["memory_top_k"],
                defaults["memory_cadence"],
                now,
            ),
        )
        # Build UPDATE SQL dynamically from the validated keys.
        cols = ", ".join(f"{k} = ?" for k in clean) + ", updated_at = ?"
        params = list(clean.values()) + [now, user_id]
        await c.execute(
            f"UPDATE user_preferences SET {cols} WHERE user_id = ?",
            params,
        )
        await c.commit()
    return await get_for_user(user_id)
