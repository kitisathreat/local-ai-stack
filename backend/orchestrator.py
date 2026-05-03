"""Multi-agent orchestrator.

Flow (independent mode — default):
    1. Reserve the orchestrator tier (Versatile by default).
    2. Ask it for a JSON plan: a list of decomposed subtasks.
    3. Release the orchestrator so workers can fit in VRAM.
    4. Run all subtasks in parallel as worker reservations. Each worker
       gets `think: false` and uses the Fast tier, unless a subtask
       requests a specialist (coding or vision).
    5. Re-reserve the orchestrator for synthesis, streaming the final
       answer back to the client.

Flow (collaborative mode):
    Same as above through step 4. After the initial parallel pass, each
    worker is shown the other workers' drafts and asked to refine its
    own answer. This repeats for `interaction_rounds` rounds, after
    which the orchestrator synthesizes the final answer from the
    last-round outputs. Trades latency + VRAM churn for rigor.

Per-request overrides:
    `MultiAgentOptions` on the chat request can override worker tier,
    orchestrator tier, worker count cap, reasoning toggles, interaction
    mode, and interaction rounds for one chat without persisting to the
    YAML config. See `_resolved_settings` for the merge order.

Fallback: if the orchestrator's JSON plan fails to parse, we fall through
to a single-shot call with the orchestrator tier and return that as the
final answer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, AsyncIterator

from .backends.llama_cpp import ToolCallAccumulator
from .config import AppConfig, MultiAgentConfig
from .schemas import ChatMessage, AgentEvent, MultiAgentOptions


logger = logging.getLogger(__name__)


ORCHESTRATOR_SYSTEM_PROMPT = """You are an orchestrator agent. Decompose the user's request into independent parallel subtasks.

Rules:
- Each subtask MUST be answerable without knowledge of the other subtasks.
- Produce {min_workers}-{max_workers} subtasks. Fewer is better if the request doesn't decompose.
- Tag each subtask's specialist need: GENERAL, CODING, VISION, or REASONING.
- Output ONLY a JSON array, no prose before or after.

Format:
[
  {{"id": 1, "task": "<self-contained prompt>", "specialist": "GENERAL"}},
  {{"id": 2, "task": "...", "specialist": "CODING"}}
]

If the request does not genuinely decompose into independent parts, return an empty array [].
"""


SYNTHESIS_SYSTEM_PROMPT = """You are synthesizing outputs from parallel worker agents into a single coherent answer to the user's original question.

You will be given:
1. The user's original question.
2. The subtasks that were dispatched.
3. Each worker's output.

Combine them into ONE polished response. Do not mention the orchestration pipeline, subtasks, or workers. Write as though you answered the question yourself. Preserve any code, tables, or structured content from the worker outputs.
"""


COLLAB_REFINE_SYSTEM_PROMPT = """You are one of several worker agents collaborating to answer a user's question. You previously produced a draft for your subtask. Your peers worked on related subtasks and produced their own drafts.

