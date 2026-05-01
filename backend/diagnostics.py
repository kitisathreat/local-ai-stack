"""
Self-diagnostic suite for the Local AI Stack backend.

Startup usage (hidden from users — results go to the application log only):
    from backend.diagnostics import run_startup_diagnostics
    await run_startup_diagnostics(cfg=cfg, registry=tools, db_path=...)

CI usage:
    Individual check functions are importable and await-able.
    See tests/test_diagnostics.py for the full suite.

Severity levels:
    OK   — check passed
    WARN — degraded / optional dependency missing; service continues
    FAIL — hard misconfiguration; operator action required
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import time
import urllib.parse
from dataclasses import dataclass
from enum import Enum
from typing import Any

log = logging.getLogger("lai.diagnostics")


# ── Result types ──────────────────────────────────────────────────────────────

class Severity(str, Enum):
    OK   = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True)
class CheckResult:
    name: str
    severity: Severity
    message: str
    detail: str = ""

    def __bool__(self) -> bool:
        return self.severity != Severity.FAIL


def _ok(name: str, msg: str) -> CheckResult:
    return CheckResult(name, Severity.OK, msg)


def _warn(name: str, msg: str, detail: str = "") -> CheckResult:
    return CheckResult(name, Severity.WARN, msg, detail)


def _fail(name: str, msg: str, detail: str = "") -> CheckResult:
    return CheckResult(name, Severity.FAIL, msg, detail)


# ── Environment / secrets ─────────────────────────────────────────────────────

def check_env_auth_secret() -> CheckResult:
    name = "env.AUTH_SECRET_KEY"
    val = os.environ.get("AUTH_SECRET_KEY", "")
    if not val:
        return _fail(name, "AUTH_SECRET_KEY is not set")
    try:
        raw = base64.urlsafe_b64decode(val + "==")
    except Exception as exc:
        return _fail(name, "AUTH_SECRET_KEY is not valid URL-safe base64", str(exc))
    if len(raw) < 32:
        return _fail(name, f"AUTH_SECRET_KEY too short ({len(raw)} bytes; need ≥32)")
    return _ok(name, f"AUTH_SECRET_KEY present and valid ({len(raw)} bytes)")


def check_env_history_secret() -> CheckResult:
    name = "env.HISTORY_SECRET_KEY"
    val = os.environ.get("HISTORY_SECRET_KEY", "")
    if not val:
        return _warn(name, "HISTORY_SECRET_KEY not set — per-user history encryption disabled")
    try:
        raw = base64.urlsafe_b64decode(val + "==")
    except Exception as exc:
        return _fail(name, "HISTORY_SECRET_KEY is not valid URL-safe base64", str(exc))
    if len(raw) < 32:
        return _fail(name, f"HISTORY_SECRET_KEY too short ({len(raw)} bytes; need ≥32)")
    return _ok(name, f"HISTORY_SECRET_KEY present and valid ({len(raw)} bytes)")


_JUPYTER_KNOWN_BAD = {"my-secret-token", "secret", "token", "jupyter", "password", "changeme", ""}


def check_env_jupyter_token() -> CheckResult:
    name = "env.JUPYTER_TOKEN"
    val = os.environ.get("JUPYTER_TOKEN", "")
    if val.lower() in _JUPYTER_KNOWN_BAD:
        return _fail(
            name,
            "JUPYTER_TOKEN is a known-insecure default or empty — set a strong random value in .env",
            f"current value: {val!r}",
        )
    if len(val) < 16:
        return _warn(name, f"JUPYTER_TOKEN is short ({len(val)} chars; recommend ≥32)")
    return _ok(name, "JUPYTER_TOKEN is set to a non-default value")


def check_env_public_base_url() -> CheckResult:
    name = "env.PUBLIC_BASE_URL"
    val = os.environ.get("PUBLIC_BASE_URL", "")
    if not val:
        return _warn(name, "PUBLIC_BASE_URL is not set — magic-link redirects may break")
    parsed = urllib.parse.urlparse(val)
    if parsed.scheme not in ("http", "https"):
        return _fail(name, f"PUBLIC_BASE_URL has unsupported scheme {parsed.scheme!r}", f"value: {val}")
    if not parsed.netloc:
        return _fail(name, f"PUBLIC_BASE_URL has no host", f"value: {val}")
    return _ok(name, f"PUBLIC_BASE_URL is a valid absolute URL ({val})")


def check_env_cookie_secure() -> CheckResult:
    name = "env.cookie_secure_consistency"
    base_url = os.environ.get("PUBLIC_BASE_URL", "")
    cookie_secure = os.environ.get("COOKIE_SECURE", "false").lower() in ("1", "true", "yes")
    if cookie_secure and base_url.startswith("http://"):
        return _fail(
            name,
            "COOKIE_SECURE=true but PUBLIC_BASE_URL is http:// — secure cookies will be silently dropped",
            "Fix: set PUBLIC_BASE_URL to https:// or set COOKIE_SECURE=false",
        )
    if not cookie_secure and base_url.startswith("https://"):
        return _warn(
            name,
            "PUBLIC_BASE_URL is https:// but COOKIE_SECURE is not enabled — sessions vulnerable over HTTP",
            "Fix: set COOKIE_SECURE=true",
        )
    return _ok(name, "COOKIE_SECURE and PUBLIC_BASE_URL are consistent")


# ── CORS ──────────────────────────────────────────────────────────────────────

def check_cors_config(
    origins: list[str] | None = None,
    allow_credentials: bool | None = None,
) -> CheckResult:
    name = "security.cors"
    if origins is None:
        raw = os.environ.get("ALLOWED_ORIGINS", "*")
        origins = [o.strip() for o in raw.split(",") if o.strip()]
    if allow_credentials is None:
        # Mirror backend/main.py's auto-disable: when origins is exactly
        # `["*"]`, the runtime overrides allow_credentials to False
        # regardless of CORS_ALLOW_CREDENTIALS, because the combination
        # is rejected by all modern browsers anyway. The diagnostic must
        # reflect that decision or it raises a hard FAIL on a config
        # the runtime is silently fixing.
        env_pref = os.environ.get("CORS_ALLOW_CREDENTIALS", "true").lower() in (
            "1", "true", "yes"
        )
        allow_credentials = env_pref and origins != ["*"]
    if "*" in origins and allow_credentials:
        return _fail(
            name,
            "CORS wildcard origin ('*') with credentials=True is rejected by all modern browsers",
            "Fix: set ALLOWED_ORIGINS to your explicit origin(s) or disable credentials",
        )
    if not origins:
        return _warn(name, "ALLOWED_ORIGINS is empty — all CORS requests will be blocked")
    if origins == ["*"] and not allow_credentials:
        return _ok(
            name,
            "CORS wildcard origin with credentials disabled — browser-valid",
        )
    return _ok(name, f"CORS configured with {len(origins)} explicit origin(s)")


# ── Crypto ────────────────────────────────────────────────────────────────────

def check_jwt_roundtrip() -> CheckResult:
    name = "crypto.jwt_roundtrip"
    secret = os.environ.get("AUTH_SECRET_KEY", "")
    if not secret:
        return _fail(name, "AUTH_SECRET_KEY not set — cannot perform JWT roundtrip test")
    try:
        from jose import jwt as jose_jwt

        payload = {"sub": "__diag__", "exp": int(time.time()) + 60}
        token = jose_jwt.encode(payload, secret, algorithm="HS256")
        decoded = jose_jwt.decode(token, secret, algorithms=["HS256"])
        if decoded.get("sub") != "__diag__":
            return _fail(name, f"JWT roundtrip subject mismatch: got {decoded.get('sub')!r}")
        return _ok(name, "JWT sign → decode roundtrip succeeded")
    except Exception as exc:
        return _fail(name, "JWT roundtrip failed", str(exc))


def check_history_encryption_roundtrip() -> CheckResult:
    name = "crypto.history_encryption_roundtrip"
    secret = os.environ.get("HISTORY_SECRET_KEY", "")
    if not secret:
        return _warn(name, "HISTORY_SECRET_KEY not set — skipping AES-GCM roundtrip test")
    try:
        import secrets as _sec

        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF

        raw_key = base64.urlsafe_b64decode(secret + "==")
        salt = _sec.token_bytes(16)
        derived = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=salt, info=b"lai-hist-v1:diag"
        ).derive(raw_key)
        nonce = _sec.token_bytes(12)
        plaintext = b"diagnostic-canary-payload"
        ciphertext = AESGCM(derived).encrypt(nonce, plaintext, None)
        recovered = AESGCM(derived).decrypt(nonce, ciphertext, None)
        if recovered != plaintext:
            return _fail(name, "AES-GCM decrypt returned wrong plaintext")
        return _ok(name, "AES-256-GCM encrypt → decrypt roundtrip succeeded")
    except Exception as exc:
        return _fail(name, "History encryption roundtrip failed", str(exc))


# ── Database ──────────────────────────────────────────────────────────────────

_EXPECTED_TABLES = frozenset({
    "users", "conversations",
    "messages", "memories", "rag_docs", "usage_events",
})


async def check_db_connectivity(db_path: str) -> CheckResult:
    name = "db.connectivity"
    if not db_path:
        return _warn(name, "db_path not provided — skipping connectivity check")
    try:
        import aiosqlite
        async with aiosqlite.connect(db_path) as conn:
            await conn.execute("SELECT 1")
        return _ok(name, f"SQLite DB reachable at {db_path}")
    except Exception as exc:
        return _fail(name, f"Cannot connect to SQLite DB at {db_path}", str(exc))


async def check_db_schema(db_path: str) -> CheckResult:
    name = "db.schema"
    if not db_path:
        return _warn(name, "db_path not provided — skipping schema check")
    try:
        import aiosqlite
        async with aiosqlite.connect(db_path) as conn:
            cur = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            rows = await cur.fetchall()
        found = {row[0] for row in rows}
        missing = _EXPECTED_TABLES - found
        if missing:
            return _fail(name, f"Missing tables: {sorted(missing)}", f"found: {sorted(found)}")
        return _ok(name, f"All {len(_EXPECTED_TABLES)} expected tables present")
    except Exception as exc:
        return _fail(name, "Schema check failed", str(exc))


async def check_db_wal_mode(db_path: str) -> CheckResult:
    name = "db.wal_mode"
    if not db_path:
        return _warn(name, "db_path not provided — skipping WAL check")
    try:
        import aiosqlite
        async with aiosqlite.connect(db_path) as conn:
            cur = await conn.execute("PRAGMA journal_mode")
            row = await cur.fetchone()
        mode = row[0] if row else "unknown"
        if mode != "wal":
            return _warn(name, f"journal_mode is {mode!r}; expected 'wal' for concurrent access")
        return _ok(name, "journal_mode=WAL is active")
    except Exception as exc:
        return _fail(name, "WAL mode check failed", str(exc))


async def check_db_canary_write(db_path: str) -> CheckResult:
    name = "db.canary_write"
    if not db_path:
        return _warn(name, "db_path not provided — skipping canary write")
    try:
        import aiosqlite
        ts = str(time.monotonic())
        async with aiosqlite.connect(db_path) as conn:
            await conn.execute(
                "CREATE TEMP TABLE IF NOT EXISTS _diag_canary (k TEXT PRIMARY KEY, v TEXT)"
            )
            await conn.execute(
                "INSERT OR REPLACE INTO _diag_canary VALUES (?, ?)", ("startup", ts)
            )
            cur = await conn.execute("SELECT v FROM _diag_canary WHERE k = 'startup'")
            row = await cur.fetchone()
            await conn.execute("DROP TABLE IF EXISTS _diag_canary")
            await conn.commit()
        if not row or row[0] != ts:
            return _fail(name, "Canary read returned unexpected value", f"expected {ts!r}, got {row!r}")
        return _ok(name, "Canary write → read → delete cycle succeeded")
    except Exception as exc:
        return _fail(name, "Canary write failed", str(exc))


# ── Service connectivity ───────────────────────────────────────────────────────

async def _http_get(url: str, timeout: float = 5.0) -> tuple[int, str]:
    import httpx
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(url)
        return r.status_code, r.text[:300]


async def check_pinned_llamacpp_tiers(cfg: Any = None) -> list[CheckResult]:
    """Pre-spawned tiers (vision, embedding) MUST be reachable; chat tiers
    cold-spawn on first request and are not gated here."""
    if cfg is None:
        return [_warn("service.llamacpp", "Config not provided — skipping per-tier check")]
    out: list[CheckResult] = []
    for tier_name, tier in cfg.models.tiers.items():
        if not tier.pinned:
            continue
        base = tier.resolved_endpoint().rstrip("/")
        check_name = f"service.llamacpp.{tier_name}"
        try:
            status, _ = await _http_get(f"{base}/models")
            if status == 200:
                out.append(_ok(check_name, f"{tier_name} llama-server reachable at {base}"))
            else:
                out.append(_warn(check_name, f"{tier_name} returned HTTP {status}", f"URL: {base}/models"))
        except Exception as exc:
            out.append(_warn(
                check_name,
                f"{tier_name} llama-server not reachable at {base} — pre-spawn missing?",
                str(exc),
            ))
    return out


async def check_qdrant_reachable(url: str | None = None) -> CheckResult:
    name = "service.qdrant"
    base = (url or os.environ.get("QDRANT_URL", "http://localhost:6333")).rstrip("/")
    try:
        status, _ = await _http_get(f"{base}/healthz")
        if status == 200:
            return _ok(name, f"Qdrant reachable at {base}")
        return _warn(name, f"Qdrant returned HTTP {status}", f"URL: {base}/healthz")
    except Exception as exc:
        return _warn(name, f"Qdrant not reachable at {base} — RAG and memory will fail", str(exc))


async def check_redis_reachable(url: str | None = None) -> CheckResult:
    name = "service.redis"
    redis_url = url or os.environ.get("REDIS_URL", "")
    if not redis_url:
        return _ok(name, "Redis not configured — using in-memory rate limiting (single-replica only)")
    try:
        import redis.asyncio as aioredis
        client = aioredis.from_url(redis_url, socket_connect_timeout=3)
        pong = await asyncio.wait_for(client.ping(), timeout=4)
        await client.aclose()
        if pong:
            return _ok(name, f"Redis reachable at {redis_url}")
        return _warn(name, "Redis PING returned falsy response")
    except Exception as exc:
        return _warn(name, f"Redis not reachable at {redis_url} — rate limiting degraded", str(exc))


async def check_web_search_provider(provider: str | None = None) -> CheckResult:
    """Validates the configured web-search provider.

    Native mode has no SearXNG container. We pick one of:
      * brave → requires BRAVE_API_KEY
      * ddg   → pure-Python, no key required
      * none  → web-search tools disabled
    """
    name = "service.web_search"
    p = (provider or os.environ.get("WEB_SEARCH_PROVIDER") or "").strip().lower()
    if not p:
        p = "brave" if os.environ.get("BRAVE_API_KEY") else "ddg"
    if p == "none":
        return _ok(name, "Web search disabled (WEB_SEARCH_PROVIDER=none)")
    if p == "brave":
        if not os.environ.get("BRAVE_API_KEY"):
            return _warn(name, "Brave provider selected but BRAVE_API_KEY is empty")
        return _ok(name, "Brave web-search provider configured")
    if p == "ddg":
        try:
            import ddgs  # noqa: F401
        except ImportError as exc:
            return _warn(name, "DuckDuckGo provider selected but 'ddgs' package not installed", str(exc))
        return _ok(name, "DuckDuckGo web-search provider configured")
    return _warn(name, f"Unknown WEB_SEARCH_PROVIDER={p!r} — expected brave|ddg|none")


# ── Tool registry ─────────────────────────────────────────────────────────────

def check_tool_registry(registry: Any = None) -> list[CheckResult]:
    """Returns two CheckResults: registry non-empty, registry names unique."""
    name_empty  = "tools.registry_nonempty"
    name_unique = "tools.registry_unique_names"

    if registry is None:
        return [
            _warn(name_empty,  "Tool registry not provided — skipping"),
            _warn(name_unique, "Tool registry not provided — skipping"),
        ]

    # Support ToolRegistry objects (have .tools dict), plain dicts, and plain lists.
    if hasattr(registry, "tools") and isinstance(registry.tools, dict):
        names: list[str] = list(registry.tools.keys())
    elif isinstance(registry, dict):
        names = list(registry.keys())
    else:
        try:
            names = list(registry)
        except Exception as exc:
            err = _fail(name_empty, "Could not enumerate tool registry", str(exc))
            return [err, err]

    results: list[CheckResult] = []

    if not names:
        results.append(_fail(name_empty, "Tool registry is empty — no tools were loaded"))
    else:
        results.append(_ok(name_empty, f"{len(names)} tool(s) registered"))

    seen: set[str] = set()
    dupes: set[str] = set()
    for n in names:
        (dupes if n in seen else seen).add(n)
    if dupes:
        results.append(_fail(name_unique, f"Duplicate tool names detected: {sorted(dupes)}"))
    else:
        results.append(_ok(name_unique, "All tool names are unique"))

    return results


# ── GPU / VRAM ────────────────────────────────────────────────────────────────

def check_gpu_available() -> CheckResult:
    name = "gpu.nvml_available"
    try:
        import pynvml
        pynvml.nvmlInit()
        count = pynvml.nvmlDeviceGetCount()
        names = [
            pynvml.nvmlDeviceGetName(pynvml.nvmlDeviceGetHandleByIndex(i))
            for i in range(count)
        ]
        pynvml.nvmlShutdown()
        if count == 0:
            return _warn(name, "pynvml initialised but no GPUs detected — CPU-only mode")
        return _ok(name, f"{count} GPU(s) detected: {', '.join(names)}")
    except Exception as exc:
        return _warn(name, "GPU not available via NVML (CPU-only or driver missing)", str(exc))


def check_vram_budget(cfg: Any = None) -> CheckResult:
    name = "gpu.vram_budget"
    if cfg is None:
        return _warn(name, "Config not provided — skipping VRAM budget check")
    try:
        total_gb: float = getattr(cfg.vram, "total_vram_gb", None)
        headroom_gb: float = getattr(cfg.vram, "headroom_gb", 0.5)
        if total_gb is None:
            return _warn(name, "vram.total_vram_gb not configured — skipping budget check")
        usable_gb = total_gb - headroom_gb
        tiers = getattr(cfg.models, "tiers", {})
        pinned_gb = sum(
            getattr(t, "vram_estimate_gb", 0)
            for t in tiers.values()
            if getattr(t, "pinned", False)
        )
        if pinned_gb > usable_gb:
            return _fail(
                name,
                f"Pinned tiers require {pinned_gb:.1f} GB but only {usable_gb:.1f} GB usable",
                f"total={total_gb:.1f} GB, headroom={headroom_gb:.1f} GB",
            )
        return _ok(
            name,
            f"Pinned VRAM ({pinned_gb:.1f} GB) fits within budget ({usable_gb:.1f} GB usable)",
        )
    except Exception as exc:
        return _fail(name, "VRAM budget check raised an exception", str(exc))


# ── Startup orchestrator ──────────────────────────────────────────────────────

async def run_startup_diagnostics(
    *,
    db_path: str = "",
    cfg: Any = None,
    registry: Any = None,
    qdrant_url: str | None = None,
    redis_url: str | None = None,
    web_search_provider: str | None = None,
) -> list[CheckResult]:
    """
    Run all diagnostic checks and log results.  Never raises.

    OK  results → DEBUG (silent unless log level is DEBUG)
    WARN results → WARNING
    FAIL results → ERROR

    Nothing is surfaced to end-users; results go to the application log only.
    Returns the full result list so callers (and tests) can inspect them.
    """
    results: list[CheckResult] = []

    # Synchronous checks — run inline, fast
    sync_checks = [
        check_env_auth_secret(),
        check_env_history_secret(),
        check_env_jupyter_token(),
        check_env_public_base_url(),
        check_env_cookie_secure(),
        check_jwt_roundtrip(),
        check_history_encryption_roundtrip(),
        check_gpu_available(),
        check_vram_budget(cfg),
        check_cors_config(),
    ]
    results.extend(sync_checks)
    results.extend(check_tool_registry(registry))

    # DB checks — sequential (same file)
    for coro in (
        check_db_connectivity(db_path),
        check_db_schema(db_path),
        check_db_wal_mode(db_path),
        check_db_canary_write(db_path),
    ):
        results.append(await coro)

    # Service connectivity checks — concurrent
    service_results = await asyncio.gather(
        check_qdrant_reachable(qdrant_url),
        check_redis_reachable(redis_url),
        check_web_search_provider(web_search_provider),
    )
    results.extend(service_results)
    results.extend(await check_pinned_llamacpp_tiers(cfg))

    # Log summary + per-check detail
    ok_n    = sum(1 for r in results if r.severity == Severity.OK)
    warn_n  = sum(1 for r in results if r.severity == Severity.WARN)
    fail_n  = sum(1 for r in results if r.severity == Severity.FAIL)

    log.info("Startup diagnostics complete: %d OK  %d WARN  %d FAIL", ok_n, warn_n, fail_n)

    for r in results:
        detail = f" — {r.detail}" if r.detail else ""
        msg = f"[{r.name}] {r.message}{detail}"
        if r.severity == Severity.OK:
            log.debug(msg)
        elif r.severity == Severity.WARN:
            log.warning(msg)
        else:
            log.error(msg)

    return results
