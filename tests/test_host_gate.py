"""Unit tests for backend.middleware.host_gate.

Mounts HostGateMiddleware on a bare FastAPI app with stub routes so we
can drive it with synthetic Host headers and assert 403 / 200.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def _build_app(monkeypatch, *, chat_host="chat.mylensandi.com", airgap=False):
    monkeypatch.setenv("CHAT_HOSTNAME", chat_host)
    monkeypatch.setenv("ADMIN_API_ALLOWED_HOSTS", "127.0.0.1,localhost")

    from backend import airgap as airgap_mod
    # Monkeypatch the airgap module-level state.
    monkeypatch.setattr(airgap_mod, "is_enabled", lambda: airgap)

    from backend.middleware.host_gate import HostGateMiddleware

    app = FastAPI()
    app.add_middleware(HostGateMiddleware)

    @app.get("/healthz")
    async def _h(): return {"ok": True}

    @app.get("/v1/models")
    async def _m(): return {"data": []}

    @app.post("/v1/chat/completions")
    async def _c(): return {"ok": True}

    @app.get("/api/chats")
    async def _ac(): return {"data": []}

    @app.get("/admin/me")
    async def _am(): return {"ok": True}

    @app.post("/auth/login")
    async def _l(): return {"ok": True}

    @app.get("/api/airgap")
    async def _ag(): return {"enabled": airgap}

    @app.get("/")
    async def _root(): return {"ok": True}

    return TestClient(app)


def test_healthz_always_allowed(monkeypatch):
    c = _build_app(monkeypatch)
    assert c.get("/healthz", headers={"host": "evil.example.com"}).status_code == 200


def test_v1_models_always_allowed(monkeypatch):
    c = _build_app(monkeypatch)
    assert c.get("/v1/models", headers={"host": "evil.example.com"}).status_code == 200


def test_chat_path_from_correct_host_allowed(monkeypatch):
    c = _build_app(monkeypatch)
    r = c.post("/v1/chat/completions", headers={"host": "chat.mylensandi.com"})
    assert r.status_code == 200


def test_chat_path_from_wrong_host_forbidden(monkeypatch):
    c = _build_app(monkeypatch)
    r = c.post("/v1/chat/completions", headers={"host": "evil.example.com"})
    assert r.status_code == 403


def test_chat_path_from_localhost_allowed_for_debugging(monkeypatch):
    """Loopback can still reach the chat API for local dev / tests."""
    c = _build_app(monkeypatch)
    r = c.post("/v1/chat/completions", headers={"host": "127.0.0.1"})
    assert r.status_code == 200


def test_root_path_from_localhost_blocked_in_normal_mode(monkeypatch):
    """The chat HTML page is chat-subdomain-only, even from loopback —
    prevents a user from accidentally browsing localhost and thinking
    chat is unprotected."""
    c = _build_app(monkeypatch)
    r = c.get("/", headers={"host": "127.0.0.1"})
    assert r.status_code == 403


def test_root_path_from_chat_host_allowed(monkeypatch):
    c = _build_app(monkeypatch)
    r = c.get("/", headers={"host": "chat.mylensandi.com"})
    assert r.status_code == 200


def test_admin_from_localhost_allowed(monkeypatch):
    c = _build_app(monkeypatch)
    r = c.get("/admin/me", headers={"host": "localhost"})
    assert r.status_code == 200


def test_admin_from_chat_host_allowed(monkeypatch):
    """Admin paths are in _ALWAYS_ALLOWED_PREFIXES — the Qt window needs
    to reach them, and cloudflared's origin check occasionally ends up
    on them too. Backend auth still protects them."""
    c = _build_app(monkeypatch)
    r = c.get("/admin/me", headers={"host": "chat.mylensandi.com"})
    assert r.status_code == 200


def test_auth_login_always_allowed(monkeypatch):
    c = _build_app(monkeypatch)
    for host in ("127.0.0.1", "chat.mylensandi.com", "evil.example.com"):
        r = c.post("/auth/login", headers={"host": host})
        assert r.status_code == 200


def test_airgap_locks_chat_subdomain(monkeypatch):
    c = _build_app(monkeypatch, airgap=True)
    r = c.post("/v1/chat/completions", headers={"host": "chat.mylensandi.com"})
    assert r.status_code == 403


def test_airgap_allows_loopback_chat(monkeypatch):
    c = _build_app(monkeypatch, airgap=True)
    r = c.post("/v1/chat/completions", headers={"host": "127.0.0.1"})
    assert r.status_code == 200
