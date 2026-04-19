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

from pathlib import Path

from . import admin, auth, db, memory, metrics, rag
from .backends.llama_cpp import LlamaCppClient
from .backends.ollama import OllamaClient
from .config import AppConfig, CompiledSignals
from .middleware.clarification import (
    format_clarifications,
    inject_clarification_instruction,
)
from .middleware.context import inject_system_context
from .middleware.rate_limit import rate_limiter
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
    tools: ToolRegistry


state = AppState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Loading config…")
    state.config = AppConfig.load()
    state.signals = state.config.compile_signals()
    app.state.app_config = state.config       # for auth dependencies

    logger.info("Initialising database…")
    await db.init_db()

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
    try:
        yield
    finally:
        await state.scheduler.stop()


app = FastAPI(title="Local AI Stack Backend", lifespan=lifespan)
app.include_router(admin.router)

# CORS — restrict to ALLOWED_ORIGINS (comma-separated). In production the
# Cloudflare Tunnel hostname is set via setup-cloudflared.sh. Local dev
# defaults to wildcard. We warn if wildcard+credentials is configured since
# browsers ignore that combination.
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


@app.get("/api/tools")
async def list_tools():
    """List discovered tools. Tool names are `<module>.<method>`."""
    return {
        "data": [
            {
                "name": t.name,
                "description": t.schema.get("function", {}).get("description", ""),
                "default_enabled": t.default_enabled,
                "requires_service": t.requires_service,
            }
            for t in state.tools.tools.values()
        ],
    }


async def _inject_user_context(req: ChatRequest, user: dict) -> None:
    """Prepend RAG + memory context to the system message for a signed-in
    user. Runs inline (needs embeddings before streaming starts)."""
    from .router import last_user_text
    last = last_user_text(req.messages)
    if not last or not last.strip():
        return
    try:
        mem_hits = await memory.retrieve_for_user(user["id"], last, k=3)
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
    rate_limiter.check(rate_key)

    # Middleware pipeline — inlet:
    #   1. Datetime/system context injection
    #   2. Clarification-protocol system prompt + ambiguity nudge
    #   3. Web-search auto-inject (if trigger patterns match the user msg)
    #   4. Per-user RAG + memory context (signed-in users only)
    inject_system_context(req.messages)
    inject_clarification_instruction(req.messages)
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
            _wrap(_multi_agent_sse(req, decision)),
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
        enabled = state.tools.all_schemas(only_enabled=True)
        # Only pass tools when the registry isn't empty — some models
        # degrade with an empty `tools: []` field.
        if enabled:
            tool_schemas = enabled
    elif req.tools:
        tool_schemas = req.tools

    max_tool_turns = 5
    try:
        for turn in range(max_tool_turns + 1):
            async with state.scheduler.reserve(decision.tier_name):
                tool_calls_accum: list[dict] = []
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

        # Distill every 5th assistant turn in a conversation to avoid
        # hammering the Versatile tier.
        msgs = await db.list_messages(req.conversation_id)
        asst_count = sum(1 for m in msgs if m["role"] == "assistant")
        if asst_count > 0 and asst_count % 5 == 0:
            logger.info("Triggering memory distillation for conv %d (asst turn %d)",
                        req.conversation_id, asst_count)
            versatile_tier = state.config.models.tiers["versatile"]
            await memory.distill_and_store(
                user["id"], req.conversation_id, state.ollama, versatile_tier,
            )
    except Exception:
        logger.exception("Post-stream finalization failed")


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
    rows = await memory.list_for_user(user["id"])
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
