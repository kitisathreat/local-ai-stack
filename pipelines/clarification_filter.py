"""
title: Clarification Gate — Auto-Detect Ambiguity & Format Clarifying Questions
author: local-ai-stack
description: A filter pipeline that (1) injects system-prompt guidance teaching the model when and how to ask for clarification using a structured [CLARIFY] block, and (2) intercepts those blocks in the model's response and renders them as polished multiple-choice UI in the chat. Works alongside the ask_clarification tool — the tool is for explicit model-initiated questions, this pipeline handles auto-formatting and provides the instruction pattern.
required_open_webui_version: 0.4.0
version: 1.0.0
licence: MIT
"""

import re
from typing import Optional, Callable, Any
from pydantic import BaseModel, Field


# ── Prompt instruction injected into every system prompt ─────────────────────

CLARIFICATION_INSTRUCTION = """
## Clarification Protocol

When a user request is genuinely ambiguous or could be interpreted in significantly different ways that would lead to meaningfully different outputs, ask for clarification BEFORE attempting the task.

Use this EXACT format — nothing else will be rendered correctly:

[CLARIFY]
Q: <your single clarifying question>
O: <option 1> | <option 2> | <option 3> | <option 4>
[/CLARIFY]

Rules:
- Only ask when NECESSARY — clear, specific requests should be answered directly
- Provide 2–5 concrete options that cover the most likely interpretations
- Options must be short phrases (3–7 words each), separated by |
- One question per [CLARIFY] block; ask only the MOST important question
- After the user replies (a number or custom text), proceed immediately — no second clarification
- For simple yes/no questions, you may use just two options: Yes | No
"""


# ── Ambiguity heuristics ──────────────────────────────────────────────────────

# Patterns that suggest the request might benefit from clarification
AMBIGUOUS_PATTERNS = [
    r"\b(something|anything|whatever|some kind of|any kind of)\b",
    r"\b(best|good|nice|cool|interesting|useful)\b.{0,20}\b(for me|to use|option)\b",
    r"\b(make|create|build|write|generate|design)\b.{0,10}\b(a|an|some)\b.{0,20}$",
    r"\b(analyze|analyse|look at|check out|explore)\b.{0,10}\b(this|it|that)\b",
    r"\b(help me with|assist with|work on)\b",
    r"^(help|ideas|suggestions|options|recommendations)\b",
    r"\b(etc|and so on|and stuff|or something)\b",
    r"\b(like before|same as last time|like we did)\b",
]

# Keywords that strongly suggest a clear intent — don't ask for clarification
CLEAR_INTENT_PATTERNS = [
    r"\b(specifically|exactly|precisely|step by step|in detail)\b",
    r"\b(using|with|in|via|through)\b.{0,15}\b(python|javascript|sql|excel|csv|json|yaml|markdown|format|method|api)\b",
    r"\bwhat (is|are|does|do|was|were|will|would)\b",
    r"\bhow (do|does|can|should|to)\b",
    r"\bwhy (is|are|does|did|would|should)\b",
    r"\bexplain\b",
    r"\bdefine\b",
    r"\bshow me (how|the|a|an)\b",
]

# Short messages that are almost always clear commands
CLEAR_SHORT_PATTERNS = [
    r"^(yes|no|ok|okay|sure|go ahead|proceed|continue|thanks|thank you|stop|cancel)\b",
    r"^\d+$",  # User picking an option number
    r"^option \d+",
]


def _is_likely_ambiguous(message: str) -> bool:
    """Heuristic check — returns True if the message might benefit from clarification."""
    msg = message.strip().lower()

    # Very short messages are usually clear (commands, option picks, confirmations)
    if len(msg) < 15:
        return False

    # Check for clear-intent signals first
    for pattern in CLEAR_INTENT_PATTERNS:
        if re.search(pattern, msg):
            return False

    for pattern in CLEAR_SHORT_PATTERNS:
        if re.search(pattern, msg):
            return False

    # Check for ambiguity signals
    for pattern in AMBIGUOUS_PATTERNS:
        if re.search(pattern, msg):
            return True

    return False


def _last_user_message(messages: list) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "user":
            return msg.get("content", "")
    return ""


