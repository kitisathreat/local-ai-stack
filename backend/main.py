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

from . import admin, airgap, auth, db, history_store, kv_cache_manager, memory, metrics, rag
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
    TierVariantInfo,
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
    # In-process bank for context segments evicted by the KV pressure
    # manager so a later turn can recall them.
    spill_store: kv_cache_manager.SpillStore
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
    """Log which tiers are missing on disk; do NOT spawn the resolver.

    Originally this spawned `model_resolver resolve --pull --tier X` per
    missing tier so a backend started outside the launcher could self-heal
    its model files. That had a destructive side effect: the resolver
    rewrites resolved-models.json with only the tiers it was asked about,
    wiping the cached entries for every other tier. After PR #149 added
    `reasoning_max` (an opt-in 120 GB GPT-OSS tier nobody downloads by
    default), every backend startup auto-pulled it and wiped the manifest
    — leaving the next chat request to fail with `tier 'X' has no
    gguf_path` even though the .gguf was on disk.

    The launcher's `-Start` already runs the resolver across every
    configured tier (LocalAIStack.ps1 line ~499), and refresh-backend.ps1
    always uses the launcher, so this self-heal path was redundant.
    Logging the missing list keeps the operator-visible signal without
    the side effect.
    """
    try:
        models_dir = Path(os.getenv("LAI_DATA_DIR") or
                          Path(__file__).resolve().parent.parent / "data") / "models"
        configured = list((state.config.models.tiers or {}).keys())
        missing = [t for t in configured if not (models_dir / f"{t}.gguf").exists()]
        if not missing:
            return
        logger.warning(
            "Tier GGUFs missing on disk: %s. Run `pwsh .\\LocalAIStack.ps1 -Start` "
            "(or `python -m backend.model_resolver resolve --pull` from the repo "
            "root) to fetch them. Auto-pull is disabled to avoid corrupting the "
            "resolved-models manifest.",
            ", ".join(missing),
        )
    except Exception as exc:
        logger.warning("Missing-tier check failed: %s", exc)


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

    state.spill_store = kv_cache_manager.SpillStore(
        max_entries_per_conv=state.config.vram.kv_cache.max_spill_entries_per_conv,
    )

    # Optional Redis client for cross-worker rate limiting.
    state.redis = await _init_redis(state.config)
    rate_limiter.configure(
        per_minute=state.config.auth.rate_limits.requests_per_minute_per_user,
        per_day=state.config.auth.rate_limits.requests_per_day_per_user,
        redis_client=state.redis,
    )

    logger.info("Discovering tools…")
    # Default to repo-relative paths (resolved from this file's location) so
    # the registry works regardless of how the backend was launched. The
    # `/app/*` Docker-era defaults are dead weight on native Windows and
    # caused silent registry-empty bugs whenever LAI_TOOLS_DIR didn't
    # propagate from the launcher's parent shell.
    _repo_root = Path(__file__).resolve().parent.parent
    tools_dir = Path(os.getenv("LAI_TOOLS_DIR") or (_repo_root / "tools"))
    config_dir = Path(os.getenv("LAI_CONFIG_DIR") or (_repo_root / "config"))
    state.tools = build_registry(tools_dir=tools_dir, config_dir=config_dir)
    logger.info("Tool registry ready: %d tools (from %s)", len(state.tools.tools), tools_dir)

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

    # Variant on-disk check: a variant is `available` only when its
    # gguf_path resolves to an actual file. Lets the UI grey-out
    # variants that haven't been pulled yet (e.g. coding_80b before
    # the user runs -CheckUpdates).
    def _variant_available(tier_name: str, variant_id: str) -> bool:
        try:
            t = tiers[tier_name].resolve_variant(variant_id)
            p = getattr(t, "gguf_path", None)
            return bool(p and Path(p).exists())
        except Exception:
            return False

    out: list[TierInfo] = []
    for name, tier in tiers.items():
        if tier.role == "embedding":
            continue
        variants: list[TierVariantInfo] = []
        for vid, vcfg in (tier.variants or {}).items():
            variants.append(TierVariantInfo(
                id=vid,
                name=getattr(vcfg, "model_tag", None) or vid,
                vram_estimate_gb=getattr(vcfg, "vram_estimate_gb", None),
                available=_variant_available(name, vid),
            ))
        out.append(TierInfo(
            id=f"tier.{name}",
            name=tier.name,
            description=tier.description,
            backend=tier.backend,
            context_window=tier.context_window,
            think_supported=tier.think_supported,
            vram_estimate_gb=tier.vram_estimate_gb,
            variants=variants,
            default_variant=tier.default_variant if variants else None,
        ))
    return ModelsListResponse(data=out)


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
    reg = state.tools
    return {
        "airgap": ag,
        "groups": _serialize_taxonomy(reg),
        "data": [
            {
                "name": t.name,
                "description": t.schema.get("function", {}).get("description", ""),
                "default_enabled": t.default_enabled,
                "requires_service": t.requires_service,
                "airgap_blocked": ag and not reg.is_airgap_safe(t.name),
                "tier": t.tier,
                "tier_title": reg.tier_title(t.tier),
                "group": t.group,
                "group_title": reg.group_title(t.group),
                "subgroup": t.subgroup,
                "subgroup_title": reg.group_title(t.group, t.subgroup),
            }
            for t in reg.tools.values()
        ],
    }


