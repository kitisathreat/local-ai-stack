"""
Tests for backend/middleware/response_mode.py.

Covers all five modes (immediate, plan, clarify, approval, manual_plan),
the no-op cases, and the interaction with an existing system message.
No running services required.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from backend.middleware.response_mode import VALID_MODES, inject_response_mode
from backend.schemas import ChatMessage


# ── helpers ───────────────────────────────────────────────────────────────────

def _msgs(with_system: str | None = None) -> list[ChatMessage]:
    msgs: list[ChatMessage] = []
    if with_system:
        msgs.append(ChatMessage(role="system", content=with_system))
    msgs.append(ChatMessage(role="user", content="Do something for me"))
    return msgs


def _system_content(msgs: list[ChatMessage]) -> str | None:
    sys_msg = next((m for m in msgs if m.role == "system"), None)
    return sys_msg.content if sys_msg else None


# ═══════════════════════════════════════════════════════════════════════════════
# No-op cases
# ═══════════════════════════════════════════════════════════════════════════════

class TestNoOp:

    def test_none_mode_is_noop(self):
        msgs = _msgs()
        result = inject_response_mode(msgs, None)
        assert all(m.role != "system" for m in result)

    def test_immediate_mode_is_noop(self):
        msgs = _msgs()
        result = inject_response_mode(msgs, "immediate")
        assert all(m.role != "system" for m in result)

    def test_unknown_mode_is_noop(self):
        msgs = _msgs()
        result = inject_response_mode(msgs, "autorun")
        assert all(m.role != "system" for m in result)

    def test_empty_string_mode_is_noop(self):
        msgs = _msgs()
        result = inject_response_mode(msgs, "")
        assert all(m.role != "system" for m in result)

    def test_returns_same_list_object(self):
        msgs = _msgs()
        result = inject_response_mode(msgs, "immediate")
        assert result is msgs


# ═══════════════════════════════════════════════════════════════════════════════
# plan mode
# ═══════════════════════════════════════════════════════════════════════════════

class TestPlanMode:

    def test_injects_system_message(self):
        msgs = _msgs()
        inject_response_mode(msgs, "plan")
        assert any(m.role == "system" for m in msgs)

    def test_system_content_mentions_plan(self):
        msgs = _msgs()
        inject_response_mode(msgs, "plan")
        content = _system_content(msgs)
        assert "plan" in content.lower() or "PLAN" in content

    def test_appended_to_existing_system_message(self):
        msgs = _msgs(with_system="You are helpful.")
        inject_response_mode(msgs, "plan")
        content = _system_content(msgs)
        assert "You are helpful." in content
        assert "Response mode:" in content

    def test_not_re_injected_if_already_present(self):
        msgs = _msgs(with_system="Some system prompt\n\n## Response mode: PLAN FIRST\nstuff")
        inject_response_mode(msgs, "plan")
        content = _system_content(msgs)
        # Should not be doubled
        assert content.count("Response mode:") == 1

    def test_only_one_system_message_created(self):
        msgs = _msgs()
        inject_response_mode(msgs, "plan")
        system_msgs = [m for m in msgs if m.role == "system"]
        assert len(system_msgs) == 1


# ═══════════════════════════════════════════════════════════════════════════════
# clarify mode
# ═══════════════════════════════════════════════════════════════════════════════

class TestClarifyMode:

    def test_injects_clarify_instruction(self):
        msgs = _msgs()
        inject_response_mode(msgs, "clarify")
        content = _system_content(msgs)
        assert "clarify" in content.lower() or "CLARIFY" in content

    def test_clarify_appended_to_existing_system(self):
        msgs = _msgs(with_system="Existing context.")
        inject_response_mode(msgs, "clarify")
        content = _system_content(msgs)
        assert "Existing context." in content
        assert "Response mode:" in content


# ═══════════════════════════════════════════════════════════════════════════════
# approval mode
# ═══════════════════════════════════════════════════════════════════════════════

class TestApprovalMode:

    def test_injects_approval_instruction(self):
        msgs = _msgs()
        inject_response_mode(msgs, "approval")
        content = _system_content(msgs)
        assert "approve" in content.lower() or "APPROVAL" in content

    def test_approval_creates_system_when_absent(self):
        msgs = _msgs()  # no system message
        inject_response_mode(msgs, "approval")
        assert msgs[0].role == "system"


# ═══════════════════════════════════════════════════════════════════════════════
# manual_plan mode
# ═══════════════════════════════════════════════════════════════════════════════

class TestManualPlanMode:

    def test_injects_user_plan_text(self):
        plan = "1. Research\n2. Summarise\n3. Report"
        msgs = _msgs()
        inject_response_mode(msgs, "manual_plan", plan_text=plan)
        content = _system_content(msgs)
        assert plan in content

    def test_wraps_plan_in_code_fence(self):
        plan = "Step 1: Do X"
        msgs = _msgs()
        inject_response_mode(msgs, "manual_plan", plan_text=plan)
        content = _system_content(msgs)
        assert "```" in content

    def test_empty_plan_text_falls_back_to_plan_mode(self):
        msgs = _msgs()
        inject_response_mode(msgs, "manual_plan", plan_text="")
        content = _system_content(msgs)
        # Falls back to plan mode scaffold (no user plan injected)
        assert "Response mode:" in content
        assert "```" not in content  # no code fence because no user plan

    def test_none_plan_text_falls_back_to_plan_mode(self):
        msgs = _msgs()
        inject_response_mode(msgs, "manual_plan", plan_text=None)
        content = _system_content(msgs)
        assert "Response mode:" in content

    def test_plan_text_appended_to_existing_system(self):
        msgs = _msgs(with_system="Context.")
        inject_response_mode(msgs, "manual_plan", plan_text="Do X then Y")
        content = _system_content(msgs)
        assert "Context." in content
        assert "Do X then Y" in content


# ═══════════════════════════════════════════════════════════════════════════════
# VALID_MODES constant
# ═══════════════════════════════════════════════════════════════════════════════

class TestValidModes:

    def test_all_five_modes_present(self):
        assert VALID_MODES == {"immediate", "plan", "clarify", "approval", "manual_plan"}

    def test_immediate_in_valid_modes(self):
        assert "immediate" in VALID_MODES

    def test_all_non_immediate_modes_inject_something(self):
        for mode in VALID_MODES - {"immediate"}:
            msgs = _msgs()
            inject_response_mode(msgs, mode, plan_text="step 1")
            assert any(m.role == "system" for m in msgs), f"{mode!r} did not inject a system message"
