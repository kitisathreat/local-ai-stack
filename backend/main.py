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
import sys
import json
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from pathlib import Path

from . import admin, airgap, auth, db, history_store, memory, metrics, rag
from .backends.llama_cpp import LlamaCppClient, ToolCallAccumulator
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
from .router import last_user_text, route
from .schemas import ChatMessage, MessagePart
from .tools import executor as tool_executor
from .tools.registry import ToolRegistry, build_registry
from .schemas import (
    AgentEvent,
    ChangePasswordRequest,
    ChatRequest,
    ConversationListResponse,
    ConversationSummary,
    ConversationUpdate,
    ConversationWithMessages,
    CreateUserRequest,
    LoginRequest,
    LoginResponse,
    MeResponse,
    MessageOut,
    ModelsListResponse,
    TierInfo,
    UpdateUserRequest,
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


async def _auto_pull_missing_tiers() -> None:
    """Spawn the resolver as a child process to fetch missing tier GGUFs.

    Detached so the FastAPI worker can keep serving requests while
    multi-GB files stream in. Tiers come online progressively as files
    land — the LlamaCppClient picks them up on the next chat request.
    """
    try:
        models_dir = Path(os.getenv("LAI_DATA_DIR") or
                          Path(__file__).resolve().parent.parent / "data") / "models"
        configured = list((state.config.models.tiers or {}).keys())
        missing = [t for t in configured if not (models_dir / f"{t}.gguf").exists()]
        if not missing:
            return
        logger.info("Auto-pull starting for missing tiers: %s", ", ".join(missing))
        # Using sys.executable + -m so we don't depend on the venv's
        # console_scripts entry being on PATH for the FastAPI worker.
        cmd = [sys.executable, "-m", "backend.model_resolver", "resolve", "--pull"]
        for t in missing:
            cmd += ["--tier", t]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        rc = await proc.wait()
        if rc == 0:
            logger.info("Auto-pull finished (exit 0). Re-checking tiers on next request.")
        else:
            logger.warning("Auto-pull resolver exited %d — some tiers may still be missing", rc)
    except Exception as exc:
        logger.warning("Auto-pull task failed: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Loading config…")
    state.config = AppConfig.load()
    state.signals = state.config.compile_signals()
    app.state.app_config = state.config       # for auth dependencies

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

    # llama.cpp is the only backend now. The client manages one
    # llama-server subprocess per tier; vision + embedding are pre-spawned
    # by the launcher and pinned (the client adopts them on first
    # ensure_loaded).
    state.llama_cpp = LlamaCppClient()

    clients = {"llama_cpp": state.llama_cpp}

    async def _llama_load(tier, free_vram_gb=None, variant=None, live_user_text=""):
        # Resolve to the variant-effective tier so build_argv sees the
        # right model_tag / gguf_path / vram_estimate_gb / draft fields.
        effective = tier.resolve_variant(variant) if variant else tier
        await state.llama_cpp.ensure_loaded(
            effective,
            free_vram_gb=free_vram_gb,
            live_user_text=live_user_text,
        )

    async def _llama_unload(tier):
        await state.llama_cpp.unload(tier)

    state.scheduler = VRAMScheduler(
        config=state.config,
        loaders={"llama_cpp": _llama_load},
        unloaders={"llama_cpp": _llama_unload},
    )
    await state.scheduler.start()

    # Phase 6: threads the tool registry into the orchestrator so workers
    # can call tools within subtasks.
    state.orchestrator = Orchestrator(state.config, state.scheduler, clients, tools=state.tools)

    # Wire RAG/memory's embedding pipeline to the per-tier llama.cpp client.
    rag.configure_embedding(state.config, state.llama_cpp)

    logger.info("Ready. Tiers: %s", list(state.config.models.tiers))

    # Auto-pull missing GGUFs in the background. The launcher already
    # does this on -Start, but if the user starts the backend any other
    # way (admin GUI, Docker, dev `uvicorn`), or removed a file by hand,
    # the running backend should still self-heal toward the configured
    # tier list. Spawn the resolver as a child process so a slow
    # multi-GB download doesn't block startup or hold the event loop.
    if not state.airgap.enabled and os.getenv("OFFLINE", "").strip() not in ("1", "true", "yes"):
        asyncio.create_task(_auto_pull_missing_tiers())

    # Self-diagnostics — results go to the application log only (lai.diagnostics logger).
    # OK results are DEBUG-level (silent at default INFO log level).
    # WARN/FAIL results are WARNING/ERROR so operators see them in logs.
    await run_startup_diagnostics(
        db_path=str(db.DB_PATH),
        cfg=state.config,
        registry=state.tools,
        qdrant_url=os.getenv("QDRANT_URL", "http://localhost:6333"),
        redis_url=state.config.concurrency.redis_url,
        web_search_provider=os.getenv("WEB_SEARCH_PROVIDER", "ddg"),
    )

    try:
        yield
    finally:
        await state.scheduler.stop()
        await state.llama_cpp.stop_all()
        if state.redis is not None:
            try:
                await state.redis.aclose()
            except Exception:
                pass


app = FastAPI(title="Local AI Stack Backend", lifespan=lifespan)
app.include_router(admin.router)

# Serve the minimal vanilla-JS chat UI at / so cloudflared fronting
# chat.mylensandi.com lands on a real page. No build step; no SPA.
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

_static_dir = Path(__file__).resolve().parent / "static"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

    @app.get("/", include_in_schema=False)
    async def chat_index():
        path = _static_dir / "chat.html"
        if not path.exists():
            return Response(status_code=404)
        return FileResponse(str(path))


# CORS — restrict to ALLOWED_ORIGINS (comma-separated). In production the
# Cloudflare Tunnel hostname is set via setup-cloudflared.sh. Local dev
# defaults to wildcard.
#
# Browsers reject `Access-Control-Allow-Origin: *` when the response also
# carries `Access-Control-Allow-Credentials: true`, so emitting both is
# never useful — Starlette's startup diagnostics (`security.cors`) flag
# the combination as a hard FAIL. Auto-disable credentials when the
# operator hasn't pinned origins; re-enable as soon as they do.
_allowed_origins = [
    o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()
]
_cors_allow_credentials = _allowed_origins != ["*"]
if not _cors_allow_credentials:
    logger.warning(
        "CORS origin is wildcard ('*'). Disabling allow_credentials so the "
        "config is browser-valid. Set ALLOWED_ORIGINS to your Cloudflare "
        "hostname (e.g. 'https://chat.example.com') to re-enable cookies."
    )
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=_cors_allow_credentials,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["content-type"],
    max_age=600,
)