def _serialize_taxonomy(reg) -> list[dict]:
    """Materialise the tool taxonomy in display order so the frontend
    doesn't have to re-sort. Empty subgroups are dropped."""
    # Build {tier: {group: {subgroup: [tool_name, ...]}}}
    tree: dict[str, dict[str, dict[str, list[str]]]] = {}
    for t in reg.tools.values():
        tree.setdefault(t.tier, {}).setdefault(t.group, {}).setdefault(t.subgroup, []).append(t.name)
    out: list[dict] = []
    for tier in sorted(tree, key=reg.tier_order):
        tier_node = {
            "tier": tier,
            "title": reg.tier_title(tier),
            "groups": [],
        }
        for group in sorted(tree[tier], key=lambda g: reg.group_order(g)):
            group_node = {
                "group": group,
                "title": reg.group_title(group),
                "subgroups": [],
            }
            for sub in sorted(tree[tier][group], key=lambda s: reg.group_order(group, s)):
                group_node["subgroups"].append({
                    "subgroup": sub,
                    "title": reg.group_title(group, sub),
                    "tools": tree[tier][group][sub],
                })
            tier_node["groups"].append(group_node)
        out.append(tier_node)
    return out


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


def _apply_kv_pressure(
    messages: list[ChatMessage],
    tier,
    *,
    conversation_id: int | None = None,
) -> tuple[list[ChatMessage], dict | None]:
    """Prune low-importance segments before dispatch when KV pressure is high.

    Returns the (possibly shaped) message list plus a spill event dict for
    SSE surfacing, or None when no action was taken. Evicted segments are
    stashed in `state.spill_store` so a later turn can recall them.
    """
    cfg = state.config.vram.kv_cache
    if not cfg.enable or not messages:
        return messages, None
    weights = kv_cache_manager.ScoringWeights(
        recency=cfg.weights.recency,
        relevance=cfg.weights.relevance,
        role_prior=cfg.weights.role_prior,
        size_penalty=cfg.weights.size_penalty,
        hot_window=cfg.weights.hot_window,
    )
    assessment = kv_cache_manager.assess_and_plan(
        messages,
        kv_budget_tokens=tier.context_window,
        reserve_for_output=cfg.reserve_output_tokens,
        spill_trigger_fraction=cfg.spill_trigger_fraction,
        weights=weights,
    )
    if assessment.plan is None or not assessment.plan.spilled:
        return messages, None
    if conversation_id is not None:
        state.spill_store.stash(conversation_id, assessment.plan.spilled)
    shaped = kv_cache_manager.apply_plan(messages, assessment.plan)
    return shaped, assessment.plan.as_event(tier_id=tier.name)


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
        producer = _wrap(_multi_agent_sse(req, decision, options=req.multi_agent_options))
    else:
        producer = _wrap(_single_agent_sse(
            req, decision, client, tier, user=user,
            user_text_for_planner=user_text_for_planner,
        ))

    # Honor OpenAI's `stream` semantics. Resolution order:
    #   1. Explicit `stream: true|false` in the request body always wins.
    #   2. When omitted, infer from the Accept header — `text/event-stream`
    #      means the client wants SSE; everything else gets JSON.
    # OpenAI's actual default is `stream: false`, so this matches every
    # tool that doesn't set the header (lm-eval, openai SDK, HF
    # InferenceClient, smolagents, Ragas). The chat UI sends
    # `Accept: text/event-stream` and gets SSE either way.
    if req.stream is None:
        accept = (request.headers.get("accept") or "").lower()
        wants_stream = "text/event-stream" in accept
    else:
        wants_stream = bool(req.stream)

    if not wants_stream:
        return await _to_non_streaming_response(
            producer, model_id=f"tier.{decision.tier_name}",
        )

    # OpenAI's stream_options.include_usage — when set, append a final
    # chunk with empty choices + usage stats just before [DONE]. Closes
    # umbrella issue #154 sub-item.
    include_usage = bool(
        (req.stream_options or {}).get("include_usage")
    )
    if include_usage:
        producer = _with_usage_chunk(producer, model_id=f"tier.{decision.tier_name}", req=req)
    return StreamingResponse(producer, media_type="text/event-stream")


