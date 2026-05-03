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


class MultiAgentOptions(BaseModel):
    """Per-chat overrides for the multi-agent workflow. Populated by the
    frontend when an elevated user tweaks the per-chat panel. All fields are
    optional; unset fields fall through to the server-side defaults from
    `router.multi_agent` in config. These overrides live only for the
    duration of the request — they never persist to the YAML config."""

    enabled: bool | None = None            # True/False to force multi-agent on or off
    num_workers: int | None = None         # cap on parallel subtasks (1..8)
    worker_tier: str | None = None         # tier name (e.g. "fast", "versatile")
    orchestrator_tier: str | None = None   # tier name
    reasoning_workers: bool | None = None  # reasoning on for workers
    # "independent" | "collaborative". In collaborative mode, workers see each
    # other's drafts and refine their answers for `interaction_rounds` rounds
    # before synthesis.
    interaction_mode: str | None = None
    interaction_rounds: int | None = None  # 0..4


class ChatRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    # `stream` is OPTIONAL on the wire. When omitted, the handler picks
    # the response shape from the Accept header (text/event-stream → SSE,
    # else → JSON). This mirrors OpenAI's behavior (their default is
    # `stream: false`) while keeping the chat UI's existing SSE flow
    # working — it sends `Accept: text/event-stream` and doesn't pass
    # the field. Setting `stream: true|false` explicitly always wins.
    stream: bool | None = None
    # OpenAI's stream_options.include_usage — when true on a streaming
    # request, the final chunk before [DONE] carries an empty `choices`
    # array plus a `usage` object with token counts. Required by some
    # billing/observability tools that depend on per-request token math.
    stream_options: dict | None = None
    # OpenAI's response_format. Two forms accepted:
    #   {"type": "json_object"}              → any valid JSON
    #   {"type": "json_schema", "json_schema": {"schema": {...}}}  → constrained
    # Translated to llama-server's json_schema request field at dispatch.
    # When None (default), the model generates free-form text.
    response_format: dict | None = None
    # OpenAI's per-token log-probabilities. When `logprobs: true`, every
    # generated token's chosen logprob is included in the response.
    # `top_logprobs: N` (0-20) additionally includes the N most-likely
    # alternatives at each position. Required by lm-evaluation-harness's
    # loglikelihood tasks (most of MMLU, HellaSwag, ARC, TruthfulQA-MC).
    logprobs: bool | None = None
    top_logprobs: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    max_tokens: int | None = None

    # Extensions beyond the OpenAI shape
    think: bool | None = None              # explicit user override for reasoning
    multi_agent: bool | None = None        # explicit user override for orchestration
    # Per-request variant override for tiers that declare `variants:` in
    # models.yaml (e.g. coding 30b vs coding 80b). The chat UI's variant
    # sub-selector populates this; the router applies it to the routing
    # decision and the VRAM scheduler spawns the variant-specific GGUF.
    # Silently ignored when the resolved tier doesn't have the named
    # variant.
    variant: str | None = None
    multi_agent_options: MultiAgentOptions | None = None  # per-chat tweaks
    user_id: str | None = None             # injected by auth middleware

    # Pass-through for tool calls (Phase 5)
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None

    # Per-request whitelist of tool names the chat composer enabled.
    # When None / omitted, the registry's default-enabled set is used
    # (legacy behaviour). When provided (even as empty list — meaning
    # "no tools at all this turn"), only those exact tool names are
    # offered to the model. Names match the registry's "<module>.<method>"
    # form returned by GET /tools.
    enabled_tools: list[str] | None = None

    # IDs returned by POST /api/chat/upload, attached to the user's
    # next message. The backend looks them up in data/uploads/<user>/
    # and either embeds the text inline or feeds image bytes to the
    # vision tier. Empty / omitted means no attachments.
    attachment_ids: list[str] | None = None

    # Phase 6: when set and the user is signed in, persist the user+assistant
    # messages after the stream completes and trigger background memory
    # distillation every N turns.
    conversation_id: int | None = None

    # Response-mode steering — see middleware/response_mode.py. One of
    # immediate | plan | clarify | approval | manual_plan. Unknown values
    # and `immediate` are no-ops.
    response_mode: str | None = None
    # For `manual_plan` mode: the user-supplied plan text the model must
    # follow verbatim.
    plan_text: str | None = None


