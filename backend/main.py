"""FastAPI entry point — exposes an OpenAI-compatible chat endpoint so Open
WebUI (Phase 1) and the custom frontend (Phase 4) can both talk to it.

Phase 1 routes:
    GET  /healthz
    GET  /v1/models                    list tiers as virtual models
    POST /v1/chat/completions          streaming chat (SSE)
    GET  /api/vram                     scheduler status (debug panel)

Phase 4+ will add /auth/*, /chats/*, /rag/*, /memory/*.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from ipaddress import ip_address, ip_network
from pathlib import Path
from urllib.parse import urlsplit

from . import admin, airgap, auth, db, history_store, memory, metrics, rag
from .backends.llama_cpp import LlamaCppClient
from .backends.ollama import OllamaClient
from .config import AppConfig, CompiledSignals
from .middleware.clarification import (
    format_clarifications,
    inject_clarification_instruction,
)
from .middleware.context import inject_system_context
from .middleware.rate_limit import rate_limiter
from .middleware.response_mode import inject_response_mode
from .middleware.web_search import inject_web_results
from .orchestrator import Orchestrator
from .router import route
from .schemas import ChatMessage, MessagePart
from .tools import executor as tool_executor
from .tools.registry import ToolRegistry, build_registry
from .schemas import (
    AgentEvent,
    ChatRequest,
    ConversationListResponse,
    ConversationSummary,
    ConversationUpdate,
    ConversationWithMessages,
    MagicLinkRequest,
    MagicLinkResponse,
    MeResponse,
    MessageOut,
    ModelsListResponse,
    TierInfo,
)
from .diagnostics import run_startup_diagnostics
from .vram_scheduler import QueueFull, QueueTimeout, VRAMScheduler, VRAMExhausted


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("backend")


# ── App state ─────────────────────────────────────────────────────────────

class AppState:
    config: AppConfig
    signals: CompiledSignals
    ollama: OllamaClient
    llama_cpp: LlamaCppClient
    scheduler: VRAMScheduler
    orchestrator: Orchestrator
    tools: ToolRegistry
    # Runtime airgap flag — toggled via /admin/airgap. When on, the
    # backend refuses outbound calls and persists all new chat + memory
    # content to separate encrypted stores.
    airgap: airgap.AirgapState
    # Optional Redis client — populated when REDIS_URL (env or config) is
    # set. Used by the rate limiter (and future cross-worker scheduler
    # coordination). None means "single-worker, in-memory only".
    redis = None  # type: ignore[assignment]


state = AppState()


# Bounded queue for fire-and-forget post-stream work so a slow distiller
# (or an unreachable Ollama) can't pile up detached tasks unbounded (#76).
# Tasks are tracked in a weak-ref set so they aren't garbage-collected
# mid-flight and so lifespan shutdown can await them.
_FINALIZE_MAX_CONCURRENCY = int(os.getenv("FINALIZE_MAX_CONCURRENCY", "8"))
_finalize_sem = asyncio.Semaphore(_FINALIZE_MAX_CONCURRENCY)
_background_tasks: set[asyncio.Task] = set()


def _spawn_background(coro) -> asyncio.Task:
    """Spawn a task, keep a strong ref while it runs, and log failures."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    def _done(t: asyncio.Task) -> None:
        _background_tasks.discard(t)
        if not t.cancelled():
            exc = t.exception()
            if exc is not None:
                logger.error("background task failed: %r", exc)
    task.add_done_callback(_done)
    return task


async def _init_redis(cfg: AppConfig):
    """Lazy-imported so the `redis` package isn't required when unused."""
    url = cfg.concurrency.redis_url
    if not url:
        return None
    try:
        from redis.asyncio import Redis
        client = Redis.from_url(url, decode_responses=True)
        # Smoke-test so we fail early rather than fail-open on first request.
        await client.ping()
        logger.info("Redis connected: %s", url)
        return client
    except Exception as e:
        logger.warning(
            "Redis requested at %s but connection failed (%s). "
            "Falling back to in-memory state.", url, e,
        )
        return None


# ── Trusted-proxy + client-IP helpers ─────────────────────────────────────

def _parse_trusted_proxies(raw: str | None) -> list:
    """TRUSTED_PROXIES is a comma-separated list of IPs or CIDRs. Entries
    that don't parse are logged and skipped — we never fail open here."""
    if not raw:
        return []
    nets = []
    for item in (p.strip() for p in raw.split(",")):
        if not item:
            continue
        try:
            nets.append(ip_network(item, strict=False))
        except ValueError:
            logger.warning("TRUSTED_PROXIES: skipping invalid entry %r", item)
    return nets


def _ip_in_networks(ip_str: str, nets) -> bool:
    try:
        ip = ip_address(ip_str)
    except ValueError:
        return False
    return any(ip in n for n in nets)