async def _with_usage_chunk(
    inner: AsyncIterator[str], *, model_id: str, req: ChatRequest,
) -> AsyncIterator[str]:
    """Wrap an SSE stream so the final chunk before `[DONE]` carries an
    OpenAI-style usage object. Same approximate token math as the metrics
    path (whitespace split — close enough for billing trends, no second
    tokenizer pass on the hot loop)."""
    out_text: list[str] = []
    pending_done: str | None = None
    async for chunk in inner:
        if chunk.strip() == "data: [DONE]":
            # Hold the [DONE] sentinel; emit usage chunk first.
            pending_done = chunk
            continue
        if chunk.startswith("data:") and '"content"' in chunk:
            try:
                payload = json.loads(chunk[5:].strip())
                t = ((payload.get("choices") or [{}])[0].get("delta") or {}).get("content")
                if isinstance(t, str):
                    out_text.append(t)
            except Exception:
                pass
        yield chunk
    # Emit the usage chunk just before [DONE].
    full = "".join(out_text)
    tokens_out = max(1, len(full.split())) if full else 0
    prompt_words = sum(
        len(m.content.split()) for m in req.messages if isinstance(m.content, str)
    )
    usage_payload = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:16]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model_id,
        "choices": [],
        "usage": {
            "prompt_tokens": prompt_words,
            "completion_tokens": tokens_out,
            "total_tokens": prompt_words + tokens_out,
        },
    }
    yield f"data: {json.dumps(usage_payload)}\n\n"
    if pending_done:
        yield pending_done


async def _to_non_streaming_response(
    sse_producer: AsyncIterator[str],
    *,
    model_id: str,
) -> JSONResponse:
    """Drain an SSE producer and return a single ChatCompletion JSON object.

    Treats `event: agent` chunks (route.decision, queue.update, errors,
    etc.) as out-of-band telemetry: error events surface as a 502 with
    the message; everything else is dropped. OpenAI-shaped `data: {...}`
    chunks are accumulated by `delta.content` into the response message.
    """
    text_chunks: list[str] = []
    finish_reason = "stop"
    last_event: str | None = None
    err_message: str | None = None
    # Aggregated logprobs across the streamed deltas. llama-server emits
    # one {"content": [...tokens...]} per chunk; concatenating the
    # `content` arrays yields the per-token logprobs for the whole
    # response, in OpenAI's documented non-streaming shape.
    logprobs_tokens: list[dict] = []

    async for raw in sse_producer:
        if raw.startswith("event:"):
            last_event = raw[len("event:"):].strip()
            continue
        if not raw.startswith("data:"):
            if raw == "" or raw == "\n":
                last_event = None
            continue
        body = raw[len("data:"):].strip()
        if body == "[DONE]":
            continue
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            continue

        # Custom agent events use {"type": ..., "data": {...}} shape.
        if last_event == "agent" and isinstance(payload.get("type"), str):
            if payload["type"] == "error":
                err_message = (payload.get("data") or {}).get("message") or "stream error"
            # Drop everything else (route.decision, vram.making_room, etc.)
            continue

        # Standard OpenAI streaming shape: {choices:[{delta:{content:...}}]}
        for choice in payload.get("choices") or []:
            delta = choice.get("delta") or {}
            content = delta.get("content")
            if isinstance(content, str):
                text_chunks.append(content)
            lp = choice.get("logprobs")
            if isinstance(lp, dict):
                tokens = lp.get("content")
                if isinstance(tokens, list):
                    logprobs_tokens.extend(tokens)
            fr = choice.get("finish_reason")
            if isinstance(fr, str):
                finish_reason = fr

    if err_message:
        return JSONResponse(
            status_code=502,
            content={"error": {"message": err_message, "type": "stream_error"}},
        )

    full = "".join(text_chunks)
    # Approximate token counts via whitespace split — matches the metrics
    # path's accuracy and avoids a second tokenizer pass on the hot loop.
    tokens_out = max(1, len(full.split())) if full else 0
    choice_obj: dict = {
        "index": 0,
        "message": {"role": "assistant", "content": full},
        "finish_reason": finish_reason,
    }
    if logprobs_tokens:
        choice_obj["logprobs"] = {"content": logprobs_tokens}
    return JSONResponse({
        "id": f"chatcmpl-{uuid.uuid4().hex[:16]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_id,
        "choices": [choice_obj],
        "usage": {
            "prompt_tokens": 0,    # filled in by future tokenizer hookup
            "completion_tokens": tokens_out,
            "total_tokens": tokens_out,
        },
    })


# ── SSE producers ─────────────────────────────────────────────────────────

def _openai_chunk(
    content: str, model: str, done: bool = False, logprobs: dict | None = None,
) -> str:
    """Format a streaming chunk in OpenAI's SSE shape.

    `logprobs` (when provided) is forwarded into the choice as-is — the
    upstream llama-server already ships an OpenAI-compatible logprobs
    object per chunk: {"content": [{"token", "logprob", "bytes",
    "top_logprobs": [...]}, ...]}.
    """
    choice: dict = {
        "index": 0,
        "delta": {} if done else {"content": content},
        "finish_reason": "stop" if done else None,
    }
    if logprobs is not None:
        choice["logprobs"] = logprobs
    payload = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:16]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [choice],
    }
    return f"data: {json.dumps(payload)}\n\n"


