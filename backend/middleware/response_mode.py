"""Response-mode middleware — steers how the assistant structures its reply.

Modes (set via `response_mode` on ChatRequest):

  - immediate  : no extra instruction (current default behavior)
  - plan       : produce a numbered plan first, then stop and wait for
                 the user's go-ahead before executing
  - clarify    : ask clarifying questions FIRST; the existing clarification
                 middleware supplies the [CLARIFY] protocol, this mode just
                 force-prepends a nudge so the model always uses it
  - approval   : execute step by step, pausing after each major step to
                 request explicit user approval
  - manual_plan: user supplies their own plan text; the model follows it
                 exactly, reporting progress step by step

Each mode maps to a short system-prompt addendum. The addendum is appended
to the existing system message (which may already carry RAG context,
datetime, and the clarification protocol), so it composes cleanly.
"""

from __future__ import annotations

from typing import Literal

from ..schemas import ChatMessage


ResponseMode = Literal["immediate", "plan", "clarify", "approval", "manual_plan"]

VALID_MODES: set[str] = {"immediate", "plan", "clarify", "approval", "manual_plan"}


_PROMPTS: dict[str, str] = {
    "plan": (
        "## Response mode: PLAN FIRST\n"
        "Before doing anything, produce a concise numbered plan of how "
        "you will tackle this request. Do NOT execute any of the steps. "
        "End your reply with:\n\n"
        "    > Reply \"go\" to execute, or edit the plan above.\n\n"
        "Wait for the user's confirmation in the next turn before acting."
    ),
    "clarify": (
        "## Response mode: CLARIFY FIRST\n"
        "The user has explicitly asked you to verify your understanding "
        "before responding. Issue one [CLARIFY] block using the "
        "Clarification Protocol above, even if the request seems clear. "
        "Focus on the single most impactful ambiguity. Do NOT attempt the "
        "task in this turn."
    ),
    "approval": (
        "## Response mode: STEP-BY-STEP APPROVAL\n"
        "Execute this task incrementally. Perform ONE major step, then "
        "STOP and summarize:\n"
        "  1. What you just did\n"
        "  2. What the next step would be\n"
        "  3. Ask: \"Approve and continue?\"\n\n"
        "Do not proceed to the next step until the user explicitly "
        "approves (reply containing \"yes\", \"approve\", \"continue\", "
        "or \"go\")."
    ),
}


def _manual_plan_prompt(plan_text: str) -> str:
    safe = plan_text.strip()
    if not safe:
        return ""
    return (
        "## Response mode: USER-PROVIDED PLAN\n"
        "The user has written the following plan. Follow it exactly, "
        "one step at a time, reporting progress after each step. If a "
        "step is impossible or requires input, stop and explain.\n\n"
        "```\n"
        f"{safe}\n"
        "```\n"
        "Do not deviate from these steps. Do not skip ahead."
    )


def inject_response_mode(
    messages: list[ChatMessage],
    mode: str | None,
    plan_text: str | None = None,
) -> list[ChatMessage]:
    """Mutate-and-return. Adds the mode-specific instruction to the system
    message (creating one if needed). Unknown or `immediate` modes are
    no-ops so the default chat behavior is unchanged."""
    if not mode or mode == "immediate" or mode not in VALID_MODES:
        return messages

    if mode == "manual_plan":
        addendum = _manual_plan_prompt(plan_text or "")
        if not addendum:
            # Fallback to plain plan mode so the user still gets the
            # planning scaffold even if they forgot to supply text.
            addendum = _PROMPTS["plan"]
    else:
        addendum = _PROMPTS[mode]

    sys_msg = next((m for m in messages if m.role == "system"), None)
    if sys_msg and isinstance(sys_msg.content, str):
        if "Response mode:" not in sys_msg.content:
            sys_msg.content = f"{sys_msg.content}\n\n{addendum}"
    else:
        messages.insert(0, ChatMessage(role="system", content=addendum))
    return messages