def _client_ip(request: Request) -> str:
    """Return the best guess at the end-user's IP.

    Only trusts X-Forwarded-For / CF-Connecting-IP when the immediate peer
    matches TRUSTED_PROXIES (or CLOUDFLARE_TUNNEL_ENABLED=1). Otherwise
    falls back to `request.client.host` — the actual socket peer —
    regardless of what headers the client sent.
    """
    peer = request.client.host if request.client else ""
    trusted = _parse_trusted_proxies(os.getenv("TRUSTED_PROXIES"))
    cf_tunnel = os.getenv("CLOUDFLARE_TUNNEL_ENABLED", "").lower() in {"1", "true", "yes"}

    peer_is_trusted = bool(peer) and (
        _ip_in_networks(peer, trusted)
        or (cf_tunnel and peer in {"127.0.0.1", "::1"})
    )

    if peer_is_trusted:
        cf = request.headers.get("cf-connecting-ip")
        if cf:
            return cf.strip()
        xff = request.headers.get("x-forwarded-for")
        if xff:
            # Take the left-most entry that isn't itself a trusted hop.
            for hop in (h.strip() for h in xff.split(",")):
                if not hop:
                    continue
                if not _ip_in_networks(hop, trusted):
                    return hop
            # All hops were trusted — fall through to peer.
    return peer or "anon"


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Loading config…")
    state.config = AppConfig.load()
    state.signals = state.config.compile_signals()
    app.state.app_config = state.config       # for auth dependencies

    _validate_public_base_url(state.config)

    logger.info("Initialising database…")
    await db.init_db()

    # Load persisted airgap state and publish it module-globally so
    # middleware and tool gates can consult it without walking app.state.
    state.airgap = airgap.AirgapState()
    airgap.set_current(state.airgap)
    if state.airgap.enabled:
        logger.warning("Airgap mode is ENABLED (from persisted state)")

    # Optional Redis client for cross-worker rate limiting.
    state.redis = await _init_redis(state.config)
    rate_limiter.configure(
        per_minute=state.config.auth.rate_limits.requests_per_minute_per_user,
        per_day=state.config.auth.rate_limits.requests_per_day_per_user,
        redis_client=state.redis,
    )

    logger.info("Discovering tools…")
    tools_dir = Path(os.getenv("LAI_TOOLS_DIR", "/app/tools"))
    config_dir = Path(os.getenv("LAI_CONFIG_DIR", "/app/config"))
    state.tools = build_registry(tools_dir=tools_dir, config_dir=config_dir)
    logger.info("Tool registry ready: %d tools", len(state.tools.tools))

    # Backend clients — endpoint overridable per-tier, these are fallbacks
    default_ollama = os.getenv("OLLAMA_URL", "http://ollama:11434")
    default_llama = os.getenv("LLAMACPP_URL", "http://llama-server:8001/v1")
    state.ollama = OllamaClient(default_ollama)
    state.llama_cpp = LlamaCppClient(default_llama)

    # Per-tier clients (routed by tier.endpoint). For Phase 1 the endpoints
    # in models.yaml match the env-defaults, so a single client per backend
    # is fine. Future: per-tier endpoint dispatch.
    clients = {"ollama": state.ollama, "llama_cpp": state.llama_cpp}

    async def _ollama_load(tier):
        await state.ollama.ensure_loaded(
            tier, keep_alive=state.config.vram.ollama.keep_alive_pinned
        )

    async def _ollama_unload(tier):
        await state.ollama.unload(tier)

    async def _noop_load(tier):
        # llama.cpp is pre-loaded at container start
        return None

    async def _noop_unload(tier):
        # llama.cpp can't unload without restart — the scheduler should
        # never reach this path because vision is pinned.
        logger.warning("Requested unload of pinned llama.cpp model: %s", tier.model_tag)

    state.scheduler = VRAMScheduler(
        config=state.config,
        loaders={"ollama": _ollama_load, "llama_cpp": _noop_load},
        unloaders={"ollama": _ollama_unload, "llama_cpp": _noop_unload},
    )
    await state.scheduler.start()

    # Phase 6: threads the tool registry into the orchestrator so workers
    # can call tools within subtasks.
    state.orchestrator = Orchestrator(state.config, state.scheduler, clients, tools=state.tools)

    logger.info("Ready. Tiers: %s", list(state.config.models.tiers))

    # Self-diagnostics — results go to the application log only (lai.diagnostics logger).
    # OK results are DEBUG-level (silent at default INFO log level).
    # WARN/FAIL results are WARNING/ERROR so operators see them in logs.
    await run_startup_diagnostics(
        db_path=str(db.DB_PATH),
        cfg=state.config,
        registry=state.tools,
        ollama_url=default_ollama,
        llamacpp_url=default_llama,
        qdrant_url=os.getenv("QDRANT_URL", "http://qdrant:6333"),
        redis_url=state.config.concurrency.redis_url,
        searxng_url=os.getenv("SEARXNG_URL", "http://searxng:8080"),
    )

    try:
        yield
    finally:
        # Drain bounded background tasks so in-flight finalizations complete
        # (or time out) before we tear down the scheduler/db connections (#76).
        if _background_tasks:
            pending = list(_background_tasks)
            logger.info("Draining %d background task(s)…", len(pending))
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True),
                    timeout=10.0,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Background drain exceeded 10s; %d task(s) still running at shutdown",
                    len(_background_tasks),
                )
        await state.scheduler.stop()
        if state.redis is not None:
            try:
                await state.redis.aclose()
            except Exception:
                pass


# ── Startup validators ────────────────────────────────────────────────────

def _is_localhost_host(host: str) -> bool:
    if not host:
        return False
    h = host.lower()
    if h in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}:
        return True
    if h.endswith(".localhost"):
        return True
    return False


