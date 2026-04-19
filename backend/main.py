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

from . import auth, db
from .backends.llama_cpp import LlamaCppClient
from .backends.ollama import OllamaClient
from .config import AppConfig, CompiledSignals
from .orchestrator import Orchestrator
from .router import route
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
from .vram_scheduler import VRAMScheduler, VRAMExhausted


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


state = AppState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Loading config…")
    state.config = AppConfig.load()
    state.signals = state.config.compile_signals()
    app.state.app_config = state.config       # for auth dependencies

    logger.info("Initialising database…")
    await db.init_db()

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

    state.orchestrator = Orchestrator(state.config, state.scheduler, clients)

    logger.info("Ready. Tiers: %s", list(state.config.models.tiers))
    try:
        yield
    finally:
        await state.scheduler.stop()


app = FastAPI(title="Local AI Stack Backend", lifespan=lifespan)

# CORS — tightened in Phase 6 to the Cloudflare hostname
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("ALLOWED_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatRequest, request: Request):
    """OpenAI-compatible chat endpoint. Always streams (SSE) — Open WebUI
    and our custom frontend both support SSE."""
    try:
        decision, req = route(req, state.config, state.signals)
    except KeyError as e:
        raise HTTPException(404, str(e))

    logger.info(
        "route: tier=%s think=%s multi=%s specialist=%s slash=%s",
        decision.tier_name, decision.think, decision.multi_agent,
        decision.specialist_reason, decision.slash_commands_applied,
    )

    tier = state.config.models.tiers[decision.tier_name]
    client = state.ollama if tier.backend == "ollama" else state.llama_cpp

    if decision.multi_agent:
        return StreamingResponse(
            _multi_agent_sse(req, decision),
            media_type="text/event-stream",
        )

    return StreamingResponse(
        _single_agent_sse(req, decision, client, tier),
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


async def _single_agent_sse(
    req: ChatRequest,
    decision,
    client,
    tier,
) -> AsyncIterator[str]:
    model_id = f"tier.{decision.tier_name}"

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

    try:
        async with state.scheduler.reserve(decision.tier_name):
            if tier.backend == "ollama":
                async for chunk in client.chat_stream(
                    tier, req.messages, think=decision.think,
                    keep_alive=state.config.vram.ollama.keep_alive_pinned,
                ):
                    msg = chunk.get("message") or {}
                    text = msg.get("content")
                    if text:
                        yield _openai_chunk(text, model_id)
                    if chunk.get("done"):
                        break
            else:  # llama_cpp
                async for chunk in client.chat_stream(
                    tier, req.messages, think=decision.think,
                ):
                    for choice in chunk.get("choices", []):
                        delta = choice.get("delta") or {}
                        text = delta.get("content")
                        if text:
                            yield _openai_chunk(text, model_id)
    except VRAMExhausted as e:
        yield _agent_event_sse(
            AgentEvent(type="error", data={"message": str(e), "kind": "vram_exhausted"}),
            model_id,
        )
    except Exception as e:
        logger.exception("Single-agent stream failed")
        yield _agent_event_sse(
            AgentEvent(type="error", data={"message": str(e)}),
            model_id,
        )

    yield _openai_chunk("", model_id, done=True)
    yield "data: [DONE]\n\n"


async def _multi_agent_sse(req: ChatRequest, decision) -> AsyncIterator[str]:
    model_id = f"tier.{decision.tier_name}"

    yield _agent_event_sse(
        AgentEvent(type="route.decision", data={
            "tier": decision.tier_name,
            "think": decision.think,
            "multi_agent": True,
            "slash_commands_applied": decision.slash_commands_applied,
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

    await auth.check_rate_limits(email, cfg)

    expiry_s = cfg.magic_link.expiry_minutes * 60
    client_ip = request.headers.get("x-forwarded-for", "") or (request.client.host if request.client else "")
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

    # Redirect to the app root with the session cookie set.
    redirect_to = os.getenv("PUBLIC_BASE_URL", "/")
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
    rows = await db.list_conversations(user["id"])
    return ConversationListResponse(data=[ConversationSummary(**r) for r in rows])


@app.post("/chats", response_model=ConversationSummary)
async def create_chat(
    body: ConversationUpdate,
    user: dict = Depends(auth.current_user),
):
    conv = await db.create_conversation(
        user["id"],
        title=body.title or "New chat",
        tier=body.tier,
    )
    return ConversationSummary(**conv)


@app.get("/chats/{conv_id}", response_model=ConversationWithMessages)
async def get_chat(conv_id: int, user: dict = Depends(auth.current_user)):
    conv = await db.get_conversation(conv_id, user["id"])
    if not conv:
        raise HTTPException(404, "Conversation not found")
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
    ok = await db.update_conversation(conv_id, user["id"], title=body.title, tier=body.tier)
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


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception")
    return JSONResponse(status_code=500, content={"detail": str(exc)})
