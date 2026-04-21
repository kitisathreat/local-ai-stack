"""
Tests for backend/metrics.py — usage event recording and aggregation.

Uses a temporary SQLite database (via the db_path fixture from test_db.py
pattern) so no persistent state leaks between tests.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def run(coro):
    return asyncio.run(coro)


# ── per-test DB fixture ───────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Redirect backend.db.DB_PATH to a fresh SQLite file for each test."""
    db_path = tmp_path / "metrics_test.db"
    monkeypatch.setenv("LAI_DB_PATH", str(db_path))
    import importlib
    from backend import db as db_mod
    db_mod.DB_PATH = db_path
    importlib.reload(db_mod)
    db_mod.DB_PATH = db_path
    run(db_mod.init_db())
    # Re-import metrics so it picks up the reloaded db module
    import backend.metrics as metrics_mod
    importlib.reload(metrics_mod)
    return db_mod


# ── helpers ───────────────────────────────────────────────────────────────────

def _record(**kw):
    import backend.metrics as metrics
    return run(metrics.record_event(**kw))


def _seed_event(**kw):
    defaults = dict(user_id=None, tier="versatile", think=False,
                    multi_agent=False, tokens_in=10, tokens_out=20, latency_ms=100)
    defaults.update(kw)
    _record(**defaults)


# ═══════════════════════════════════════════════════════════════════════════════
# record_event
# ═══════════════════════════════════════════════════════════════════════════════

class TestRecordEvent:

    def test_event_persisted(self, isolated_db):
        import backend.metrics as metrics
        _seed_event(tier="fast", tokens_in=5, tokens_out=15)
        result = run(metrics.overview())
        assert result["requests"] == 1
        assert result["tokens_in"] == 5
        assert result["tokens_out"] == 15

    def test_multiple_events_summed(self, isolated_db):
        import backend.metrics as metrics
        _seed_event(tokens_in=10, tokens_out=20)
        _seed_event(tokens_in=30, tokens_out=40)
        result = run(metrics.overview())
        assert result["requests"] == 2
        assert result["tokens_in"] == 40
        assert result["tokens_out"] == 60

    def test_error_event_recorded(self, isolated_db):
        import backend.metrics as metrics
        _seed_event(error="timeout")
        result = run(metrics.overview())
        assert result["errors"] == 1

    def test_event_with_user_id(self, isolated_db):
        import backend.metrics as metrics
        import backend.db as db_mod

        async def _insert_user():
            async with db_mod.get_conn() as c:
                cur = await c.execute(
                    "INSERT INTO users (email, created_at, last_login_at) VALUES (?, ?, ?)",
                    ("test@example.com", 0, 0),
                )
                await c.commit()
                return cur.lastrowid

        user_id = run(_insert_user())
        _seed_event(user_id=user_id)
        result = run(metrics.overview())
        assert result["active_users"] == 1

    def test_record_event_never_raises(self, isolated_db, monkeypatch):
        """record_event should swallow all errors silently."""
        import backend.metrics as metrics
        import backend.db as db_mod

        async def _bad_get_conn():
            raise RuntimeError("DB gone")

        monkeypatch.setattr(db_mod, "get_conn", _bad_get_conn)
        # Should not raise
        run(metrics.record_event(user_id=None, tier="fast", think=False,
                                  multi_agent=False))

    def test_record_event_bg_no_loop(self):
        """record_event_bg with no running event loop must not raise."""
        import backend.metrics as metrics
        # Call outside any event loop — should silently no-op
        metrics.record_event_bg(user_id=None, tier="versatile", think=False, multi_agent=False)


# ═══════════════════════════════════════════════════════════════════════════════
# overview
# ═══════════════════════════════════════════════════════════════════════════════

