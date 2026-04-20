"""KV cache pressure manager — keep important context on GPU, spill the rest.

Motivation:
  Ollama/llama.cpp sizes the KV cache at model-load time (num_parallel *
  num_ctx). When the live context for a request would push the card into
  RAM spillover, throughput collapses (typical 3-5x degradation once
  spillover engages). We can't steer llama.cpp's internal KV slots
  directly, but we *can* shape what gets sent into a turn. If we classify
  each message by importance and prune / stash the least-interesting
  segments before the request reaches the backend, the effective KV
  footprint shrinks and the card stays resident.

What this module provides:
  - `classify_segments(messages, current_user_text)` — assigns an
    Importance score + category to every message, using role, recency,
    lexical overlap with the live turn, pinning signals (system prompt,
    active tool-call / tool-result pairs), and size penalties for long
    <think> blocks.
  - `assess_pressure(token_budget_kv, segments, reserve_for_output)` —
    decides whether we're about to spill and by how much.
  - `plan_spillover(segments, target_tokens)` — picks a minimal set of
    low-importance segments to evict, honouring pins and the
    system-prompt floor.
  - `SpillStore` — an in-memory bank keyed by (conversation_id,
    segment_fingerprint) so evicted text can be recalled on a later turn
    without re-fetching from the DB.
  - `apply_plan(messages, plan)` — returns the pruned message list that
    actually goes out to the backend, with a marker noting what was
    dropped so the orchestrator can log / surface it.

Scope:
  Pure assessment + shaping logic. No I/O, no VRAM probing (call sites
  feed in the free-VRAM reading from `VRAMScheduler.probe`). No
  integration into `vram_scheduler.py` here — wired from
  `main.py`/`orchestrator.py` at request entry so normal flows stay
  untouched.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable

from .schemas import ChatMessage, MessagePart


logger = logging.getLogger(__name__)


# ── Token estimation ────────────────────────────────────────────────────────
# We avoid a tokenizer dependency — a char/3.8 heuristic is close enough
# for budgeting across Qwen/Llama-family BPEs. The scheduler only needs
# relative sizing, not exact counts.
_CHARS_PER_TOKEN = 3.8


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, int(len(text) / _CHARS_PER_TOKEN))


def _message_text(msg: ChatMessage) -> str:
    if isinstance(msg.content, str):
        return msg.content
    parts: list[str] = []
    for p in msg.content or []:
        if isinstance(p, MessagePart) and p.type == "text" and p.text:
            parts.append(p.text)
    return " ".join(parts)


# ── Taxonomy ────────────────────────────────────────────────────────────────

class SegmentKind(str, Enum):
    SYSTEM = "system"                    # system prompt — always pinned
    MEMORY_BLOCK = "memory_block"        # memory.format_memory_block() injection
    TOOL_CALL = "tool_call"              # assistant message containing a tool_call
    TOOL_RESULT = "tool_result"          # role=tool response bound to a call_id
    USER_LIVE = "user_live"              # the most recent user turn
    USER_PRIOR = "user_prior"            # earlier user turns
    ASSISTANT_PRIOR = "assistant_prior"  # earlier assistant turns
    THINK_BLOCK = "think_block"          # <think>…</think> content, typically stale


@dataclass
class ContextSegment:
    """One classified message + its importance score."""

    index: int                           # position in the original messages list
    kind: SegmentKind
    role: str
    text: str
    tokens: int
    importance: float                    # 0.0 = drop first, 1.0 = pinned
    pinned: bool = False
    tool_call_id: str | None = None
    # Fingerprint for the spill store. Hash of role+text so the same
    # segment across turns maps to the same slot.
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if not self.fingerprint:
            h = hashlib.blake2b(
                f"{self.role}:{self.text}".encode("utf-8"), digest_size=12,
            )
            self.fingerprint = h.hexdigest()


# ── Classification ──────────────────────────────────────────────────────────

@dataclass
class ScoringWeights:
    """Tunable weights for `classify_segments`. Defaults tuned for 24GB
    single-card workloads where the hottest signal is recency."""

    recency: float = 0.45
    relevance: float = 0.30
    role_prior: float = 0.15
    size_penalty: float = 0.10
    # How many recent non-system turns get an automatic high score — these
    # are treated as the "live working set" the model is reasoning over.
    hot_window: int = 4


_THINK_PATTERN = re.compile(r"<think>[\s\S]*?</think>", re.IGNORECASE)
_WORD_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")


def _role_prior(kind: SegmentKind) -> float:
    return {
        SegmentKind.SYSTEM: 1.0,
        SegmentKind.MEMORY_BLOCK: 0.85,
        SegmentKind.USER_LIVE: 1.0,
        SegmentKind.TOOL_CALL: 0.75,
        SegmentKind.TOOL_RESULT: 0.70,
        SegmentKind.USER_PRIOR: 0.55,
        SegmentKind.ASSISTANT_PRIOR: 0.45,
        SegmentKind.THINK_BLOCK: 0.15,
    }[kind]


def _classify_kind(msg: ChatMessage, is_latest_user: bool) -> SegmentKind:
    if msg.role == "system":
        text = _message_text(msg)
        if text.lstrip().startswith("[Things to remember about this user"):
            return SegmentKind.MEMORY_BLOCK
        return SegmentKind.SYSTEM
    if msg.role == "tool":
        return SegmentKind.TOOL_RESULT
    if msg.role == "assistant":
        text = _message_text(msg)
        if _THINK_PATTERN.search(text):
            return SegmentKind.THINK_BLOCK
        if msg.tool_call_id or "tool_call" in text.lower()[:200]:
            return SegmentKind.TOOL_CALL
        return SegmentKind.ASSISTANT_PRIOR
    if msg.role == "user":
        return SegmentKind.USER_LIVE if is_latest_user else SegmentKind.USER_PRIOR
    return SegmentKind.ASSISTANT_PRIOR


def _lexical_overlap(a: str, b: str) -> float:
    """Jaccard on content words. Cheap proxy for relevance; the memory
    subsystem already handles deep retrieval via Qdrant."""
    wa = set(_WORD_PATTERN.findall(a.lower()))
    wb = set(_WORD_PATTERN.findall(b.lower()))
    if not wa or not wb:
        return 0.0
    inter = wa & wb
    union = wa | wb
    return len(inter) / len(union)


def classify_segments(
    messages: Iterable[ChatMessage],
    *,
    weights: ScoringWeights | None = None,
) -> list[ContextSegment]:
    """Score every message. Later messages weigh more, the live turn
    weighs most, and tool-call/result pairs are held together."""
    msgs = list(messages)
    if not msgs:
        return []
    w = weights or ScoringWeights()

    latest_user_idx = next(
        (i for i in range(len(msgs) - 1, -1, -1) if msgs[i].role == "user"),
        None,
    )
    live_text = _message_text(msgs[latest_user_idx]) if latest_user_idx is not None else ""
    total = len(msgs)
    segments: list[ContextSegment] = []

    for i, m in enumerate(msgs):
        text = _message_text(m)
        kind = _classify_kind(m, is_latest_user=(i == latest_user_idx))
        tokens = estimate_tokens(text)

        # Recency: linear ramp across the transcript, with a step up for
        # the hot window of the last N turns.
        rel_pos = i / max(1, total - 1)
        within_hot = (total - i) <= max(1, w.hot_window)
        recency = rel_pos if not within_hot else max(rel_pos, 0.9)

        relevance = _lexical_overlap(text, live_text) if live_text and i != latest_user_idx else 0.0
        role_prior = _role_prior(kind)

        # Long messages get a small penalty — they cost the most KV but
        # usually carry redundant detail. Capped so a single long system
        # prompt can't be demoted.
        size_pen = min(1.0, tokens / 4096.0)

        importance = (
            w.recency * recency
            + w.relevance * relevance
            + w.role_prior * role_prior
            - w.size_penalty * size_pen
        )

        pinned = kind in (SegmentKind.SYSTEM, SegmentKind.USER_LIVE)
        if pinned:
            importance = 1.0

        segments.append(
            ContextSegment(
                index=i,
                kind=kind,
                role=m.role,
                text=text,
                tokens=tokens,
                importance=round(max(0.0, min(1.0, importance)), 4),
                pinned=pinned,
                tool_call_id=m.tool_call_id,
            )
        )

    # Pair tool_result with the tool_call it answers: if one is pinned
    # (e.g. very recent), pin the other so we never ship a dangling call.
    _link_tool_pairs(segments)
    return segments


def _link_tool_pairs(segments: list[ContextSegment]) -> None:
    by_call: dict[str, list[ContextSegment]] = {}
    for s in segments:
        if s.tool_call_id:
            by_call.setdefault(s.tool_call_id, []).append(s)
    for pair in by_call.values():
        if len(pair) < 2:
            continue
        if any(p.pinned for p in pair):
            for p in pair:
                p.pinned = True
                p.importance = 1.0
        else:
            top = max(p.importance for p in pair)
            for p in pair:
                p.importance = top


# ── Pressure assessment ─────────────────────────────────────────────────────

@dataclass
class PressureReport:
    kv_budget_tokens: int                # effective KV slot budget (num_ctx)
    context_tokens: int                  # sum of classified segments
    reserve_for_output: int              # headroom for response tokens
    over_by_tokens: int                  # >0 means spillover imminent
    spill_trigger_tokens: int            # threshold at which we act
    spill_needed: bool


def assess_pressure(
    kv_budget_tokens: int,
    segments: list[ContextSegment],
    *,
    reserve_for_output: int = 512,
    spill_trigger_fraction: float = 0.92,
) -> PressureReport:
    """Decide whether the current request is about to engage RAM spillover.

    `kv_budget_tokens` should be the tier's num_ctx (the per-request KV
    window). Set `spill_trigger_fraction` below 1.0 so we act before
    llama.cpp starts paging rather than after.
    """
    used = sum(s.tokens for s in segments)
    threshold = int(kv_budget_tokens * spill_trigger_fraction) - reserve_for_output
    threshold = max(1, threshold)
    over = (used + reserve_for_output) - int(kv_budget_tokens * spill_trigger_fraction)
    return PressureReport(
        kv_budget_tokens=kv_budget_tokens,
        context_tokens=used,
        reserve_for_output=reserve_for_output,
        over_by_tokens=max(0, over),
        spill_trigger_tokens=threshold,
        spill_needed=used > threshold,
    )


# ── Spill planning ──────────────────────────────────────────────────────────

@dataclass
class SpillPlan:
    target_tokens: int                   # budget we're trying to hit
    kept: list[ContextSegment]
    spilled: list[ContextSegment]
    freed_tokens: int

    def as_event(self, tier_id: str | None = None) -> dict:
        return {
            "kind": "kv.spillover",
            "tier": tier_id,
            "target_tokens": self.target_tokens,
            "freed_tokens": self.freed_tokens,
            "spilled": [
                {
                    "index": s.index,
                    "kind": s.kind.value,
                    "tokens": s.tokens,
                    "importance": s.importance,
                    "fingerprint": s.fingerprint,
                }
                for s in self.spilled
            ],
        }


def plan_spillover(
    segments: list[ContextSegment],
    target_tokens: int,
) -> SpillPlan:
    """Pick the lowest-importance unpinned segments to drop until the
    surviving set fits under `target_tokens`. Stable order within a tie
    prefers evicting older segments first."""
    total = sum(s.tokens for s in segments)
    if total <= target_tokens:
        return SpillPlan(target_tokens, list(segments), [], 0)

    evictable = [s for s in segments if not s.pinned]
    evictable.sort(key=lambda s: (s.importance, s.index))
    spilled_ids: set[int] = set()
    freed = 0
    remaining = total
    for seg in evictable:
        if remaining <= target_tokens:
            break
        spilled_ids.add(seg.index)
        freed += seg.tokens
        remaining -= seg.tokens

    kept = [s for s in segments if s.index not in spilled_ids]
    spilled = [s for s in segments if s.index in spilled_ids]
    return SpillPlan(target_tokens, kept, spilled, freed)


# ── Spill store ─────────────────────────────────────────────────────────────

class SpillStore:
    """Per-conversation bank of evicted segments.

    Spilled segments stay available for recall: a later turn can pull a
    stashed tool_result back into context if the live question starts
    referring to it again. The store is in-process (dict) because the
    context we're evicting already lives in SQLite history — this is just
    a fast L1 so we don't round-trip the DB on every turn."""

    def __init__(self, max_entries_per_conv: int = 256) -> None:
        self._entries: dict[int, dict[str, tuple[ContextSegment, float]]] = {}
        self._cap = max_entries_per_conv

    def stash(self, conversation_id: int, segments: list[ContextSegment]) -> None:
        bucket = self._entries.setdefault(conversation_id, {})
        now = time.time()
        for s in segments:
            bucket[s.fingerprint] = (s, now)
        # Evict oldest entries past the cap so long sessions don't grow
        # unbounded. We're caching evicted context — losing the tail is
        # fine because history_store has the full transcript.
        if len(bucket) > self._cap:
            excess = len(bucket) - self._cap
            oldest = sorted(bucket.items(), key=lambda kv: kv[1][1])[:excess]
            for fp, _ in oldest:
                bucket.pop(fp, None)

    def recall(self, conversation_id: int, fingerprint: str) -> ContextSegment | None:
        bucket = self._entries.get(conversation_id)
        if not bucket:
            return None
        hit = bucket.get(fingerprint)
        return hit[0] if hit else None

    def size(self, conversation_id: int) -> int:
        return len(self._entries.get(conversation_id, {}))

    def forget(self, conversation_id: int) -> None:
        self._entries.pop(conversation_id, None)


