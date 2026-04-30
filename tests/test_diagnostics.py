"""
CI test suite for backend/diagnostics.py.

Every check function is exercised with happy-path and failure-path inputs.
No running services required — HTTP calls are monkeypatched.

Live-service tests are skipped unless the corresponding env var is set:
    LIVE_QDRANT_URL, LIVE_REDIS_URL

Run:
    pytest tests/test_diagnostics.py -v
"""
from __future__ import annotations

import asyncio
import base64
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from backend.diagnostics import (
    CheckResult,
    Severity,
    check_cors_config,
    check_db_canary_write,
    check_db_connectivity,
    check_db_schema,
    check_db_wal_mode,
    check_env_auth_secret,
    check_env_cookie_secure,
    check_env_history_secret,
    check_env_jupyter_token,
    check_env_n8n_auth,
    check_env_public_base_url,
    check_gpu_available,
    check_history_encryption_roundtrip,
    check_jwt_roundtrip,
    check_pinned_llamacpp_tiers,
    check_qdrant_reachable,
    check_redis_reachable,
    check_web_search_provider,
    check_tool_registry,
    check_vram_budget,
    run_startup_diagnostics,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def run(coro):
    return asyncio.run(coro)


def good_secret() -> str:
    """Valid URL-safe base64 encoding of 32 bytes."""
    return base64.urlsafe_b64encode(b"a" * 32).decode()


def short_secret() -> str:
    """Valid base64 but only 5 decoded bytes."""
    return base64.urlsafe_b64encode(b"short").decode()


async def _make_db(path: Path) -> str:
    """Create a minimal initialised SQLite DB with all expected tables."""
    import aiosqlite

    db_path = str(path / "test.db")
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute("PRAGMA journal_mode=WAL")
        for table in (
            "users", "conversations",
            "messages", "memories", "rag_docs", "usage_events",
        ):
            await conn.execute(
                f"CREATE TABLE IF NOT EXISTS {table} (id TEXT PRIMARY KEY)"
            )
        await conn.commit()
    return db_path


# ── ENV: AUTH_SECRET_KEY ──────────────────────────────────────────────────────

class TestEnvAuthSecret:
    def test_ok(self, monkeypatch):
        monkeypatch.setenv("AUTH_SECRET_KEY", good_secret())
        assert check_env_auth_secret().severity == Severity.OK

    def test_missing_is_fail(self, monkeypatch):
        monkeypatch.delenv("AUTH_SECRET_KEY", raising=False)
        assert check_env_auth_secret().severity == Severity.FAIL

    def test_invalid_base64_is_fail(self, monkeypatch):
        monkeypatch.setenv("AUTH_SECRET_KEY", "not!!valid%%base64@@@")
        assert check_env_auth_secret().severity == Severity.FAIL

    def test_too_short_is_fail(self, monkeypatch):
        monkeypatch.setenv("AUTH_SECRET_KEY", short_secret())
        assert check_env_auth_secret().severity == Severity.FAIL

    def test_result_is_frozen_dataclass(self, monkeypatch):
        monkeypatch.setenv("AUTH_SECRET_KEY", good_secret())
        r = check_env_auth_secret()
        assert isinstance(r, CheckResult)
        with pytest.raises((AttributeError, TypeError)):
            r.severity = Severity.FAIL  # type: ignore[misc]


# ── ENV: HISTORY_SECRET_KEY ───────────────────────────────────────────────────

class TestEnvHistorySecret:
    def test_ok(self, monkeypatch):
        monkeypatch.setenv("HISTORY_SECRET_KEY", good_secret())
        assert check_env_history_secret().severity == Severity.OK

    def test_missing_is_warn_not_fail(self, monkeypatch):
        monkeypatch.delenv("HISTORY_SECRET_KEY", raising=False)
        r = check_env_history_secret()
        assert r.severity == Severity.WARN

    def test_too_short_is_fail(self, monkeypatch):
        monkeypatch.setenv("HISTORY_SECRET_KEY", short_secret())
        assert check_env_history_secret().severity == Severity.FAIL

    def test_invalid_base64_is_fail(self, monkeypatch):
        monkeypatch.setenv("HISTORY_SECRET_KEY", "%%%invalid%%%")
        assert check_env_history_secret().severity == Severity.FAIL


# ── ENV: JUPYTER_TOKEN ────────────────────────────────────────────────────────

class TestEnvJupyterToken:
    def test_ok(self, monkeypatch):
        monkeypatch.setenv("JUPYTER_TOKEN", "x" * 32)
        assert check_env_jupyter_token().severity == Severity.OK

    def test_known_default_is_fail(self, monkeypatch):
        monkeypatch.setenv("JUPYTER_TOKEN", "my-secret-token")
        assert check_env_jupyter_token().severity == Severity.FAIL

    def test_empty_is_fail(self, monkeypatch):
        monkeypatch.setenv("JUPYTER_TOKEN", "")
        assert check_env_jupyter_token().severity == Severity.FAIL

    def test_short_is_warn(self, monkeypatch):
        monkeypatch.setenv("JUPYTER_TOKEN", "abc123")  # 6 chars
        assert check_env_jupyter_token().severity == Severity.WARN

    def test_case_insensitive_default_detection(self, monkeypatch):
        monkeypatch.setenv("JUPYTER_TOKEN", "SECRET")  # upper-cased known bad
        assert check_env_jupyter_token().severity == Severity.FAIL


# ── ENV: PUBLIC_BASE_URL ──────────────────────────────────────────────────────

class TestEnvPublicBaseUrl:
    def test_ok_https(self, monkeypatch):
        monkeypatch.setenv("PUBLIC_BASE_URL", "https://chat.example.com")
        assert check_env_public_base_url().severity == Severity.OK

    def test_ok_http_localhost(self, monkeypatch):
        monkeypatch.setenv("PUBLIC_BASE_URL", "http://localhost:8000")
        assert check_env_public_base_url().severity == Severity.OK

    def test_missing_is_warn(self, monkeypatch):
        monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
        assert check_env_public_base_url().severity == Severity.WARN

    def test_relative_url_is_fail(self, monkeypatch):
        monkeypatch.setenv("PUBLIC_BASE_URL", "/chat")
        assert check_env_public_base_url().severity == Severity.FAIL

    def test_bad_scheme_is_fail(self, monkeypatch):
        monkeypatch.setenv("PUBLIC_BASE_URL", "ftp://example.com")
        assert check_env_public_base_url().severity == Severity.FAIL

    def test_no_host_is_fail(self, monkeypatch):
        monkeypatch.setenv("PUBLIC_BASE_URL", "https://")
        assert check_env_public_base_url().severity == Severity.FAIL


# ── ENV: cookie_secure consistency ───────────────────────────────────────────

class TestEnvCookieSecure:
    def test_ok_https_and_secure(self, monkeypatch):
        monkeypatch.setenv("PUBLIC_BASE_URL", "https://chat.example.com")
        monkeypatch.setenv("COOKIE_SECURE", "true")
        assert check_env_cookie_secure().severity == Severity.OK

    def test_ok_http_and_not_secure(self, monkeypatch):
        monkeypatch.setenv("PUBLIC_BASE_URL", "http://localhost:8000")
        monkeypatch.setenv("COOKIE_SECURE", "false")
        assert check_env_cookie_secure().severity == Severity.OK

    def test_fail_secure_true_over_http(self, monkeypatch):
        monkeypatch.setenv("PUBLIC_BASE_URL", "http://example.com")
        monkeypatch.setenv("COOKIE_SECURE", "true")
        assert check_env_cookie_secure().severity == Severity.FAIL

    def test_warn_https_without_secure(self, monkeypatch):
        monkeypatch.setenv("PUBLIC_BASE_URL", "https://chat.example.com")
        monkeypatch.setenv("COOKIE_SECURE", "false")
        assert check_env_cookie_secure().severity == Severity.WARN


# ── ENV: n8n auth ─────────────────────────────────────────────────────────────

class TestEnvN8nAuth:
    def test_ok(self, monkeypatch):
        monkeypatch.setenv("N8N_BASIC_AUTH_ACTIVE", "true")
        monkeypatch.setenv("N8N_ADMIN_USER", "admin")
        monkeypatch.setenv("N8N_ADMIN_PASSWORD", "s3cr3tP@ss!")
        assert check_env_n8n_auth().severity == Severity.OK

    def test_disabled_is_warn(self, monkeypatch):
        monkeypatch.setenv("N8N_BASIC_AUTH_ACTIVE", "false")
        assert check_env_n8n_auth().severity == Severity.WARN

    def test_enabled_but_no_credentials_is_fail(self, monkeypatch):
        monkeypatch.setenv("N8N_BASIC_AUTH_ACTIVE", "true")
        monkeypatch.delenv("N8N_ADMIN_USER", raising=False)
        monkeypatch.delenv("N8N_ADMIN_PASSWORD", raising=False)
        monkeypatch.delenv("N8N_BASIC_AUTH_PASSWORD", raising=False)
        assert check_env_n8n_auth().severity == Severity.FAIL

    def test_alternative_password_env_var(self, monkeypatch):
        monkeypatch.setenv("N8N_BASIC_AUTH_ACTIVE", "true")
        monkeypatch.setenv("N8N_ADMIN_USER", "admin")
        monkeypatch.setenv("N8N_BASIC_AUTH_PASSWORD", "altpass")
        assert check_env_n8n_auth().severity == Severity.OK


# ── CORS ──────────────────────────────────────────────────────────────────────

class TestCorsConfig:
    def test_ok_explicit_origins(self):
        r = check_cors_config(
            origins=["https://chat.example.com"], allow_credentials=True
        )
        assert r.severity == Severity.OK

    def test_fail_wildcard_with_credentials(self):
        r = check_cors_config(origins=["*"], allow_credentials=True)
        assert r.severity == Severity.FAIL

    def test_ok_wildcard_without_credentials(self):
        r = check_cors_config(origins=["*"], allow_credentials=False)
        assert r.severity == Severity.OK

    def test_warn_empty_origins(self):
        r = check_cors_config(origins=[], allow_credentials=False)
        assert r.severity == Severity.WARN

    def test_reads_from_env(self, monkeypatch):
        monkeypatch.setenv("ALLOWED_ORIGINS", "https://example.com")
        monkeypatch.setenv("CORS_ALLOW_CREDENTIALS", "true")
        assert check_cors_config().severity == Severity.OK


# ── Crypto: JWT roundtrip ─────────────────────────────────────────────────────

class TestJwtRoundtrip:
    def test_ok(self, monkeypatch):
        monkeypatch.setenv("AUTH_SECRET_KEY", good_secret())
        assert check_jwt_roundtrip().severity == Severity.OK

    def test_no_secret_is_fail(self, monkeypatch):
        monkeypatch.delenv("AUTH_SECRET_KEY", raising=False)
        assert check_jwt_roundtrip().severity == Severity.FAIL


# ── Crypto: history encryption roundtrip ─────────────────────────────────────

class TestHistoryEncryptionRoundtrip:
    def test_ok(self, monkeypatch):
        monkeypatch.setenv("HISTORY_SECRET_KEY", good_secret())
        assert check_history_encryption_roundtrip().severity == Severity.OK

    def test_no_secret_is_warn(self, monkeypatch):
        monkeypatch.delenv("HISTORY_SECRET_KEY", raising=False)
        assert check_history_encryption_roundtrip().severity == Severity.WARN


# ── Database ──────────────────────────────────────────────────────────────────

class TestDatabase:
    def test_connectivity_ok(self, tmp_path):
        db_path = run(_make_db(tmp_path))
        assert run(check_db_connectivity(db_path)).severity == Severity.OK

    def test_connectivity_missing_path_is_fail(self):
        r = run(check_db_connectivity("/nonexistent/path/diag.sqlite"))
        assert r.severity == Severity.FAIL

    def test_connectivity_empty_path_is_warn(self):
        r = run(check_db_connectivity(""))
        assert r.severity == Severity.WARN

    def test_schema_ok(self, tmp_path):
        db_path = run(_make_db(tmp_path))
        assert run(check_db_schema(db_path)).severity == Severity.OK

    def test_schema_missing_tables(self, tmp_path):
        import aiosqlite

        db_path = str(tmp_path / "partial.db")

        async def _create():
            async with aiosqlite.connect(db_path) as conn:
                await conn.execute("CREATE TABLE users (id TEXT PRIMARY KEY)")
                await conn.commit()

        run(_create())
        r = run(check_db_schema(db_path))
        assert r.severity == Severity.FAIL
        assert "Missing" in r.message

    def test_wal_mode_ok(self, tmp_path):
        db_path = run(_make_db(tmp_path))
        assert run(check_db_wal_mode(db_path)).severity == Severity.OK

    def test_wal_mode_absent_is_warn(self, tmp_path):
        import aiosqlite

        db_path = str(tmp_path / "nowal.db")

        async def _create():
            async with aiosqlite.connect(db_path) as conn:
                await conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
                await conn.commit()

        run(_create())
        r = run(check_db_wal_mode(db_path))
        assert r.severity == Severity.WARN  # default mode is 'delete'

    def test_canary_write_ok(self, tmp_path):
        db_path = run(_make_db(tmp_path))
        assert run(check_db_canary_write(db_path)).severity == Severity.OK

    def test_canary_write_empty_path_is_warn(self):
        assert run(check_db_canary_write("")).severity == Severity.WARN


# ── Tool registry ─────────────────────────────────────────────────────────────

class TestToolRegistry:
    def test_ok_with_dict(self):
        results = check_tool_registry({"calculator": object(), "web_search": object()})
        assert all(r.severity == Severity.OK for r in results)

    def test_fail_empty_dict(self):
        results = check_tool_registry({})
        assert any(r.severity == Severity.FAIL for r in results)

    def test_duplicate_names_from_list(self):
        results = check_tool_registry(["tool_a", "tool_b", "tool_a"])
        assert any(r.severity == Severity.FAIL and "uplicate" in r.message for r in results)

    def test_none_registry_is_warn(self):
        results = check_tool_registry(None)
        assert all(r.severity == Severity.WARN for r in results)

    def test_registry_object_with_tools_attr(self):
        class FakeRegistry:
            tools = {"t1": object(), "t2": object()}

        results = check_tool_registry(FakeRegistry())
        assert all(r.severity == Severity.OK for r in results)

    def test_returns_exactly_two_results(self):
        assert len(check_tool_registry({"a": 1})) == 2


# ── VRAM budget ───────────────────────────────────────────────────────────────

class TestVramBudget:
    def _cfg(self, total_gb, headroom_gb, tiers):
        class _Tier:
            def __init__(self, vram, pinned):
                self.vram_estimate_gb = vram
                self.pinned = pinned

        class _Vram:
            def __init__(self, t, h):
                self.total_vram_gb = t
                self.headroom_gb = h

        class _Models:
            def __init__(self, t):
                self.tiers = {k: _Tier(v["vram"], v["pinned"]) for k, v in t.items()}

        class _Cfg:
            def __init__(self):
                self.vram = _Vram(total_gb, headroom_gb)
                self.models = _Models(tiers)

        return _Cfg()

    def test_ok_fits(self):
        cfg = self._cfg(24, 2, {"vision": {"vram": 8, "pinned": True}})
        assert check_vram_budget(cfg).severity == Severity.OK

    def test_fail_pinned_exceeds_usable(self):
        cfg = self._cfg(8, 1, {
            "vision":  {"vram": 5, "pinned": True},
            "quality": {"vram": 4, "pinned": True},
        })
        assert check_vram_budget(cfg).severity == Severity.FAIL

    def test_unpinned_tiers_not_counted(self):
        cfg = self._cfg(8, 1, {
            "vision":    {"vram": 5, "pinned": True},
            "versatile": {"vram": 10, "pinned": False},  # not pinned → not counted
        })
        assert check_vram_budget(cfg).severity == Severity.OK

    def test_no_cfg_is_warn(self):
        assert check_vram_budget(None).severity == Severity.WARN


# ── GPU ───────────────────────────────────────────────────────────────────────

class TestGpuAvailable:
    def test_no_gpu_is_warn_not_fail(self):
        # In CI (no GPU driver) pynvml raises — result must be WARN, not FAIL.
        r = check_gpu_available()
        assert r.severity in (Severity.OK, Severity.WARN)


# ── Service connectivity (mocked HTTP) ────────────────────────────────────────

class TestServiceConnectivityMocked:
    def _patch_get(self, monkeypatch, status: int, body: str = "{}"):
        import httpx

        async def _fake_get(self_client, url, **kwargs):
            return httpx.Response(status, text=body)

        monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get)

    def _patch_get_raise(self, monkeypatch, exc):
        import httpx

        async def _fake_get(self_client, url, **kwargs):
            raise exc

        monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get)

    # llama.cpp pinned tiers (vision, embedding)
    def _fake_cfg(self):
        from types import SimpleNamespace
        return SimpleNamespace(models=SimpleNamespace(tiers={
            "embedding": SimpleNamespace(
                pinned=True,
                resolved_endpoint=lambda: "http://localhost:8090/v1",
            ),
            "fast": SimpleNamespace(
                pinned=False,
                resolved_endpoint=lambda: "http://localhost:8012/v1",
            ),
        }))

    def test_pinned_llamacpp_ok(self, monkeypatch):
        self._patch_get(monkeypatch, 200, '{"data":[]}')
        results = run(check_pinned_llamacpp_tiers(self._fake_cfg()))
        # Only the pinned tier is checked
        assert len(results) == 1
        assert results[0].severity == Severity.OK

    def test_pinned_llamacpp_unreachable_is_warn(self, monkeypatch):
        import httpx
        self._patch_get_raise(monkeypatch, httpx.ConnectError("refused"))
        results = run(check_pinned_llamacpp_tiers(self._fake_cfg()))
        assert len(results) == 1
        assert results[0].severity == Severity.WARN

    # Qdrant
    def test_qdrant_ok(self, monkeypatch):
        self._patch_get(monkeypatch, 200)
        assert run(check_qdrant_reachable("http://localhost:6333")).severity == Severity.OK

    def test_qdrant_unreachable_is_warn(self, monkeypatch):
        import httpx
        self._patch_get_raise(monkeypatch, httpx.ConnectError("refused"))
        assert run(check_qdrant_reachable("http://localhost:6333")).severity == Severity.WARN

    # Web search provider (native mode — no SearXNG container)
    def test_web_search_none_is_ok(self, monkeypatch):
        monkeypatch.setenv("WEB_SEARCH_PROVIDER", "none")
        monkeypatch.delenv("BRAVE_API_KEY", raising=False)
        assert run(check_web_search_provider(None)).severity == Severity.OK

    def test_web_search_brave_without_key_warns(self, monkeypatch):
        monkeypatch.setenv("WEB_SEARCH_PROVIDER", "brave")
        monkeypatch.delenv("BRAVE_API_KEY", raising=False)
        assert run(check_web_search_provider(None)).severity == Severity.WARN

    def test_web_search_brave_with_key_is_ok(self, monkeypatch):
        monkeypatch.setenv("WEB_SEARCH_PROVIDER", "brave")
        monkeypatch.setenv("BRAVE_API_KEY", "test-key")
        assert run(check_web_search_provider(None)).severity == Severity.OK

    # Redis
    def test_redis_no_url_is_ok(self, monkeypatch):
        monkeypatch.delenv("REDIS_URL", raising=False)
        assert run(check_redis_reachable(None)).severity == Severity.OK

    def test_redis_unreachable_is_warn(self, monkeypatch):
        import redis.asyncio as aioredis

        async def _fake_ping(self_client):
            raise ConnectionRefusedError("refused")

        monkeypatch.setattr(aioredis.Redis, "ping", _fake_ping)
        assert run(check_redis_reachable("redis://localhost:6379/0")).severity == Severity.WARN


