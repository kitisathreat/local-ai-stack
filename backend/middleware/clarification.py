"""Clarification protocol — teach the model when to ask for clarification
using a structured [CLARIFY] block, and reformat any [CLARIFY] blocks in
the assistant's response into polished markdown.

Ported from pipelines/clarification_filter.py. Two functions:
  - `inject_clarification_instruction` — runs on each request, adds the
    protocol to the system prompt (skipped if the last N turns already
    contain a clarify block, to avoid loops).
  - `format_clarifications` — runs on each assistant response; parses any
    `[CLARIFY]...[/CLARIFY]` blocks and renders them as multiple-choice
    markdown with numbered emoji options.
"""

from __future__ import annotations

import os
import re
from typing import Iterable

from ..schemas import ChatMessage


CLARIFICATION_INSTRUCTION = """
## Clarification Protocol

When a user request is genuinely ambiguous or could be interpreted in
significantly different ways that would lead to meaningfully different
outputs, ask for clarification BEFORE attempting the task.

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


AMBIGUOUS_PATTERNS = [
    r"\b(something|anything|whatever|some kind of|any kind of)\b",
    r"\b(best|good|nice|cool|interesting|useful)\b.{0,20}\b(for me|to use|option)\b",
    r"\b(make|create|build|write|generate|design)\b.{0,10}\b(a|an|some)\b.{0,20}$",
    r"\b(analyze|analyse|look at|check out|explore)\b.{0,10}\b(this|it|that)\b",
    r"\b(help me with|assist with|work on)\b",
    r"^(help|ideas|suggestions|options|recommendations)\b",
]

CLEAR_INTENT_PATTERNS = [
    r"\b(specifically|exactly|precisely|step by step|in detail)\b",
    r"\bwhat (is|are|does|do|was|were|will|would)\b",
    r"\bhow (do|does|can|should|to)\b",
    r"\bwhy (is|are|does|did|would|should)\b",
    r"\b(explain|define|show me (how|the|a|an))\b",
]


def is_likely_ambiguous(message: str) -> bool:
    msg = message.strip().lower()
    if len(msg) < 15:
        return False
    for p in CLEAR_INTENT_PATTERNS:
        if re.search(p, msg):
            return False
    return any(re.search(p, msg) for p in AMBIGUOUS_PATTERNS)


def has_recent_clarification(messages: list[ChatMessage], window: int = 4) -> bool:
    recent = messages[-window:] if len(messages) >= window else messages
    for m in recent:
        if m.role == "assistant" and isinstance(m.content, str):
            if "[CLARIFY]" in m.content or "🤔" in m.content:
                return True
    return False


def inject_clarification_instruction(messages: list[ChatMessage]) -> list[ChatMessage]:
    """Mutate-and-return. Appends the protocol to an existing system
    message (or inserts a new one). Also adds a gentle nudge if the
    latest user message is flagged ambiguous."""
    # Bench mode bypass — bench prompts are deterministic Q&A, the
    # protocol just adds noise that the coding tier (in particular)
    # interprets as "ask a clarifying question instead of answering".
    if os.getenv("LAI_DISABLE_CLARIFICATION") == "1":
        return messages
    if has_recent_clarification(messages):
        return messages

    sys_msg = next((m for m in messages if m.role == "system"), None)
    if sys_msg and isinstance(sys_msg.content, str):
        if "[CLARIFY]" not in sys_msg.content and "Clarification Protocol" not in sys_msg.content:
            sys_msg.content = f"{sys_msg.content}\n{CLARIFICATION_INSTRUCTION}"
    else:
        messages.insert(0, ChatMessage(role="system", content=CLARIFICATION_INSTRUCTION.strip()))

    last_user = ""
    for m in reversed(messages):
        if m.role == "user" and isinstance(m.content, str):
            last_user = m.content
            break
    if last_user and is_likely_ambiguous(last_user):
        sys_msg = next((m for m in messages if m.role == "system"), None)
        if sys_msg and isinstance(sys_msg.content, str):
            sys_msg.content += (
                "\n\n[Note: The current user message appears open-ended. "
                "Consider whether a [CLARIFY] question would help you give a better answer.]"
            )
    return messages


_NUMBER_EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣"]


def _format_clarify_block(block: str) -> str:
    q_match = re.search(r"Q:\s*(.+?)(?=\nO:|$)", block, re.DOTALL)
    o_match = re.search(r"O:\s*(.+?)$", block, re.DOTALL)
    question = q_match.group(1).strip() if q_match else block.strip()
    options_raw = o_match.group(1).strip() if o_match else ""
    options = [o.strip() for o in options_raw.split("|") if o.strip()][:6]

    lines = ["\n\n---\n", f"🤔 **{question}**\n"]
    if options:
        for i, opt in enumerate(options):
            icon = _NUMBER_EMOJIS[i] if i < len(_NUMBER_EMOJIS) else f"{i+1}."
            lines.append(f">{icon} {opt}")
        lines.append("\n>✏️ **Custom:** Type your own answer")
    else:
        lines.append(">✏️ Please type your answer below.")
    lines.append("\n_Reply with a number to select an option, or type a custom answer._\n")
    lines.append("---\n")
    return "\n".join(lines)


def format_clarifications(content: str) -> str:
    """Replace all [CLARIFY]...[/CLARIFY] blocks with rendered markdown."""
    return re.sub(
        r"\[CLARIFY\](.*?)\[/CLARIFY\]",
        lambda m: _format_clarify_block(m.group(1)),
        content,
        flags=re.DOTALL,
    )