def _validate_public_base_url(cfg: AppConfig) -> None:
    """Fail-closed if PUBLIC_BASE_URL is missing or mis-matched with
    cookie.secure / ALLOWED_ORIGINS (#64, #72, #73)."""
    raw = os.getenv("PUBLIC_BASE_URL", "").strip()
    if not raw:
        # Dev shortcut — the /auth/verify redirect falls back to "/" below.
        logger.warning(
            "PUBLIC_BASE_URL is unset; magic-link redirects default to '/'. "
            "Set it to your public URL (e.g. https://chat.example.com) in production."
        )
        return

    parts = urlsplit(raw)
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if scheme not in {"http", "https"} or not host:
        raise RuntimeError(
            f"PUBLIC_BASE_URL={raw!r} must be an absolute http(s) URL."
        )

    is_local_dev = scheme == "http" and _is_localhost_host(host)

    # #64: production deploys must be HTTPS.
    if scheme == "http" and not is_local_dev:
        raise RuntimeError(
            f"PUBLIC_BASE_URL={raw!r} is plaintext http:// and does not point at "
            "localhost. Use https:// in production or point it at localhost for "
            "local dev."
        )

    # #73: cookie.secure=True silently drops cookies over http://. Refuse
    # to boot on that mismatch.
    if cfg.auth.session.cookie_secure and scheme == "http":
        raise RuntimeError(
            "cookie_secure=True requires https:// PUBLIC_BASE_URL. Either enable "
            "TLS (via Cloudflare Tunnel) or set auth.session.cookie_secure=false "
            "for local dev."
        )
    if scheme == "https" and not cfg.auth.session.cookie_secure:
        logger.warning(
            "cookie_secure=False while PUBLIC_BASE_URL is https:// — enable "
            "cookie_secure in auth.yaml to prevent session-cookie leakage."
        )

    # #72: reject ALLOWED_ORIGINS=* in production (HTTPS deploy).
    allowed = [
        o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()
    ]
    if scheme == "https" and allowed == ["*"]:
        raise RuntimeError(
            "ALLOWED_ORIGINS=* is unsafe with cookie-based auth over HTTPS. "
            "Set it to your public origin(s) explicitly, e.g. "
            f"ALLOWED_ORIGINS={parts.scheme}://{parts.netloc}"
        )
    for origin in allowed:
        if origin == "*":
            continue
        op = urlsplit(origin)
        if op.scheme not in {"http", "https"} or not op.hostname:
            raise RuntimeError(
                f"ALLOWED_ORIGINS entry {origin!r} must be an absolute URL "
                "with an explicit scheme and host."
            )


app = FastAPI(title="Local AI Stack Backend", lifespan=lifespan)
app.include_router(admin.router)

# CORS — restrict to ALLOWED_ORIGINS (comma-separated). In production the
# Cloudflare Tunnel hostname is set via setup-cloudflared.sh. Local dev
# defaults to wildcard. When wildcard+credentials is configured we warn
# because browsers ignore that combination — the startup validator in
# lifespan() fail-closes this in production (#72).
_allowed_origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
if _allowed_origins == ["*"]:
    logger.warning(
        "CORS set to wildcard with credentials=True — browsers will ignore "
        "credentials. Set ALLOWED_ORIGINS to your Cloudflare hostname in "
        "production (see scripts/setup-cloudflared.sh)."
    )
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["content-type"],
    max_age=600,
)


