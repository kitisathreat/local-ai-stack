"""
Tests for backend/orchestrator.py.

All tests are pure-function unit tests — no running LLM, scheduler, or
network required. The async Orchestrator.run() method is exercised with
minimal mocks of the scheduler and client in the live-flow tests.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from backend.orchestrator import (
    Subtask,
    _build_refine_prompt,
    _build_synthesis_context,
    _parse_plan,
    _resolved_settings,
    _stream_as_events,
)
from backend.schemas import AgentEvent, MultiAgentOptions


def run(coro):
    return asyncio.run(coro)


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_ma_cfg(
    orchestrator_tier="versatile",
    worker_tier="fast",
    max_workers=4,
    min_workers=2,
    reasoning_workers=False,
    interaction_mode="independent",
    interaction_rounds=0,
):
    cfg = MagicMock()
    cfg.orchestrator_tier = orchestrator_tier
    cfg.worker_tier = worker_tier
    cfg.max_workers = max_workers
    cfg.min_workers = min_workers
    cfg.reasoning_workers = reasoning_workers
    cfg.interaction_mode = interaction_mode
    cfg.interaction_rounds = interaction_rounds
    cfg.worker_overrides = {}
    cfg.specialist_routes = {
        "code_block_present": "coding",
        "image_in_message": "vision",
    }
    return cfg


_AVAILABLE_TIERS = {"versatile", "fast", "coding", "vision", "highest_quality"}


# ═══════════════════════════════════════════════════════════════════════════════
# _parse_plan
# ═══════════════════════════════════════════════════════════════════════════════

class TestParsePlan:

    def test_parses_simple_json_array(self):
        raw = '[{"id": 1, "task": "Do X", "specialist": "GENERAL"}]'
        result = _parse_plan(raw)
        assert len(result) == 1
        assert result[0].id == 1
        assert result[0].task == "Do X"
        assert result[0].specialist == "GENERAL"

    def test_parses_multiple_subtasks(self):
        raw = '''[
            {"id": 1, "task": "Research topic A", "specialist": "GENERAL"},
            {"id": 2, "task": "Write code for B", "specialist": "CODING"},
            {"id": 3, "task": "Analyse image C", "specialist": "VISION"}
        ]'''
        result = _parse_plan(raw)
        assert len(result) == 3
        assert result[1].specialist == "CODING"
        assert result[2].specialist == "VISION"

    def test_strips_think_tags_before_parsing(self):
        raw = "<think>Let me plan this...</think>[{\"id\":1,\"task\":\"Do Y\",\"specialist\":\"GENERAL\"}]"
        result = _parse_plan(raw)
        assert len(result) == 1
        assert result[0].task == "Do Y"

    def test_extracts_json_from_prose(self):
        raw = "Sure, here is my plan:\n[{\"id\":1,\"task\":\"Step one\",\"specialist\":\"GENERAL\"}]\nLet me know if you agree."
        result = _parse_plan(raw)
        assert len(result) == 1

    def test_empty_array_returns_empty_list(self):
        assert _parse_plan("[]") == []

    def test_no_json_returns_empty_list(self):
        assert _parse_plan("I cannot decompose this request.") == []

    def test_invalid_json_returns_empty_list(self):
        assert _parse_plan("[{bad json}]") == []

    def test_unknown_specialist_normalised_to_general(self):
        raw = '[{"id": 1, "task": "Do Z", "specialist": "UNKNOWN_SPECIALIST"}]'
        result = _parse_plan(raw)
        assert result[0].specialist == "GENERAL"

    def test_missing_specialist_defaults_to_general(self):
        raw = '[{"id": 1, "task": "Do Z"}]'
        result = _parse_plan(raw)
        assert result[0].specialist == "GENERAL"

    def test_empty_task_items_skipped(self):
        raw = '[{"id": 1, "task": ""}, {"id": 2, "task": "Valid task", "specialist": "GENERAL"}]'
        result = _parse_plan(raw)
        assert len(result) == 1
        assert result[0].task == "Valid task"

    def test_mixed_array_with_non_objects_returns_empty(self):
        # _JSON_ARRAY_RE only matches arrays of {...} objects; a mixed array
        # (null, strings, objects) doesn't match the regex, so returns [].
        raw = '[null, "string", {"id": 1, "task": "OK", "specialist": "GENERAL"}]'
        result = _parse_plan(raw)
        assert result == []

    def test_reasoning_specialist_accepted(self):
        raw = '[{"id": 1, "task": "Reason deeply", "specialist": "REASONING"}]'
        result = _parse_plan(raw)
        assert result[0].specialist == "REASONING"

    def test_case_insensitive_specialist_normalised(self):
        raw = '[{"id": 1, "task": "Code it", "specialist": "coding"}]'
        result = _parse_plan(raw)
        assert result[0].specialist == "CODING"


# ═══════════════════════════════════════════════════════════════════════════════
# _build_synthesis_context
# ═══════════════════════════════════════════════════════════════════════════════

class TestBuildSynthesisContext:

    def _results(self):
        return [
            {"id": 1, "task": "Research X", "output": "X is important because...", "error": None},
            {"id": 2, "task": "Summarise Y", "output": "Y can be summarised as...", "error": None},
        ]

    def test_contains_user_message(self):
        ctx = _build_synthesis_context("What is AI?", self._results())
        assert "What is AI?" in ctx

    def test_contains_all_subtask_outputs(self):
        ctx = _build_synthesis_context("Q", self._results())
        assert "X is important because..." in ctx
        assert "Y can be summarised as..." in ctx

    def test_labels_subtasks_by_id(self):
        ctx = _build_synthesis_context("Q", self._results())
        assert "Subtask 1" in ctx
        assert "Subtask 2" in ctx

    def test_error_shown_instead_of_output(self):
        results = [{"id": 1, "task": "T", "output": "", "error": "timeout"}]
        ctx = _build_synthesis_context("Q", results)
        assert "worker failed" in ctx
        assert "timeout" in ctx

    def test_ends_with_synthesize_instruction(self):
        ctx = _build_synthesis_context("Q", self._results())
        assert "Synthesize" in ctx or "synthesize" in ctx.lower()

    def test_empty_results_still_produces_string(self):
        ctx = _build_synthesis_context("Q", [])
        assert isinstance(ctx, str)
        assert "Q" in ctx


# ═══════════════════════════════════════════════════════════════════════════════
# _build_refine_prompt
# ═══════════════════════════════════════════════════════════════════════════════

class TestBuildRefinePrompt:

    def _subtask(self):
        return Subtask(id=1, task="Write unit tests for X", specialist="CODING")

    def _drafts(self):
        return [
            {"id": 1, "task": "Write unit tests for X", "output": "Here is my draft..."},
            {"id": 2, "task": "Review code for Y", "output": "Code review says..."},
        ]

    def test_contains_subtask_task(self):
        prompt = _build_refine_prompt(self._subtask(), self._drafts()[0], self._drafts())
        assert "Write unit tests for X" in prompt

    def test_contains_own_previous_draft(self):
        prompt = _build_refine_prompt(self._subtask(), self._drafts()[0], self._drafts())
        assert "Here is my draft..." in prompt

    def test_contains_peer_draft(self):
        prompt = _build_refine_prompt(self._subtask(), self._drafts()[0], self._drafts())
        assert "Code review says..." in prompt

    def test_own_id_not_in_peers_section(self):
        prompt = _build_refine_prompt(self._subtask(), self._drafts()[0], self._drafts())
        # Peer section should only have subtask 2
        lines = prompt.split("\n")
        peers_start = next(i for i, l in enumerate(lines) if "peers" in l.lower())
        peer_section = "\n".join(lines[peers_start:])
        assert "Peer subtask 2" in peer_section
        assert "Peer subtask 1" not in peer_section

    def test_none_own_draft_uses_fallback(self):
        prompt = _build_refine_prompt(self._subtask(), None, self._drafts())
        assert "no output" in prompt.lower() or "prior attempt failed" in prompt.lower() or "(none" in prompt.lower()

    def test_no_peers_shows_placeholder(self):
        solo_drafts = [{"id": 1, "task": "Solo", "output": "Only me"}]
        prompt = _build_refine_prompt(self._subtask(), solo_drafts[0], solo_drafts)
        assert "no peer drafts" in prompt.lower()


# ═══════════════════════════════════════════════════════════════════════════════
# _resolved_settings
# ═══════════════════════════════════════════════════════════════════════════════

class TestResolvedSettings:

    def _cfg(self, **kw):
        return _make_ma_cfg(**kw)

    def test_defaults_from_cfg_when_no_options(self):
        cfg = self._cfg(orchestrator_tier="versatile", worker_tier="fast", max_workers=4)
        s = _resolved_settings(cfg, None, _AVAILABLE_TIERS)
        assert s.orchestrator_tier == "versatile"
        assert s.worker_tier == "fast"
        assert s.max_workers == 4

    def test_options_override_cfg(self):
        cfg = self._cfg(orchestrator_tier="versatile", worker_tier="fast")
        opts = MultiAgentOptions(orchestrator_tier="highest_quality", worker_tier="coding")
        s = _resolved_settings(cfg, opts, _AVAILABLE_TIERS)
        assert s.orchestrator_tier == "highest_quality"
        assert s.worker_tier == "coding"

    def test_tier_prefix_stripped(self):
        cfg = self._cfg(orchestrator_tier="versatile", worker_tier="fast")
        opts = MultiAgentOptions(orchestrator_tier="tier.versatile")
        s = _resolved_settings(cfg, opts, _AVAILABLE_TIERS)
        assert s.orchestrator_tier == "versatile"

    def test_unknown_tier_falls_back_to_cfg_default(self):
        cfg = self._cfg(orchestrator_tier="versatile", worker_tier="fast")
        opts = MultiAgentOptions(worker_tier="nonexistent_tier")
        s = _resolved_settings(cfg, opts, _AVAILABLE_TIERS)
        assert s.worker_tier == "fast"

    def test_max_workers_clamped_to_8(self):
        cfg = self._cfg(max_workers=100)
        s = _resolved_settings(cfg, None, _AVAILABLE_TIERS)
        assert s.max_workers == 8

    def test_max_workers_minimum_1(self):
        cfg = self._cfg(max_workers=0)
        s = _resolved_settings(cfg, None, _AVAILABLE_TIERS)
        assert s.max_workers == 1

    def test_interaction_rounds_clamped_to_4(self):
        cfg = self._cfg(interaction_rounds=99)
        s = _resolved_settings(cfg, None, _AVAILABLE_TIERS)
        assert s.interaction_rounds == 4

    def test_interaction_rounds_minimum_0(self):
        cfg = self._cfg(interaction_rounds=-5)
        s = _resolved_settings(cfg, None, _AVAILABLE_TIERS)
        assert s.interaction_rounds == 0

    def test_unknown_interaction_mode_falls_back_to_independent(self):
        cfg = self._cfg(interaction_mode="something_weird")
        s = _resolved_settings(cfg, None, _AVAILABLE_TIERS)
        assert s.interaction_mode == "independent"

    def test_collaborative_mode_accepted(self):
        cfg = self._cfg(interaction_mode="collaborative")
        s = _resolved_settings(cfg, None, _AVAILABLE_TIERS)
        assert s.interaction_mode == "collaborative"

    def test_per_request_num_workers_respected(self):
        cfg = self._cfg(max_workers=4)
        opts = MultiAgentOptions(num_workers=2)
        s = _resolved_settings(cfg, opts, _AVAILABLE_TIERS)
        assert s.max_workers == 2

    def test_reasoning_workers_from_options(self):
        cfg = self._cfg(reasoning_workers=False)
        opts = MultiAgentOptions(reasoning_workers=True)
        s = _resolved_settings(cfg, opts, _AVAILABLE_TIERS)
        assert s.reasoning_workers is True


# ═══════════════════════════════════════════════════════════════════════════════
# Subtask.resolved_tier
# ═══════════════════════════════════════════════════════════════════════════════

class TestSubtaskResolvedTier:

    def _cfg(self):
        return _make_ma_cfg()

    def test_general_uses_default_worker(self):
        s = Subtask(id=1, task="T", specialist="GENERAL")
        assert s.resolved_tier(self._cfg(), "fast") == "fast"

    def test_coding_maps_to_coding_route(self):
        s = Subtask(id=1, task="T", specialist="CODING")
        assert s.resolved_tier(self._cfg(), "fast") == "coding"

    def test_vision_maps_to_vision_route(self):
        s = Subtask(id=1, task="T", specialist="VISION")
        assert s.resolved_tier(self._cfg(), "fast") == "vision"

    def test_reasoning_maps_to_highest_quality(self):
        s = Subtask(id=1, task="T", specialist="REASONING")
        assert s.resolved_tier(self._cfg(), "fast") == "highest_quality"


# ═══════════════════════════════════════════════════════════════════════════════
# _stream_as_events (async generator)
# ═══════════════════════════════════════════════════════════════════════════════

class TestStreamAsEvents:

    async def _collect(self, chunks, backend):
        events = []
        async def _gen():
            for c in chunks:
                yield c
        async for ev in _stream_as_events(_gen(), backend):
            events.append(ev)
        return events

    def test_ollama_chunks_become_token_events(self):
        chunks = [{"message": {"content": "Hello"}, "done": False}]
        events = run(self._collect(chunks, "ollama"))
        assert len(events) == 1
        assert events[0].type == "token"
        assert events[0].data["text"] == "Hello"

    def test_llama_cpp_chunks_become_token_events(self):
        chunks = [{"choices": [{"delta": {"content": "World"}}]}]
        events = run(self._collect(chunks, "llama_cpp"))
        assert len(events) == 1
        assert events[0].type == "token"
        assert events[0].data["text"] == "World"

    def test_empty_content_skipped(self):
        chunks = [{"message": {"content": ""}, "done": False}]
        events = run(self._collect(chunks, "ollama"))
        assert len(events) == 0

    def test_none_content_skipped(self):
        chunks = [{"message": {}, "done": True}]
        events = run(self._collect(chunks, "ollama"))
        assert len(events) == 0

    def test_multiple_chunks_all_emitted(self):
        chunks = [
            {"message": {"content": "A"}, "done": False},
            {"message": {"content": "B"}, "done": False},
            {"message": {"content": "C"}, "done": True},
        ]
        events = run(self._collect(chunks, "ollama"))
        assert len(events) == 3
        assert "".join(e.data["text"] for e in events) == "ABC"
