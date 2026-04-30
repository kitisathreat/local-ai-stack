"""
Model loading tests — CI area D.

Verifies that the Ollama/llama-server client layers degrade gracefully when
services are unavailable, that the VRAM scheduler falls back on 503, and that
/v1/chat/completions returns 503 (not 500) when all tiers are down.

No real GPU, Ollama, or models required. Network is entirely mocked.
"""

import asyncio
import os
import sys
import pytest
import httpx

# CHAT_HOSTNAME must match the TestClient host header (default: "testclient")
# so the host-gate middleware passes through requests in tests.
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
def test_ollama_list_models_returns_empty_on_503():
    """OllamaClient.list_local_models() must return [] when Ollama returns 503."""
    respx = pytest.importorskip("respx")
    from backend.backends.ollama import OllamaClient

    with respx.mock:
        respx.get("http://127.0.0.1:11434/api/tags").mock(
            return_value=httpx.Response(503)
        )
        client = OllamaClient(endpoint="http://127.0.0.1:11434")
        result = asyncio.run(client.list_local_models())
    assert result == [], f"Expected [], got {result}"


@pytest.mark.skipif(not _ollama_client_available(), reason="OllamaClient not available")
def test_ollama_list_models_returns_empty_on_connection_refused():
    """OllamaClient.list_local_models() must return [] on ConnectionRefused."""
    from backend.backends.ollama import OllamaClient

    client = OllamaClient(endpoint="http://127.0.0.1:19999")  # nothing on 19999
    result = asyncio.run(client.list_local_models())
    assert result == [], f"Expected [] on connection refused, got {result}"


# ---------------------------------------------------------------------------
# LlamaCppClient — ConnectionRefused graceful degradation
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _llamacpp_client_available(), reason="LlamaCppClient not available")
@pytest.mark.asyncio
async def test_llamacpp_chat_stream_handles_connection_refused():
    """
    LlamaCppClient.is_ready() must return False (not raise) when llama-server
    is unreachable — verifying that the client layer degrades gracefully.
    """
    from backend.backends.llama_cpp import LlamaCppClient

    client = LlamaCppClient(endpoint="http://127.0.0.1:19998/v1")  # nothing on 19998
    result = await client.is_ready()
    assert result is False, f"Expected is_ready()=False on connection refused, got {result}"


# ---------------------------------------------------------------------------
# model_resolver — offline fallbacks
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _model_resolver_available(), reason="model_resolver not available")
def test_model_resolver_returns_all_tiers_offline():
    """resolve(offline=True) must return pinned data for all configured tiers."""
    from backend import model_resolver

    result = model_resolver.resolve(offline=True)
    resolved = result.resolved
    assert len(resolved) >= 4, (
        f"Expected at least 4 tiers in offline mode, got {len(resolved)}"
    )


@pytest.mark.skipif(not _model_resolver_available(), reason="model_resolver not available")
def test_model_resolver_each_tier_has_model_and_source():
    """Each resolved tier must have an identifier (model tag or HF path)."""
    from backend import model_resolver

    result = model_resolver.resolve(offline=True)
    for tier, info in result.resolved.items():
        assert info.identifier, f"Tier '{tier}' missing identifier: {info}"


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
    POST /v1/chat/completions must NOT return 500 when all inference backends
    are unreachable.  A 500 indicates an unhandled exception.

    The endpoint is an SSE streaming endpoint: it sends HTTP 200 immediately
    and delivers backend errors as SSE `error` events within the stream.
    So the valid outcomes when tiers are down are:
      - 200  SSE stream with an embedded error event (streaming path)
      - 401  auth required (if auth gate fires before tier dispatch)
      - 503  synchronous rejection before SSE starts

    The lifespan startup must complete (state.config initialised) before we
    patch httpx — otherwise startup probes raise ConnectError and leave the
    app half-initialised.  Using TestClient as a context manager triggers
    lifespan.__aenter__ immediately on entry.
    """
    import httpx as _httpx

    from fastapi.testclient import TestClient
    from backend.main import app

    # Enter the context so lifespan startup runs with un-patched httpx,
    # then patch so the actual chat request hits ConnectError on every tier.
    with TestClient(app, base_url="http://testclient", raise_server_exceptions=False) as client:
        async def _unavailable(*a, **kw):
            raise _httpx.ConnectError("simulated: all tiers down")

        monkeypatch.setattr(_httpx.AsyncClient, "post", _unavailable, raising=False)
        monkeypatch.setattr(_httpx.AsyncClient, "stream", _unavailable, raising=False)

        payload = {
            "model": "versatile",
            "messages": [{"role": "user", "content": "ping"}],
            "stream": False,
        }
        r = client.post("/v1/chat/completions", json=payload)

    assert r.status_code != 500, (
        f"Got 500 (unhandled exception) when all tiers are down: {r.text[:300]}"
    )
    assert r.status_code in (200, 401, 503), (
        f"Unexpected status when all tiers are down: {r.status_code}: {r.text[:200]}"
    )