# #75: Content-Security-Policy caps the blast radius of any future XSS.
# The chat UI currently renders message bodies as text, so there's no
# known vector today. Override via CSP_HEADER env for deployments that
# load a different origin or need a stricter connect-src.
_DEFAULT_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'"
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Attach HSTS, content-type protection, and CSP headers to every response.
    HSTS is only meaningful when served behind HTTPS (Cloudflare Tunnel)."""
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    csp = os.getenv("CSP_HEADER") or _DEFAULT_CSP
    if csp and csp.lower() != "off":
        resp.headers.setdefault("Content-Security-Policy", csp)
    if os.getenv("PUBLIC_BASE_URL", "").startswith("https://"):
        resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return resp


# ── Routes ────────────────────────────────────────────────────────────────

@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/v1/models", response_model=ModelsListResponse)
async def list_models():
    tiers = state.config.models.tiers
    return ModelsListResponse(data=[
        TierInfo(
            id=f"tier.{name}",
            name=tier.name,
            description=tier.description,
            backend=tier.backend,
            context_window=tier.context_window,
            think_supported=tier.think_supported,
            vram_estimate_gb=tier.vram_estimate_gb,
        )
        for name, tier in tiers.items()
    ])


@app.get("/api/vram")
async def vram_status():
    return await state.scheduler.status()


def _read_meminfo() -> dict[str, int]:
    """Parse /proc/meminfo into bytes. Linux-only; non-Linux hosts return {}."""
    try:
        lines = Path("/proc/meminfo").read_text().splitlines()
    except OSError:
        return {}
    out: dict[str, int] = {}
    for line in lines:
        key, _, rest = line.partition(":")
        parts = rest.strip().split()
        if not parts:
            continue
        try:
            val = int(parts[0])
        except ValueError:
            continue
        # Values are in kB per meminfo convention; normalize to bytes.
        if len(parts) > 1 and parts[1].lower() == "kb":
            val *= 1024
        out[key.strip()] = val
    return out


@app.get("/api/system")
async def system_status():
    """Lightweight telemetry snapshot for the chat-side status panel.

    Pulls GPU stats from the scheduler (which already caches NVML reads)
    and system RAM straight from /proc/meminfo so we don't add a psutil
    dep for what amounts to four integers.
    """
    vram = await state.scheduler.status()
    mi = _read_meminfo()
    ram_total_b = mi.get("MemTotal", 0)
    ram_avail_b = mi.get("MemAvailable", mi.get("MemFree", 0))
    ram_used_b = max(0, ram_total_b - ram_avail_b)
    gb = 1 << 30
    return {
        "vram": {
            "total_gb": vram.get("total_vram_gb", 0.0),
            "free_gb": vram.get("free_vram_gb_actual", vram.get("free_vram_gb_projected", 0.0)),
            "used_gb": max(0.0, vram.get("total_vram_gb", 0.0) - vram.get("free_vram_gb_projected", 0.0)),
            "loaded_tiers": [m.get("tier_id") for m in vram.get("loaded", [])],
        },
        "ram": {
            "total_gb": ram_total_b / gb,
            "used_gb": ram_used_b / gb,
            "free_gb": ram_avail_b / gb,
        },
        "ts": time.time(),
    }


@app.get("/api/tools")
async def list_tools():
    """List discovered tools. Tool names are `<module>.<method>`.

    When airgap mode is on we mark tools that depend on non-local
    services with `airgap_blocked=True` so the frontend can grey them
    out. They remain in the list so users understand *why* an option
    is unavailable."""
    ag = airgap.is_enabled()
    return {
        "airgap": ag,
        "data": [
            {
                "name": t.name,
                "description": t.schema.get("function", {}).get("description", ""),
                "default_enabled": t.default_enabled,
                "requires_service": t.requires_service,
                "airgap_blocked": ag and not state.tools.is_airgap_safe(t.name),
            }
            for t in state.tools.tools.values()
        ],
    }


@app.get("/api/airgap")
async def airgap_status():
    """Public-ish airgap state. Any signed-in user can read this so the
    chat UI can render a banner. Writes are admin-only (see /admin/airgap)."""
    snap = state.airgap.snapshot()
    return snap


async def _inject_user_context(req: ChatRequest, user: dict) -> None:
    """Prepend RAG + memory context to the system message for a signed-in
    user. Runs inline (needs embeddings before streaming starts).

    When airgap mode is on we pull from the airgap-scoped memory
    collection only, so facts from normal conversations don't leak into
    an airgap chat and vice versa."""
    from .router import last_user_text
    last = last_user_text(req.messages)
    if not last or not last.strip():
        return
    is_airgap = airgap.is_enabled()
    try:
        mem_hits = await memory.retrieve_for_user(
            user["id"], last, k=3, airgap=is_airgap,
        )
    except Exception as e:
        logger.warning(
            "memory retrieve failed for user=%s: %s", user.get("id"), e,
        )
        await db.record_backend_error(
            user_id=user.get("id"), conversation_id=None,
            stage="retrieve_memory", error=repr(e),
        )
        mem_hits = []
    try:
        rag_hits = await rag.retrieve(user["id"], last, k=3)
    except Exception as e:
        logger.warning(
            "rag retrieve failed for user=%s: %s", user.get("id"), e,
        )
        await db.record_backend_error(
            user_id=user.get("id"), conversation_id=None,
            stage="retrieve_rag", error=repr(e),
        )
        rag_hits = []
    blocks = []
    if mem_hits:
        blocks.append(memory.format_memory_block(mem_hits))
    if rag_hits:
        blocks.append(rag.format_context_block(rag_hits))
    if not blocks:
        return
    injection = "\n\n".join(blocks)
    sys_msg = next((m for m in req.messages if m.role == "system"), None)
    if sys_msg:
        if isinstance(sys_msg.content, str):
            sys_msg.content = f"{sys_msg.content}\n\n{injection}"
    else:
        req.messages.insert(0, ChatMessage(role="system", content=injection))


@app.post("/v1/chat/completions")
async def chat_completions(
    req: ChatRequest,
    request: Request,
    user: dict | None = Depends(auth.optional_user),
):
    """OpenAI-compatible chat endpoint. Always streams (SSE)."""
    try:
        decision, req = route(req, state.config, state.signals)
    except KeyError as e:
        raise HTTPException(404, str(e))

    # Rate-limit by user ID (falls back to a validated client IP for anonymous).
    rate_key = str(user["id"]) if user else _client_ip(request)
    await rate_limiter.check(rate_key)

    # Middleware pipeline — inlet:
    #   1. Datetime/system context injection
    #   2. Clarification-protocol system prompt + ambiguity nudge
    #   3. Web-search auto-inject (if trigger patterns match the user msg)
    #   4. Per-user RAG + memory context (signed-in users only)
    inject_system_context(req.messages)
    inject_clarification_instruction(req.messages)
    inject_response_mode(req.messages, req.response_mode, req.plan_text)
    await inject_web_results(req.messages)
    if user:
        await _inject_user_context(req, user)

    logger.info(
        "route: tier=%s think=%s multi=%s specialist=%s slash=%s user=%s",
        decision.tier_name, decision.think, decision.multi_agent,
        decision.specialist_reason, decision.slash_commands_applied,
        user["email"] if user else "anon",
    )

    tier = state.config.models.tiers[decision.tier_name]
    client = state.ollama if tier.backend == "ollama" else state.llama_cpp

    started = time.time()

    async def _wrap(inner: AsyncIterator[str]) -> AsyncIterator[str]:
        """Wrap an SSE producer to record a usage event on stream completion.

        Counts tokens via a whitespace-split proxy — cheap, close enough for
        dashboard trends, and avoids a second tokenizer pass on the hot path.
        """
        out_text: list[str] = []
        err: str | None = None
        try:
            async for chunk in inner:
                # OpenAI-style data lines carry `"content": "..."` snippets;
                # we only need an approximate output size, not byte-accurate.
                if chunk.startswith("data:") and '"content"' in chunk:
                    try:
                        payload = json.loads(chunk[5:].strip())
                        delta = (payload.get("choices") or [{}])[0].get("delta") or {}
                        t = delta.get("content")
                        if isinstance(t, str):
                            out_text.append(t)
                    except Exception:
                        pass
                yield chunk
        except Exception as e:
            err = str(e)
            raise
        finally:
            full = "".join(out_text)
            tokens_out = max(1, len(full.split())) if full else 0
            prompt_words = 0
            for m in req.messages:
                if isinstance(m.content, str):
                    prompt_words += len(m.content.split())
            metrics.record_event_bg(
                user_id=user["id"] if user else None,
                tier=decision.tier_name,
                think=decision.think,
                multi_agent=decision.multi_agent,
                tokens_in=prompt_words,
                tokens_out=tokens_out,
                latency_ms=int((time.time() - started) * 1000),
                error=err,
            )

    if decision.multi_agent:
        return StreamingResponse(
            _wrap(_multi_agent_sse(req, decision, options=req.multi_agent_options)),
            media_type="text/event-stream",
        )

    return StreamingResponse(
        _wrap(_single_agent_sse(req, decision, client, tier, user=user)),
        media_type="text/event-stream",
    )


# ── SSE producers ─────────────────────────────────────────────────────────

def _openai_chunk(content: str, model: str, done: bool = False) -> str:
    """Format a streaming chunk in OpenAI's SSE shape."""
    payload = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:16]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "delta": {} if done else {"content": content},
            "finish_reason": "stop" if done else None,
        }],
    }
    return f"data: {json.dumps(payload)}\n\n"