def _response_format_to_extra_options(response_format: dict | None) -> dict | None:
    """Translate OpenAI's response_format field into llama-server's
    request-level constraint fields. Returns extra_options to merge into
    the chat payload, or None when no constraint is requested.

    Two OpenAI shapes supported:
      {"type": "json_object"}                           → any JSON
      {"type": "json_schema", "json_schema": {...}}     → schema-constrained

    For json_schema mode, OpenAI nests the actual schema under
    `json_schema.schema` (alongside name/description). llama-server's
    `json_schema` field expects the schema object itself, so we unwrap.
    """
    if not response_format:
        return None
    rtype = response_format.get("type")
    if rtype == "json_object":
        # `type: object` is the broadest JSON-object schema. llama-server
        # treats an empty {} as "no constraint" at the request level
        # (despite the CLI doc note), so we make the constraint explicit.
        # additionalProperties:true keeps it permissive — any keys, any
        # values — same effect as OpenAI's documented json_object mode.
        return {"json_schema": {"type": "object", "additionalProperties": True}}
    if rtype == "json_schema":
        wrapper = response_format.get("json_schema") or {}
        # OpenAI wraps as {"name": "...", "schema": {...}}; some clients
        # send the schema flat. Accept both.
        schema = wrapper.get("schema") if isinstance(wrapper, dict) else None
        if schema is None and isinstance(wrapper, dict) and "type" in wrapper:
            schema = wrapper
        if isinstance(schema, dict):
            return {"json_schema": schema}
    return None


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
    user_text_for_planner: str = "",
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

    # Shape the conversation if it would push the tier's KV slot into RAM
    # spillover. The manager keeps system prompts, the live user turn, and
    # tool_call/result pairs pinned; only stale assistant chatter and
    # think-block bloat get evicted.
    shaped_messages, spill_event = _apply_kv_pressure(
        req.messages, tier, conversation_id=req.conversation_id,
    )
    if spill_event is not None:
        yield _agent_event_sse(
            AgentEvent(type="kv.spillover", data=spill_event), model_id,
        )

    # Serialize messages for the tool loop (needs to append role=tool entries).
    from .backends.llama_cpp import _messages_to_payload as _llama_msgs
    msg_payload = _llama_msgs(shaped_messages)

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

    # Honor OpenAI's tool_choice. Belt-and-suspenders: filter the tools
    # list client-side AND pass tool_choice to llama-server (which
    # supports it natively in chat completions). The client-side filter
    # is the strong guarantee — even a model that ignores tool_choice
    # can't call a function we didn't send. Closes umbrella #154.
    tool_choice_extra: dict[str, Any] | None = None
    if req.tool_choice is not None and tool_schemas:
        if req.tool_choice == "none":
            # Suppress tools entirely. Model can't call what it can't see.
            tool_schemas = None
        elif isinstance(req.tool_choice, dict):
            # OpenAI shape: {"type":"function","function":{"name":"X"}}
            wanted = (req.tool_choice.get("function") or {}).get("name")
            if wanted:
                tool_schemas = [
                    s for s in tool_schemas
                    if (s.get("function") or {}).get("name") == wanted
                ]
            tool_choice_extra = {"tool_choice": req.tool_choice}
        elif req.tool_choice in ("auto", "required"):
            # Forward verbatim — llama-server interprets these.
            tool_choice_extra = {"tool_choice": req.tool_choice}

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
            # Build the chat_stream extra_options merge: response_format
            # constraint + logprobs flags + tool_choice. All three can be
            # set on the same request and llama-server combines them.
            extra_options: dict = {}
            rf_extra = _response_format_to_extra_options(req.response_format)
            if rf_extra:
                extra_options.update(rf_extra)
            if req.logprobs:
                extra_options["logprobs"] = True
                if req.top_logprobs is not None:
                    extra_options["top_logprobs"] = int(req.top_logprobs)
            if tool_choice_extra:
                extra_options.update(tool_choice_extra)
            try:
                async for chunk in client.chat_stream(
                    tier, msg_payload, think=decision.think,
                    tools=tool_schemas,
                    extra_options=(extra_options or None),
                ):
                    for choice in chunk.get("choices", []):
                        delta = choice.get("delta") or {}
                        text = delta.get("content")
                        # Pass logprobs through verbatim — llama-server
                        # already ships them in OpenAI's documented shape.
                        chunk_logprobs = choice.get("logprobs") if req.logprobs else None
                        if text:
                            assembled_text.append(text)
                            yield _openai_chunk(text, model_id, logprobs=chunk_logprobs)
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

        # Auto-title on the first assistant turn — only when the user
        # hasn't already renamed the chat (default sentinel "New chat").
        # Spawned as its own task so a slow title-gen doesn't delay the
        # rest of finalization. Best-effort: failures are logged and
        # the chat keeps the default title.
        if asst_count == 1 and (conv.get("title") or "").strip().lower() == "new chat":
            asyncio.create_task(_auto_title_chat(
                req.conversation_id, user["id"], user_text or "", assistant_text,
            ))
    except Exception:
        logger.exception("Post-stream finalization failed")


