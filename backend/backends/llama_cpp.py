"""llama.cpp client + per-tier process manager.

The backend manages one ``llama-server`` subprocess per tier. The
``LlamaCppClient`` exposes:

  - chat_stream / chat_once  — OpenAI-compatible streaming + non-streaming
                              chat over the tier's per-process endpoint.
  - ensure_loaded / unload   — spawn / terminate the per-tier llama-server
                              subprocess. Wired into VRAMScheduler.
  - list_running             — diagnostic snapshot of live processes.

Tool calling: when llama-server is launched with ``--jinja`` and a
tool-aware chat template, it returns OpenAI-shaped streaming tool-call
deltas. The client provides a small accumulator that merges
``choices[0].delta.tool_calls`` fragments into complete calls.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

import httpx

from ..config import TierConfig
from ..schemas import ChatMessage


logger = logging.getLogger(__name__)


# ── Argv builder ────────────────────────────────────────────────────────────

def llama_server_binary() -> str:
    """Resolve the path to ``llama-server[.exe]``.

    Honours the ``LLAMA_SERVER_BIN`` env var; otherwise looks for the
    vendored Windows binary at ``vendor/llama-server/llama-server.exe`` and
    falls back to whatever is on PATH.
    """
    explicit = os.getenv("LLAMA_SERVER_BIN")
    if explicit:
        return explicit
    repo_root = Path(__file__).resolve().parent.parent.parent
    exe_name = "llama-server.exe" if os.name == "nt" else "llama-server"
    vendored = repo_root / "vendor" / "llama-server" / exe_name
    if vendored.exists():
        return str(vendored)
    found = shutil.which(exe_name) or shutil.which("llama-server")
    return found or exe_name


def build_argv(tier: TierConfig) -> list[str]:
    """Produce the llama-server argv for `tier`."""
    if not tier.gguf_path:
        raise ValueError(
            f"tier {tier.name!r} has no gguf_path — run -Setup or model_resolver"
        )
    if tier.port is None:
        raise ValueError(f"tier {tier.name!r} has no port")

    argv: list[str] = [
        llama_server_binary(),
        "--host", "127.0.0.1",
        "--port", str(tier.port),
        "-m", tier.gguf_path,
        "--ctx-size", str(tier.context_window),
        "--parallel", str(max(1, tier.parallel_slots)),
        "-ngl", str(tier.n_gpu_layers),
        "--cache-type-k", tier.cache_type_k,
        "--cache-type-v", tier.cache_type_v,
        "--jinja",
    ]
    if tier.flash_attention:
        argv.append("-fa")
    if tier.mmproj_path:
        argv += ["--mmproj", tier.mmproj_path]
    if tier.use_mlock:
        argv.append("--mlock")
    if not tier.use_mmap:
        argv.append("--no-mmap")
    if tier.rope_scaling and tier.rope_scaling.factor and tier.rope_scaling.factor != 1.0:
        argv += ["--rope-scaling", tier.rope_scaling.type]
        argv += ["--rope-scale", str(tier.rope_scaling.factor)]
        if tier.rope_scaling.orig_ctx:
            argv += ["--yarn-orig-ctx", str(tier.rope_scaling.orig_ctx)]
    if tier.extra_args:
        argv += list(tier.extra_args)
    return argv


# ── Per-tier process handle ────────────────────────────────────────────────

@dataclass
class LlamaServerProcess:
    """One llama-server subprocess. Owned by the LlamaCppClient registry."""

    tier_id: str
    port: int
    endpoint: str
    argv: list[str] = field(default_factory=list)
    popen: subprocess.Popen | None = None
    started_at: float = 0.0
    stderr_tail: deque = field(default_factory=lambda: deque(maxlen=128))
    _reader_task: asyncio.Task | None = None
    _externally_managed: bool = False    # True when adopted (PS1 pre-spawn)

    def is_alive(self) -> bool:
        if self._externally_managed:
            return True
        if self.popen is None:
            return False
        return self.popen.poll() is None

    async def wait_ready(self, timeout: float = 180.0) -> None:
        """Poll GET /health on the endpoint until 200 or timeout. The
        embedding server doesn't expose /health; we fall back to /v1/models."""
        deadline = time.monotonic() + timeout
        last_err: Exception | None = None
        async with httpx.AsyncClient(timeout=4.0) as client:
            while time.monotonic() < deadline:
                if not self.is_alive():
                    raise RuntimeError(
                        f"llama-server for {self.tier_id} exited during startup\n"
                        + "\n".join(list(self.stderr_tail)[-20:])
                    )
                for path in ("/health", "/models"):
                    try:
                        url = self.endpoint.rstrip("/") + path
                        r = await client.get(url)
                        if r.status_code == 200:
                            return
                    except httpx.HTTPError as exc:
                        last_err = exc
                await asyncio.sleep(0.5)
        raise TimeoutError(
            f"llama-server for {self.tier_id} did not become ready in "
            f"{timeout:.0f}s ({last_err})"
        )

    async def start(self) -> None:
        if self.is_alive():
            return
        creationflags = 0
        if os.name == "nt":
            # CREATE_NEW_PROCESS_GROUP so terminate() works on Windows.
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        logger.info("Starting llama-server for %s: %s", self.tier_id, " ".join(self.argv))
        try:
            self.popen = subprocess.Popen(
                self.argv,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=creationflags,
                close_fds=(os.name != "nt"),
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"llama-server binary not found ({self.argv[0]!r}). "
                "Run -Setup or set LLAMA_SERVER_BIN."
            ) from exc
        self.started_at = time.monotonic()
        self._reader_task = asyncio.create_task(self._tail_stderr())

    async def _tail_stderr(self) -> None:
        if self.popen is None or self.popen.stderr is None:
            return
        loop = asyncio.get_running_loop()
        while True:
            line = await loop.run_in_executor(None, self.popen.stderr.readline)
            if not line:
                break
            try:
                self.stderr_tail.append(line.decode("utf-8", errors="replace").rstrip())
            except Exception:
                pass

    async def stop(self, timeout: float = 30.0) -> None:
        if self._externally_managed:
            return
        if self.popen is None:
            return
        if self.popen.poll() is not None:
            self.popen = None
            return
        logger.info("Stopping llama-server for %s (pid=%s)", self.tier_id, self.popen.pid)
        try:
            if os.name == "nt":
                self.popen.terminate()
            else:
                self.popen.send_signal(signal.SIGTERM)
        except Exception as exc:
            logger.warning("terminate() failed for %s: %s", self.tier_id, exc)

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, lambda: self.popen.wait(timeout))
        except subprocess.TimeoutExpired:
            logger.warning("llama-server %s did not exit; killing", self.tier_id)
            try:
                self.popen.kill()
            except Exception:
                pass
            with contextlib.suppress(Exception):
                await loop.run_in_executor(None, lambda: self.popen.wait(5))
        finally:
            self.popen = None
            if self._reader_task is not None:
                self._reader_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._reader_task
                self._reader_task = None


