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

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from .backends.llama_cpp import LlamaCppClient
from .backends.ollama import OllamaClient
from .config import AppConfig, CompiledSignals
from .orchestrator import Orchestrator
from .router import route
from .schemas import (
    AgentEvent,
    ChatRequest,
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


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception")
    return JSONResponse(status_code=500, content={"detail": str(exc)})