_TITLE_PROMPT = (
    "Summarize this conversation in 3-6 words for a chat sidebar title. "
    "Output ONLY the title — no quotes, no punctuation at the end, no "
    "prefix like 'Title:'. Use sentence case.\n\n"
    "User: {user}\n"
    "Assistant: {asst}\n\n"
    "Title:"
)


async def _auto_title_chat(
    conv_id: int, user_id: int, user_text: str, assistant_text: str,
) -> None:
    """Generate a short title from the first user/assistant exchange and
    save it on the conversation. Uses the fast tier (cheapest); never
    raises. Truncates inputs so a long first message doesn't blow the
    title-gen budget."""
    try:
        # Trim to keep the title-gen prompt small. The model only needs a
        # representative slice — no point feeding it 10 KB of context.
        u = (user_text or "")[:600].strip()
        a = (assistant_text or "")[:600].strip()
        if not u and not a:
            return
        prompt = _TITLE_PROMPT.format(user=u, asst=a)
        # Fast tier is the cheapest chat model; fall back to versatile
        # only if fast isn't configured (rare).
        tier_id = "fast" if "fast" in state.config.models.tiers else "versatile"
        tier = state.config.models.tiers.get(tier_id)
        if tier is None:
            return
        async with state.scheduler.reserve(tier_id):
            raw = await state.llama_cpp.chat_once(
                tier,
                [ChatMessage(role="user", content=prompt)],
                think=False,
            )
        title = _clean_auto_title(raw)
        if not title:
            return
        ok = await db.update_conversation(conv_id, user_id, title=title)
        if ok:
            logger.info("Auto-titled conv %d -> %r", conv_id, title)
    except Exception as e:
        logger.warning("Auto-title for conv %d failed: %s", conv_id, e)