class TestOverview:

    def test_empty_db_returns_zeros(self, isolated_db):
        import backend.metrics as metrics
        result = run(metrics.overview())
        assert result["requests"] == 0
        assert result["tokens_in"] == 0
        assert result["tokens_out"] == 0
        assert result["errors"] == 0
        assert result["active_users"] == 0

    def test_has_all_expected_keys(self, isolated_db):
        import backend.metrics as metrics
        result = run(metrics.overview())
        expected = {
            "window_seconds", "requests", "tokens_in", "tokens_out",
            "latency_ms_avg", "errors", "active_users",
            "total_users", "total_conversations", "total_messages",
            "total_rag_docs", "total_rag_bytes", "total_memories",
        }
        assert expected <= result.keys()

    def test_window_filters_old_events(self, isolated_db):
        import backend.metrics as metrics
        import backend.db as db_mod

        async def _old_event():
            async with db_mod.get_conn() as c:
                await c.execute(
                    "INSERT INTO usage_events "
                    "(user_id, ts, tier, think, multi_agent, tokens_in, tokens_out, latency_ms) "
                    "VALUES (?, ?, ?, 0, 0, 5, 5, 10)",
                    (None, time.time() - 90000, "fast"),  # 25 hours ago
                )
                await c.commit()

        run(_old_event())
        result = run(metrics.overview(window_seconds=86400))
        assert result["requests"] == 0  # outside 24h window

    def test_latency_avg_computed(self, isolated_db):
        import backend.metrics as metrics
        _seed_event(latency_ms=100)
        _seed_event(latency_ms=200)
        result = run(metrics.overview())
        assert result["latency_ms_avg"] == pytest.approx(150.0, rel=0.1)


# ═══════════════════════════════════════════════════════════════════════════════
# timeseries
# ═══════════════════════════════════════════════════════════════════════════════

class TestTimeseries:

    def test_returns_correct_structure(self, isolated_db):
        import backend.metrics as metrics
        result = run(metrics.timeseries(window_seconds=3600, buckets=12))
        assert "requests" in result
        assert "tokens_in" in result
        assert "tokens_out" in result
        assert "latency_ms_avg" in result
        assert "errors" in result
        assert len(result["requests"]) == 12

    def test_bucket_count_matches_requested(self, isolated_db):
        import backend.metrics as metrics
        for buckets in (6, 24, 48):
            result = run(metrics.timeseries(buckets=buckets))
            assert len(result["requests"]) == buckets

    def test_event_appears_in_series(self, isolated_db):
        import backend.metrics as metrics
        _seed_event(tokens_in=99)
        result = run(metrics.timeseries(window_seconds=3600, buckets=12))
        assert sum(result["tokens_in"]) == 99

    def test_has_start_end_labels(self, isolated_db):
        import backend.metrics as metrics
        result = run(metrics.timeseries())
        assert "start" in result
        assert "end" in result
        assert "labels" in result
        assert result["end"] > result["start"]


# ═══════════════════════════════════════════════════════════════════════════════
# by_tier
# ═══════════════════════════════════════════════════════════════════════════════

class TestByTier:

    def test_empty_returns_empty_list(self, isolated_db):
        import backend.metrics as metrics
        assert run(metrics.by_tier()) == []

    def test_groups_by_tier(self, isolated_db):
        import backend.metrics as metrics
        _seed_event(tier="fast", tokens_in=10)
        _seed_event(tier="fast", tokens_in=20)
        _seed_event(tier="versatile", tokens_in=5)
        result = run(metrics.by_tier())
        tiers = {r["tier"]: r for r in result}
        assert tiers["fast"]["requests"] == 2
        assert tiers["fast"]["tokens_in"] == 30
        assert tiers["versatile"]["requests"] == 1

    def test_each_row_has_expected_keys(self, isolated_db):
        import backend.metrics as metrics
        _seed_event(tier="coding")
        rows = run(metrics.by_tier())
        assert len(rows) == 1
        assert {"tier", "requests", "tokens_in", "tokens_out", "latency_ms_avg"} <= rows[0].keys()


# ═══════════════════════════════════════════════════════════════════════════════
# recent_errors
# ═══════════════════════════════════════════════════════════════════════════════

class TestRecentErrors:

    def test_empty_when_no_errors(self, isolated_db):
        import backend.metrics as metrics
        _seed_event()  # no error
        assert run(metrics.recent_errors()) == []

    def test_returns_only_error_events(self, isolated_db):
        import backend.metrics as metrics
        _seed_event()  # no error
        _seed_event(error="timeout")
        _seed_event(error="rate_limit")
        rows = run(metrics.recent_errors())
        assert len(rows) == 2
        for r in rows:
            assert r["error"] is not None

    def test_limit_applied(self, isolated_db):
        import backend.metrics as metrics
        for i in range(10):
            _seed_event(error=f"err{i}")
        rows = run(metrics.recent_errors(limit=3))
        assert len(rows) == 3