# Host-gate must be LAST-added so it's OUTERMOST (Starlette middleware
# runs in reverse registration order). Rejections short-circuit before
# CORS preflight and before any handler runs.
from .middleware.host_gate import HostGateMiddleware  # noqa: E402
app.add_middleware(HostGateMiddleware)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Attach HSTS and content-type protection headers to every response.
    Only meaningful when served behind HTTPS (Cloudflare Tunnel)."""
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    if os.getenv("PUBLIC_BASE_URL", "").startswith("https://"):
        resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return resp


# ── Routes ────────────────────────────────────────────────────────────────

@app.get("/healthz")
async def healthz():
    """Liveness for the launcher health-wait. Probes the embedding tier
    (always pre-spawned) and Qdrant; chat tiers cold-spawn on first
    request and are not part of this gate."""
    embedding_ok = True
    qdrant_ok = True
    try:
        import httpx as _httpx
        emb_tier = state.config.models.tiers.get("embedding")
        if emb_tier is not None:
            async with _httpx.AsyncClient(timeout=2.0) as c:
                r = await c.get(emb_tier.resolved_endpoint().rstrip("/") + "/models")
                embedding_ok = r.status_code == 200
    except Exception:
        embedding_ok = False
    try:
        import httpx as _httpx
        async with _httpx.AsyncClient(timeout=2.0) as c:
            r = await c.get(os.getenv("QDRANT_URL", "http://localhost:6333") + "/healthz")
            qdrant_ok = r.status_code == 200
    except Exception:
        qdrant_ok = False

    all_ok = embedding_ok and qdrant_ok
    status = "ok" if all_ok else "degraded"
    return {
        "ok": all_ok,
        "status": status,
        "services": {"embedding": embedding_ok, "qdrant": qdrant_ok},
    }


@app.get("/v1/models", response_model=ModelsListResponse)
async def list_models():
    """Return user-selectable chat tiers as virtual OpenAI-compatible models.

    Skips any tier whose `role` is "embedding" — the always-on embedding
    server is RAG infrastructure, not a chat tier the user picks. This
    keeps the GUI's tier dropdown clean.
    """
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
        if tier.role != "embedding"
    ])


@app.get("/vram")
async def vram_status():
    return await state.scheduler.status()


@app.get("/resolved-models")
async def resolved_models():
    """Returns data/resolved-models.json — written by backend.model_resolver
    before each -Start. Native GUI reads this to show the tier status tab.

    Annotates each tier with `available: bool` based on whether the
    GGUF actually exists on disk (the manifest is written eagerly when
    the resolver decides which file to fetch, before the pull finishes,
    so `gguf_path` being present does NOT mean the file is downloaded).
    The chat UI uses this to disable tier options that are still
    mid-download.
    """
    data_dir = Path(os.getenv("LAI_DATA_DIR") or Path(__file__).resolve().parent.parent / "data")
    path = data_dir / "resolved-models.json"
    empty = {"tiers": {}, "resolved_at": 0, "offline": False, "cached": False}
    if not path.exists():
        return empty
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty
    for tier_name, info in (data.get("tiers") or {}).items():
        gguf = info.get("gguf_path")
        info["available"] = bool(gguf and Path(gguf).exists())
    return data


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


@app.get("/system")
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


@app.get("/tools")
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


@app.get("/airgap")
@app.get("/api/airgap")
async def airgap_status():
    """Public-ish airgap state. Any signed-in user can read this so the
    chat UI can render a banner. Writes are admin-only (see /admin/airgap).

    Exposed at both paths: `/airgap` (pre-Phase-5 path, kept for
    back-compat) and `/api/airgap` (what the Qt GUI + host-gate
    middleware expect)."""
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
    except Exception:
        mem_hits = []
    try:
        rag_hits = await rag.retrieve(user["id"], last, k=3)
    except Exception:
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

    # Rate-limit by user ID (falls back to client IP for anonymous).
    rate_key = str(user["id"]) if user else (
        request.headers.get("x-forwarded-for") or
        (request.client.host if request.client else "anon")
    )
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
    # If the router picked a variant (e.g. /coder big -> '80b'), point the
    # request payload at the variant-effective tier — same llama-server
    # endpoint, but build_argv-relevant fields (model_tag, draft, vram)
    # come from the variant. The scheduler has already loaded the right
    # variant by this point via decision.variant.
    if decision.variant:
        tier = tier.resolve_variant(decision.variant)
    client = state.llama_cpp

    # Extract the latest user message text (post-slash-stripping) for the
    # residency planner's complexity heuristic. Truncate to 4 KB so a
    # pathologically large message doesn't cost CPU in plan_residency —
    # the planner only needs a representative sample of the request.
    user_text_for_planner = last_user_text(req.messages)[:4096]

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
    variant: str | None = None,
    live_user_text: str = "",
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

    def _ev_type(ev: dict) -> str:
        """Forward scheduler events with their original type when set
        (e.g. 'tier.loading'), else default to 'queue' for queue
        position updates. Lets the chat UI label them differently."""
        return str(ev.get("type") or "queue")

    acquire_task = asyncio.create_task(
        scheduler.acquire(
            tier_id, on_event,
            variant=variant,
            live_user_text=live_user_text,
        ),
    )
    try:
        while not acquire_task.done():
            try:
                ev = await asyncio.wait_for(evs.get(), timeout=0.25)
            except asyncio.TimeoutError:
                continue
            yield _agent_event_sse(
                AgentEvent(type=_ev_type(ev), data=ev), model_id,
            )
        # Drain any events that arrived after acquisition.
        while not evs.empty():
            try:
                ev = evs.get_nowait()
            except asyncio.QueueEmpty:
                break
            yield _agent_event_sse(
                AgentEvent(type=_ev_type(ev), data=ev), model_id,
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
    from .backends.llama_cpp import _messages_to_payload as _llama_msgs
    msg_payload = _llama_msgs(req.messages)

    tool_schemas: list[dict] | None = None
    if req.tools is None:
        # Per-request whitelist via the chat composer's 🔧 Tools popover.
        # Three cases:
        #   enabled_tools=None  → NO tools (default). Tool schemas
        #     can swell to 25k+ tokens once the registry has 200+ entries,
        #     which with --parallel 4 + ctx 65k blows past the per-slot
        #     16k window before the user message even hits. Better to let
        #     the user opt in explicitly via the popover.
        #   enabled_tools=[]    → also no tools (matches user intent).
        #   enabled_tools=[…]   → exactly those names.
        if req.enabled_tools:
            enabled = state.tools.all_schemas(
                airgap=airgap.is_enabled(),
                names=req.enabled_tools,
            )
            if enabled:
                tool_schemas = enabled
    else:
        tool_schemas = req.tools

    max_tool_turns = 5
    try:
        for turn in range(max_tool_turns + 1):
            # Acquire a slot on the tier's loaded model, forwarding any
            # queue-progress events to the client while we wait.
            async for sse in _reserve_with_sse(
                state.scheduler, decision.tier_name, model_id,
                variant=decision.variant,
                live_user_text=user_text_for_planner,
            ):
                yield sse
            accumulator = ToolCallAccumulator()
            try:
                async for chunk in client.chat_stream(
                    tier, msg_payload, think=decision.think,
                    tools=tool_schemas,
                ):
                    for choice in chunk.get("choices", []):
                        delta = choice.get("delta") or {}
                        text = delta.get("content")
                        if text:
                            assembled_text.append(text)
                            yield _openai_chunk(text, model_id)
                        accumulator.feed(delta.get("tool_calls"))
            finally:
                await state.scheduler.release(decision.tier_name)

            tool_calls_accum = accumulator.calls()
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
    asyncio.create_task(_finalize_conversation(req, user, decision, full_text))

    yield _openai_chunk("", model_id, done=True)
    yield "data: [DONE]\n\n"


async def _finalize_conversation(
    req: ChatRequest, user: dict | None, decision, assistant_text: str,
) -> None:
    """Persist the user+assistant messages and periodically distill memory.
    Runs as a background task after the stream completes; never raises."""
    if not user or not req.conversation_id or not assistant_text.strip():
        return
    try:
        conv = await db.get_conversation(req.conversation_id, user["id"])
        if not conv:
            return

        # Save the trailing user message + the assistant reply.
        from .router import last_user_text
        user_text = last_user_text(req.messages)
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
                user["id"], req.conversation_id, state.llama_cpp, versatile_tier,
                airgap=conv_airgap,
            )
    except Exception:
        logger.exception("Post-stream finalization failed")


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

@app.post("/auth/login", response_model=LoginResponse)
async def auth_login(body: LoginRequest, request: Request):
    """Username + password login. Sets the `lai_session` JWT cookie.

    Constant-time on failure (see `auth.authenticate`) so timing doesn't
    leak whether the username exists.
    """
    cfg = state.config.auth
    user = await auth.authenticate(body.username, body.password)
    if not user:
        raise HTTPException(401, "Invalid username or password")
    session_token = auth.issue_session_token(user["id"], cfg)
    resp = JSONResponse(
        LoginResponse(
            ok=True,
            is_admin=bool(user.get("is_admin")),
            username=user["username"],
        ).model_dump()
    )
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


@app.post("/auth/change-password")
async def auth_change_password(
    body: ChangePasswordRequest,
    user: dict = Depends(auth.current_user),
):
    from . import passwords as _pw
    if not _pw.verify_password(body.current_password, user.get("password_hash") or ""):
        raise HTTPException(401, "Current password is incorrect")
    if not body.new_password or len(body.new_password) < 8:
        raise HTTPException(400, "New password must be at least 8 characters")
    await db.set_user_password(user["id"], _pw.hash_password(body.new_password))
    return {"ok": True}


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


# ── Chat attachments (per-message, ephemeral) ───────────────────────────
#
# Different from /rag/upload: chat attachments are scoped to the next
# chat turn (and at most a handful of follow-ups). They live under
# data/uploads/<user_id>/<id>.<ext> and the chat composer surfaces them
# as removable chips. The chat handler picks them up via
# req.attachment_ids — it inlines text content into the user message and
# sends image bytes through to the vision tier when one is selected.
import secrets as _secrets

_ATTACH_MAX_BYTES = 20 * 1024 * 1024   # 20 MB per file, like /rag/upload


def _attachments_dir(user_id: int) -> Path:
    base = Path(os.getenv("LAI_DATA_DIR") or
                Path(__file__).resolve().parent.parent / "data")
    d = base / "uploads" / str(user_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


@app.post("/api/chat/upload")
async def chat_upload(
    file: UploadFile = File(...),
    user: dict = Depends(auth.current_user),
):
    """Stash a file for the user's next chat turn. Returns an opaque id
    the chat composer attaches to the next /v1/chat/completions request
    via attachment_ids=[...]. Files are NOT indexed into RAG (use
    /rag/upload for that)."""
    content = await file.read()
    if not content:
        raise HTTPException(400, "Empty file")
    if len(content) > _ATTACH_MAX_BYTES:
        raise HTTPException(413, f"File too large (>{_ATTACH_MAX_BYTES // (1024*1024)} MB)")
    aid = _secrets.token_urlsafe(12)
    # Preserve a sane suffix so downstream code can sniff text-vs-image
    # without re-reading the file. We don't trust the original name for
    # disk path; only the suffix.
    fname = (file.filename or "upload").lower()
    ext = Path(fname).suffix[:8] or ""
    if ext and not re.fullmatch(r"\.[a-z0-9]+", ext):
        ext = ""
    dest = _attachments_dir(user["id"]) / f"{aid}{ext}"
    dest.write_bytes(content)
    return {
        "id": aid,
        "name": file.filename or "upload",
        "size": len(content),
        "content_type": file.content_type or "application/octet-stream",
    }


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
    try:
        ingest = await rag.ingest_document(
            user["id"], file.filename or "upload", content, mime=file.content_type,
        )
    except Exception as e:
        logger.exception("RAG ingest failed")
        raise HTTPException(500, f"Ingest failed: {e}")

    # Persist doc metadata so the UI can list + delete.
    now = time.time()
    async with db.get_conn() as c:
        import json as _json
        await c.execute(
            "INSERT INTO rag_docs (user_id, filename, mime_type, size_bytes, chunk_count, qdrant_ids, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                user["id"], file.filename, file.content_type, len(content),
                ingest["chunks"], _json.dumps(ingest["qdrant_ids"]), now,
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
    import json as _json
    async with db.get_conn() as c:
        row = await (await c.execute(
            "SELECT id, qdrant_ids FROM rag_docs WHERE id = ? AND user_id = ?",
            (doc_id, user["id"]),
        )).fetchone()
        if not row:
            raise HTTPException(404, "Document not found")
        ids = _json.loads(row["qdrant_ids"] or "[]")
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
