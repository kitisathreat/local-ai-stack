"""Ollama HTTP client — async streaming, keep_alive control, eager load
helpers for the VRAM scheduler.

Ollama API reference: https://github.com/ollama/ollama/blob/main/docs/api.md
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx

from ..config import TierConfig
from ..schemas import ChatMessage


def build_options(tier: TierConfig, think: bool, extra: dict[str, Any] | None = None) -> dict:
    """Assemble Ollama's `options` dict from tier params + reasoning toggle.

    `num_parallel` is set from `tier.parallel_slots` so Ollama allocates
    that many KV-cache slots when first loading this model. Subsequent
    requests share those slots. Ollama reads num_parallel on first load
    only — changing it requires evicting and reloading the model.
    """
    opts: dict[str, Any] = dict(tier.params or {})
    opts["num_ctx"] = tier.context_window
    slots = max(1, int(getattr(tier, "parallel_slots", 1)))
    opts["num_parallel"] = slots
    if tier.think_supported:
        opts["think"] = think
    if extra:
        opts.update(extra)
    return opts


def _messages_to_payload(messages: list[ChatMessage]) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        if isinstance(m.content, str):
            out.append({"role": m.role, "content": m.content})
        else:
            # Ollama accepts multimodal via `images` field (base64). For Phase 1
            # we pass text-only; vision requests should never hit this backend
            # because the router sends them to llama_cpp.
            text = " ".join(p.text or "" for p in m.content if p.type == "text")
            out.append({"role": m.role, "content": text})
    return out


class OllamaClient:
    def __init__(self, endpoint: str, timeout_sec: float = 300.0):
        self.endpoint = endpoint.rstrip("/")
        self.timeout = httpx.Timeout(timeout_sec, connect=10.0)

    async def chat_stream(
        self,
        tier: TierConfig,
        messages: list[ChatMessage] | list[dict],
        think: bool,
        keep_alive: str | int = "30m",
        extra_options: dict[str, Any] | None = None,
        tools: list[dict] | None = None,
    ) -> AsyncIterator[dict]:
        """Yields NDJSON chunks from Ollama's /api/chat."""
        # Allow pre-serialized message dicts (used by the tool loop which
        # needs to include role=tool messages that don't fit ChatMessage).
        if messages and isinstance(messages[0], ChatMessage):
            msgs_payload = _messages_to_payload(messages)  # type: ignore[arg-type]
        else:
            msgs_payload = messages  # type: ignore[assignment]
        payload: dict[str, Any] = {
            "model": tier.model_tag,
            "messages": msgs_payload,
            "stream": True,
            "options": build_options(tier, think, extra_options),
            "keep_alive": keep_alive,
        }
        if tools:
            payload["tools"] = tools
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream("POST", f"{self.endpoint}/api/chat", json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue

    async def chat_once(
        self,
        tier: TierConfig,
        messages: list[ChatMessage],
        think: bool,
        keep_alive: str | int = "30m",
        extra_options: dict[str, Any] | None = None,
    ) -> str:
        """Non-streaming convenience for orchestrator planning calls."""
        chunks = []
        async for c in self.chat_stream(tier, messages, think, keep_alive, extra_options):
            if "message" in c and "content" in c["message"]:
                chunks.append(c["message"]["content"])
            if c.get("done"):
                break
        return "".join(chunks)

    async def ensure_loaded(self, tier: TierConfig, keep_alive: int | str = -1) -> float:
        """Force-load model into VRAM with a 1-token dummy completion.
        Returns elapsed seconds (useful for observed-cost measurement).

        Uses the same num_ctx / num_parallel as chat_stream so Ollama doesn't
        reload the model again when the first real request arrives.
        """
        import time
        t0 = time.monotonic()
        payload = {
            "model": tier.model_tag,
            "messages": [{"role": "user", "content": "."}],
            "stream": False,
            "options": {**build_options(tier, think=False), "num_predict": 1},
            "keep_alive": keep_alive,
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(f"{self.endpoint}/api/chat", json=payload)
            r.raise_for_status()
        return time.monotonic() - t0

    async def unload(self, tier: TierConfig) -> None:
        """Trigger Ollama to drop the model from VRAM."""
        payload = {
            "model": tier.model_tag,
            "messages": [{"role": "user", "content": ""}],
            "stream": False,
            "keep_alive": 0,
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Fire-and-forget; non-2xx here is not fatal
            try:
                await client.post(f"{self.endpoint}/api/chat", json=payload)
            except httpx.HTTPError:
                pass

    async def list_running(self) -> list[dict]:
        """GET /api/ps — returns currently-loaded models. Used by scheduler
        at startup to repopulate its registry."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{self.endpoint}/api/ps")
            r.raise_for_status()
            return r.json().get("models", [])

    async def list_installed(self) -> list[dict]:
        """GET /api/tags — list all pulled models."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{self.endpoint}/api/tags")
            r.raise_for_status()
            return r.json().get("models", [])