def _has_recent_clarification(messages: list) -> bool:
    """Check if we already asked for clarification recently (avoid loops)."""
    recent = messages[-4:] if len(messages) >= 4 else messages
    for msg in recent:
        if msg.get("role") == "assistant":
            content = msg.get("content", "")
            if "[CLARIFY]" in content or "🤔" in content:
                return True
    return False


# ── Response formatter ────────────────────────────────────────────────────────

def _format_clarify_block(block_content: str) -> str:
    """Parse a [CLARIFY]...[/CLARIFY] block and render as polished markdown."""
    q_match = re.search(r"Q:\s*(.+?)(?=\nO:|$)", block_content, re.DOTALL)
    o_match = re.search(r"O:\s*(.+?)$", block_content, re.DOTALL)

    question = q_match.group(1).strip() if q_match else block_content.strip()
    options_raw = o_match.group(1).strip() if o_match else ""
    options = [o.strip() for o in options_raw.split("|") if o.strip()][:6]

    number_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣"]

    lines = ["\n\n---\n"]
    lines.append(f"🤔 **{question}**\n")

    if options:
        for i, opt in enumerate(options):
            icon = number_emojis[i] if i < len(number_emojis) else f"{i+1}."
            lines.append(f">{icon} {opt}")
        lines.append("\n>✏️ **Custom:** Type your own answer")
    else:
        lines.append(">✏️ Please type your answer below.")

    lines.append("\n_Reply with a number to select an option, or type a custom answer._\n")
    lines.append("---\n")

    return "\n".join(lines)


def _reformat_clarifications(content: str) -> str:
    """Replace all [CLARIFY]...[/CLARIFY] blocks in a string with formatted markdown."""
    return re.sub(
        r"\[CLARIFY\](.*?)\[/CLARIFY\]",
        lambda m: _format_clarify_block(m.group(1)),
        content,
        flags=re.DOTALL,
    )


# ── Pipeline ──────────────────────────────────────────────────────────────────

class Filter:
    class Valves(BaseModel):
        INJECT_INSTRUCTION: bool = Field(
            default=True,
            description="Inject clarification protocol into the system prompt so the model knows how to ask questions",
        )
        AUTO_DETECT_AMBIGUITY: bool = Field(
            default=True,
            description="Use heuristics to detect ambiguous requests and add a gentle nudge to ask for clarification",
        )
        FORMAT_RESPONSES: bool = Field(
            default=True,
            description="Automatically reformat [CLARIFY] blocks in model responses into polished markdown",
        )

    def __init__(self):
        self.valves = self.Valves()

    async def inlet(
        self,
        body: dict,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> dict:
        if not self.valves.INJECT_INSTRUCTION:
            return body

        messages = body.get("messages", [])
        if not messages:
            return body

        # Don't inject if we already recently asked for clarification (avoid loops)
        if _has_recent_clarification(messages):
            return body

        # Find or create system message
        system_msg = next((m for m in messages if m.get("role") == "system"), None)

        if system_msg:
            existing = system_msg.get("content", "")
            # Avoid double-injection if already present
            if "[CLARIFY]" not in existing and "Clarification Protocol" not in existing:
                system_msg["content"] = existing + "\n" + CLARIFICATION_INSTRUCTION
        else:
            messages.insert(0, {"role": "system", "content": CLARIFICATION_INSTRUCTION.strip()})
            body["messages"] = messages

        # Optional: if the request looks ambiguous, add an extra nudge
        if self.valves.AUTO_DETECT_AMBIGUITY:
            last_msg = _last_user_message(messages)
            if _is_likely_ambiguous(last_msg):
                # Find the system message and append a gentle nudge
                sys_msg = next((m for m in body["messages"] if m.get("role") == "system"), None)
                if sys_msg:
                    sys_msg["content"] += (
                        "\n\n[Note: The current user message appears open-ended. "
                        "Consider whether a [CLARIFY] question would help you give a better answer.]"
                    )

        return body

    async def outlet(
        self,
        body: dict,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> dict:
        if not self.valves.FORMAT_RESPONSES:
            return body

        messages = body.get("messages", [])
        if not messages:
            return body

        last = messages[-1]
        if last.get("role") != "assistant":
            return body

        content = last.get("content", "")
        if "[CLARIFY]" in content:
            last["content"] = _reformat_clarifications(content)

        return body