class TierVariantInfo(BaseModel):
    """One per-tier variant. Surfaced by GET /v1/models so the chat UI
    can render a sub-selector when a tier has multiple swap-in models
    (e.g. coding 30B vs coding 80B)."""

    id: str                                # "30b" | "80b" | ...
    name: str | None = None                # display label (falls back to id)
    vram_estimate_gb: float | None = None
    available: bool = True                 # gguf on disk + resolver entry exists


class TierInfo(BaseModel):
    """Returned by GET /v1/models — one entry per tier."""

    id: str                                # "tier.versatile"
    name: str
    description: str
    backend: str
    context_window: int
    think_supported: bool
    vram_estimate_gb: float
    # Variants for tiers like coding (30B vs 80B). Empty for tiers
    # with no variant choice. `default_variant` is the one used when
    # the request doesn't specify one (e.g. plain `tier.coding`).
    variants: list[TierVariantInfo] = Field(default_factory=list)
    default_variant: str | None = None
    # Optgroup label for the chat UI's tier dropdown. Empty string
    # means top-level (no group). Multiple tiers sharing the same
    # category render under one <optgroup>.
    category: str = ""
    # True when the tier's working set is expected to exceed system
    # RAM and therefore relies on NVMe-backed mmap spillover. UI uses
    # this to badge the tier so operators see the disk-throughput
    # requirement before they pick it.
    requires_nvme_spillover: bool = False


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
    # Variant override resolved from the slash command (e.g. /coder big -> '80b').
    # When None, the tier's default_variant (or self) is used at spawn time.
    # Only meaningful for tiers that declare `variants:` in models.yaml.
    variant: str | None = None
    slash_commands_applied: list[str] = Field(default_factory=list)
    overrides: dict[str, Any] = Field(default_factory=dict)
    specialist_reason: str | None = None   # "image_in_message" | "code_block_present" | None


class AgentEvent(BaseModel):
    """SSE event type for multi-agent visualization."""

    type: Literal[
        "agent.plan_start", "agent.plan_done",
        "agent.workers_start", "agent.worker_progress", "agent.worker_done",
        "agent.refine_start",
        "agent.synthesis_start", "agent.synthesis_done",
        "route.decision", "vram.eviction", "token", "error",
        # Pre-stream status events surfaced by _reserve_with_sse so the
        # chat UI can show "Loading <model>..." / "Unloading idle
        # models..." while the scheduler is working off-band. New in
        # the b8992 / Qwen3 lineup; older clients silently ignore types
        # they don't recognize.
        "tier.loading", "vram.making_room", "queue",
        # KV pressure manager dropped low-importance segments before
        # dispatch so the tier's KV slot wouldn't engage RAM spillover.
        "kv.spillover",
    ]
    data: dict[str, Any] = Field(default_factory=dict)


# ── Auth ────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    ok: bool
    is_admin: bool
    username: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class CreateUserRequest(BaseModel):
    username: str
    email: str
    password: str
    is_admin: bool = False


class UpdateUserRequest(BaseModel):
    username: str | None = None
    email: str | None = None
    password: str | None = None
    is_admin: bool | None = None


class MeResponse(BaseModel):
    id: int
    username: str = ""
    email: str
    is_admin: bool = False
    created_at: float
    last_login_at: float | None = None


# ── Chats ────────────────────────────────────────────────────────────────

class ConversationSummary(BaseModel):
    id: int
    title: str
    tier: str | None = None
    # Default True on the wire for backwards-compat with older clients.
    # When False, this chat is excluded from memory distillation and the
    # encrypted per-user history log.
    memory_enabled: bool = True
    # True for chats created while airgap mode was on. The server only
    # surfaces chats matching the current mode, so the client shouldn't
    # normally see a mix, but the flag is exposed so the UI can label.
    airgap: bool = False
    # When True the conv is hidden from the default sidebar listing.
    # Clients fetch archived ones explicitly via ?include_archived=true.
    archived: bool = False
    created_at: float
    updated_at: float


class ConversationListResponse(BaseModel):
    data: list[ConversationSummary]


class MessageOut(BaseModel):
    id: int
    role: str
    content: str
    tier: str | None = None
    think: bool | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    created_at: float


class ConversationWithMessages(BaseModel):
    id: int
    title: str
    tier: str | None = None
    memory_enabled: bool = True
    airgap: bool = False
    archived: bool = False
    created_at: float
    updated_at: float
    messages: list[MessageOut]


class ConversationUpdate(BaseModel):
    title: str | None = None
    tier: str | None = None
    memory_enabled: bool | None = None
    archived: bool | None = None
