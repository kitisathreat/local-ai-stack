"""llama.cpp server client — OpenAI-compatible /v1/chat/completions.
Used for the Vision tier (Qwen3.6 35B with mmproj-F16.gguf).

Supports multimodal messages (image_url parts pass through verbatim).
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx

from ..config import TierConfig
from ..schemas import ChatMessage


def _messages_to_payload(messages: list[ChatMessage]) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        if isinstance(m.content, str):
            out.append({"role": m.role, "content": m.content})
        else:
            # OpenAI multimodal shape: content is a list of {type, ...} parts
            parts: list[dict] = []
            for p in m.content:
                if p.type == "text":
                    parts.append({"type": "text", "text": p.text or ""})
                elif p.type == "image_url" and p.image_url:
                    parts.append({"type": "image_url", "image_url": p.image_url})
            out.append({"role": m.role, "content": parts})
    return out


class LlamaCppClient:
    def __init__(self, endpoint: str, timeout_sec: float = 300.0):
        self.endpoint = endpoint.rstrip("/")
        self.timeout = httpx.Timeout(timeout_sec, connect=10.0)

    async def chat_stream(
        self,
        tier: TierConfig,
        messages: list[ChatMessage],
        think: bool,
        extra_options: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict]:
        """Yields OpenAI-style SSE data objects (parsed)."""
        params = tier.params or {}
        chat_template_kwargs = dict(tier.chat_template_kwargs or {})
        if tier.think_supported:
            chat_template_kwargs["enable_thinking"] = think

        payload: dict[str, Any] = {
            "model": tier.model_tag,
            "messages": _messages_to_payload(messages),
            "stream": True,
            "temperature": params.get("temperature"),
            "top_p": params.get("top_p"),
            "top_k": params.get("top_k"),
            "max_tokens": params.get("num_predict"),
            "chat_template_kwargs": chat_template_kwargs,
        }
        if extra_options:
            payload.update(extra_options)
        payload = {k: v for k, v in payload.items() if v is not None}

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream(
                "POST", f"{self.endpoint}/chat/completions", json=payload
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        yield json.loads(data)
                    except json.JSONDecodeError:
                        continue

    async def chat_once(
        self,
        tier: TierConfig,
        messages: list[ChatMessage],
        think: bool,
        extra_options: dict[str, Any] | None = None,
    ) -> str:
        chunks: list[str] = []
        async for ev in self.chat_stream(tier, messages, think, extra_options):
            for choice in ev.get("choices", []):
                delta = choice.get("delta", {})
                if "content" in delta and delta["content"]:
                    chunks.append(delta["content"])
        return "".join(chunks)

    async def is_ready(self) -> bool:
        """llama.cpp serves models at container start; /v1/models confirms."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"{self.endpoint}/models")
                return r.status_code == 200
        except httpx.HTTPError:
            return False