def _clean_auto_title(raw: str) -> str:
    """Strip quotes, leading 'Title:' prefixes, trailing punctuation, and
    cap at ~60 chars. Returns '' for unusable output (so we leave the
    default title in place rather than save garbage)."""
    if not raw:
        return ""
    # Drop any think-block content (Qwen3 may emit one despite think=False).
    s = re.sub(r"<think>[\s\S]*?</think>", "", raw, flags=re.IGNORECASE).strip()
    # First non-empty line is the title.
    s = next((ln.strip() for ln in s.splitlines() if ln.strip()), "")
    # Strip "Title:" / "Title -" prefixes (case-insensitive).
    s = re.sub(r"^title\s*[:\-—]\s*", "", s, flags=re.IGNORECASE)
    # Strip wrapping quotes.
    s = s.strip().strip("\"'`«»“”").strip()
    # Strip trailing period / ellipsis.
    s = re.sub(r"[\.…]+\s*$", "", s).strip()
    if len(s) > 60:
        s = s[:57].rstrip() + "…"
    # Refuse if the model echoed back the prompt or returned the sentinel.
    if not s or s.lower() in {"new chat", "title", "untitled"}:
        return ""
    return s


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

    # Apply KV pressure shaping at the orchestrator entry. The orchestrator
    # tier carries the full transcript through planning + synthesis, so it
    # benefits most from pruning before dispatch.
    orch_tier_name = state.config.router.multi_agent.orchestrator_tier
    orch_tier = state.config.models.tiers.get(orch_tier_name)
    if orch_tier is not None:
        shaped_messages, spill_event = _apply_kv_pressure(
            req.messages, orch_tier, conversation_id=req.conversation_id,
        )
        if spill_event is not None:
            yield _agent_event_sse(
                AgentEvent(type="kv.spillover", data=spill_event), model_id,
            )
    else:
        shaped_messages = req.messages

    try:
        async for ev in state.orchestrator.run(
            user_message=user_msg,
            conversation=shaped_messages,
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


# ── OpenAI-compat passthrough endpoints ──────────────────────────────────

@app.post("/v1/completions")
async def v1_completions(req: dict, request: Request, user: dict | None = Depends(auth.optional_user)):
    """OpenAI-compatible legacy text completions endpoint.

    Wraps the request as a single user-message chat and reuses the chat
    pipeline (router, scheduler, RAG injection, middleware), then
    unwraps the assistant response into a `text_completion` shape. Most
    modern tools use /v1/chat/completions, but lm-evaluation-harness's
    `local-completions` backend, plus a long tail of older HF tutorials
    and notebooks, default to text completions. Closes umbrella #154.

    Required field: `prompt` (str | list[str]).
    Optional fields forwarded to the chat path: `model`, `temperature`,
    `top_p`, `max_tokens`, `stream`, `stream_options`, `stop`.
    """
    prompt_in = req.get("prompt")
    if prompt_in is None:
        raise HTTPException(400, "Missing required field 'prompt'")

    # text-completions accepts either a single string or a list of strings;
    # we batch internally by sequentially turning each into one chat call.
    prompts: list[str]
    if isinstance(prompt_in, str):
        prompts = [prompt_in]
    elif isinstance(prompt_in, list) and all(isinstance(p, str) for p in prompt_in):
        prompts = prompt_in
    else:
        raise HTTPException(400, "'prompt' must be a string or list of strings")

    # Streaming legacy completions semantics differ from chat (different
    # SSE shape: `text` instead of `delta.content`). Until there's a
    # caller that actually needs that, force non-streaming and surface
    # a 400 if the client asks for stream — better than silently
    # returning a chat-shaped stream.
    if req.get("stream") is True:
        raise HTTPException(
            400,
            "Streaming /v1/completions is not implemented — "
            "use /v1/chat/completions for streaming responses.",
        )

    chat_body: dict = {
        "model": req.get("model") or state.config.models.default,
        "messages": [],   # filled per-prompt below
        "stream": False,
    }
    for k in ("temperature", "top_p", "max_tokens", "stop"):
        if k in req and req[k] is not None:
            chat_body[k] = req[k]

    choices: list[dict] = []
    completion_tokens_total = 0
    for idx, p in enumerate(prompts):
        chat_body["messages"] = [{"role": "user", "content": p}]
        chat_req = ChatRequest(**chat_body)
        try:
            decision, chat_req = route(chat_req, state.config, state.signals)
        except KeyError as e:
            raise HTTPException(404, str(e))

        rate_key = str(user["id"]) if user else (
            request.headers.get("x-forwarded-for")
            or (request.client.host if request.client else "anon")
        )
        await rate_limiter.check(rate_key)

        # Reuse the same middleware pipeline as chat so the legacy endpoint
        # gets identical context injection / RAG / clarification semantics.
        inject_system_context(chat_req.messages)
        inject_clarification_instruction(chat_req.messages)
        inject_response_mode(chat_req.messages, chat_req.response_mode, chat_req.plan_text)
        await inject_web_results(chat_req.messages)
        if user:
            await _inject_user_context(chat_req, user)

        tier = state.config.models.tiers[decision.tier_name]
        if decision.variant:
            tier = tier.resolve_variant(decision.variant)

        producer = _single_agent_sse(
            chat_req, decision, state.llama_cpp, tier, user=user,
            user_text_for_planner=last_user_text(chat_req.messages)[:4096],
        )
        # Drain to a single text — same accumulator as the chat path's
        # non-streaming branch, just emitting `text` instead of `message`.
        text_chunks: list[str] = []
        finish_reason = "stop"
        last_event: str | None = None
        err_message: str | None = None
        async for raw in producer:
            if raw.startswith("event:"):
                last_event = raw[len("event:"):].strip()
                continue
            if not raw.startswith("data:"):
                if raw == "" or raw == "\n":
                    last_event = None
                continue
            body = raw[len("data:"):].strip()
            if body == "[DONE]":
                continue
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                continue
            if last_event == "agent" and isinstance(payload.get("type"), str):
                if payload["type"] == "error":
                    err_message = (payload.get("data") or {}).get("message") or "stream error"
                continue
            for ch in payload.get("choices") or []:
                t = (ch.get("delta") or {}).get("content")
                if isinstance(t, str):
                    text_chunks.append(t)
                fr = ch.get("finish_reason")
                if isinstance(fr, str):
                    finish_reason = fr

        if err_message:
            raise HTTPException(502, err_message)
        text = "".join(text_chunks)
        ct = max(1, len(text.split())) if text else 0
        completion_tokens_total += ct
        choices.append({
            "index": idx,
            "text": text,
            "finish_reason": finish_reason,
            "logprobs": None,
        })

    return JSONResponse({
        "id": f"cmpl-{uuid.uuid4().hex[:16]}",
        "object": "text_completion",
        "created": int(time.time()),
        "model": chat_body["model"],
        "choices": choices,
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": completion_tokens_total,
            "total_tokens": completion_tokens_total,
        },
    })


@app.post("/v1/embeddings")
async def v1_embeddings(req: dict, request: Request):
    """OpenAI-compatible embeddings proxy. Forwards to the always-on
    embedding tier llama-server (port 8090). Closes umbrella issue #154
    sub-item: external tools (RAG eval frameworks, vector DB ingestion
    scripts, lm-eval embedding benchmarks) can now hit the public
    `:18000/v1/*` namespace as a drop-in for `api.openai.com`.

    Request body matches OpenAI's spec: {"model": "...", "input": "..." | [...]}.
    `model` is ignored (we always route to the embedding tier).
    """
    emb_tier = state.config.models.tiers.get("embedding")
    if emb_tier is None:
        raise HTTPException(503, "Embedding tier not configured")
    target = emb_tier.resolved_endpoint().rstrip("/") + "/embeddings"
    # Rate-limit by user/IP just like chat — embedders are cheap but
    # batch ingestion can still saturate the slot pool.
    rate_key = (
        request.headers.get("x-forwarded-for")
        or (request.client.host if request.client else "anon")
    )
    await rate_limiter.check(rate_key)
    try:
        async with httpx_async_client() as cx:
            r = await cx.post(target, json=req)
            return JSONResponse(status_code=r.status_code, content=r.json())
    except httpx_module().RequestError as e:
        raise HTTPException(502, f"Embedding upstream error: {e}")