# ── Plan application ────────────────────────────────────────────────────────

def apply_plan(
    messages: list[ChatMessage],
    plan: SpillPlan,
) -> list[ChatMessage]:
    """Return the message list with spilled segments removed, preserving
    original order. Tool-call/result pairs are enforced upstream by
    `_link_tool_pairs`, so simple index filtering is safe."""
    if not plan.spilled:
        return list(messages)
    drop = {s.index for s in plan.spilled}
    return [m for i, m in enumerate(messages) if i not in drop]


# ── Top-level entry point ───────────────────────────────────────────────────

@dataclass
class KVAssessment:
    report: PressureReport
    plan: SpillPlan | None
    segments: list[ContextSegment]


def assess_and_plan(
    messages: list[ChatMessage],
    kv_budget_tokens: int,
    *,
    reserve_for_output: int = 512,
    spill_trigger_fraction: float = 0.92,
    weights: ScoringWeights | None = None,
) -> KVAssessment:
    """One-shot entry the request path calls before dispatch.

    Returns the classified segments, the pressure report, and a
    spill plan if action is needed (else `plan=None`). Caller decides
    whether to `apply_plan` (and stash to a `SpillStore`) or proceed as-is.
    """
    segments = classify_segments(messages, weights=weights)
    report = assess_pressure(
        kv_budget_tokens,
        segments,
        reserve_for_output=reserve_for_output,
        spill_trigger_fraction=spill_trigger_fraction,
    )
    if not report.spill_needed:
        return KVAssessment(report=report, plan=None, segments=segments)
    target = report.spill_trigger_tokens
    plan = plan_spillover(segments, target_tokens=target)
    logger.info(
        "KV pressure: %d/%d tok, spilling %d segments (%d tok freed)",
        report.context_tokens, kv_budget_tokens,
        len(plan.spilled), plan.freed_tokens,
    )
    return KVAssessment(report=report, plan=plan, segments=segments)
