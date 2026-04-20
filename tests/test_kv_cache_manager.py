"""Unit tests for backend/kv_cache_manager.py — classification scoring,
pressure detection, spill planning, and apply_plan."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from backend.kv_cache_manager import (
    KVAssessment,
    SegmentKind,
    SpillStore,
    apply_plan,
    assess_and_plan,
    assess_pressure,
    classify_segments,
    estimate_tokens,
    plan_spillover,
)
from backend.schemas import ChatMessage


def _msg(role, content, tool_call_id=None):
    return ChatMessage(role=role, content=content, tool_call_id=tool_call_id)


# ── estimate_tokens ─────────────────────────────────────────────────────────

def test_estimate_tokens_rough_ratio():
    # ~3.8 chars per token
    assert estimate_tokens("") == 0
    assert 5 <= estimate_tokens("x" * 20) <= 6
    assert estimate_tokens("word " * 100) > 100


# ── classify_segments ──────────────────────────────────────────────────────

def test_classify_roles():
    msgs = [
        _msg("system", "You are a helpful assistant."),
        _msg("user", "What's the capital of France?"),
        _msg("assistant", "Paris."),
        _msg("user", "And its population?"),
    ]
    segs = classify_segments(msgs)
    assert [s.kind for s in segs] == [
        SegmentKind.SYSTEM,
        SegmentKind.USER_PRIOR,
        SegmentKind.ASSISTANT_PRIOR,
        SegmentKind.USER_LIVE,
    ]


def test_classify_memory_block_vs_system():
    msgs = [
        _msg("system", "You are a helpful assistant."),
        _msg("system", "[Things to remember about this user from past conversations:]\n- Uses vim."),
        _msg("user", "Help me refactor."),
    ]
    segs = classify_segments(msgs)
    assert segs[0].kind == SegmentKind.SYSTEM
    assert segs[1].kind == SegmentKind.MEMORY_BLOCK


def test_classify_think_block_demoted():
    msgs = [
        _msg("user", "Prove it."),
        _msg("assistant", "<think>long internal reasoning about the problem...</think>answer"),
        _msg("user", "Continue."),
    ]
    segs = classify_segments(msgs)
    think_seg = next(s for s in segs if s.kind == SegmentKind.THINK_BLOCK)
    live_seg = next(s for s in segs if s.kind == SegmentKind.USER_LIVE)
    assert think_seg.importance < live_seg.importance


def test_system_and_live_user_always_pinned():
    msgs = [_msg("system", "s"), _msg("user", "u")]
    segs = classify_segments(msgs)
    assert all(s.pinned for s in segs)
    assert all(s.importance == 1.0 for s in segs)


def test_recency_boosts_newer_messages():
    # Long enough that old_a (index 2) falls outside the hot_window
    # (default 4), so its recency score is its raw position, while mid_a
    # (index 10) sits inside and gets the hot-window boost.
    msgs = [
        _msg("system", "sys"),
        _msg("user", "q0"),
        _msg("assistant", "old a"),
        _msg("user", "q1"),
        _msg("assistant", "a1"),
        _msg("user", "q2"),
        _msg("assistant", "a2"),
        _msg("user", "q3"),
        _msg("assistant", "a3"),
        _msg("user", "q4"),
        _msg("assistant", "mid a"),
        _msg("user", "fresh question about widgets"),
    ]
    segs = classify_segments(msgs)
    old_a = next(s for s in segs if s.index == 2)
    mid_a = next(s for s in segs if s.index == 10)
    assert mid_a.importance > old_a.importance


def test_tool_pair_linked_pins_both():
    msgs = [
        _msg("assistant", "calling tool", tool_call_id="call_1"),
        _msg("tool", "result payload", tool_call_id="call_1"),
        _msg("user", "what do you think about that?"),
    ]
    segs = classify_segments(msgs)
    pair = [s for s in segs if s.tool_call_id == "call_1"]
    assert len(pair) == 2
    # Both share the same importance (promoted or equalised)
    assert pair[0].importance == pair[1].importance


# ── assess_pressure ────────────────────────────────────────────────────────

def test_no_pressure_when_small():
    msgs = [_msg("system", "s"), _msg("user", "hi")]
    segs = classify_segments(msgs)
    report = assess_pressure(kv_budget_tokens=4096, segments=segs)
    assert not report.spill_needed
    assert report.over_by_tokens == 0


def test_pressure_triggers_over_threshold():
    big = "word " * 2000
    msgs = [_msg("system", "s"), _msg("user", big), _msg("assistant", big), _msg("user", "final")]
    segs = classify_segments(msgs)
    report = assess_pressure(kv_budget_tokens=1024, segments=segs)
    assert report.spill_needed
    assert report.over_by_tokens > 0


# ── plan_spillover ─────────────────────────────────────────────────────────

def test_plan_preserves_pinned():
    big = "word " * 500
    msgs = [
        _msg("system", "pinned system prompt"),
        _msg("user", "old"),
        _msg("assistant", big),
        _msg("user", "another"),
        _msg("assistant", big),
        _msg("user", "live question"),
    ]
    segs = classify_segments(msgs)
    total = sum(s.tokens for s in segs)
    plan = plan_spillover(segs, target_tokens=total // 3)
    # Pinned segments (system, live user) never spill
    spilled_kinds = {s.kind for s in plan.spilled}
    assert SegmentKind.SYSTEM not in spilled_kinds
    assert SegmentKind.USER_LIVE not in spilled_kinds
    assert plan.freed_tokens > 0


def test_plan_evicts_lowest_importance_first():
    msgs = [
        _msg("system", "s"),
        _msg("assistant", "<think>bulky internal monologue " * 200 + "</think>"),
        _msg("user", "old factual question"),
        _msg("assistant", "old factual answer"),
        _msg("user", "live question"),
    ]
    segs = classify_segments(msgs)
    total = sum(s.tokens for s in segs)
    plan = plan_spillover(segs, target_tokens=int(total * 0.6))
    assert any(s.kind == SegmentKind.THINK_BLOCK for s in plan.spilled)


def test_plan_noop_when_under_budget():
    msgs = [_msg("system", "s"), _msg("user", "hi")]
    segs = classify_segments(msgs)
    plan = plan_spillover(segs, target_tokens=100_000)
    assert plan.spilled == []
    assert plan.freed_tokens == 0


# ── apply_plan ─────────────────────────────────────────────────────────────

def test_apply_plan_removes_spilled_indices():
    msgs = [
        _msg("system", "s"),
        _msg("user", "old"),
        _msg("assistant", "reply"),
        _msg("user", "live"),
    ]
    segs = classify_segments(msgs)
    # Force a spill by targeting a budget below total
    total = sum(s.tokens for s in segs)
    plan = plan_spillover(segs, target_tokens=max(1, total // 2))
    pruned = apply_plan(msgs, plan)
    assert len(pruned) == len(msgs) - len(plan.spilled)
    # System + live user survive
    roles = [m.role for m in pruned]
    assert roles[0] == "system"
    assert roles[-1] == "user"


# ── SpillStore ─────────────────────────────────────────────────────────────

def test_spill_store_stash_and_recall():
    store = SpillStore()
    msgs = [_msg("user", "old"), _msg("user", "live")]
    segs = classify_segments(msgs)
    store.stash(conversation_id=42, segments=segs)
    assert store.size(42) == 2
    fp = segs[0].fingerprint
    hit = store.recall(42, fp)
    assert hit is not None and hit.text == "old"


def test_spill_store_cap_evicts_oldest():
    store = SpillStore(max_entries_per_conv=3)
    for i in range(5):
        segs = classify_segments([_msg("user", f"msg-{i}")])
        store.stash(7, segs)
    assert store.size(7) == 3


def test_spill_store_forget():
    store = SpillStore()
    segs = classify_segments([_msg("user", "x")])
    store.stash(1, segs)
    store.forget(1)
    assert store.size(1) == 0


# ── assess_and_plan entry point ────────────────────────────────────────────

def test_assess_and_plan_returns_no_plan_when_fits():
    msgs = [_msg("system", "s"), _msg("user", "hello")]
    a = assess_and_plan(msgs, kv_budget_tokens=4096)
    assert isinstance(a, KVAssessment)
    assert a.plan is None
    assert not a.report.spill_needed


def test_assess_and_plan_produces_plan_under_pressure():
    big = "word " * 800
    msgs = [
        _msg("system", "s"),
        _msg("user", big),
        _msg("assistant", big),
        _msg("user", big),
        _msg("assistant", big),
        _msg("user", "live"),
    ]
    a = assess_and_plan(msgs, kv_budget_tokens=1024)
    assert a.plan is not None
    assert a.plan.freed_tokens > 0
    # Event payload is serialisable
    event = a.plan.as_event(tier_id="fast")
    assert event["kind"] == "kv.spillover"
    assert event["tier"] == "fast"
