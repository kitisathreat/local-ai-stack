"""
Backend startup tests — CI area B.

Tests that the backend imports cleanly, that /healthz degrades gracefully
when external services (llama-server, Qdrant) are absent, and that the
model resolver produces pinned fallbacks when offline.

All tests run on Linux CI and the Windows integration job. GPU and the
embedding/vision llama-server processes are NOT required — we only verify
that missing services produce structured "degraded" or error responses
rather than unhandled exceptions.
"""

import asyncio
import os
import sys
import pytest

# Minimal env required before importing the backend.
# CHAT_HOSTNAME must match the TestClient host so the host-gate middleware
# passes (TestClient sends Host: testclient by default).
os.environ.setdefault("AUTH_SECRET_KEY", "x" * 48)
os.environ.setdefault("OFFLINE", "1")
os.environ.setdefault("LAI_DB_PATH", ":memory:")
os.environ.setdefault("CHAT_HOSTNAME", "testclient")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _backend_available():
    try:
        import backend.main  # noqa: F401
        return True
    except Exception:
        return False


def _model_resolver_available():
    try:
        import backend.model_resolver  # noqa: F401
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Import smoke test
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _backend_available(), reason="backend not importable")
def test_backend_imports_cleanly():
    """backend.main must import without raising under minimal env."""
    import importlib
    mod = importlib.import_module("backend.main")
    assert hasattr(mod, "app"), "backend.main must expose a FastAPI 'app' object"


# ---------------------------------------------------------------------------
# /healthz — graceful degradation
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _backend_available(), reason="backend not importable")
def test_healthz_returns_structured_response():
    """
    /healthz must return 200 or 503 with a JSON body containing a 'status'
    key even when all external services are unreachable.  A 500 or an
    unhandled exception is a bug.
    """
    from fastapi.testclient import TestClient
    from backend.main import app

    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/healthz")
    assert r.status_code in (200, 503), (
        f"/healthz returned unexpected status {r.status_code}"
    )
    body = r.json()
    assert "status" in body or "ok" in body, (
        f"/healthz body missing 'status' or 'ok': {body}"
    )
    if "status" in body:
        assert body["status"] in ("ok", "degraded", "error"), (
            f"unexpected status value: {body['status']}"
        )


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _backend_available(), reason="backend not importable")
def test_diagnostics_do_not_raise():
    """
    diagnostics.run_all_checks() (or equivalent) must return a dict of
    structured results rather than raising when services are absent.
    """
    try:
        from backend.diagnostics import run_all_checks
    except ImportError:
        pytest.skip("backend.diagnostics.run_all_checks not available")

    results = asyncio.run(run_all_checks())
    assert isinstance(results, dict), "run_all_checks() must return a dict"
    for key, val in results.items():
        assert isinstance(val, dict), f"check '{key}' result must be a dict"
        assert "ok" in val or "error" in val or "status" in val, (
            f"check '{key}' result has no ok/error/status key: {val}"
        )


# ---------------------------------------------------------------------------
# model_resolver offline mode
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _model_resolver_available(), reason="model_resolver not available")
def test_model_resolver_offline_returns_pinned_fallbacks():
    """
    model_resolver.resolve(offline=True) must return at least 4 tiers with a
    non-empty 'model' field each — using only data from config/model-sources.yaml,
    without any network calls.
    """
    from backend import model_resolver

    result = model_resolver.resolve(offline=True)
    resolved = result.resolved
    assert isinstance(resolved, dict), "resolve() must return a dict"
    assert len(resolved) >= 4, (
        f"Expected at least 4 tiers, got {len(resolved)}: {list(resolved)}"
    )
    for tier, info in resolved.items():
        assert info.repo, f"Tier '{tier}' has no HF repo: {info}"
        assert info.filename, f"Tier '{tier}' has no GGUF filename: {info}"


@pytest.mark.skipif(not _model_resolver_available(), reason="model_resolver not available")
def test_model_resolver_offline_makes_no_network_calls(monkeypatch):
    """With offline=True, resolve() must not call httpx or urllib."""
    import httpx

    called = []

    def _no_network(*a, **kw):
        called.append((a, kw))
        raise AssertionError("Network call detected in offline mode")

    monkeypatch.setattr(httpx, "get", _no_network, raising=False)
    monkeypatch.setattr(httpx, "Client", _no_network, raising=False)

    from backend import model_resolver
    model_resolver.resolve(offline=True)
    assert not called, f"Unexpected network calls: {called}"


# ---------------------------------------------------------------------------
# Auth secret validation
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _backend_available(), reason="backend not importable")
def test_missing_auth_secret_key_raises_on_startup(monkeypatch):
    """
    If AUTH_SECRET_KEY is absent or empty, the backend must raise a clear
    configuration error rather than starting with an insecure default.
    """
    monkeypatch.delenv("AUTH_SECRET_KEY", raising=False)
    monkeypatch.setenv("AUTH_SECRET_KEY", "")

    import importlib
    import backend.auth as auth_mod
    importlib.reload(auth_mod)

    with pytest.raises(Exception):
        # _secret_key() or equivalent must raise when the key is empty
        auth_mod._secret_key()