# ── run_startup_diagnostics orchestrator ─────────────────────────────────────

class TestRunStartupDiagnostics:
    def _patch_http(self, monkeypatch):
        import httpx

        async def _fake_get(self_client, url, **kwargs):
            raise httpx.ConnectError("mocked — no live services")

        monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get)

    def test_returns_list_of_check_results(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AUTH_SECRET_KEY", good_secret())
        monkeypatch.setenv("HISTORY_SECRET_KEY", good_secret())
        monkeypatch.setenv("JUPYTER_TOKEN", "x" * 32)
        monkeypatch.setenv("PUBLIC_BASE_URL", "http://localhost:8000")
        monkeypatch.setenv("COOKIE_SECURE", "false")
        monkeypatch.setenv("N8N_BASIC_AUTH_ACTIVE", "true")
        monkeypatch.setenv("N8N_ADMIN_USER", "admin")
        monkeypatch.setenv("N8N_ADMIN_PASSWORD", "s3cr3t!")
        monkeypatch.setenv("ALLOWED_ORIGINS", "http://localhost:3001")
        monkeypatch.delenv("REDIS_URL", raising=False)
        self._patch_http(monkeypatch)

        results = run(run_startup_diagnostics(db_path=""))
        assert isinstance(results, list)
        assert len(results) > 0
        assert all(isinstance(r, CheckResult) for r in results)

    def test_never_raises_on_broken_env(self, monkeypatch):
        """run_startup_diagnostics must complete without raising regardless of config."""
        monkeypatch.delenv("AUTH_SECRET_KEY", raising=False)
        monkeypatch.delenv("HISTORY_SECRET_KEY", raising=False)
        monkeypatch.delenv("REDIS_URL", raising=False)
        self._patch_http(monkeypatch)

        results = run(run_startup_diagnostics(db_path=""))
        assert isinstance(results, list)

    def test_fail_results_present_when_secret_missing(self, monkeypatch):
        monkeypatch.delenv("AUTH_SECRET_KEY", raising=False)
        monkeypatch.delenv("REDIS_URL", raising=False)
        self._patch_http(monkeypatch)

        results = run(run_startup_diagnostics(db_path=""))
        fails = [r for r in results if r.severity == Severity.FAIL]
        assert any("AUTH_SECRET_KEY" in r.name for r in fails)

    def test_all_ok_with_good_config(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AUTH_SECRET_KEY", good_secret())
        monkeypatch.setenv("HISTORY_SECRET_KEY", good_secret())
        monkeypatch.setenv("JUPYTER_TOKEN", "x" * 32)
        monkeypatch.setenv("PUBLIC_BASE_URL", "https://chat.example.com")
        monkeypatch.setenv("COOKIE_SECURE", "true")
        monkeypatch.setenv("N8N_BASIC_AUTH_ACTIVE", "true")
        monkeypatch.setenv("N8N_ADMIN_USER", "admin")
        monkeypatch.setenv("N8N_ADMIN_PASSWORD", "s3cr3t!")
        monkeypatch.setenv("ALLOWED_ORIGINS", "https://chat.example.com")
        monkeypatch.delenv("REDIS_URL", raising=False)
        self._patch_http(monkeypatch)

        db_path = run(_make_db(tmp_path))
        results = run(run_startup_diagnostics(
            db_path=db_path,
            registry={"calculator": object(), "web_search": object()},
        ))

        # Every non-service result should be OK or WARN (services are mocked unreachable → WARN)
        hard_fails = [r for r in results if r.severity == Severity.FAIL]
        assert hard_fails == [], f"Unexpected failures: {hard_fails}"


# ── Live service tests (skipped in CI unless env vars set) ────────────────────

@pytest.mark.skipif(not os.getenv("LIVE_QDRANT_URL"), reason="set LIVE_QDRANT_URL to run")
def test_live_qdrant():
    r = run(check_qdrant_reachable(os.environ["LIVE_QDRANT_URL"]))
    assert r.severity == Severity.OK, r.message


@pytest.mark.skipif(not os.getenv("LIVE_REDIS_URL"), reason="set LIVE_REDIS_URL to run")
def test_live_redis():
    r = run(check_redis_reachable(os.environ["LIVE_REDIS_URL"]))
    assert r.severity == Severity.OK, r.message