@app.post("/v1/rerank")
async def v1_rerank(req: dict, request: Request):
    """Rerank proxy. Mirrors Cohere's /rerank shape (and llama-server's
    own implementation): {"model": "...", "query": "...", "documents": [...]}.
    Forwards to the dedicated reranker llama-server on :8091.

    Closes umbrella issue #154 sub-item — exposing this means external
    RAG pipelines (Haystack, LangChain, LlamaIndex) can use the local
    reranker without bypassing the public ingress.
    """
    target_url = os.getenv("RERANKER_URL", "http://127.0.0.1:8091").rstrip("/") + "/v1/rerank"
    rate_key = (
        request.headers.get("x-forwarded-for")
        or (request.client.host if request.client else "anon")
    )
    await rate_limiter.check(rate_key)
    try:
        async with httpx_async_client() as cx:
            r = await cx.post(target_url, json=req)
            return JSONResponse(status_code=r.status_code, content=r.json())
    except httpx_module().RequestError as e:
        # Reranker is best-effort — never block the public endpoint.
        raise HTTPException(502, f"Reranker upstream error: {e}")


def httpx_module():
    import httpx as _httpx
    return _httpx


# ── Tier pre-warm ───────────────────────────────────────────────────────
# Pre-warm is OPT-IN: on a 24 GB card, even the "lightweight" versatile
# tier really costs ~15-20 GB once parallel_slots × ctx KV cache + the
# spec-decode draft + llama.cpp scratch buffers are accounted for. Auto-
# warming versatile on every login / page-load / visibility-change meant
# 20 GB of VRAM was tied up before the user even sent a message — and
# any subsequent route to coding/vision/fast had to evict it.
#
# Now: load only the model the user actually targets. The /api/warm
# endpoint still exists for operators on bigger cards; pass an explicit
# `{"tiers": [...]}` body to invoke it. The default body warms nothing.
_DEFAULT_WARM_TIERS: tuple[str, ...] = ()


async def _warm_chat_tiers(tier_ids: tuple[str, ...] | list[str]) -> dict[str, str]:
    """Pre-warm the named chat tiers SEQUENTIALLY. Each tier is acquired
    + immediately released through the VRAM scheduler — that triggers
    the cold-spawn loader if not resident, and is a fast no-op if
    already resident. Errors are swallowed per-tier so one slow load
    doesn't block the others. Returns a per-tier status dict.

    Sequential rather than parallel: cold-spawn allocates transient
    buffers (weights streaming, KV pre-allocation) that peak well above
    the model's steady-state VRAM. Two parallel cold-spawns of mid-
    sized chat tiers (versatile ~6.5 GB steady / ~12 GB peak +
    fast ~7.5 GB steady / ~10 GB peak) collide at ~22 GB peak on a
    24 GB card and trigger the sweeper to evict the first to make room
    for the second. Sequential keeps peak at "first-tier-steady +
    second-tier-peak" ≈ 16.5 GB which fits with headroom."""
    results: dict[str, str] = {}
    for tid in tier_ids:
        if tid not in state.config.models.tiers:
            results[tid] = "unknown"
            continue
        try:
            await state.scheduler.acquire(tid)
            try:
                pass   # acquire+release leaves the model resident
            finally:
                await state.scheduler.release(tid)
            results[tid] = "loaded"
        except QueueFull:
            results[tid] = "queue_full"
        except QueueTimeout:
            results[tid] = "queue_timeout"
        except VRAMExhausted:
            results[tid] = "vram_exhausted"
        except Exception as exc:
            logger.debug("Pre-warm of %s skipped: %s", tid, exc)
            results[tid] = f"error:{type(exc).__name__}"
    return results


