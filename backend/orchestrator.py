"""Multi-agent orchestrator.

Flow:
    1. Reserve the orchestrator tier (Versatile by default).
    2. Ask it for a JSON plan: a list of decomposed subtasks.
    3. Release the orchestrator so workers can fit in VRAM.
    4. Run all subtasks in parallel as worker reservations. Each worker
       gets `think: false` and uses the Fast tier, unless a subtask
       requests a specialist (coding or vision).
    5. Re-reserve the orchestrator for synthesis, streaming the final
       answer back to the client.

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

from .config import AppConfig
from .schemas import ChatMessage, AgentEvent


logger = logging.getLogger(__name__)


ORCHESTRATOR_SYSTEM_PROMPT = """You are an orchestrator agent. Decompose the user's request into independent parallel subtasks.

Rules:
- Each subtask MUST be answerable without knowledge of the other subtasks.
- Produce 2-5 subtasks. Fewer is better if the request doesn't decompose.
- Tag each subtask's specialist need: GENERAL, CODING, VISION, or REASONING.
- Output ONLY a JSON array, no prose before or after.

Format:
[
  {"id": 1, "task": "<self-contained prompt>", "specialist": "GENERAL"},
  {"id": 2, "task": "...", "specialist": "CODING"}
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


@dataclass
class Subtask:
    id: int
    task: str
    specialist: str                        # GENERAL | CODING | VISION | REASONING

    def resolved_tier(self, multi_agent_cfg) -> str:
        routes = multi_agent_cfg.specialist_routes
        if self.specialist == "CODING":
            return routes.get("code_block_present", "coding")
        if self.specialist == "VISION":
            return routes.get("image_in_message", "vision")
        if self.specialist == "REASONING":
            return "highest_quality"
        return multi_agent_cfg.worker_tier


class Orchestrator:
    def __init__(self, config: AppConfig, scheduler, backends: dict, tools=None):
        """
        backends: {"ollama": OllamaClient, "llama_cpp": LlamaCppClient}
        tools:    optional ToolRegistry. When set, Ollama workers receive the
                  tool schemas — letting them call functions within subtasks.
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
    ) -> AsyncIterator[AgentEvent]:
        ma_cfg = self.cfg.router.multi_agent
        orch_tier_name = ma_cfg.orchestrator_tier
        orch_tier = self.cfg.models.tiers[orch_tier_name]
        orch_client = self.backends.get(orch_tier.backend)
        if orch_client is None:
            yield AgentEvent(type="error", data={"message": f"No client for backend {orch_tier.backend}"})
            return

        # ── 1. Plan ──────────────────────────────────────────────────────
        yield AgentEvent(type="agent.plan_start", data={"tier": orch_tier_name})

        planning_msgs = [
            ChatMessage(role="system", content=ORCHESTRATOR_SYSTEM_PROMPT),
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
        yield AgentEvent(type="agent.plan_done", data={"subtask_count": len(subtasks)})

        # Fallback: no decomposition → single-shot on orchestrator
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
            "workers": [{"id": s.id, "tier": s.resolved_tier(ma_cfg), "task": s.task[:100]} for s in subtasks],
        })

        worker_tool_schemas: list[dict] | None = None
        if self.tools is not None:
            enabled = self.tools.all_schemas(only_enabled=True)
            if enabled:
                worker_tool_schemas = enabled

        async def run_worker(s: Subtask) -> dict:
            worker_tier_name = s.resolved_tier(ma_cfg)
            worker_tier = self.cfg.models.tiers[worker_tier_name]
            worker_client = self.backends[worker_tier.backend]
            try:
                async with self.scheduler.reserve(worker_tier_name):
                    overrides = {"think": ma_cfg.worker_overrides.get("think", False)}
                    # Only Ollama workers get the tool schema today (llama_cpp
                    # vision tier bypasses tools anyway).
                    kwargs = {}
                    if worker_tier.backend == "ollama" and worker_tool_schemas:
                        kwargs["extra_options"] = None
                        # chat_once is a thin wrapper — pass tools via a
                        # custom path: call chat_stream manually.
                        text_chunks: list[str] = []
                        async for chunk in worker_client.chat_stream(
                            worker_tier,
                            [ChatMessage(role="user", content=s.task)],
                            think=overrides["think"],
                            tools=worker_tool_schemas,
                        ):
                            msg = chunk.get("message") or {}
                            if msg.get("content"):
                                text_chunks.append(msg["content"])
                            if chunk.get("done"):
                                break
                        output = "".join(text_chunks)
                    else:
                        output = await worker_client.chat_once(
                            worker_tier,
                            [ChatMessage(role="user", content=s.task)],
                            think=overrides["think"],
                        )
                return {"id": s.id, "task": s.task, "output": output, "error": None}
            except Exception as e:
                return {"id": s.id, "task": s.task, "output": "", "error": str(e)}

        results = await asyncio.gather(*(run_worker(s) for s in subtasks))

        for r in results:
            yield AgentEvent(
                type="agent.worker_done",
                data={"id": r["id"], "chars": len(r["output"]), "error": r["error"]},
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


async def _stream_as_events(
    raw_stream: AsyncIterator[dict],
    backend: str,
) -> AsyncIterator[AgentEvent]:
    """Normalize ollama/llama_cpp streaming shapes into `AgentEvent(type=token)`."""
    async for chunk in raw_stream:
        text: str | None = None
        if backend == "ollama":
            msg = chunk.get("message") or {}
            text = msg.get("content")
        elif backend == "llama_cpp":
            choices = chunk.get("choices") or []
            if choices:
                text = (choices[0].get("delta") or {}).get("content")
        if text:
            yield AgentEvent(type="token", data={"text": text})