def _agent_event_sse(ev: AgentEvent, model: str) -> str:
    """Custom event payload for multi-agent visualization. Uses a named SSE
    event so our frontend can filter, while preserving OpenAI-shaped data
    chunks for token events (Open WebUI compatibility)."""
    if ev.type == "token":
        return _openai_chunk(ev.data.get("text", ""), model)
    payload = {"type": ev.type, "data": ev.data}
    return f"event: agent\ndata: {json.dumps(payload)}\n\n"


async def _reserve_with_sse(
    scheduler: VRAMScheduler,
    tier_id: str,
    model_id: str,
) -> AsyncIterator[str]:
    """Acquire a scheduler slot, forwarding queue-progress events as SSE.

    Yields `event: agent` chunks with `type:"queue"` while the request is
    waiting for a slot. Returns (no more yields) once the slot is held.
    Raises `QueueFull` or `QueueTimeout` to the caller, which must convert
    them into an SSE error event.
    """
    evs: asyncio.Queue = asyncio.Queue()

    async def on_event(ev: dict) -> None:
        await evs.put(ev)

    acquire_task = asyncio.create_task(scheduler.acquire(tier_id, on_event))
    try:
        while not acquire_task.done():
            try:
                ev = await asyncio.wait_for(evs.get(), timeout=0.25)
            except asyncio.TimeoutError:
                continue
            yield _agent_event_sse(
                AgentEvent(type="queue", data=ev), model_id,
            )
        # Drain any events that arrived after acquisition.
        while not evs.empty():
            try:
                ev = evs.get_nowait()
            except asyncio.QueueEmpty:
                break
            yield _agent_event_sse(
                AgentEvent(type="queue", data=ev), model_id,
            )
        # Propagate QueueFull/QueueTimeout/VRAMExhausted/etc.
        await acquire_task
    except BaseException:
        if not acquire_task.done():
            acquire_task.cancel()
            try:
                await acquire_task
            except BaseException:
                pass
        raise


async def _single_agent_sse(
    req: ChatRequest,
    decision,
    client,
    tier,
    user: dict | None = None,
) -> AsyncIterator[str]:
    model_id = f"tier.{decision.tier_name}"
    assembled_text = []   # accumulated assistant content for post-stream persist

    # Route decision event (for UI display)
    yield _agent_event_sse(
        AgentEvent(type="route.decision", data={
            "tier": decision.tier_name,
            "think": decision.think,
            "specialist_reason": decision.specialist_reason,
            "slash_commands_applied": decision.slash_commands_applied,
        }),
        model_id,
    )

    # Serialize messages for the tool loop (needs to append role=tool entries).
    from .backends.ollama import _messages_to_payload as _ollama_msgs
    msg_payload = _ollama_msgs(req.messages) if tier.backend == "ollama" else None

    tool_schemas: list[dict] | None = None
    if tier.backend == "ollama" and req.tools is None:
        enabled = state.tools.all_schemas(
            only_enabled=True, airgap=airgap.is_enabled(),
        )
        # Only pass tools when the registry isn't empty — some models
        # degrade with an empty `tools: []` field.
        if enabled:
            tool_schemas = enabled
    elif req.tools:
        tool_schemas = req.tools

    max_tool_turns = 5
    try:
        for turn in range(max_tool_turns + 1):
            # Acquire a slot on the tier's loaded model, forwarding any
            # queue-progress events to the client while we wait.
            async for sse in _reserve_with_sse(
                state.scheduler, decision.tier_name, model_id,
            ):
                yield sse
            tool_calls_accum: list[dict] = []
            llama_cpp_done = False
            try:
                if tier.backend == "ollama":
                    async for chunk in client.chat_stream(
                        tier, msg_payload, think=decision.think,
                        keep_alive=state.config.vram.ollama.keep_alive_pinned,
                        tools=tool_schemas,
                    ):
                        msg = chunk.get("message") or {}
                        text = msg.get("content")
                        if text:
                            assembled_text.append(text)
                            yield _openai_chunk(text, model_id)
                        if msg.get("tool_calls"):
                            tool_calls_accum.extend(msg["tool_calls"])
                        if chunk.get("done"):
                            break
                else:  # llama_cpp — no tool support yet
                    async for chunk in client.chat_stream(
                        tier, req.messages, think=decision.think,
                    ):
                        for choice in chunk.get("choices", []):
                            delta = choice.get("delta") or {}
                            text = delta.get("content")
                            if text:
                                assembled_text.append(text)
                                yield _openai_chunk(text, model_id)
                    llama_cpp_done = True
            finally:
                await state.scheduler.release(decision.tier_name)

            if llama_cpp_done:
                break
            if not tool_calls_accum or turn >= max_tool_turns:
                break

            # Dispatch tools, append results to the conversation, loop again.
            for tc in tool_calls_accum:
                yield _agent_event_sse(
                    AgentEvent(type="route.decision", data={
                        "tool_call": tc.get("function", {}).get("name", ""),
                    }),
                    model_id,
                )
            results = await tool_executor.dispatch_many(
                tool_calls_accum, state.tools, user=user,
            )
            # Append the assistant message (with tool_calls) then the tool
            # results, so the model can continue its response.
            msg_payload = (msg_payload or []) + [
                {"role": "assistant", "content": "", "tool_calls": tool_calls_accum}
            ] + results
    except VRAMExhausted as e:
        yield _agent_event_sse(
            AgentEvent(type="error", data={"message": str(e), "kind": "vram_exhausted"}),
            model_id,
        )
    except QueueFull as e:
        yield _agent_event_sse(
            AgentEvent(type="error", data={
                "message": str(e),
                "kind": "queue_full",
                "retry_after_sec": 30,
            }),
            model_id,
        )
    except QueueTimeout as e:
        yield _agent_event_sse(
            AgentEvent(type="error", data={
                "message": str(e),
                "kind": "queue_timeout",
                "retry_after_sec": 15,
            }),
            model_id,
        )
    except Exception as e:
        logger.exception("Single-agent stream failed")
        yield _agent_event_sse(
            AgentEvent(type="error", data={"message": str(e)}),
            model_id,
        )

    # Phase 6: post-stream persistence + memory distillation (fire-and-forget).
    full_text = "".join(assembled_text)
    _spawn_background(_finalize_conversation(req, user, decision, full_text))

    yield _openai_chunk("", model_id, done=True)
    yield "data: [DONE]\n\n"


