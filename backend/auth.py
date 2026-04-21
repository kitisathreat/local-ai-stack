"""Magic-link authentication.

Flow:
    1. User POSTs email → we issue a random token, store it with expiry,
       email them a link with ?token=<random>.
    2. User clicks link → GET /auth/verify?token=... → we consume the
       token, upsert the user, set a signed JWT session cookie, redirect.
    3. Every subsequent request carries the cookie; a FastAPI dependency
       decodes it and injects a user_id.

Secrets from env:
    AUTH_SECRET_KEY        — JWT signing key (32+ bytes urlsafe base64)
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_STARTTLS

If SMTP env is unset, magic links are printed to the backend log instead
of emailed — useful for local development without an SMTP server.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Optional

import aiosmtplib
from email.message import EmailMessage
from fastapi import Cookie, HTTPException, Request
from jose import JWTError, jwt

from . import db
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


# ── Email validation ────────────────────────────────────────────────────

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def valid_email(email: str, cfg: AuthConfig) -> bool:
    if not _EMAIL_RE.match(email):
        return False
    if cfg.allowed_email_domains:
        domain = email.split("@", 1)[1].lower()
        if domain not in [d.lower() for d in cfg.allowed_email_domains]:
            return False
    return True


# ── SMTP sender ─────────────────────────────────────────────────────────

async def send_magic_email(
    to_email: str, verify_url: str, cfg: AuthConfig,
) -> None:
    """Send the magic-link email, or log it if SMTP isn't configured.

    Configured means all of SMTP_HOST, SMTP_USER, SMTP_PASS set.
    """
    smtp_host = os.getenv("SMTP_HOST")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER")
    smtp_pass = os.getenv("SMTP_PASS")
    starttls = os.getenv("SMTP_STARTTLS", "true").lower() != "false"
    email_from = os.getenv("AUTH_EMAIL_FROM") or cfg.magic_link.email_from

    body = cfg.magic_link.email_body_template.format(
        expiry_minutes=cfg.magic_link.expiry_minutes,
        verify_url=verify_url,
    ) if cfg.magic_link.email_body_template else (
        f"Click to sign in (expires in {cfg.magic_link.expiry_minutes}m):\n\n{verify_url}\n"
    )

    if not (smtp_host and smtp_user and smtp_pass):
        # Do NOT log the full verify_url by default: it contains a live magic-link
        # token that would grant sign-in to anyone with log access. Opt in with
        # AUTH_DEV_LINK_LOG=1 for local dev; otherwise log a redacted form.
        if os.getenv("AUTH_DEV_LINK_LOG", "").lower() in {"1", "true", "yes"}:
            logger.info(
                "SMTP not configured — would have emailed %s: %s", to_email, verify_url
            )
        else:
            logger.info(
                "SMTP not configured — would have emailed %s (token redacted; set AUTH_DEV_LINK_LOG=1 to log the URL)",
                to_email,
            )
        return

    msg = EmailMessage()
    msg["From"] = email_from
    msg["To"] = to_email
    msg["Subject"] = cfg.magic_link.email_subject
    msg.set_content(body)

    await aiosmtplib.send(
        msg,
        hostname=smtp_host,
        port=smtp_port,
        username=smtp_user,
        password=smtp_pass,
        start_tls=starttls,
    )


# ── Rate-limit gate ─────────────────────────────────────────────────────

async def check_rate_limits(
    email: str, cfg: AuthConfig, client_ip: str | None = None,
) -> None:
    """Raise HTTPException 429 if too many magic-link requests have been
    made recently for this email **or** originating client IP.

    We return the same generic 429 regardless of which bucket tripped so
    attackers can't use it to enumerate known emails vs. rate-limited IPs
    (#71).
    """
    n_email = await db.count_recent_magic_links_for_email(email, window_seconds=3600)
    n_ip = 0
    if client_ip and cfg.rate_limits.requests_per_hour_per_ip > 0:
        n_ip = await db.count_recent_magic_links_for_ip(client_ip, window_seconds=3600)
    if (
        n_email >= cfg.rate_limits.requests_per_hour_per_email
        or n_ip >= cfg.rate_limits.requests_per_hour_per_ip
    ):
        raise HTTPException(
            429,
            "Too many sign-in requests. Try again in an hour.",
        )
