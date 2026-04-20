"""Generic OpenAI-compatible `/v1/chat/completions` client.

Covers third-party inference proxies (vLLM, TGI, LiteLLM, Together,
OpenRouter, etc.) plus any cloud Ollama deployment put behind a LiteLLM-
style compatibility layer. The wire format matches `LlamaCppClient` — both
are OpenAI-compatible — so this is a thin subclass that only overrides
multimodal payload construction (proxies typically don't accept the
`chat_template_kwargs` bag llama.cpp needs).
"""

from __future__ import annotations

from typing import Any, AsyncIterator

from ..config import TierConfig
from ..schemas import ChatMessage
from .llama_cpp import LlamaCppClient, _messages_to_payload


class OpenAIClient(LlamaCppClient):
    """OpenAI-compatible client that strips llama.cpp-specific payload bits.

    Retains streaming, multimodal image parts, and `chat_once` from the
    parent. The override below drops `chat_template_kwargs` because most
    OpenAI-compatible proxies return 400 on unknown top-level fields.
    """

    async def chat_stream(
        self,
        tier: TierConfig,
        messages: list[ChatMessage],
        think: bool,
        extra_options: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict]:
        import httpx
        import json

        params = tier.params or {}
        payload: dict[str, Any] = {
            "model": tier.model_tag,
            "messages": _messages_to_payload(messages),
            "stream": True,
            "temperature": params.get("temperature"),
            "top_p": params.get("top_p"),
            "top_k": params.get("top_k"),
            "max_tokens": params.get("num_predict"),
        }
        if extra_options:
            payload.update(extra_options)
        payload = {k: v for k, v in payload.items() if v is not None}

        async with httpx.AsyncClient(**self._session_kwargs) as client:
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
