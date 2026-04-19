"""Unit tests for backend/auth.py — email validation, token issue/decode,
rate limits. SMTP send is NOT tested live (stub when no SMTP env)."""

import asyncio
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    p = tmp_path / "auth_test.db"
    monkeypatch.setenv("LAI_DB_PATH", str(p))
    monkeypatch.setenv("AUTH_SECRET_KEY", "test-secret-key-at-least-32-bytes-long-xxxx")
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


# ── Email validation ────────────────────────────────────────────────────

def test_valid_email_accepts_basic(auth_cfg):
    from backend.auth import valid_email
    assert valid_email("alice@example.com", auth_cfg) is True


@pytest.mark.parametrize("bad", ["", "no-at-sign", "a@", "@b.io", "a b@c.io"])
def test_valid_email_rejects_malformed(bad, auth_cfg):
    from backend.auth import valid_email
    assert valid_email(bad, auth_cfg) is False


def test_domain_restriction(auth_cfg):
    from backend.auth import valid_email
    restricted = auth_cfg.model_copy(update={"allowed_email_domains": ["mydomain.tld"]})
    assert valid_email("a@mydomain.tld", restricted) is True
    assert valid_email("a@other.io", restricted) is False


# ── Session tokens ───────────────────────────────────────────────────────

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


# ── SMTP stub ───────────────────────────────────────────────────────────

def test_send_magic_email_no_smtp_env_logs_and_returns(auth_cfg, caplog, monkeypatch):
    """With SMTP env unset, send_magic_email should log and not raise."""
    for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS"):
        monkeypatch.delenv(k, raising=False)
    from backend.auth import send_magic_email
    import logging
    caplog.set_level(logging.INFO)
    run(send_magic_email("a@x.io", "http://link/verify?token=xxx", auth_cfg))
    assert any("SMTP not configured" in r.message or "would have emailed" in r.message
               for r in caplog.records)


# ── Rate limit ──────────────────────────────────────────────────────────

def test_rate_limit_triggers(auth_cfg, db_path):
    from backend import db as db_mod
    from backend.auth import check_rate_limits
    from fastapi import HTTPException

    run(db_mod.init_db())
    email = "rl@x.io"
    for _ in range(auth_cfg.rate_limits.requests_per_hour_per_email):
        run(db_mod.create_magic_link(email, 60, None))

    # Next one should hit the limit.
    with pytest.raises(HTTPException) as excinfo:
        run(check_rate_limits(email, auth_cfg))
    assert excinfo.value.status_code == 429


def test_rate_limit_allows_below_threshold(auth_cfg, db_path):
    from backend import db as db_mod
    from backend.auth import check_rate_limits

    run(db_mod.init_db())
    run(db_mod.create_magic_link("ok@x.io", 60, None))
    # Should not raise.
    run(check_rate_limits("ok@x.io", auth_cfg))
