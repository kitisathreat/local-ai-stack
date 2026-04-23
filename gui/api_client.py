"""Thin async HTTP client for the backend.

Uses ``httpx.AsyncClient`` with Qt via ``qasync``. Exposes typed
wrappers for the endpoints the GUI needs: model list, SSE chat stream,
metrics, admin read/write, airgap toggle.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import AsyncIterator

import httpx


@dataclass(frozen=True)
class ChatTurn:
    role: str
    content: str


class BackendClient:
    def __init__(self, base_url: str, token: str | None = None, timeout: float = 60.0):
        self._base = base_url.rstrip("/")
        self._headers: dict[str, str] = {}
        if token:
            self._headers["Authorization"] = f"Bearer {token}"
        self._timeout = timeout

    # ── Convenience ────────────────────────────────────────────────────

    def set_token(self, token: str | None) -> None:
        if token:
            self._headers["Authorization"] = f"Bearer {token}"
        else:
            self._headers.pop("Authorization", None)

    async def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base,
            headers=self._headers,
            timeout=self._timeout,
        )

    # ── Read endpoints ─────────────────────────────────────────────────

    async def healthz(self) -> bool:
        async with await self._client() as c:
            r = await c.get("/healthz")
            return r.status_code == 200

    async def list_models(self) -> list[dict]:
        async with await self._client() as c:
            r = await c.get("/v1/models")
            r.raise_for_status()
            return (r.json() or {}).get("data", [])

    async def vram_status(self) -> dict:
        async with await self._client() as c:
            r = await c.get("/vram")
            r.raise_for_status()
            return r.json()

    async def resolved_models(self) -> dict:
        async with await self._client() as c:
            r = await c.get("/resolved-models")
            if r.status_code == 404:
                return {}
            r.raise_for_status()
            return r.json()

    # ── Chat streaming ─────────────────────────────────────────────────

    async def stream_chat(
        self,
        messages: list[ChatTurn],
        model: str,
        *,
        think: bool | None = None,
    ) -> AsyncIterator[str]:
        """Yields incremental text tokens from the SSE response."""
        payload = {
            "model": model,
            "stream": True,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
        }
        if think is not None:
            payload["think"] = think
        async with await self._client() as c:
            async with c.stream("POST", "/v1/chat/completions", json=payload) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    body = line[5:].strip()
                    if body == "[DONE]":
                        return
                    try:
                        chunk = json.loads(body)
                    except json.JSONDecodeError:
                        continue
                    for choice in chunk.get("choices", []):
                        delta = (choice.get("delta") or {}).get("content")
                        if delta:
                            yield delta
