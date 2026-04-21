"""
Tests for backend/schemas.py — Pydantic model validation.

Covers required fields, optional defaults, literal constraints, and edge
cases the type system alone doesn't catch. No running services required.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from backend.schemas import (
    AgentEvent,
    ChatMessage,
    ChatRequest,
    ConversationSummary,
    ConversationUpdate,
    MessageOut,
    MessagePart,
    MeResponse,
    ModelsListResponse,
    MultiAgentOptions,
    RouteDecision,
    TierInfo,
)


# ═══════════════════════════════════════════════════════════════════════════════
# MessagePart
# ═══════════════════════════════════════════════════════════════════════════════

class TestMessagePart:

    def test_text_part(self):
        p = MessagePart(type="text", text="Hello")
        assert p.type == "text"
        assert p.text == "Hello"
        assert p.image_url is None

    def test_image_url_part(self):
        p = MessagePart(type="image_url", image_url={"url": "http://example.com/img.png"})
        assert p.type == "image_url"
        assert p.image_url == {"url": "http://example.com/img.png"}
        assert p.text is None

    def test_invalid_type_rejected(self):
        with pytest.raises(ValidationError):
            MessagePart(type="audio")

    def test_optional_fields_default_none(self):
        p = MessagePart(type="text")
        assert p.text is None
        assert p.image_url is None


# ═══════════════════════════════════════════════════════════════════════════════
# ChatMessage
# ═══════════════════════════════════════════════════════════════════════════════

class TestChatMessage:

    def test_user_role_with_string_content(self):
        m = ChatMessage(role="user", content="Hello")
        assert m.role == "user"
        assert m.content == "Hello"

    def test_assistant_role(self):
        m = ChatMessage(role="assistant", content="Hi there")
        assert m.role == "assistant"

    def test_system_role(self):
        m = ChatMessage(role="system", content="You are helpful.")
        assert m.role == "system"

    def test_tool_role(self):
        m = ChatMessage(role="tool", content="result", tool_call_id="call_123")
        assert m.role == "tool"
        assert m.tool_call_id == "call_123"

    def test_invalid_role_rejected(self):
        with pytest.raises(ValidationError):
            ChatMessage(role="moderator", content="bad")

    def test_content_as_list_of_parts(self):
        parts = [MessagePart(type="text", text="Describe this:"),
                 MessagePart(type="image_url", image_url={"url": "http://x.com/a.png"})]
        m = ChatMessage(role="user", content=parts)
        assert isinstance(m.content, list)
        assert len(m.content) == 2

    def test_optional_name_and_tool_call_id_default_none(self):
        m = ChatMessage(role="user", content="Hi")
        assert m.name is None
        assert m.tool_call_id is None


# ═══════════════════════════════════════════════════════════════════════════════
# ChatRequest
# ═══════════════════════════════════════════════════════════════════════════════

class TestChatRequest:

    def _minimal(self):
        return ChatRequest(
            model="tier.versatile",
            messages=[ChatMessage(role="user", content="Hi")],
        )

    def test_minimal_request_valid(self):
        r = self._minimal()
        assert r.model == "tier.versatile"
        assert r.stream is True  # default

    def test_stream_defaults_true(self):
        assert self._minimal().stream is True

    def test_think_defaults_none(self):
        assert self._minimal().think is None

    def test_multi_agent_defaults_none(self):
        assert self._minimal().multi_agent is None

    def test_missing_model_raises(self):
        with pytest.raises(ValidationError):
            ChatRequest(messages=[ChatMessage(role="user", content="Hi")])

    def test_missing_messages_raises(self):
        with pytest.raises(ValidationError):
            ChatRequest(model="versatile")

    def test_multi_agent_options_embedded(self):
        r = ChatRequest(
            model="versatile",
            messages=[ChatMessage(role="user", content="Q")],
            multi_agent_options=MultiAgentOptions(num_workers=3),
        )
        assert r.multi_agent_options.num_workers == 3

    def test_temperature_stored(self):
        r = ChatRequest(
            model="versatile",
            messages=[ChatMessage(role="user", content="Q")],
            temperature=0.7,
        )
        assert r.temperature == pytest.approx(0.7)


# ═══════════════════════════════════════════════════════════════════════════════
# MultiAgentOptions
# ═══════════════════════════════════════════════════════════════════════════════

class TestMultiAgentOptions:

    def test_all_fields_default_none(self):
        o = MultiAgentOptions()
        assert o.enabled is None
        assert o.num_workers is None
        assert o.worker_tier is None
        assert o.orchestrator_tier is None
        assert o.reasoning_workers is None
        assert o.interaction_mode is None
        assert o.interaction_rounds is None

    def test_explicit_values_stored(self):
        o = MultiAgentOptions(enabled=True, num_workers=3, worker_tier="fast",
                              interaction_mode="collaborative", interaction_rounds=2)
        assert o.enabled is True
        assert o.num_workers == 3
        assert o.worker_tier == "fast"
        assert o.interaction_mode == "collaborative"
        assert o.interaction_rounds == 2


# ═══════════════════════════════════════════════════════════════════════════════
# AgentEvent
# ═══════════════════════════════════════════════════════════════════════════════

class TestAgentEvent:

    def test_token_event(self):
        e = AgentEvent(type="token", data={"text": "hello"})
        assert e.type == "token"
        assert e.data == {"text": "hello"}

    def test_data_defaults_to_empty_dict(self):
        e = AgentEvent(type="agent.plan_start")
        assert e.data == {}

    def test_all_valid_types_accepted(self):
        valid_types = [
            "agent.plan_start", "agent.plan_done",
            "agent.workers_start", "agent.worker_progress", "agent.worker_done",
            "agent.refine_start",
            "agent.synthesis_start", "agent.synthesis_done",
            "route.decision", "vram.eviction", "token", "error",
        ]
        for t in valid_types:
            e = AgentEvent(type=t)
            assert e.type == t

    def test_invalid_type_rejected(self):
        with pytest.raises(ValidationError):
            AgentEvent(type="unknown.event")


# ═══════════════════════════════════════════════════════════════════════════════
# RouteDecision
# ═══════════════════════════════════════════════════════════════════════════════

class TestRouteDecision:

    def test_required_fields(self):
        r = RouteDecision(tier_name="versatile", think=False, multi_agent=False)
        assert r.tier_name == "versatile"
        assert r.think is False
        assert r.multi_agent is False

    def test_slash_commands_default_empty(self):
        r = RouteDecision(tier_name="versatile", think=False, multi_agent=False)
        assert r.slash_commands_applied == []

    def test_overrides_default_empty(self):
        r = RouteDecision(tier_name="versatile", think=False, multi_agent=False)
        assert r.overrides == {}

    def test_specialist_reason_default_none(self):
        r = RouteDecision(tier_name="versatile", think=False, multi_agent=False)
        assert r.specialist_reason is None

    def test_with_all_fields(self):
        r = RouteDecision(
            tier_name="coding",
            think=True,
            multi_agent=True,
            slash_commands_applied=["/think"],
            overrides={"temperature": 0.2},
            specialist_reason="code_block_present",
        )
        assert r.specialist_reason == "code_block_present"
        assert "/think" in r.slash_commands_applied


# ═══════════════════════════════════════════════════════════════════════════════
# TierInfo / ModelsListResponse
# ═══════════════════════════════════════════════════════════════════════════════

class TestTierInfo:

    def _tier(self):
        return TierInfo(
            id="tier.versatile",
            name="Versatile",
            description="Default tier",
            backend="ollama",
            context_window=32768,
            think_supported=True,
            vram_estimate_gb=21.0,
        )

    def test_fields_stored(self):
        t = self._tier()
        assert t.id == "tier.versatile"
        assert t.backend == "ollama"
        assert t.context_window == 32768

    def test_models_list_response(self):
        resp = ModelsListResponse(data=[self._tier()])
        assert resp.object == "list"
        assert len(resp.data) == 1

    def test_models_list_defaults_object_to_list(self):
        resp = ModelsListResponse(data=[])
        assert resp.object == "list"


# ═══════════════════════════════════════════════════════════════════════════════
# Conversation / Message models
# ═══════════════════════════════════════════════════════════════════════════════

class TestConversationModels:

    def test_conversation_summary_defaults(self):
        import time
        now = time.time()
        c = ConversationSummary(id=1, title="Chat 1", created_at=now, updated_at=now)
        assert c.memory_enabled is True
        assert c.airgap is False
        assert c.tier is None

    def test_message_out_fields(self):
        import time
        m = MessageOut(id=5, role="assistant", content="Hi", created_at=time.time())
        assert m.role == "assistant"
        assert m.tier is None
        assert m.think is None

    def test_conversation_update_all_optional(self):
        u = ConversationUpdate()
        assert u.title is None
        assert u.tier is None
        assert u.memory_enabled is None
