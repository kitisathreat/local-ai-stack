"""Host-gating middleware.

Chat endpoints are only reachable via the configured chat subdomain
(default: ``chat.mylensandi.com``) unless airgap mode is on, in which
case only loopback hosts are allowed.

Admin + health + auth paths stay open on both loopback and subdomain so
the local Qt admin window, health probes, and cloudflared's origin
check all keep working.

Registered in ``backend/main.py`` BEFORE CORSMiddleware so rejections
short-circuit before preflight.
"""
from __future__ import annotations

import os
from typing import Iterable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.types import ASGIApp


# Path prefixes that are chat-scoped (gated by CHAT_HOSTNAME in normal
# mode, loopback-only in airgap mode).
_CHAT_PREFIXES: tuple[str, ...] = (
    "/v1/chat/completions",
    "/api/chats",
    "/api/rag",
    "/api/memory",
)

# Path prefixes that are always allowed from localhost + from the chat
# subdomain (needed by Qt windows + cloudflared origin).
_ALWAYS_ALLOWED_PREFIXES: tuple[str, ...] = (
    "/healthz",
    "/v1/models",
    "/admin/",
    "/auth/",
    "/api/airgap",
    "/static/",
    "/resolved-models",
    "/vram",
    "/tools",
    "/docs",
    "/openapi.json",
)


def _split_csv(val: str | None) -> frozenset[str]:
    return frozenset(
        part.strip().lower()
        for part in (val or "").split(",")
        if part.strip()
    )


class HostGateMiddleware(BaseHTTPMiddleware):
    """Deny chat paths from hosts other than CHAT_HOSTNAME.

    Configured via environment:
        CHAT_HOSTNAME               default "chat.mylensandi.com"
        ADMIN_API_ALLOWED_HOSTS     default "127.0.0.1,localhost"
    """

    def __init__(self, app: ASGIApp):
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        host = (request.headers.get("host") or "").split(":", 1)[0].lower()
        path = request.url.path

        chat_host = os.getenv("CHAT_HOSTNAME", "chat.mylensandi.com").lower()
        local_hosts = _split_csv(os.getenv("ADMIN_API_ALLOWED_HOSTS", "127.0.0.1,localhost"))

        # Import here — airgap state is hot-swappable.
        from .. import airgap
        airgap_on = airgap.is_enabled()

        if self._is_always_allowed(path):
            return await call_next(request)

        is_chat_path = path == "/" or any(path.startswith(p) for p in _CHAT_PREFIXES)

        if airgap_on:
            # Everything (chat + root) is locked to loopback.
            if host in local_hosts:
                return await call_next(request)
            return self._deny("Airgap mode: remote access disabled.")

        # Normal mode: chat paths are subdomain-only.
        if is_chat_path:
            if host == chat_host:
                return await call_next(request)
            # Permit loopback for local debugging of the API only, but NOT
            # for the root chat page (keeps the Qt window honest).
            if host in local_hosts and path != "/":
                return await call_next(request)
            return self._deny(
                f"Chat is only reachable at https://{chat_host}."
            )

        # Non-chat path + non-chat host → allow from loopback; reject from
        # unknown hosts so the subdomain can't discover admin paths.
        if host in local_hosts or host == chat_host:
            return await call_next(request)
        return self._deny("Host not allowed.")

    @staticmethod
    def _is_always_allowed(path: str) -> bool:
        return any(path == p or path.startswith(p) for p in _ALWAYS_ALLOWED_PREFIXES)

    @staticmethod
    def _deny(msg: str) -> Response:
        return PlainTextResponse(msg, status_code=403)