# ── Message conversion ─────────────────────────────────────────────────────

def _messages_to_payload(messages: list[ChatMessage] | list[dict]) -> list[dict]:
    """Convert ChatMessage objects to OpenAI-shaped dicts. Pre-serialized
    dicts (used by the tool loop for role=tool entries) pass through."""
    out: list[dict] = []
    for m in messages:
        if isinstance(m, dict):
            out.append(m)
            continue
        if isinstance(m.content, str):
            out.append({"role": m.role, "content": m.content})
        else:
            parts: list[dict] = []
            for p in m.content:
                if p.type == "text":
                    parts.append({"type": "text", "text": p.text or ""})
                elif p.type == "image_url" and p.image_url:
                    parts.append({"type": "image_url", "image_url": p.image_url})
            out.append({"role": m.role, "content": parts})
    return out


# ── Streaming tool-call accumulator ────────────────────────────────────────

class ToolCallAccumulator:
    """Aggregates streaming OpenAI-shaped tool_call delta fragments into
    complete `{id, type, function: {name, arguments}}` records."""

    def __init__(self) -> None:
        self._buf: dict[int, dict] = {}

    def feed(self, fragments: list[dict] | None) -> None:
        if not fragments:
            return
        for frag in fragments:
            idx = int(frag.get("index", 0))
            slot = self._buf.setdefault(
                idx,
                {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
            )
            if frag.get("id"):
                slot["id"] = frag["id"]
            if frag.get("type"):
                slot["type"] = frag["type"]
            fn_frag = frag.get("function") or {}
            if fn_frag.get("name"):
                slot["function"]["name"] = fn_frag["name"]
            if fn_frag.get("arguments"):
                slot["function"]["arguments"] += fn_frag["arguments"]

    def calls(self) -> list[dict]:
        return [self._buf[k] for k in sorted(self._buf)]


# ── Client ─────────────────────────────────────────────────────────────────

class LlamaCppClient:
    """OpenAI-compatible client managing one llama-server per tier.

    The constructor takes no global endpoint — each call routes by
    `tier.resolved_endpoint()`. The registry is used to track which tiers
    are currently RESIDENT (process alive + /health 200).
    """

    def __init__(self, timeout_sec: float = 600.0):
        self.timeout = httpx.Timeout(timeout_sec, connect=10.0)
        self.processes: dict[str, LlamaServerProcess] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, tier_id: str) -> asyncio.Lock:
        lock = self._locks.get(tier_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[tier_id] = lock
        return lock

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def ensure_loaded(self, tier: TierConfig, *, spawn_timeout: float | None = None) -> float:
        """Make sure the per-tier llama-server is up + healthy.

        Returns elapsed seconds (useful for VRAMScheduler observed-cost
        measurement). Idempotent.

        If a process is already listening on `tier.port` (e.g. pre-spawned by
        the launcher for vision/embedding), the client adopts it instead of
        spawning a duplicate.
        """
        endpoint = tier.resolved_endpoint()
        timeout = spawn_timeout if spawn_timeout is not None else float(tier.spawn_timeout_sec)
        async with self._lock(tier.name):
            proc = self.processes.get(tier.name)
            t0 = time.monotonic()
            if proc and proc.is_alive():
                return 0.0
            # Adopt a pre-spawned external process, if any.
            if await self._port_is_serving(endpoint):
                logger.info("Adopting external llama-server for tier %s on %s", tier.name, endpoint)
                self.processes[tier.name] = LlamaServerProcess(
                    tier_id=tier.name,
                    port=tier.port or 0,
                    endpoint=endpoint,
                    _externally_managed=True,
                )
                return time.monotonic() - t0

            argv = build_argv(tier)
            proc = LlamaServerProcess(
                tier_id=tier.name,
                port=tier.port or 0,
                endpoint=endpoint,
                argv=argv,
            )
            await proc.start()
            try:
                await proc.wait_ready(timeout=timeout)
            except Exception:
                await proc.stop(timeout=5.0)
                raise
            self.processes[tier.name] = proc
            return time.monotonic() - t0

    async def unload(self, tier: TierConfig) -> None:
        async with self._lock(tier.name):
            proc = self.processes.pop(tier.name, None)
            if proc is None:
                return
            await proc.stop()

    async def list_running(self) -> list[dict]:
        return [
            {
                "tier_id": p.tier_id,
                "port": p.port,
                "endpoint": p.endpoint,
                "alive": p.is_alive(),
                "external": p._externally_managed,
                "uptime_sec": (time.monotonic() - p.started_at) if p.started_at else 0,
            }
            for p in self.processes.values()
        ]

    async def stop_all(self) -> None:
        for tier_id in list(self.processes):
            proc = self.processes.pop(tier_id, None)
            if proc is not None:
                with contextlib.suppress(Exception):
                    await proc.stop()

    async def _port_is_serving(self, endpoint: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                for path in ("/health", "/models"):
                    r = await client.get(endpoint.rstrip("/") + path)
                    if r.status_code == 200:
                        return True
        except httpx.HTTPError:
            pass
        return False

    # ── Chat ────────────────────────────────────────────────────────────

    async def chat_stream(
        self,
        tier: TierConfig,
        messages: list[ChatMessage] | list[dict],
        think: bool,
        extra_options: dict[str, Any] | None = None,
        tools: list[dict] | None = None,
        keep_alive: Any = None,           # accepted for API parity; ignored
    ) -> AsyncIterator[dict]:
        """Yields OpenAI-shaped streaming events.

        Each event has the shape:
          {choices: [{delta: {content?, tool_calls?}, finish_reason?}], ...}

        Tool-call deltas are NOT pre-aggregated — callers should feed them
        through ``ToolCallAccumulator`` if they need full calls.
        """
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
        if tools:
            payload["tools"] = tools
        if extra_options:
            payload.update(extra_options)
        payload = {k: v for k, v in payload.items() if v is not None}

        endpoint = tier.resolved_endpoint()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream(
                "POST", f"{endpoint}/chat/completions", json=payload
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
        messages: list[ChatMessage] | list[dict],
        think: bool,
        extra_options: dict[str, Any] | None = None,
        keep_alive: Any = None,
    ) -> str:
        chunks: list[str] = []
        async for ev in self.chat_stream(tier, messages, think, extra_options):
            for choice in ev.get("choices", []):
                delta = choice.get("delta") or {}
                if "content" in delta and delta["content"]:
                    chunks.append(delta["content"])
        return "".join(chunks)

    # ── Embeddings ─────────────────────────────────────────────────────

    async def embed(self, tier: TierConfig, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        endpoint = tier.resolved_endpoint()
        payload = {"model": tier.model_tag, "input": texts}
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=5.0)) as c:
            r = await c.post(f"{endpoint}/embeddings", json=payload)
            r.raise_for_status()
            data = r.json()
        return [row.get("embedding") for row in (data.get("data") or [])]

    # ── Health ─────────────────────────────────────────────────────────

    async def is_ready(self, tier: TierConfig) -> bool:
        try:
            return await self._port_is_serving(tier.resolved_endpoint())
        except Exception:
            return False