@app.post("/api/warm")
async def api_warm(
    body: dict | None = None,
    user: dict | None = Depends(auth.optional_user),
):
    """Trigger a background pre-warm of named chat tiers. Returns
    immediately; the warm itself runs as a background task.

    Body MUST carry `{"tiers": ["versatile", ...]}` — the default
    set is empty so a stray `{}` POST from the chat UI doesn't
    auto-load anything. This matches the "only load the model the
    user is actually using" policy. Anonymous callers are accepted
    because the chat UI may fire this before /auth/login completes
    (race with cookie set)."""
    requested = (body or {}).get("tiers") if isinstance(body, dict) else None
    tiers = tuple(requested) if isinstance(requested, list) and requested else _DEFAULT_WARM_TIERS
    if not tiers:
        return {"ok": True, "warming": [], "user": user["email"] if user else None}
    asyncio.create_task(_warm_chat_tiers(tiers))
    return {"ok": True, "warming": list(tiers), "user": user["email"] if user else None}


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
    # Pre-warm is opt-in (default empty) — see _DEFAULT_WARM_TIERS.
    # Spawning models the user hasn't asked for caused VRAM exhaustion
    # on the 24 GB card; chat tiers cold-spawn on first message instead.
    if _DEFAULT_WARM_TIERS:
        asyncio.create_task(_warm_chat_tiers(_DEFAULT_WARM_TIERS))
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


# ── Per-user preferences ────────────────────────────────────────────────
# Opaque JSON blob owned by the client. Backend never inspects keys —
# stores and returns whatever the client writes. Used today for tool
# toggle persistence (`enabled_tools: ["module.method", ...]`); any
# future per-user UI setting goes here without a backend change.

async def _read_user_prefs(user_id: int) -> dict:
    async with db.get_conn() as c:
        row = await (await c.execute(
            "SELECT preferences FROM users WHERE id = ?", (user_id,),
        )).fetchone()
    if not row:
        return {}
    raw = row["preferences"] or "{}"
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


async def _write_user_prefs(user_id: int, prefs: dict) -> None:
    payload = json.dumps(prefs, separators=(",", ":"))
    async with db.get_conn() as c:
        await c.execute(
            "UPDATE users SET preferences = ? WHERE id = ?",
            (payload, user_id),
        )
        await c.commit()


@app.get("/me/preferences")
async def get_preferences(user: dict = Depends(auth.current_user)):
    """Return the user's preference blob. Always a dict — empty when unset."""
    return {"preferences": await _read_user_prefs(user["id"])}


@app.patch("/me/preferences")
async def patch_preferences(
    body: dict, user: dict = Depends(auth.current_user),
):
    """Shallow-merge the supplied dict into the user's preference blob.
    To delete a key, send `{"key": null}` — null values are stripped on
    write so they don't accumulate. Returns the merged result so the
    client doesn't need to round-trip a follow-up GET."""
    if not isinstance(body, dict):
        raise HTTPException(400, "preferences body must be a JSON object")
    current = await _read_user_prefs(user["id"])
    for k, v in body.items():
        if v is None:
            current.pop(k, None)
        else:
            current[k] = v
    await _write_user_prefs(user["id"], current)
    return {"preferences": current}


# ── Conversations ───────────────────────────────────────────────────────

@app.get("/chats", response_model=ConversationListResponse)
async def list_chats(
    user: dict = Depends(auth.current_user),
    include_archived: bool = False,
    archived_only: bool = False,
):
    """Return conversations matching the current airgap mode.
    By default archived chats are hidden from the sidebar. Pass
    `?archived_only=true` to list only archived (used by the
    "Archived" view) or `?include_archived=true` to list both."""
    if archived_only:
        archived_filter: bool | None = True
    elif include_archived:
        archived_filter = None
    else:
        archived_filter = False
    rows = await db.list_conversations(
        user["id"], airgap=airgap.is_enabled(), archived=archived_filter,
    )
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
        archived=body.archived,
    )
    if not ok:
        raise HTTPException(404, "Conversation not found")
    conv = await db.get_conversation(conv_id, user["id"])
    return ConversationSummary(**conv)


@app.delete("/chats/{conv_id}")
async def delete_chat(
    conv_id: int,
    user: dict = Depends(auth.current_user),
    keep_summary: bool = True,
):
    """Delete a conversation. By default, distill it into a memory entry
    first (`?keep_summary=true`, default) so the user keeps the gist
    without the verbatim transcript. Pass `?keep_summary=false` to
    skip distillation (e.g., for accidental chats with nothing worth
    remembering)."""
    conv = await db.get_conversation(conv_id, user["id"])
    if not conv:
        raise HTTPException(404, "Conversation not found")
    if bool(conv.get("airgap")) != airgap.is_enabled():
        raise HTTPException(404, "Conversation not found (other airgap mode)")
    if keep_summary and conv.get("memory_enabled", True):
        # Best-effort summary — never let memory failure block delete.
        try:
            versatile_tier = state.config.models.tiers.get("versatile")
            if versatile_tier is not None:
                await memory.distill_and_store(
                    user["id"], conv_id, state.llama_cpp, versatile_tier,
                    airgap=bool(conv.get("airgap")),
                )
                logger.info("Pre-delete summary stored for conv %d", conv_id)
        except Exception as e:
            logger.warning("Pre-delete summary failed for conv %d: %s", conv_id, e)
    ok = await db.delete_conversation(conv_id, user["id"])
    return {"ok": True, "summarized": keep_summary, "deleted": ok}


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
