"""Airgap mode — runtime-togglable offline-only state.

When the admin turns airgap ON, the backend must:
  - Refuse any outbound calls to services not in the local stack (web
    search, external tool APIs, etc.). `is_enabled()` is consulted on the
    hot path by middleware and tool gates.
  - Route conversation + memory persistence to separate encrypted stores
    so airgap content never mixes with normal content on disk.
  - Keep the local GUI fully functional against the in-process models
    (per-tier llama-server subprocesses on loopback), which are part of
    the local stack and therefore not "external" by this rule.

The flag is persisted to $LAI_AIRGAP_STATE (default /app/data/airgap.state)
as a tiny JSON blob so it survives restarts. The in-process state is
mirrored on a module-level singleton for cheap hot-path lookups.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


_DEFAULT_STATE_PATH = "/app/data/airgap.state"


def _path() -> Path:
    return Path(os.getenv("LAI_AIRGAP_STATE", _DEFAULT_STATE_PATH))


def _load_state() -> dict[str, Any]:
    p = _path()
    if not p.exists():
        return {"enabled": False, "changed_at": 0.0, "changed_by": None}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("state file is not an object")
        return {
            "enabled": bool(data.get("enabled", False)),
            "changed_at": float(data.get("changed_at") or 0.0),
            "changed_by": data.get("changed_by"),
        }
    except Exception:
        logger.exception("Failed to read airgap state file %s — defaulting to OFF", p)
        return {"enabled": False, "changed_at": 0.0, "changed_by": None}


def _save_state(state: dict[str, Any]) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


class AirgapState:
    """Process-local cache of the airgap flag with async write lock.

    `set()` atomically updates both the in-memory cache and the on-disk
    state file. Single-worker deployments are fine; a multi-worker
    deployment would need Redis pub/sub to propagate toggles to sibling
    workers — a TODO noted in the admin UI hint.
    """

    def __init__(self) -> None:
        s = _load_state()
        self.enabled: bool = bool(s["enabled"])
        self.changed_at: float = float(s["changed_at"])
        self.changed_by: str | None = s.get("changed_by")
        self._lock = asyncio.Lock()

    async def set(self, enabled: bool, actor_email: str | None) -> dict:
        async with self._lock:
            self.enabled = bool(enabled)
            self.changed_at = time.time()
            self.changed_by = actor_email
            state = self.snapshot()
            _save_state(state)
            logger.warning(
                "Airgap mode %s (actor=%s)",
                "ENABLED" if enabled else "DISABLED",
                actor_email or "(unknown)",
            )
            return state

    def snapshot(self) -> dict:
        return {
            "enabled": self.enabled,
            "changed_at": self.changed_at,
            "changed_by": self.changed_by,
        }


_current: AirgapState | None = None


def set_current(state: AirgapState) -> None:
    """Install the module-level singleton. Called once at app startup."""
    global _current
    _current = state


def current() -> AirgapState | None:
    return _current


def is_enabled() -> bool:
    """Fast check for the current airgap state. Returns False before the
    lifespan hook has initialised the singleton so tests that import
    submodules without starting the app don't trip."""
    return bool(_current and _current.enabled)
