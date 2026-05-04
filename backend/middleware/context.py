"""Inject current date, time, and system context into the system prompt.

Ported from pipelines/context_injector.py. Runs inline on every request
(cheap — no I/O), prepends a `[Context: ...]` block to an existing
system message or inserts a new one at position 0.
"""

from __future__ import annotations

import logging
import os
import platform
from datetime import datetime, timezone
from typing import Iterable

from ..schemas import ChatMessage


logger = logging.getLogger(__name__)


DEFAULT_TZ = os.getenv("LAI_TZ", "America/New_York")


def build_context_string(
    *,
    inject_datetime: bool = True,
    inject_system_info: bool = False,
    custom_text: str = "",
    tz_name: str = DEFAULT_TZ,
) -> str:
    parts: list[str] = []
    if inject_datetime:
        try:
            import zoneinfo
            tz = zoneinfo.ZoneInfo(tz_name)
            now = datetime.now(tz)
        except Exception:
            now = datetime.now(timezone.utc)
        parts.append(
            f"Current date and time: {now.strftime('%A, %B %d, %Y at %I:%M %p')} "
            f"({now.strftime('%Z')})"
        )
    if inject_system_info:
        parts.append(f"System: {platform.system()} {platform.release()}")
    if custom_text.strip():
        parts.append(custom_text.strip())
    return "\n".join(parts)


def inject_system_context(
    messages: list[ChatMessage],
    *,
    inject_datetime: bool = True,
    inject_system_info: bool = False,
    custom_text: str = "",
    tz_name: str = DEFAULT_TZ,
) -> list[ChatMessage]:
    """Mutate-and-return. Adds `[Context: ...]` to an existing system
    message, or inserts a new one if none is present."""
    ctx = build_context_string(
        inject_datetime=inject_datetime,
        inject_system_info=inject_system_info,
        custom_text=custom_text,
        tz_name=tz_name,
    )
    if not ctx:
        return messages

    injection = f"[Context: {ctx}]"
    for msg in messages:
        if msg.role == "system" and isinstance(msg.content, str):
            msg.content = f"{msg.content}\n\n{injection}" if msg.content else injection
            return messages
    messages.insert(0, ChatMessage(role="system", content=injection))
    return messages


def inject_skills(
    messages: list[ChatMessage],
    skills_registry,
    slugs: Iterable[str] | None,
) -> list[ChatMessage]:
    """Prepend the rendered system-prompt fragments for every active skill
    to the system message (or insert a fresh one). No-op when ``slugs`` is
    falsy or no slug resolves to a known, enabled skill — keeps the
    request shape backwards-compatible when the chat client doesn't yet
    know about skills."""
    if not slugs:
        return messages
    rendered = skills_registry.render_combined_prompt(list(slugs))
    if not rendered:
        return messages
    for msg in messages:
        if msg.role == "system" and isinstance(msg.content, str):
            # Skills go BEFORE [Context: ...] so the model sees the
            # capability framing first.
            msg.content = f"{rendered}\n\n{msg.content}" if msg.content else rendered
            return messages
    messages.insert(0, ChatMessage(role="system", content=rendered))
    return messages