async def _finalize_conversation(
    req: ChatRequest, user: dict | None, decision, assistant_text: str,
) -> None:
    """Persist the user+assistant messages and periodically distill memory.
    Runs as a background task after the stream completes; never raises.

    Bounded by _finalize_sem (#76) so a stuck distiller can't queue up
    unlimited tasks. Silent failures are surfaced via the `backend_errors`
    table so the admin dashboard can show them (#18).
    """
    if not user or not req.conversation_id or not assistant_text.strip():
        return
    async with _finalize_sem:
      try:
        conv = await db.get_conversation(req.conversation_id, user["id"])
        if not conv:
            return

        # #16 Back-fill any earlier turns in the request that haven't yet
        # been persisted. Keeps SQLite authoritative even if the frontend
        # stops calling /chats POST for every message — we compare the
        # request's message count against what's on disk and insert the
        # missing prefix in order.
        from .router import last_user_text
        user_text = last_user_text(req.messages)
        existing = await db.list_messages(req.conversation_id)
        existing_count = len(existing)
        # The final user message (user_text) and the assistant reply below
        # are persisted separately; only back-fill messages strictly
        # earlier than those.
        earlier = [
            m for m in req.messages
            if m.role in {"user", "assistant", "system"}
        ]
        if earlier and earlier[-1].role == "user":
            earlier = earlier[:-1]
        to_backfill = earlier[existing_count:]
        for m in to_backfill:
            content = m.content if isinstance(m.content, str) else str(m.content)
            if not content:
                continue
            await db.add_message(
                req.conversation_id, m.role, content,
                tier=decision.tier_name, think=False,
            )

        # Save the trailing user message + the assistant reply.
        if user_text:
            await db.add_message(
                req.conversation_id, "user", user_text,
                tier=decision.tier_name, think=False,
            )
        await db.add_message(
            req.conversation_id, "assistant", assistant_text,
            tier=decision.tier_name, think=decision.think,
        )

        # Per-chat opt-out: when `memory_enabled` is false on this conv,
        # skip both the encrypted-history append AND distillation. SQLite
        # persistence above still happens so the chat stays navigable —
        # the toggle is about *contribution to long-term memory*, not
        # about hiding the chat transcript from the user.
        if not conv.get("memory_enabled", True):
            return

        # The conversation's own airgap flag (set at creation) determines
        # which history file and memory collection receive this turn —
        # not the live runtime flag. A conversation that was started in
        # airgap stays airgap-scoped even if airgap is toggled off later,
        # so the encrypted record never bleeds into the normal stores.
        conv_airgap = bool(conv.get("airgap"))

        ts = time.time()
        records = []
        if user_text:
            records.append({
                "conv_id": req.conversation_id,
                "role": "user",
                "content": user_text,
                "tier": decision.tier_name,
                "think": False,
                "ts": ts,
            })
        records.append({
            "conv_id": req.conversation_id,
            "role": "assistant",
            "content": assistant_text,
            "tier": decision.tier_name,
            "think": decision.think,
            "ts": ts,
        })
        await history_store.append_many(user["id"], records, airgap=conv_airgap)

        # Distill every 5th assistant turn in a conversation to avoid
        # hammering the Versatile tier.
        msgs = await db.list_messages(req.conversation_id)
        asst_count = sum(1 for m in msgs if m["role"] == "assistant")
        if asst_count > 0 and asst_count % 5 == 0:
            logger.info("Triggering memory distillation for conv %d (asst turn %d, airgap=%s)",
                        req.conversation_id, asst_count, conv_airgap)
            versatile_tier = state.config.models.tiers["versatile"]
            await memory.distill_and_store(
                user["id"], req.conversation_id, state.ollama, versatile_tier,
                airgap=conv_airgap,
            )
      except Exception as e:
        logger.exception("Post-stream finalization failed")
        await db.record_backend_error(
            user_id=user.get("id") if user else None,
            conversation_id=req.conversation_id,
            stage="finalize_conversation",
            error=repr(e),
        )


