"""Usage-event recording and aggregation for the admin dashboard.

Writes are fire-and-forget from the request hot path so a stalled SQLite
transaction never blocks the stream finish. Reads (aggregation) are only
invoked from admin endpoints.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from . import db


logger = logging.getLogger(__name__)


async def record_event(
    *,
    user_id: int | None,
    tier: str,
    think: bool,
    multi_agent: bool,
    tokens_in: int = 0,
    tokens_out: int = 0,
    latency_ms: int = 0,
    error: str | None = None,
) -> None:
    """Insert one row into usage_events. Never raises."""
    try:
        async with db.get_conn() as c:
            await c.execute(
                "INSERT INTO usage_events "
                "(user_id, ts, tier, think, multi_agent, tokens_in, tokens_out, latency_ms, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id, time.time(), tier,
                    1 if think else 0, 1 if multi_agent else 0,
                    tokens_in, tokens_out, latency_ms, error,
                ),
            )
            await c.commit()
    except Exception:
        logger.exception("record_event failed")


def record_event_bg(**kw: Any) -> None:
    """Schedule a record_event on the running loop without awaiting it."""
    try:
        loop = asyncio.get_event_loop()
        loop.create_task(record_event(**kw))
    except RuntimeError:
        # No running loop (unit tests) — drop the event silently.
        pass


# ── Aggregations ──────────────────────────────────────────────────────────

async def overview(window_seconds: int = 86400) -> dict[str, Any]:
    """Counters and sums over the last window. Used for dashboard header."""
    since = time.time() - window_seconds
    async with db.get_conn() as c:
        row = await (await c.execute(
            "SELECT COUNT(*) AS n, "
            "       COALESCE(SUM(tokens_in), 0) AS tin, "
            "       COALESCE(SUM(tokens_out), 0) AS tout, "
            "       COALESCE(AVG(latency_ms), 0) AS lat_avg, "
            "       COALESCE(SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END), 0) AS errs "
            "FROM usage_events WHERE ts >= ?",
            (since,),
        )).fetchone()
        users_row = await (await c.execute(
            "SELECT COUNT(DISTINCT user_id) AS n FROM usage_events "
            "WHERE ts >= ? AND user_id IS NOT NULL",
            (since,),
        )).fetchone()
        total_users = await (await c.execute(
            "SELECT COUNT(*) AS n FROM users",
        )).fetchone()
        total_convs = await (await c.execute(
            "SELECT COUNT(*) AS n FROM conversations",
        )).fetchone()
        total_msgs = await (await c.execute(
            "SELECT COUNT(*) AS n FROM messages",
        )).fetchone()
        total_rag = await (await c.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(size_bytes), 0) AS b FROM rag_docs",
        )).fetchone()
        total_mem = await (await c.execute(
            "SELECT COUNT(*) AS n FROM memories",
        )).fetchone()
    return {
        "window_seconds": window_seconds,
        "requests": int(row["n"] or 0),
        "tokens_in": int(row["tin"] or 0),
        "tokens_out": int(row["tout"] or 0),
        "latency_ms_avg": float(row["lat_avg"] or 0.0),
        "errors": int(row["errs"] or 0),
        "active_users": int(users_row["n"] or 0),
        "total_users": int(total_users["n"] or 0),
        "total_conversations": int(total_convs["n"] or 0),
        "total_messages": int(total_msgs["n"] or 0),
        "total_rag_docs": int(total_rag["n"] or 0),
        "total_rag_bytes": int(total_rag["b"] or 0),
        "total_memories": int(total_mem["n"] or 0),
    }


async def timeseries(window_seconds: int = 86400, buckets: int = 48) -> dict[str, Any]:
    """Bucketed request/token/latency series for the sparkline charts."""
    end = time.time()
    start = end - window_seconds
    bucket_s = window_seconds / buckets
    async with db.get_conn() as c:
        rows = await (await c.execute(
            "SELECT CAST((ts - ?) / ? AS INTEGER) AS b, "
            "       COUNT(*) AS n, "
            "       COALESCE(SUM(tokens_in), 0) AS tin, "
            "       COALESCE(SUM(tokens_out), 0) AS tout, "
            "       COALESCE(AVG(latency_ms), 0) AS lat, "
            "       COALESCE(SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END), 0) AS errs "
            "FROM usage_events WHERE ts >= ? "
            "GROUP BY b ORDER BY b",
            (start, bucket_s, start),
        )).fetchall()
    by_b = {int(r["b"]): r for r in rows}
    series = {
        "requests": [0] * buckets,
        "tokens_in": [0] * buckets,
        "tokens_out": [0] * buckets,
        "latency_ms_avg": [0.0] * buckets,
        "errors": [0] * buckets,
    }
    labels = []
    for i in range(buckets):
        labels.append(start + i * bucket_s)
        r = by_b.get(i)
        if not r:
            continue
        series["requests"][i] = int(r["n"] or 0)
        series["tokens_in"][i] = int(r["tin"] or 0)
        series["tokens_out"][i] = int(r["tout"] or 0)
        series["latency_ms_avg"][i] = float(r["lat"] or 0.0)
        series["errors"][i] = int(r["errs"] or 0)
    return {
        "start": start, "end": end, "bucket_seconds": bucket_s,
        "labels": labels, **series,
    }


async def by_tier(window_seconds: int = 86400) -> list[dict[str, Any]]:
    since = time.time() - window_seconds
    async with db.get_conn() as c:
        rows = await (await c.execute(
            "SELECT tier, COUNT(*) AS n, "
            "       COALESCE(SUM(tokens_in), 0) AS tin, "
            "       COALESCE(SUM(tokens_out), 0) AS tout, "
            "       COALESCE(AVG(latency_ms), 0) AS lat "
            "FROM usage_events WHERE ts >= ? "
            "GROUP BY tier ORDER BY n DESC",
            (since,),
        )).fetchall()
    return [
        {
            "tier": r["tier"],
            "requests": int(r["n"] or 0),
            "tokens_in": int(r["tin"] or 0),
            "tokens_out": int(r["tout"] or 0),
            "latency_ms_avg": float(r["lat"] or 0.0),
        }
        for r in rows
    ]


async def by_user(window_seconds: int = 86400, limit: int = 50) -> list[dict[str, Any]]:
    since = time.time() - window_seconds
    async with db.get_conn() as c:
        rows = await (await c.execute(
            "SELECT u.id AS id, u.email AS email, u.created_at AS created_at, "
            "       u.last_login_at AS last_login_at, "
            "       COUNT(e.id) AS n, "
            "       COALESCE(SUM(e.tokens_in), 0) AS tin, "
            "       COALESCE(SUM(e.tokens_out), 0) AS tout, "
            "       (SELECT COUNT(*) FROM conversations c WHERE c.user_id = u.id) AS convs "
            "FROM users u "
            "LEFT JOIN usage_events e ON e.user_id = u.id AND e.ts >= ? "
            "GROUP BY u.id ORDER BY n DESC, u.last_login_at DESC LIMIT ?",
            (since, limit),
        )).fetchall()
    return [dict(r) for r in rows]


async def recent_errors(limit: int = 25) -> list[dict[str, Any]]:
    """Merge chat-loop errors (usage_events) with fire-and-forget failures
    surfaced via the `backend_errors` table (#18, #32)."""
    async with db.get_conn() as c:
        chat_rows = await (await c.execute(
            "SELECT ts, tier, user_id, error, 'chat' AS source FROM usage_events "
            "WHERE error IS NOT NULL ORDER BY ts DESC LIMIT ?",
            (limit,),
        )).fetchall()
        bg_rows = await (await c.execute(
            "SELECT created_at AS ts, stage AS tier, user_id, error, "
            "       'background' AS source "
            "FROM backend_errors ORDER BY id DESC LIMIT ?",
            (limit,),
        )).fetchall()
    merged = [dict(r) for r in chat_rows] + [dict(r) for r in bg_rows]
    merged.sort(key=lambda r: r.get("ts") or 0, reverse=True)
    return merged[:limit]
