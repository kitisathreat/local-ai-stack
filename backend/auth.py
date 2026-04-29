"""Username + password authentication.

Flow:
    1. Admin creates a user (username, email, password) via the Qt admin
       window or the `backend.seed_admin` CLI. Passwords are bcrypt-hashed
       via `backend.passwords`.
    2. User POSTs credentials to `/auth/login`; on success we set a
       signed JWT session cookie, same machinery magic-link used.
    3. Every subsequent request carries the cookie; `current_user` /
       `optional_user` FastAPI dependencies decode it and fetch the
       user row from SQLite.

Secrets from env:
    AUTH_SECRET_KEY        — JWT signing key (32+ bytes urlsafe base64)
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

from fastapi import Cookie, HTTPException, Request
from jose import JWTError, jwt

from . import db, passwords
from .config import AuthConfig


logger = logging.getLogger(__name__)


# ── JWT session cookies ─────────────────────────────────────────────────

def _secret_key() -> str:
    key = os.getenv("AUTH_SECRET_KEY")
    if not key:
        raise RuntimeError(
            "AUTH_SECRET_KEY env var is not set. Generate with: "
            "python -c 'import secrets; print(secrets.token_urlsafe(48))'"
        )
    return key


def issue_session_token(user_id: int, cfg: AuthConfig) -> str:
    now = int(time.time())
    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + cfg.session.cookie_ttl_days * 86400,
    }
    return jwt.encode(payload, _secret_key(), algorithm=cfg.session.jwt_algorithm)


def decode_session_token(token: str, cfg: AuthConfig) -> int | None:
    try:
        payload = jwt.decode(token, _secret_key(), algorithms=[cfg.session.jwt_algorithm])
    except JWTError:
        return None
    sub = payload.get("sub")
    if not sub:
        return None
    try:
        return int(sub)
    except ValueError:
        return None


# ── FastAPI dependencies ────────────────────────────────────────────────

async def current_user(
    request: Request,
    lai_session: Optional[str] = Cookie(None, alias="lai_session"),
) -> dict:
    """Require a valid session. Raises 401 otherwise."""
    cfg: AuthConfig = request.app.state.app_config.auth
    if not lai_session:
        raise HTTPException(401, "Not authenticated")
    user_id = decode_session_token(lai_session, cfg)
    if user_id is None:
        raise HTTPException(401, "Invalid or expired session")
    user = await db.get_user(user_id)
    if not user:
        raise HTTPException(401, "User not found")
    return user


async def optional_user(
    request: Request,
    lai_session: Optional[str] = Cookie(None, alias="lai_session"),
) -> dict | None:
    if not lai_session:
        return None
    cfg: AuthConfig = request.app.state.app_config.auth
    user_id = decode_session_token(lai_session, cfg)
    if user_id is None:
        return None
    return await db.get_user(user_id)


# ── Password login ──────────────────────────────────────────────────────

# Constant-time floor so brute-force timing can't distinguish
# "user not found" from "wrong password".
_MIN_VERIFY_SECONDS = 0.25


async def authenticate(username: str, password: str) -> dict | None:
    """Return the user dict on success, None on any failure.

    Always runs a bcrypt verify (even for unknown usernames) so the
    observable timing is flat.
    """
    start = time.perf_counter()
    user = await db.get_user_by_username(username)
    if user:
        ok = passwords.verify_password(password, user.get("password_hash") or "")
    else:
        # Waste the same amount of CPU so timing doesn't leak existence.
        passwords.verify_password(password, "$2b$12$" + "x" * 53)
        ok = False
    elapsed = time.perf_counter() - start
    if elapsed < _MIN_VERIFY_SECONDS:
        await asyncio.sleep(_MIN_VERIFY_SECONDS - elapsed)
    if not ok or not user:
        return None
    await db.mark_login(user["id"])
    return user
