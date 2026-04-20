"""Shared HTTP session helpers for backend clients.

The multi-host dispatch layer constructs one client per enabled host; each
client needs consistent auth / TLS / timeout behaviour. These helpers
centralise that so `OllamaClient`, `LlamaCppClient`, and the new generic
`OpenAIClient` don't each reinvent header injection.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx


logger = logging.getLogger(__name__)


def resolve_bearer_token(auth_env: str | None) -> str | None:
    """Read a bearer token from the named env var. Returns None if unset or
    blank. Emits a warning when the env var is declared but empty — a common
    misconfiguration when rotating credentials."""
    if not auth_env:
        return None
    val = os.getenv(auth_env, "")
    val = val.strip()
    if not val:
        logger.warning(
            "Host declares auth_env=%r but the env var is empty — sending no Authorization header",
            auth_env,
        )
        return None
    return val


def build_session_kwargs(
    *,
    auth_token: str | None = None,
    verify_tls: bool = True,
    connect_timeout_sec: float = 10.0,
    request_timeout_sec: float = 300.0,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Return kwargs suitable for `httpx.AsyncClient(**kwargs)`.

    Passing these through `AsyncClient` up-front lets us keep the existing
    `async with httpx.AsyncClient(...)` call sites in OllamaClient /
    LlamaCppClient intact — we just expand the kwargs dict.
    """
    headers: dict[str, str] = {}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    if extra_headers:
        headers.update(extra_headers)
    return {
        "timeout": httpx.Timeout(request_timeout_sec, connect=connect_timeout_sec),
        "verify": verify_tls,
        "headers": headers or None,
    }
