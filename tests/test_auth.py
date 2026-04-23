"""Unit tests for backend/auth.py + backend/passwords.py.

Phase 3: magic-link replaced with bcrypt username/password. These tests
exercise the password round-trip, JWT session tokens, and the
constant-time `authenticate` helper.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    p = tmp_path / "auth_test.db"
    monkeypatch.setenv("LAI_DB_PATH", str(p))
    monkeypatch.setenv("AUTH_SECRET_KEY", "test-secret-key-at-least-32-bytes-long-xxxx")
    # Use the minimum bcrypt cost so tests run quickly.
    monkeypatch.setenv("BCRYPT_ROUNDS", "4")
    import importlib
    from backend import db as db_mod
    importlib.reload(db_mod)
    db_mod.DB_PATH = p
    return p


@pytest.fixture
def auth_cfg():
    from backend.config import AppConfig
    cfg = AppConfig.load(config_dir=ROOT / "config")
    return cfg.auth


def run(coro):
    return asyncio.run(coro)


# ── Session tokens (JWT) ────────────────────────────────────────────────

def test_session_token_roundtrip(auth_cfg, db_path):
    from backend.auth import issue_session_token, decode_session_token
    tok = issue_session_token(42, auth_cfg)
    assert decode_session_token(tok, auth_cfg) == 42


def test_session_token_tampered(auth_cfg, db_path):
    from backend.auth import issue_session_token, decode_session_token
    tok = issue_session_token(42, auth_cfg)
    assert decode_session_token(tok + "x", auth_cfg) is None


def test_session_token_wrong_key(auth_cfg, db_path, monkeypatch):
    from backend.auth import issue_session_token, decode_session_token
    tok = issue_session_token(42, auth_cfg)
    monkeypatch.setenv("AUTH_SECRET_KEY", "a-different-secret-key-at-least-32b-long-xxxx")
    assert decode_session_token(tok, auth_cfg) is None


# ── Password hashing ────────────────────────────────────────────────────

def test_password_hash_roundtrip(monkeypatch):
    monkeypatch.setenv("BCRYPT_ROUNDS", "4")
    from backend.passwords import hash_password, verify_password
    h = hash_password("correct horse battery staple")
    assert h.startswith("$2") and len(h) > 50
    assert verify_password("correct horse battery staple", h) is True
    assert verify_password("wrong", h) is False


def test_password_hash_rejects_empty(monkeypatch):
    monkeypatch.setenv("BCRYPT_ROUNDS", "4")
    from backend.passwords import hash_password, verify_password
    with pytest.raises(ValueError):
        hash_password("")
    assert verify_password("anything", "") is False
    assert verify_password("", "$2b$04$xxxxxxxxxx") is False


def test_password_hash_survives_malformed():
    from backend.passwords import verify_password
    assert verify_password("foo", "not-a-bcrypt-hash") is False


# ── authenticate() ──────────────────────────────────────────────────────

def test_authenticate_happy_path(db_path):
    from backend import db as db_mod
    from backend import passwords as pw_mod
    from backend.auth import authenticate

    run(db_mod.init_db())
    run(db_mod.create_user(
        username="alice", email="alice@example.com",
        password_hash=pw_mod.hash_password("s3cret-p4ss"),
    ))
    user = run(authenticate("alice", "s3cret-p4ss"))
    assert user is not None
    assert user["username"] == "alice"


def test_authenticate_wrong_password_returns_none(db_path):
    from backend import db as db_mod
    from backend import passwords as pw_mod
    from backend.auth import authenticate

    run(db_mod.init_db())
    run(db_mod.create_user(
        username="bob", email="bob@example.com",
        password_hash=pw_mod.hash_password("correct"),
    ))
    assert run(authenticate("bob", "wrong")) is None


def test_authenticate_unknown_user_returns_none(db_path):
    from backend import db as db_mod
    from backend.auth import authenticate
    run(db_mod.init_db())
    assert run(authenticate("nobody", "whatever")) is None


def test_authenticate_timing_floor(db_path):
    """Both unknown-user and wrong-password paths wait at least
    _MIN_VERIFY_SECONDS. This test doesn't assert exact timing (CI is
    noisy) — it just confirms authenticate() is slower than a simple
    DB lookup."""
    import time
    from backend import db as db_mod
    from backend.auth import authenticate

    run(db_mod.init_db())
    t0 = time.perf_counter()
    run(authenticate("nobody", "whatever"))
    elapsed = time.perf_counter() - t0
    # 0.25s floor is the constant in auth.py; allow margin for CI.
    assert elapsed >= 0.2
