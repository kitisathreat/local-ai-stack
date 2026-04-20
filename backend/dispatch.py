"""Tier→client dispatcher with circuit-breaker and legacy fallback.

The dispatcher is the single place that decides *which* backend client
services a given tier's request. It scaffolds multi-host support on top of
the two always-on legacy clients (`state.ollama` / `state.llama_cpp` built
from `OLLAMA_URL` / `LLAMACPP_URL`) without ever removing them:

    1. Try the tier's primary host (`tier.host`).
    2. Try each name in `tier.host_fallbacks` in order.
    3. If nothing healthy is left and `tier.allow_legacy_fallback` is true
       and `failover.legacy_fallback_enabled` is true, fall back to the
       legacy client whose kind matches the tier's backend.

A host is considered unhealthy when its circuit breaker is open. The breaker
opens after `failover.open_after` consecutive failures (ConnectError, 5xx,
timeout, or explicit `record_failure`) and half-opens after
`failover.half_open_probe_sec` so recovered hosts come back automatically.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

import httpx

from .backends import BackendClient
from .config import AppConfig, HostConfig, TierConfig


logger = logging.getLogger(__name__)


LEGACY_OLLAMA = "__legacy_ollama__"
LEGACY_LLAMA_CPP = "__legacy_llama_cpp__"


class AllHostsUnavailable(RuntimeError):
    """Raised when every candidate host (including legacy) is unhealthy."""


@dataclass
class HostHealth:
    """Circuit-breaker state for a single host."""

    consecutive_failures: int = 0
    opened_at: float = 0.0          # 0 = closed
    last_probe_ok_at: float = 0.0
    last_error: str = ""

    @property
    def is_open(self) -> bool:
        return self.opened_at > 0.0


@dataclass
class DispatchChoice:
    """Outcome of `client_for_tier` — client + which host was picked."""

    client: BackendClient
    host_name: str
    host: HostConfig


class TierDispatcher:
    def __init__(
        self,
        config: AppConfig,
        clients: dict[str, BackendClient],
    ):
        self.cfg = config
        self.clients = clients                   # host_name → client
        self.health: dict[str, HostHealth] = {
            name: HostHealth() for name in clients
        }
        self._probe_locks: dict[str, asyncio.Lock] = {
            name: asyncio.Lock() for name in clients
        }

    # ── Breaker state mutations ─────────────────────────────────────────

    def record_success(self, host_name: str) -> None:
        h = self.health.get(host_name)
        if h is None:
            return
        was_open = h.is_open
        h.consecutive_failures = 0
        h.opened_at = 0.0
        h.last_probe_ok_at = time.monotonic()
        h.last_error = ""
        if was_open:
            logger.info("host %s: circuit CLOSED (recovered)", host_name)

    def record_failure(self, host_name: str, err: BaseException | str) -> None:
        h = self.health.get(host_name)
        if h is None:
            return
        h.consecutive_failures += 1
        h.last_error = str(err)
        if not h.is_open and h.consecutive_failures >= self.cfg.hosts.failover.open_after:
            h.opened_at = time.monotonic()
            logger.warning(
                "host %s: circuit OPEN after %d consecutive failures (last: %s)",
                host_name, h.consecutive_failures, h.last_error,
            )

    def _half_open_ready(self, h: HostHealth) -> bool:
        return (
            h.is_open
            and (time.monotonic() - h.opened_at) >= self.cfg.hosts.failover.half_open_probe_sec
        )

    # ── Candidate ordering ──────────────────────────────────────────────

    def candidates_for(self, tier: TierConfig) -> list[str]:
        """Return the ordered list of host names to try for this tier.

        Disabled hosts are filtered out. Legacy hosts are appended last when
        permitted.
        """
        order: list[str] = []
        seen: set[str] = set()
        hosts_cfg = self.cfg.hosts.hosts

        def _push(name: str) -> None:
            if name in seen:
                return
            cfg = hosts_cfg.get(name)
            if cfg is None or not cfg.enabled:
                return
            if name not in self.clients:
                return
            order.append(name)
            seen.add(name)

        if tier.host:
            _push(tier.host)
        for fb in tier.host_fallbacks:
            _push(fb)

        # Legacy floor — matched by backend kind.
        if (
            tier.allow_legacy_fallback
            and self.cfg.hosts.failover.legacy_fallback_enabled
        ):
            legacy = LEGACY_OLLAMA if tier.backend == "ollama" else LEGACY_LLAMA_CPP
            _push(legacy)

        return order

    def client_for_tier(self, tier: TierConfig) -> DispatchChoice:
        """Pick the first candidate whose circuit is closed (or half-open
        ready). Does NOT make any network calls — the caller invokes the
        client, and on failure calls `record_failure(choice.host_name, ...)`
        before calling this method again to get the next candidate."""
        cands = self.candidates_for(tier)
        if not cands:
            raise AllHostsUnavailable(
                f"No candidate hosts for tier {tier.name!r} "
                f"(host={tier.host!r}, fallbacks={tier.host_fallbacks!r}, "
                f"legacy_allowed={tier.allow_legacy_fallback})"
            )

        for name in cands:
            h = self.health[name]
            if not h.is_open or self._half_open_ready(h):
                return DispatchChoice(
                    client=self.clients[name],
                    host_name=name,
                    host=self.cfg.hosts.hosts[name],
                )

        # Every circuit is open. Pick the one with the oldest `opened_at`
        # so we still make forward progress — half-open-on-demand rather
        # than refusing outright.
        name = min(cands, key=lambda n: self.health[n].opened_at or 0.0)
        logger.warning(
            "tier %s: all %d candidate hosts open; forcing half-open probe on %s",
            tier.name, len(cands), name,
        )
        return DispatchChoice(
            client=self.clients[name],
            host_name=name,
            host=self.cfg.hosts.hosts[name],
        )

    async def execute(
        self,
        tier: TierConfig,
        fn: Callable[[BackendClient], Awaitable[object]],
    ):
        """Run `fn(client)` with retry-on-next-host semantics.

        On transient errors (httpx.ConnectError / ReadTimeout / HTTP 5xx),
        marks the host as failed and retries with the next candidate. Raises
        `AllHostsUnavailable` only after every candidate has been tried.

        Streaming callers should NOT use this wrapper — they need to manage
        the stream lifecycle themselves and can only retry before the first
        token is emitted. See `main.py` chat stream path for the streaming
        failover pattern.
        """
        cands = self.candidates_for(tier)
        if not cands:
            raise AllHostsUnavailable(f"No candidate hosts for tier {tier.name!r}")

        last_exc: Optional[BaseException] = None
        tried: list[str] = []
        for name in cands:
            h = self.health[name]
            if h.is_open and not self._half_open_ready(h):
                continue
            tried.append(name)
            client = self.clients[name]
            try:
                result = await fn(client)
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout,
                    httpx.PoolTimeout, httpx.RemoteProtocolError) as e:
                self.record_failure(name, e)
                last_exc = e
                continue
            except httpx.HTTPStatusError as e:
                if 500 <= e.response.status_code < 600:
                    self.record_failure(name, e)
                    last_exc = e
                    continue
                # 4xx is a client problem; don't rotate hosts.
                raise
            else:
                self.record_success(name)
                return result

        raise AllHostsUnavailable(
            f"All {len(tried)} candidate hosts failed for tier {tier.name!r}: "
            f"{tried}"
        ) from last_exc