async def _multi_agent_sse(
    req: ChatRequest, decision, options=None,
) -> AsyncIterator[str]:
    model_id = f"tier.{decision.tier_name}"

    yield _agent_event_sse(
        AgentEvent(type="route.decision", data={
            "tier": decision.tier_name,
            "think": decision.think,
            "multi_agent": True,
            "slash_commands_applied": decision.slash_commands_applied,
            "options": options.model_dump(exclude_none=True) if options else None,
        }),
        model_id,
    )

    from .router import last_user_text
    user_msg = last_user_text(req.messages)

    try:
        async for ev in state.orchestrator.run(
            user_message=user_msg,
            conversation=req.messages,
            think_synthesis=decision.think,
            options=options,
        ):
            yield _agent_event_sse(ev, model_id)
    except VRAMExhausted as e:
        yield _agent_event_sse(
            AgentEvent(type="error", data={"message": str(e), "kind": "vram_exhausted"}),
            model_id,
        )
    except Exception as e:
        logger.exception("Multi-agent run failed")
        yield _agent_event_sse(
            AgentEvent(type="error", data={"message": str(e)}),
            model_id,
        )

    yield _openai_chunk("", model_id, done=True)
    yield "data: [DONE]\n\n"


# ── Auth routes ─────────────────────────────────────────────────────────

@app.post("/auth/request", response_model=MagicLinkResponse)
async def auth_request(req: MagicLinkRequest, request: Request):
    cfg = state.config.auth
    email = req.email.lower().strip()
    if not auth.valid_email(email, cfg):
        raise HTTPException(400, "Invalid or disallowed email address")

    client_ip = _client_ip(request)
    await auth.check_rate_limits(email, cfg, client_ip=client_ip)

    expiry_s = cfg.magic_link.expiry_minutes * 60
    token = await db.create_magic_link(email, expiry_s, client_ip)

    base = os.getenv("PUBLIC_BASE_URL", f"http://{request.url.hostname}:{request.url.port or 8000}")
    verify_url = f"{base}/auth/verify?token={token}"
    try:
        await auth.send_magic_email(email, verify_url, cfg)
    except Exception as e:
        logger.exception("Failed to send magic-link email")
        raise HTTPException(500, f"Failed to send email: {e}")

    return MagicLinkResponse(ok=True, message="Check your inbox for a sign-in link.")


@app.get("/auth/verify")
async def auth_verify(token: str, request: Request):
    cfg = state.config.auth
    consumed = await db.consume_magic_link(token)
    if not consumed:
        raise HTTPException(400, "Magic link is invalid, expired, or already used")

    user = await db.get_or_create_user(consumed["email"])
    session_token = auth.issue_session_token(user["id"], cfg)

    # Redirect to the app root with the session cookie set. We cross-check
    # the configured URL against the host we were asked to redirect to so a
    # mis-set or tampered PUBLIC_BASE_URL can't send a freshly-authenticated
    # user to an attacker-controlled origin (#64).
    redirect_to = os.getenv("PUBLIC_BASE_URL", "/")
    if redirect_to != "/":
        parts = urlsplit(redirect_to)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            logger.warning(
                "PUBLIC_BASE_URL=%r is not an absolute URL; redirecting to '/'",
                redirect_to,
            )
            redirect_to = "/"
    resp = Response(status_code=302)
    resp.headers["Location"] = redirect_to
    resp.set_cookie(
        key=cfg.session.cookie_name,
        value=session_token,
        max_age=cfg.session.cookie_ttl_days * 86400,
        httponly=True,
        secure=cfg.session.cookie_secure,
        samesite=cfg.session.cookie_samesite,
        path="/",
    )
    return resp


@app.post("/auth/logout")
async def auth_logout():
    cfg = state.config.auth
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(cfg.session.cookie_name, path="/")
    return resp


@app.get("/me", response_model=MeResponse)
async def me(user: dict = Depends(auth.current_user)):
    return MeResponse(**user)


# ── Conversations ───────────────────────────────────────────────────────

@app.get("/chats", response_model=ConversationListResponse)
async def list_chats(user: dict = Depends(auth.current_user)):
    """Return only conversations that match the current airgap mode so a
    user in airgap mode never sees normal chats in the sidebar (and vice
    versa). Switching modes reveals/hides the other set."""
    rows = await db.list_conversations(user["id"], airgap=airgap.is_enabled())
    return ConversationListResponse(data=[ConversationSummary(**r) for r in rows])


@app.post("/chats", response_model=ConversationSummary)
async def create_chat(
    body: ConversationUpdate,
    user: dict = Depends(auth.current_user),
):
    # New chats inherit the current mode — you can't create a "normal"
    # chat while airgap is on, period.
    is_airgap = airgap.is_enabled()
    conv = await db.create_conversation(
        user["id"],
        title=body.title or "New chat",
        tier=body.tier,
        # Default to enabled when the client doesn't say otherwise.
        memory_enabled=True if body.memory_enabled is None else body.memory_enabled,
        airgap=is_airgap,
    )
    return ConversationSummary(**conv)