Read every peer's draft. Then revise YOUR OWN answer to your subtask, doing all of:
- Correct any mistakes you now see in your draft (use peers' work as a sanity check).
- Add facts, edge cases, or nuance your peers raised that your subtask should also cover.
- Resolve contradictions between your draft and peers' work, or call them out explicitly if you can't.
- Stay focused on YOUR subtask — do not answer your peers' subtasks for them.

Output ONLY the refined answer to your subtask. No meta-commentary about the collaboration.
"""


@dataclass
class _RuntimeSettings:
    """Resolved per-call settings: per-request options layered on top of
    the global MultiAgentConfig defaults."""

    orchestrator_tier: str
    worker_tier: str
    # Per-agent worker tier list. When non-empty, max_workers and
    # min_workers are clamped to len(worker_tiers) and each subtask N
    # runs on worker_tiers[N] regardless of specialist routing.
    # Sanitised at resolve time: unknown tier names are replaced with
    # `worker_tier` (the global default).
    worker_tiers: list[str]
    max_workers: int
    min_workers: int
    reasoning_workers: bool
    interaction_mode: str            # "independent" | "collaborative"
    interaction_rounds: int


def _resolved_settings(
    cfg: MultiAgentConfig,
    options: MultiAgentOptions | None,
    available_tiers: set[str],
) -> _RuntimeSettings:
    o = options or MultiAgentOptions()

    def _tier_or_default(name: str | None, default: str) -> str:
        if not name:
            return default
        # Strip "tier." prefix the frontend may send.
        name = name[5:] if name.startswith("tier.") else name
        return name if name in available_tiers else default

    mode = (o.interaction_mode or cfg.interaction_mode or "independent").lower()
    if mode not in {"independent", "collaborative"}:
        mode = "independent"

    rounds = o.interaction_rounds if o.interaction_rounds is not None else cfg.interaction_rounds
    rounds = max(0, min(int(rounds or 0), 4))   # clamp 0..4 to bound cost

    worker_tier_default = _tier_or_default(o.worker_tier, cfg.worker_tier)

    # Per-agent worker tier list (Phase 2). When provided and non-empty,
    # it pins both the count of workers (max == min == len(list)) and
    # the per-position tier assignment. Sanitise unknown names to the
    # global default so a typo doesn't 404 the spawn — the operator
    # sees the corrected list in the agent.workers_start event.
    worker_tiers_raw = list(o.worker_tiers or [])
    worker_tiers: list[str] = []
    for name in worker_tiers_raw:
        if not name:
            continue
        worker_tiers.append(_tier_or_default(name, worker_tier_default))
    # Clamp to 1..8 even when the list is given (defence against UI bugs
    # sending arrays of arbitrary length).
    if len(worker_tiers) > 8:
        worker_tiers = worker_tiers[:8]

    if worker_tiers:
        # Per-agent list pins worker count.
        max_w = len(worker_tiers)
        min_w = max_w
    else:
        max_w = o.num_workers if o.num_workers is not None else cfg.max_workers
        max_w = max(1, min(int(max_w), 8))          # clamp 1..8
        min_w = max(1, min(int(cfg.min_workers), max_w))

    reasoning = (
        o.reasoning_workers if o.reasoning_workers is not None
        else bool(cfg.reasoning_workers)
        or bool(cfg.worker_overrides.get("think", False))
    )

    return _RuntimeSettings(
        orchestrator_tier=_tier_or_default(o.orchestrator_tier, cfg.orchestrator_tier),
        worker_tier=worker_tier_default,
        worker_tiers=worker_tiers,
        max_workers=max_w,
        min_workers=min_w,
        reasoning_workers=bool(reasoning),
        interaction_mode=mode,
        interaction_rounds=rounds,
    )


@dataclass
class Subtask:
    id: int
    task: str
    specialist: str                        # GENERAL | CODING | VISION | REASONING

    def resolved_tier(
        self,
        multi_agent_cfg: MultiAgentConfig,
        default_worker: str,
        *,
        worker_tiers: list[str] | None = None,
        index: int | None = None,
    ) -> str:
        # Per-agent override (Phase 2): when the user supplied an
        # explicit per-agent tier list, position-index wins over
        # specialist routing. The user has explicitly chosen tier T
        # for this slot — honour that even if the specialist heuristic
        # would have picked something else. Out-of-range indices fall
        # through to the normal specialist path (defensive: shouldn't
        # happen because settings.max_workers == len(worker_tiers)).
        if worker_tiers and index is not None and 0 <= index < len(worker_tiers):
            return worker_tiers[index]
        routes = multi_agent_cfg.specialist_routes
        if self.specialist == "CODING":
            return routes.get("code_block_present", "coding")
        if self.specialist == "VISION":
            return routes.get("image_in_message", "vision")
        if self.specialist == "REASONING":
            return "highest_quality"
        return default_worker


class Orchestrator:
    def __init__(self, config: AppConfig, scheduler, backends: dict, tools=None):
        """
        backends: {"llama_cpp": LlamaCppClient} — only llama.cpp now.
        tools:    optional ToolRegistry. When set, workers receive the tool
                  schemas (passed to llama-server with --jinja) — letting
                  them call functions within subtasks.
                  Phase 6: added to thread tool use into multi-agent workers.
        """
        self.cfg = config
        self.scheduler = scheduler
        self.backends = backends
        self.tools = tools

    async def run(
        self,
        user_message: str,
        conversation: list[ChatMessage],
        think_synthesis: bool,
        options: MultiAgentOptions | None = None,
    ) -> AsyncIterator[AgentEvent]:
        ma_cfg = self.cfg.router.multi_agent
        settings = _resolved_settings(
            ma_cfg, options, available_tiers=set(self.cfg.models.tiers),
        )

        orch_tier_name = settings.orchestrator_tier
        orch_tier = self.cfg.models.tiers[orch_tier_name]
        orch_client = self.backends.get(orch_tier.backend)
        if orch_client is None:
            yield AgentEvent(type="error", data={"message": f"No client for backend {orch_tier.backend}"})
            return

        # ── 1. Plan ──────────────────────────────────────────────────────
        yield AgentEvent(type="agent.plan_start", data={
            "tier": orch_tier_name,
            "interaction_mode": settings.interaction_mode,
            "max_workers": settings.max_workers,
        })

        planning_msgs = [
            ChatMessage(
                role="system",
                content=ORCHESTRATOR_SYSTEM_PROMPT.format(
                    min_workers=settings.min_workers,
                    max_workers=settings.max_workers,
                ),
            ),
            ChatMessage(role="user", content=user_message),
        ]
        try:
            async with self.scheduler.reserve(orch_tier_name):
                think_plan = ma_cfg.orchestrator_overrides.get("think", True)
                plan_text = await orch_client.chat_once(
                    orch_tier, planning_msgs, think=think_plan,
                )
        except Exception as e:
            yield AgentEvent(type="error", data={"message": f"Planning failed: {e}"})
            return

        subtasks = _parse_plan(plan_text)
        # Cap at the resolved max so per-request limits are honored even if
        # the orchestrator over-decomposes.
        if len(subtasks) > settings.max_workers:
            subtasks = subtasks[: settings.max_workers]

        # Per-agent worker tier list (Phase 2): the user explicitly
        # asked for N agents. Pad the subtask list to len(worker_tiers)
        # so every requested tier actually runs. Padded subtasks share
        # the original user question — the per-tier diversity is the
        # point. If the orchestrator returned ZERO subtasks (no
        # decomposition possible), this also turns the request into an
        # N-tier ensemble of the original question rather than a
        # single-shot fallback.
        if settings.worker_tiers and len(subtasks) < len(settings.worker_tiers):
            next_id = max((s.id for s in subtasks), default=0) + 1
            while len(subtasks) < len(settings.worker_tiers):
                subtasks.append(Subtask(
                    id=next_id, task=user_message, specialist="GENERAL",
                ))
                next_id += 1

        yield AgentEvent(type="agent.plan_done", data={"subtask_count": len(subtasks)})

        # Fallback: no decomposition → single-shot on orchestrator
        # (only when the user did NOT pin a per-agent tier list — with
        # an explicit list, the padding above already produced enough
        # subtasks, so this branch is effectively unreachable then).
        if not subtasks:
            async with self.scheduler.reserve(orch_tier_name):
                async for chunk in _stream_as_events(
                    orch_client.chat_stream(orch_tier, conversation, think=think_synthesis),
                    backend=orch_tier.backend,
                ):
                    yield chunk
            return

        # ── 2. Workers (parallel) ────────────────────────────────────────
        yield AgentEvent(type="agent.workers_start", data={
            "count": len(subtasks),
            "interaction_mode": settings.interaction_mode,
            "reasoning_workers": settings.reasoning_workers,
            "workers": [
                {
                    "id": s.id,
                    "tier": s.resolved_tier(
                        ma_cfg, settings.worker_tier,
                        worker_tiers=settings.worker_tiers, index=i,
                    ),
                    "task": s.task[:100],
                }
                for i, s in enumerate(subtasks)
            ],
        })

        worker_tool_schemas: list[dict] | None = None
        if self.tools is not None:
            enabled = self.tools.all_schemas(only_enabled=True)
            if enabled:
                worker_tool_schemas = enabled

        # Initial round (every mode runs this).
        results = await self._run_workers(
            subtasks=subtasks,
            settings=settings,
            ma_cfg=ma_cfg,
            worker_tool_schemas=worker_tool_schemas,
            peer_drafts=None,
        )
        for r in results:
            yield AgentEvent(
                type="agent.worker_done",
                data={"id": r["id"], "chars": len(r["output"]), "error": r["error"], "round": 1},
            )

        # ── 2b. Collaborative refinement rounds ──────────────────────────
        if settings.interaction_mode == "collaborative" and settings.interaction_rounds > 0:
            for round_idx in range(2, settings.interaction_rounds + 2):
                yield AgentEvent(type="agent.refine_start", data={
                    "round": round_idx,
                    "total_rounds": settings.interaction_rounds + 1,
                })
                # Snapshot peer drafts BEFORE the round so all workers see
                # the same prior-round state (no read-after-write races).
                peer_drafts = [
                    {"id": r["id"], "task": r["task"], "output": r["output"]}
                    for r in results
                ]
                results = await self._run_workers(
                    subtasks=subtasks,
                    settings=settings,
                    ma_cfg=ma_cfg,
                    worker_tool_schemas=worker_tool_schemas,
                    peer_drafts=peer_drafts,
                )
                for r in results:
                    yield AgentEvent(
                        type="agent.worker_done",
                        data={
                            "id": r["id"], "chars": len(r["output"]),
                            "error": r["error"], "round": round_idx,
                        },
                    )

        # ── 3. Synthesis ─────────────────────────────────────────────────
        yield AgentEvent(type="agent.synthesis_start", data={"tier": orch_tier_name})

        synthesis_context = _build_synthesis_context(user_message, results)
        synthesis_msgs = [
            ChatMessage(role="system", content=SYNTHESIS_SYSTEM_PROMPT),
            ChatMessage(role="user", content=synthesis_context),
        ]

        async with self.scheduler.reserve(orch_tier_name):
            async for chunk in _stream_as_events(
                orch_client.chat_stream(orch_tier, synthesis_msgs, think=think_synthesis),
                backend=orch_tier.backend,
            ):
                yield chunk

        yield AgentEvent(type="agent.synthesis_done")

    async def _run_workers(
        self,
        subtasks: list["Subtask"],
        settings: _RuntimeSettings,
        ma_cfg: MultiAgentConfig,
        worker_tool_schemas: list[dict] | None,
        peer_drafts: list[dict] | None,
    ) -> list[dict]:
        """Run one round of workers in parallel. When `peer_drafts` is set,
        each worker is given the other workers' prior-round outputs and asked
        to refine its own (collaborative mode)."""

        # Build a stable subtask-id -> position-index map so the
        # per-agent `worker_tiers` list can be honoured by run_worker
        # without changing run_worker's signature (kept tight because
        # asyncio.gather wraps it).
        subtask_index = {s.id: i for i, s in enumerate(subtasks)}

        async def run_worker(s: Subtask) -> dict:
            worker_tier_name = s.resolved_tier(
                ma_cfg, settings.worker_tier,
                worker_tiers=settings.worker_tiers,
                index=subtask_index.get(s.id),
            )
            worker_tier = self.cfg.models.tiers[worker_tier_name]
            worker_client = self.backends[worker_tier.backend]

            if peer_drafts is None:
                # Initial round — single user message containing the subtask.
                messages = [ChatMessage(role="user", content=s.task)]
            else:
                # Refinement round — feed the worker its peers' drafts and
                # its own prior draft, ask it to revise.
                own = next((d for d in peer_drafts if d["id"] == s.id), None)
                messages = [
                    ChatMessage(role="system", content=COLLAB_REFINE_SYSTEM_PROMPT),
                    ChatMessage(
                        role="user",
                        content=_build_refine_prompt(s, own, peer_drafts),
                    ),
                ]

            try:
                async with self.scheduler.reserve(worker_tier_name):
                    think_workers = settings.reasoning_workers
                    if worker_tool_schemas:
                        text_chunks: list[str] = []
                        accumulator = ToolCallAccumulator()
                        async for chunk in worker_client.chat_stream(
                            worker_tier,
                            messages,
                            think=think_workers,
                            tools=worker_tool_schemas,
                        ):
                            for choice in chunk.get("choices", []):
                                delta = choice.get("delta") or {}
                                if delta.get("content"):
                                    text_chunks.append(delta["content"])
                                accumulator.feed(delta.get("tool_calls"))
                        # Workers don't run a full tool loop today — we capture
                        # the model's text and surface tool_calls back to the
                        # orchestrator for inspection.
                        output = "".join(text_chunks)
                        if accumulator.calls():
                            output += "\n\n[worker requested tool calls — not yet executed]"
                    else:
                        output = await worker_client.chat_once(
                            worker_tier, messages, think=think_workers,
                        )
                return {"id": s.id, "task": s.task, "output": output, "error": None}
            except Exception as e:
                return {"id": s.id, "task": s.task, "output": "", "error": str(e)}

        return await asyncio.gather(*(run_worker(s) for s in subtasks))


# ── Plan parsing ────────────────────────────────────────────────────────

_JSON_ARRAY_RE = re.compile(r"\[\s*(?:\{[\s\S]*?\}\s*,?\s*)*\]")


def _parse_plan(raw: str) -> list[Subtask]:
    """Extract the first JSON array from the orchestrator's response. The
    model may wrap it in prose or <think> tags; we find the first balanced
    JSON array and parse it."""
    # Strip <think>...</think> blocks
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", raw, flags=re.IGNORECASE)

    match = _JSON_ARRAY_RE.search(cleaned)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []

    subtasks: list[Subtask] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            continue
        task = item.get("task", "").strip()
        if not task:
            continue
        specialist = str(item.get("specialist", "GENERAL")).upper()
        if specialist not in {"GENERAL", "CODING", "VISION", "REASONING"}:
            specialist = "GENERAL"
        subtasks.append(Subtask(
            id=int(item.get("id", i + 1)),
            task=task,
            specialist=specialist,
        ))
    return subtasks


def _build_synthesis_context(user_message: str, results: list[dict]) -> str:
    lines = [f"User's question:\n{user_message}\n", "\nWorker outputs:\n"]
    for r in results:
        lines.append(f"\n--- Subtask {r['id']} ---")
        lines.append(f"Task: {r['task']}")
        if r["error"]:
            lines.append(f"(worker failed: {r['error']})")
        else:
            lines.append(r["output"])
    lines.append("\n\nSynthesize a single coherent answer to the user's question now.")
    return "\n".join(lines)


def _build_refine_prompt(
    subtask: Subtask, own: dict | None, peer_drafts: list[dict],
) -> str:
    """Format the user-side prompt for a collaborative-refinement round."""
    lines = [f"Your subtask:\n{subtask.task}\n"]
    if own and own.get("output"):
        lines.append("Your previous draft:\n")
        lines.append(own["output"])
    else:
        lines.append("Your previous draft:\n(none — your prior attempt failed; produce a fresh answer)")
    lines.append("\n\nYour peers' drafts (other subtasks):\n")
    peers = [d for d in peer_drafts if d["id"] != subtask.id]
    if not peers:
        lines.append("(no peer drafts available)")
    else:
        for d in peers:
            lines.append(f"\n--- Peer subtask {d['id']} ---")
            lines.append(f"Task: {d['task']}")
            lines.append(d["output"] or "(no output)")
    lines.append("\n\nRevise your answer to your own subtask now.")
    return "\n".join(lines)


async def _stream_as_events(
    raw_stream: AsyncIterator[dict],
    backend: str,
) -> AsyncIterator[AgentEvent]:
    """Normalize llama.cpp's OpenAI-shaped streaming chunks into
    `AgentEvent(type=token)`. The `backend` arg is retained for forward
    compatibility but is currently always 'llama_cpp'."""
    async for chunk in raw_stream:
        choices = chunk.get("choices") or []
        if not choices:
            continue
        text = (choices[0].get("delta") or {}).get("content")
        if text:
            yield AgentEvent(type="token", data={"text": text})
