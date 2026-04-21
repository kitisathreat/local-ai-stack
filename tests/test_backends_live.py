"""Live-backend smoke tests (#22).

Skipped unless `LIVE_BACKEND_TESTS=1`. When enabled, these probe the
actually-running services — Ollama, Qdrant, llama-server, SearXNG, and
the FastAPI backend itself — to confirm end-to-end connectivity.

Usage:
    docker compose up -d
    LIVE_BACKEND_TESTS=1 pytest tests/test_backends_live.py -v

All tests deliberately use short timeouts. If a service isn't up yet,
the test is marked SKIP rather than failed — reruns while the stack is
still warming up should be productive.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


LIVE = os.environ.get("LIVE_BACKEND_TESTS", "").lower() in {"1", "true", "yes"}

pytestmark = pytest.mark.skipif(
    not LIVE, reason="Set LIVE_BACKEND_TESTS=1 to run (requires docker compose up)",
)


OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
LLAMACPP_URL = os.environ.get("LLAMACPP_URL", "http://localhost:8001/v1")
BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")
SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://localhost:4000")


def _skip_if_down(url: str, reason: str):
    import httpx
    try:
        r = httpx.get(url, timeout=2.0)
    except Exception as e:
        pytest.skip(f"{reason}: {e}")
    if r.status_code >= 500:
        pytest.skip(f"{reason}: HTTP {r.status_code}")


# ── Ollama ────────────────────────────────────────────────────────────────

def test_ollama_tags_returns_model_list():
    import httpx
    _skip_if_down(f"{OLLAMA_URL}/api/tags", "Ollama not reachable")
    r = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=5)
    assert r.status_code == 200
    body = r.json()
    assert "models" in body and isinstance(body["models"], list)


def test_ollama_embed_returns_768d_vector():
    """Smoke-tests the embedding path used by RAG + memory."""
    import httpx
    _skip_if_down(f"{OLLAMA_URL}/api/tags", "Ollama not reachable")
    r = httpx.post(
        f"{OLLAMA_URL}/api/embed",
        json={"model": "nomic-embed-text", "input": ["hello world"]},
        timeout=30,
    )
    if r.status_code == 404:
        pytest.skip("nomic-embed-text not pulled; run scripts/setup-models.sh")
    assert r.status_code == 200
    vecs = r.json().get("embeddings") or []
    assert len(vecs) == 1
    assert len(vecs[0]) == 768


# ── Qdrant ────────────────────────────────────────────────────────────────

def test_qdrant_collections_endpoint_responds():
    import httpx
    _skip_if_down(f"{QDRANT_URL}/collections", "Qdrant not reachable")
    r = httpx.get(f"{QDRANT_URL}/collections", timeout=5)
    assert r.status_code == 200
    assert "result" in r.json()


# ── llama.cpp ─────────────────────────────────────────────────────────────

def test_llamacpp_models_endpoint_responds():
    """Optional — skipped if the vision GGUF isn't on disk."""
    import httpx
    try:
        r = httpx.get(f"{LLAMACPP_URL}/models", timeout=3)
    except Exception as e:
        pytest.skip(f"llama-server not reachable: {e}")
    if r.status_code != 200:
        pytest.skip(f"llama-server not ready: HTTP {r.status_code}")
    body = r.json()
    assert "data" in body


# ── Backend ───────────────────────────────────────────────────────────────

def test_backend_healthz():
    import httpx
    _skip_if_down(f"{BACKEND_URL}/healthz", "Backend not reachable")
    r = httpx.get(f"{BACKEND_URL}/healthz", timeout=5)
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_backend_v1_models_lists_tiers():
    import httpx
    _skip_if_down(f"{BACKEND_URL}/healthz", "Backend not reachable")
    r = httpx.get(f"{BACKEND_URL}/v1/models", timeout=5)
    assert r.status_code == 200
    data = r.json().get("data") or []
    # 5 tiers by default (fast/versatile/highest_quality/coding/vision).
    assert len(data) >= 3, f"Expected ≥3 tiers, got {len(data)}: {data}"
    for entry in data:
        assert entry["id"].startswith("tier.")
        assert "context_window" in entry


def test_backend_auth_request_accepts_valid_email():
    """Verify /auth/request round-trips without requiring a working SMTP."""
    import httpx
    _skip_if_down(f"{BACKEND_URL}/healthz", "Backend not reachable")
    r = httpx.post(
        f"{BACKEND_URL}/auth/request",
        json={"email": "live-test@example.com"},
        timeout=5,
    )
    # 200 (sent / logged) or 429 (rate-limited on replay) are both healthy.
    assert r.status_code in {200, 429}, f"Unexpected {r.status_code}: {r.text}"


# ── SearXNG (optional) ────────────────────────────────────────────────────

def test_searxng_search_endpoint_responds():
    import httpx
    try:
        r = httpx.get(f"{SEARXNG_URL}/search", params={"q": "ping", "format": "json"}, timeout=5)
    except Exception as e:
        pytest.skip(f"SearXNG not reachable: {e}")
    if r.status_code >= 500:
        pytest.skip(f"SearXNG not ready: HTTP {r.status_code}")
    # SearXNG may return 403 if it's configured to block anonymous JSON — that's
    # still a healthy service; the backend uses the authenticated path.
    assert r.status_code in {200, 403}, f"Unexpected {r.status_code}"
