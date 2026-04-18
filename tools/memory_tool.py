"""
title: Long-Term Memory
author: local-ai-stack
description: Store and recall facts across conversations using Open WebUI's memory API. Persists important context about the user.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from pydantic import BaseModel, Field
from typing import Callable, Any, Optional


class Tools:
    class Valves(BaseModel):
        WEBUI_URL: str = Field(
            default="http://localhost:3000",
            description="Open WebUI base URL for the memory API",
        )

    def __init__(self):
        self.valves = self.Valves()

    async def save_memory(
        self,
        content: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Save an important fact or piece of information to long-term memory.
        Use this when the user shares something worth remembering across conversations.
        :param content: The fact or information to remember
        :return: Confirmation that the memory was saved
        """
        if __event_emitter__:
            await __event_emitter__(
                {"type": "status", "data": {"description": "Saving to memory...", "done": False}}
            )

        try:
            token = __user__.get("token", "") if __user__ else ""
            headers = {"Authorization": f"Bearer {token}"} if token else {}

            payload = {"content": content}
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{self.valves.WEBUI_URL}/api/v1/memories/add",
                    json=payload,
                    headers=headers,
                )

            if resp.status_code in (200, 201):
                if __event_emitter__:
                    await __event_emitter__(
                        {"type": "status", "data": {"description": "Memory saved", "done": True}}
                    )
                return f"Saved to memory: {content}"
            else:
                return f"Memory save failed (HTTP {resp.status_code}). The memory API may not be enabled."

        except httpx.ConnectError:
            return "Cannot reach memory API. Ensure Open WebUI is running."
        except Exception as e:
            return f"Memory error: {str(e)}"

    async def search_memories(
        self,
        query: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search stored memories for relevant information about the user or past conversations.
        :param query: What to search for in memory
        :return: Relevant memories found
        """
        try:
            token = __user__.get("token", "") if __user__ else ""
            headers = {"Authorization": f"Bearer {token}"} if token else {}

            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{self.valves.WEBUI_URL}/api/v1/memories",
                    headers=headers,
                )

            if resp.status_code != 200:
                return "Cannot retrieve memories. Memory API may not be enabled."

            memories = resp.json()
            if not memories:
                return "No memories stored yet."

            query_lower = query.lower()
            relevant = [
                m for m in memories
                if query_lower in m.get("content", "").lower()
            ]

            if not relevant:
                return f"No memories found related to: {query}"

            lines = [f"## Memories related to '{query}':\n"]
            for m in relevant[:10]:
                lines.append(f"- {m.get('content', '')}")

            return "\n".join(lines)

        except Exception as e:
            return f"Memory search error: {str(e)}"
