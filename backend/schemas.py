"""Pydantic request/response models for the backend HTTP API.

These mirror OpenAI's chat completion shape enough that Open WebUI (kept
during Phase 1) and our custom frontend (Phase 4) can both use them with
minimal adapter code. The `model` field accepts tier IDs like
`tier.versatile`, raw tier names like `versatile`, or backwards-compat
aliases like `quality`.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


Role = Literal["system", "user", "assistant", "tool"]


class MessagePart(BaseModel):
    """Multimodal message part — either text or an image URL."""

    type: Literal["text", "image_url"]
    text: str | None = None
    image_url: dict[str, str] | None = None


class ChatMessage(BaseModel):
    role: Role
    content: str | list[MessagePart]
    name: str | None = None
    tool_call_id: str | None = None


class ChatRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    stream: bool = True
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    max_tokens: int | None = None

    # Extensions beyond the OpenAI shape
    think: bool | None = None              # explicit user override for reasoning
    multi_agent: bool | None = None        # explicit user override for orchestration
    user_id: str | None = None             # injected by auth middleware

    # Pass-through for tool calls (Phase 5)
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None


class TierInfo(BaseModel):
    """Returned by GET /v1/models — one entry per tier."""

    id: str                                # "tier.versatile"
    name: str
    description: str
    backend: str
    context_window: int
    think_supported: bool
    vram_estimate_gb: float


class ModelsListResponse(BaseModel):
    object: Literal["list"] = "list"
    data: list[TierInfo]


class VRAMStatusLoaded(BaseModel):
    tier_id: str
    model_tag: str
    backend: str
    state: str
    refcount: int
    vram_cost_gb: float
    observed_cost_gb: float | None
    last_used_sec_ago: float


class VRAMStatus(BaseModel):
    total_vram_gb: float
    free_vram_gb_actual: float
    free_vram_gb_projected: float
    headroom_gb: float
    loaded: list[VRAMStatusLoaded]


class RouteDecision(BaseModel):
    """Internal — what the router decided about a request."""

    tier_name: str
    think: bool
    multi_agent: bool
    slash_commands_applied: list[str] = Field(default_factory=list)
    overrides: dict[str, Any] = Field(default_factory=dict)
    specialist_reason: str | None = None   # "image_in_message" | "code_block_present" | None


class AgentEvent(BaseModel):
    """SSE event type for multi-agent visualization."""

    type: Literal[
        "agent.plan_start", "agent.plan_done",
        "agent.workers_start", "agent.worker_progress", "agent.worker_done",
        "agent.synthesis_start", "agent.synthesis_done",
        "route.decision", "vram.eviction", "token", "error",
    ]
    data: dict[str, Any] = Field(default_factory=dict)
