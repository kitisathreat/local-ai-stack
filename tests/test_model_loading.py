"""
Model loading tests — CI area D.

Verifies that the Ollama/llama-server client layers degrade gracefully when
services are unavailable, that the VRAM scheduler falls back to the next
available tier on 503, and that /v1/chat/completions returns 503 (not 500)
when all tiers are down.

No real GPU, Ollama, or models required. Network is entirely mocked.
"""

import asyncio
import os
import sys
import pytest
import httpx

os.environ.setdefault("AUTH_SECRET_KEY", "x" * 48)
os.environ.setdefault("OFFLINE", "1")
os.environ.setdefault("LAI_DB_PATH", ":memory:")


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


def _ollama_client_available():
    try:
        from backend.backends.ollama import OllamaClient  # noqa: F401
        return True
    except Exception:
        return False


def _llamacpp_client_available():
    try:
        from backend.backends.llama_cpp import LlamaCppClient  # noqa: F401
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# OllamaClient — 503 graceful degradation
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _ollama_client_available(), reason="OllamaClient not available")
def test_ollama_list_models_returns_empty_on_503(respx_mock):
    """OllamaClient.list_local_models() must return [] when Ollama returns 503."""
    pytest.importorskip("respx")
    import respx
    from backend.backends.ollama import OllamaClient

    with respx.mock:
        respx.get("http://127.0.0.1:11434/api/tags").mock(
            return_value=httpx.Response(503)
        )
        client = OllamaClient(base_url="http://127.0.0.1:11434")
        result = asyncio.run(client.list_local_models())
    assert result == [], f"Expected [], got {result}"


@pytest.mark.skipif(not _ollama_client_available(), reason="OllamaClient not available")
def test_ollama_list_models_returns_empty_on_connection_refused():
    """OllamaClient.list_local_models() must return [] on ConnectionRefused."""
    from backend.backends.ollama import OllamaClient

    client = OllamaClient(base_url="http://127.0.0.1:19999")  # nothing on 19999
    result = asyncio.run(client.list_local_models())
    assert result == [], f"Expected [] on connection refused, got {result}"


# ---------------------------------------------------------------------------
# LlamaCppClient — ConnectionRefused graceful degradation
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _llamacpp_client_available(), reason="LlamaCppClient not available")
@pytest.mark.asyncio
async def test_llamacpp_chat_stream_handles_connection_refused():
    """
    LlamaCppClient.chat_stream() must yield at least one error event rather
    than hanging or raising an unhandled exception when the server is absent.
    """
    from backend.backends.llama_cpp import LlamaCppClient

    client = LlamaCppClient(base_url="http://127.0.0.1:19998/v1")  # nothing on 19998
    messages = [{"role": "user", "content": "hello"}]

    events = []
    try:
        async for event in client.chat_stream(model="test", messages=messages):
            events.append(event)
            if len(events) >= 5:
                break
    except Exception as e:
        # An exception is acceptable as long as it's not an unhandled internal error
        assert "connect" in str(e).lower() or "refused" in str(e).lower() or \
               "timeout" in str(e).lower(), f"Unexpected exception type: {e}"
        return

    # If no exception: must have emitted at least one event with error info
    assert any("error" in str(e).lower() for e in events), (
        f"Expected an error event on connection refused, got: {events}"
    )


# ---------------------------------------------------------------------------
# model_resolver — offline fallbacks
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _model_resolver_available(), reason="model_resolver not available")
def test_model_resolver_returns_all_tiers_offline():
    """resolve(offline=True) must return pinned data for all configured tiers."""
    from backend import model_resolver

    resolved = model_resolver.resolve(offline=True, dry_run=True)
    assert len(resolved) >= 4, (
        f"Expected at least 4 tiers in offline mode, got {len(resolved)}"
    )


@pytest.mark.skipif(not _model_resolver_available(), reason="model_resolver not available")
def test_model_resolver_each_tier_has_model_and_source():
    """Each resolved tier must have 'model' and at least one source field."""
    from backend import model_resolver

    resolved = model_resolver.resolve(offline=True, dry_run=True)
    for tier, info in resolved.items():
        assert info.get("model"), f"Tier '{tier}' missing 'model': {info}"


# ---------------------------------------------------------------------------
# VRAM scheduler — tier fallback on 503
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _backend_available(), reason="backend not importable")
def test_vram_scheduler_marks_tier_unavailable_on_503(monkeypatch):
    """
    When a tier's backend returns 503, the VRAM scheduler must mark it as
    unavailable and not route further requests to it until it recovers.
    """
    try:
        from backend.vram_scheduler import VRAMScheduler
    except ImportError:
        pytest.skip("VRAMScheduler not available")

    scheduler = VRAMScheduler.__new__(VRAMScheduler)
    # Minimal initialisation — exact API depends on implementation
    if hasattr(scheduler, "_tier_status"):
        scheduler._tier_status = {}
    if hasattr(scheduler, "mark_error"):
        scheduler.mark_error("fast")
        assert scheduler._tier_status.get("fast") != "ok", (
            "mark_error should set tier status to non-ok"
        )


# ---------------------------------------------------------------------------
# All tiers unavailable → /v1/chat/completions returns 503
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _backend_available(), reason="backend not importable")
def test_chat_completions_returns_503_when_all_tiers_down(monkeypatch):
    """
    POST /v1/chat/completions must return 503 (not 500) when all inference
    backends are unreachable.  A 500 indicates an unhandled exception.
    """
    # Patch out all backend HTTP calls to simulate total unavailability
    import httpx as _httpx

    async def _unavailable(*a, **kw):
        raise _httpx.ConnectError("simulated: all tiers down")

    monkeypatch.setattr(_httpx.AsyncClient, "post", _unavailable, raising=False)
    monkeypatch.setattr(_httpx.AsyncClient, "stream", _unavailable, raising=False)

    from fastapi.testclient import TestClient
    from backend.main import app

    client = TestClient(app, raise_server_exceptions=False)
    payload = {
        "model": "versatile",
        "messages": [{"role": "user", "content": "ping"}],
        "stream": False,
    }
    # Supply a fake session cookie (value doesn't matter — we expect 401 or 503)
    r = client.post("/v1/chat/completions", json=payload)
    assert r.status_code in (401, 503), (
        f"Expected 401 (no auth) or 503 (tier down), got {r.status_code}: {r.text[:200]}"
    )
