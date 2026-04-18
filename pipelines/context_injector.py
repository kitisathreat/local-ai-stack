"""
title: System Context Injector
author: local-ai-stack
description: Automatically injects current date, time, and system context into every conversation. Keeps the model aware of temporal context.
required_open_webui_version: 0.4.0
version: 1.0.0
licence: MIT
"""

from datetime import datetime
from typing import Optional, Callable, Any
from pydantic import BaseModel, Field
import platform


class Filter:
    class Valves(BaseModel):
        INJECT_DATETIME: bool = Field(
            default=True,
            description="Inject current date and time into system prompt",
        )
        INJECT_SYSTEM_INFO: bool = Field(
            default=False,
            description="Inject basic system information (OS, hostname)",
        )
        TIMEZONE: str = Field(
            default="America/New_York",
            description="Timezone for injected date/time (IANA format)",
        )
        CUSTOM_CONTEXT: str = Field(
            default="",
            description="Optional custom text to always append to the system prompt",
        )

    def __init__(self):
        self.valves = self.Valves()

    def _get_context(self) -> str:
        lines = []

        if self.valves.INJECT_DATETIME:
            try:
                import pytz
                tz = pytz.timezone(self.valves.TIMEZONE)
                now = datetime.now(tz)
            except Exception:
                now = datetime.utcnow()

            lines.append(
                f"Current date and time: {now.strftime('%A, %B %d, %Y at %I:%M %p')} "
                f"({now.strftime('%Z')})"
            )

        if self.valves.INJECT_SYSTEM_INFO:
            lines.append(f"System: {platform.system()} {platform.release()}")

        if self.valves.CUSTOM_CONTEXT.strip():
            lines.append(self.valves.CUSTOM_CONTEXT.strip())

        return "\n".join(lines)

    async def inlet(
        self,
        body: dict,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> dict:
        context = self._get_context()
        if not context:
            return body

        messages = body.get("messages", [])
        if not messages:
            return body

        system_msg = None
        for msg in messages:
            if msg.get("role") == "system":
                system_msg = msg
                break

        if system_msg:
            existing = system_msg.get("content", "")
            system_msg["content"] = f"{existing}\n\n[Context: {context}]"
        else:
            messages.insert(0, {"role": "system", "content": f"[Context: {context}]"})
            body["messages"] = messages

        return body

    async def outlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        return body