@app.get("/chats/{conv_id}", response_model=ConversationWithMessages)
async def get_chat(conv_id: int, user: dict = Depends(auth.current_user)):
    conv = await db.get_conversation(conv_id, user["id"])
    if not conv:
        raise HTTPException(404, "Conversation not found")
    # Hide cross-mode chats: an airgap conversation must not be openable
    # while the server is in normal mode and vice versa.
    if bool(conv.get("airgap")) != airgap.is_enabled():
        raise HTTPException(
            404,
            "Conversation not found (owned by the other airgap mode).",
        )
    msgs = await db.list_messages(conv_id)
    return ConversationWithMessages(
        **conv,
        messages=[MessageOut(**m) for m in msgs],
    )


@app.patch("/chats/{conv_id}", response_model=ConversationSummary)
async def update_chat(
    conv_id: int,
    body: ConversationUpdate,
    user: dict = Depends(auth.current_user),
):
    ok = await db.update_conversation(
        conv_id, user["id"],
        title=body.title, tier=body.tier,
        memory_enabled=body.memory_enabled,
    )
    if not ok:
        raise HTTPException(404, "Conversation not found")
    conv = await db.get_conversation(conv_id, user["id"])
    return ConversationSummary(**conv)


@app.delete("/chats/{conv_id}")
async def delete_chat(conv_id: int, user: dict = Depends(auth.current_user)):
    ok = await db.delete_conversation(conv_id, user["id"])
    if not ok:
        raise HTTPException(404, "Conversation not found")
    return {"ok": True}


# ── RAG ─────────────────────────────────────────────────────────────────

from fastapi import UploadFile, File


@app.post("/rag/upload")
async def rag_upload(
    file: UploadFile = File(...),
    user: dict = Depends(auth.current_user),
):
    content = await file.read()
    if not content:
        raise HTTPException(400, "Empty file")
    if len(content) > 20 * 1024 * 1024:
        raise HTTPException(413, "File too large (>20MB)")
    # #67: detect the file type from the body's magic bytes, not the
    # attacker-controlled content-type header / extension. This throws
    # UnsupportedUploadError which we translate to 415 so the client
    # sees why the upload was rejected.
    try:
        rag.sniff_kind(content, filename=file.filename or "")
    except rag.UnsupportedUploadError as e:
        raise HTTPException(415, str(e))
    try:
        ingest = await rag.ingest_document(
            user["id"], file.filename or "upload", content, mime=file.content_type,
        )
    except rag.UnsupportedUploadError as e:
        raise HTTPException(415, str(e))
    except Exception as e:
        logger.exception("RAG ingest failed")
        raise HTTPException(500, f"Ingest failed: {e}")

    # Persist doc metadata so the UI can list + delete.
    now = time.time()
    async with db.get_conn() as c:
        await c.execute(
            "INSERT INTO rag_docs (user_id, filename, mime_type, size_bytes, chunk_count, qdrant_ids, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                user["id"], file.filename, file.content_type, len(content),
                ingest["chunks"], json.dumps(ingest["qdrant_ids"]), now,
            ),
        )
        await c.commit()
    return {"ok": True, "chunks": ingest["chunks"], "filename": file.filename}


@app.get("/rag/docs")
async def rag_list(user: dict = Depends(auth.current_user)):
    async with db.get_conn() as c:
        rows = await (await c.execute(
            "SELECT id, filename, mime_type, size_bytes, chunk_count, created_at "
            "FROM rag_docs WHERE user_id = ? ORDER BY created_at DESC",
            (user["id"],),
        )).fetchall()
        return {"data": [dict(r) for r in rows]}


@app.delete("/rag/docs/{doc_id}")
async def rag_delete(doc_id: int, user: dict = Depends(auth.current_user)):
    async with db.get_conn() as c:
        row = await (await c.execute(
            "SELECT id, qdrant_ids FROM rag_docs WHERE id = ? AND user_id = ?",
            (doc_id, user["id"]),
        )).fetchone()
        if not row:
            raise HTTPException(404, "Document not found")
        ids = json.loads(row["qdrant_ids"] or "[]")
        await c.execute("DELETE FROM rag_docs WHERE id = ?", (doc_id,))
        await c.commit()
    # Best-effort Qdrant cleanup (the upload persists point IDs, not doc_id).
    if ids:
        coll = rag.collection_name(user["id"])
        async with httpx_async_client() as cx:
            await cx.post(
                f"{rag.QDRANT_URL.rstrip('/')}/collections/{coll}/points/delete",
                json={"points": ids},
                params={"wait": "true"},
            )
    return {"ok": True}


def httpx_async_client():
    import httpx as _httpx
    return _httpx.AsyncClient(timeout=30.0)


# ── Memory ──────────────────────────────────────────────────────────────

@app.get("/memory")
async def memory_list(user: dict = Depends(auth.current_user)):
    """List the user's stored memories for the *current* mode only.
    Airgap and non-airgap memories never appear together so a user in
    airgap mode can't accidentally see distilled facts from their
    normal conversations (or vice versa)."""
    rows = await memory.list_for_user(user["id"], airgap=airgap.is_enabled())
    return {"data": rows}


@app.delete("/memory/{memory_id}")
async def memory_delete(memory_id: int, user: dict = Depends(auth.current_user)):
    ok = await memory.delete(user["id"], memory_id)
    if not ok:
        raise HTTPException(404, "Memory not found")
    return {"ok": True}


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception")
    return JSONResponse(status_code=500, content={"detail": str(exc)})
