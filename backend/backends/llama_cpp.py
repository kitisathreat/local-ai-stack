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
import re
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
from ..model_residency import plan_residency, ResidencyPolicy
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


_jinja_supported_cache: bool | None = None
_help_text_cache: str | None = None


def _llama_help_text() -> str:
    """Cached output of `llama-server --help` (stdout+stderr concatenated)."""
    global _help_text_cache
    if _help_text_cache is not None:
        return _help_text_cache
    import subprocess
    try:
        out = subprocess.run(
            [llama_server_binary(), "--help"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        _help_text_cache = (out.stdout or "") + (out.stderr or "")
    except (OSError, subprocess.TimeoutExpired):
        _help_text_cache = ""
    return _help_text_cache


def _llama_supports_jinja() -> bool:
    """Older builds (≤ b4499) emit only `--chat-template`; newer ones list
    `--jinja` separately. Cached."""
    global _jinja_supported_cache
    if _jinja_supported_cache is None:
        _jinja_supported_cache = bool(
            re.search(r"(?m)^\s*--jinja\b", _llama_help_text())
        )
    return _jinja_supported_cache


def _draft_flag_names() -> tuple[str, str]:
    """Return the (max-flag, min-flag) names for the spec-decode draft.

    Recent llama.cpp upstream renamed `--draft-max`/`--draft-min` to
    `--spec-draft-n-max`/`--spec-draft-n-min`. The vendored binary may
    be either side of that change depending on when it was last
    re-downloaded by the launcher, so probe `--help` instead of pinning
    a single name. Spawning with the wrong name kills the server at
    startup with the cryptic `argument has been removed` error.
    """
    help_text = _llama_help_text()
    if re.search(r"(?m)^\s*--spec-draft-n-max\b", help_text):
        return ("--spec-draft-n-max", "--spec-draft-n-min")
    return ("--draft-max", "--draft-min")


def _resolve_for_llama(gguf_path: str) -> str:
    """Resolve symlinks before passing to llama-server.

    For sharded GGUFs (`<base>-NNNNN-of-MMMMM.gguf`), llama.cpp walks
    the directory of the first shard to discover shards 2..M by
    matching the filename pattern. The canonical `<tier>.gguf` symlink
    we maintain in `data/models/` doesn't match that pattern, so
    passing the symlink path as-is makes llama.cpp error with
    `invalid split file name: <tier>.gguf`. Resolving to the symlink
    target (which IS named `<base>-00001-of-MMMMM.gguf`) makes the
    pattern walk succeed for all sharded tiers (reasoning_max,
    reasoning_xl, frontier, coding_80b when sharded). Single-file
    GGUFs are unaffected — the resolution is a no-op when the path
    isn't a symlink.
    """
    try:
        # Path.resolve() follows symlinks transitively; on Windows this
        # works for both NTFS symlinks and reparse points.
        from pathlib import Path
        target = Path(gguf_path).resolve(strict=False)
        return str(target)
    except (OSError, RuntimeError):
        # If resolution fails (race, missing file), fall through to the
        # original path so llama-server's own error surfacing kicks in
        # with the actual filesystem error (rather than silent breakage).
        return gguf_path


def build_argv(tier: TierConfig) -> list[str]:
    """Produce the llama-server argv for `tier`."""
    if not tier.gguf_path:
        raise ValueError(
            f"tier {tier.name!r} has no gguf_path — run -Setup or model_resolver"
        )
    if tier.port is None:
        raise ValueError(f"tier {tier.name!r} has no port")

    # llama-server's --ctx-size is the TOTAL KV pool divided across
    # --parallel slots; per-slot context = ctx_size / parallel. The
    # config's `context_window` is the per-slot value the operator
    # actually wants (= what a single conversation can use), so the
    # launcher must multiply by parallel_slots before passing to
    # llama-server.
    #
    # Without this multiplication the bench's long-context cells (and
    # any user prompt over 2k tokens on tiers with parallel_slots ≥ 8)
    # would silently fail with HTTP 400 "exceeds the available context
    # size" — for example swarm with context_window=16384 and
    # parallel_slots=8 used to give EACH slot only 2048 tokens of
    # context, rejecting every needle prompt.
    parallel = max(1, tier.parallel_slots)
    total_ctx = tier.context_window * parallel
    argv: list[str] = [
        llama_server_binary(),
        "--host", "127.0.0.1",
        "--port", str(tier.port),
        "-m", _resolve_for_llama(tier.gguf_path),
        "--ctx-size", str(total_ctx),
        "--parallel", str(parallel),
        "-ngl", str(tier.n_gpu_layers),
        "--cache-type-k", tier.cache_type_k,
        "--cache-type-v", tier.cache_type_v,
    ]
    # `--jinja` enables strict Jinja2-template-driven tool-call grammar
    # but only exists in llama.cpp ≥ b4500 (early 2025). Older builds
    # (e.g. b4404, the launcher's current pin) reject it with
    # "error: invalid argument: --jinja" and exit during startup,
    # killing every chat tier. Probe the binary's --help once and only
    # add the flag when supported. Without it, llama-server falls back
    # to the model's built-in chat template which is fine for Qwen.
    if _llama_supports_jinja():
        argv.append("--jinja")
    if tier.flash_attention:
        # llama.cpp ≥ b8992 requires -fa to take a value (on/off/auto);
        # older builds accepted bare -fa. Always pass `on` — both old
        # and new builds tolerate it.
        argv += ["-fa", "on"]
    if tier.mmproj_path:
        argv += ["--mmproj", tier.mmproj_path]
    if tier.use_mlock:
        argv.append("--mlock")
    if not tier.use_mmap:
        argv.append("--no-mmap")
    # When the residency cascade chose to spill the KV cache to system
    # RAM (set via tier.kv_offload after plan_residency tightens for fit),
    # llama-server's --no-kv-offload flag keeps the cache off the GPU.
    # Frees several GB at long contexts; costs attention bandwidth.
    if getattr(tier, "kv_offload", False):
        argv.append("--no-kv-offload")
    if tier.rope_scaling and tier.rope_scaling.factor and tier.rope_scaling.factor != 1.0:
        argv += ["--rope-scaling", tier.rope_scaling.type]
        argv += ["--rope-scale", str(tier.rope_scaling.factor)]
        if tier.rope_scaling.orig_ctx:
            argv += ["--yarn-orig-ctx", str(tier.rope_scaling.orig_ctx)]
    if tier.extra_args:
        argv += list(tier.extra_args)
    for pattern in tier.override_tensors:
        argv += ["-ot", pattern]
    # Speculative decoding. When a draft GGUF is resolved for this tier,
    # llama-server runs Leviathan-style spec decode against it: the
    # draft proposes draft_max tokens, the target verifies them in one
    # parallel batch, rejection-sampling keeps the joint distribution
    # identical to running the target alone. Quality is preserved
    # exactly — speedup is purely from amortizing memory bandwidth.
    # Required: draft and target must share a tokenizer (caller's
    # responsibility — the YAML wires Qwen3-0.6B for Qwen3-family tiers
    # only). Flag spelling matches llama.cpp ≥ b8992 (LocalAIStack.ps1
    # pinned version).
    if tier.draft_gguf_path:
        # Defensive: if the draft GGUF file isn't actually on disk
        # (resolver wrote the manifest entry but the pull failed — common
        # when HF_TOKEN isn't set and the draft model is gated),
        # llama-server crashes during startup with a cryptic error. Skip
        # spec-decode in that case and run target-only — quality is
        # identical, just no speedup.
        from pathlib import Path as _P
        if _P(tier.draft_gguf_path).exists():
            # Recent llama.cpp upstream renamed --draft-max / --draft-min to
            # --spec-draft-n-max / --spec-draft-n-min. Probe the binary for
            # the right pair instead of pinning either — wrong name aborts
            # the server before it even loads weights.
            max_flag, min_flag = _draft_flag_names()
            argv += [
                "-md", _resolve_for_llama(tier.draft_gguf_path),
                "-ngld", str(tier.draft_n_gpu_layers),
                max_flag, str(tier.draft_max),
                min_flag, str(tier.draft_min),
            ]
        else:
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "Spec-decode draft missing for tier %s (%s) — running "
                "target-only. To enable spec-decode, set HF_TOKEN in .env "
                "and run `pwsh .\\LocalAIStack.ps1 -CheckUpdates`.",
                tier.name, tier.draft_gguf_path,
            )
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
            # CREATE_NO_WINDOW hides the conhost flash that would
            # otherwise pop on every chat-tier cold-spawn — visible to
            # the user even when the launcher itself is hidden, because
            # conhost.exe is a foreground UI window the OS opens for any
            # console subprocess spawned without this flag.
            creationflags = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
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

    async def ensure_loaded(
        self,
        tier: TierConfig,
        *,
        free_vram_gb: float | None = None,
        live_user_text: str = "",
        spawn_timeout: float | None = None,
    ) -> float:
        """Make sure the per-tier llama-server is up + healthy.

        Returns elapsed seconds (useful for VRAMScheduler observed-cost
        measurement). Idempotent.

        If a process is already listening on `tier.port` (e.g. pre-spawned by
        the launcher for vision/embedding), the client adopts it instead of
        spawning a duplicate.

        When ``LAI_RESIDENCY_PLANNER=1`` is set in the environment AND
        ``free_vram_gb`` is provided, the per-spawn ``n_gpu_layers /
        use_mmap / use_mlock`` are computed by ``plan_residency`` instead
        of read from YAML. Otherwise the YAML values are used verbatim
        (existing behavior).
        """
        endpoint = tier.resolved_endpoint()
        timeout = spawn_timeout if spawn_timeout is not None else float(tier.spawn_timeout_sec)
        async with self._lock(tier.name):
            proc = self.processes.get(tier.name)
            t0 = time.monotonic()
            if proc and proc.is_alive():
                return 0.0
            # Pre-spawn orphan reap: a previous evict() may have logged
            # "Evicted tier X" but the llama-server process can survive
            # the terminate() and keep ~10–15 GB of VRAM allocated until
            # we forcibly kill it. Reaping NOW (before we spawn this
            # tier) means the new process loads into clean VRAM rather
            # than triggering the residency planner's KV→CPU cascade
            # because the GPU looks falsely full. Skipped silently if
            # there's nothing to reap.
            try:
                killed = await self.kill_orphans()
                if killed:
                    logger.warning(
                        "Pre-spawn reap killed %d stray llama-server PID(s) "
                        "before loading %s: %s", len(killed), tier.name, killed,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Pre-spawn reap failed for %s: %s", tier.name, exc)
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

            # Residency planner — fitting cascade: layer offload → KV-on-CPU
            # → ctx shrink. Gated by `vram.residency.enable` in YAML; the
            # legacy LAI_RESIDENCY_PLANNER env var still force-enables the
            # planner so existing rollouts keep working until the YAML
            # default propagates.
            cfg = None
            try:
                from ..config import get_config
                cfg = get_config()
            except Exception:
                cfg = None
            planner_on = (
                (cfg is not None and getattr(cfg.vram.residency, "enable", False))
                or os.getenv("LAI_RESIDENCY_PLANNER") == "1"
            )
            if planner_on and free_vram_gb is not None and cfg is not None:
                policy = ResidencyPolicy(
                    full_headroom_multiplier=cfg.vram.residency.full_headroom_multiplier,
                    partial_min_ratio=cfg.vram.residency.partial_min_ratio,
                    minimal_ratio=cfg.vram.residency.minimal_ratio,
                    low_complexity_savings=cfg.vram.residency.low_complexity_savings,
                    mlock_full_mode=cfg.vram.residency.mlock_full_mode,
                    mlock_partial_mode=cfg.vram.residency.mlock_partial_mode,
                    enable_kv_offload=cfg.vram.residency.enable_kv_offload,
                    enable_ctx_shrink=cfg.vram.residency.enable_ctx_shrink,
                    min_context_window=cfg.vram.residency.min_context_window,
                )
                plan = plan_residency(
                    tier,
                    free_vram_gb=free_vram_gb,
                    live_user_text=live_user_text,
                    policy=policy,
                )
                # Apply the plan to a tier copy (don't mutate the registry singleton).
                # to_backend_options() already returns kv_offload + ctx override
                # when the cascade flipped them.
                tier = tier.model_copy(update=plan.to_backend_options())
                logger.info(
                    "Residency plan for %s: mode=%s layers=%d/%d ctx=%d "
                    "kv_offload=%s reason=%s",
                    tier.name, plan.mode.value,
                    plan.num_gpu_layers, plan.total_layers,
                    plan.context_window or tier.context_window,
                    plan.kv_offload, plan.reason,
                )

            argv = build_argv(tier)
            proc = LlamaServerProcess(
                tier_id=tier.name,
                port=tier.port or 0,
                endpoint=endpoint,
                argv=argv,
            )
            await proc.start()
            # Register in self.processes BEFORE wait_ready: the 80B
            # tiers take 25–30s to become HTTP-ready, and the periodic
            # orphan-reap (every ~30s) would otherwise see the live
            # PID with no registry entry and SIGKILL it as a stray.
            # Observed May 2026: highest_quality (pid 26616, port 8010)
            # was killed mid-spawn, breaking the cell with tok=0
            # cascades. Registering early closes the race; if
            # wait_ready fails we still pop+stop in the except.
            self.processes[tier.name] = proc
            try:
                await proc.wait_ready(timeout=timeout)
            except Exception:
                self.processes.pop(tier.name, None)
                await proc.stop(timeout=5.0)
                raise
            return time.monotonic() - t0

    async def unload(self, tier: TierConfig) -> None:
        async with self._lock(tier.name):
            proc = self.processes.pop(tier.name, None)
            if proc is None:
                return
            await proc.stop()
        # After stop(), the popen handle is gone but Windows can leave the
        # llama-server process briefly in zombie state, OR — as observed
        # during May 2026 benches — the process can survive terminate()
        # entirely (especially with -ot expert offload + KV pinning) and
        # keep its VRAM allocated. Sweep llama-server PIDs not tracked by
        # the registry and SIGKILL anything that isn't the embedding /
        # reranker / vision tier the launcher pre-spawned.
        try:
            killed = await self.kill_orphans()
            if killed:
                logger.warning(
                    "Post-unload reap killed %d stray llama-server PID(s) after "
                    "evicting %s: %s", len(killed), tier.name, killed,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Post-unload reap failed for %s: %s", tier.name, exc)

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

    async def kill_orphans(self, preserve_ports: set[int] | None = None) -> list[int]:
        """Kill llama-server processes not tracked by this client.

        Backend bounces (refresh-backend.ps1, dev autoreload, crashes)
        can leave llama-server children alive but unreachable from the
        new backend's process registry. Their VRAM stays allocated, NVML
        reports it as used, and the next ``_make_room_for`` decision
        sees a phantom shortfall. This walks the live llama-server PIDs
        and SIGTERMs anything we don't own.

        ``preserve_ports`` is the set of ports that belong to externally-
        managed processes the backend should leave alone (typically the
        launcher's pre-spawned vision / embedding / reranker on
        :8089–8091). PIDs whose listening port is in that set are
        skipped even if not in ``self.processes``.

        Returns the list of PIDs killed.
        """
        preserve_ports = set(preserve_ports or set())
        # Always preserve the launcher's pre-spawned support tiers
        # (vision 8089, embedding 8090, reranker 8091). They're spawned
        # by LocalAIStack.ps1 before the backend starts and only get
        # adopted into self.processes lazily on first request, so a
        # pre-spawn reap that runs before adoption would kill them and
        # silently break embeddings + retrieval.
        preserve_ports.update({8089, 8090, 8091})
        # Tracked PIDs from our own subprocess.Popen handles.
        tracked: set[int] = set()
        for proc in self.processes.values():
            if proc.popen is not None and proc.popen.poll() is None:
                tracked.add(proc.popen.pid)
            # Externally-managed (we adopted on startup) — preserve by port.
            if proc._externally_managed and proc.port:
                preserve_ports.add(proc.port)

        candidates = await _list_llama_server_pids()
        killed: list[int] = []
        for pid, port in candidates:
            if pid in tracked:
                continue
            if port and port in preserve_ports:
                continue
            try:
                if os.name == "nt":
                    # Use taskkill for clean exit; fall back to os.kill.
                    proc = await asyncio.create_subprocess_exec(
                        "taskkill", "/F", "/PID", str(pid),
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                    await proc.wait()
                else:
                    os.kill(pid, signal.SIGTERM)
                killed.append(pid)
                logger.warning(
                    "Killed orphan llama-server pid=%s port=%s "
                    "(left from previous backend run)", pid, port,
                )
            except OSError as exc:
                logger.debug("Failed to kill orphan pid=%s: %s", pid, exc)
        return killed

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


async def _list_llama_server_pids() -> list[tuple[int, int | None]]:
    """Return [(pid, listening_port_or_None)] for every live llama-server
    process. Best-effort + cross-platform. Used by
    ``LlamaCppClient.kill_orphans`` and the ``/admin/vram/probe`` diagnostic.
    """
    pids: list[int] = []
    if os.name == "nt":
        # tasklist is faster + dependency-free on Windows.
        try:
            proc = await asyncio.create_subprocess_exec(
                "tasklist", "/FI", "IMAGENAME eq llama-server.exe",
                "/FO", "CSV", "/NH",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            stdout, _ = await proc.communicate()
        except (OSError, FileNotFoundError):
            return []
        for line in stdout.decode("utf-8", errors="ignore").splitlines():
            parts = [p.strip().strip('"') for p in line.split(",")]
            if len(parts) >= 2:
                try:
                    pids.append(int(parts[1]))
                except ValueError:
                    pass
    else:
        try:
            proc = await asyncio.create_subprocess_exec(
                "pgrep", "-x", "llama-server",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await proc.communicate()
        except (OSError, FileNotFoundError):
            return []
        for line in stdout.decode("utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line.isdigit():
                pids.append(int(line))

    if not pids:
        return []

    # Resolve listening ports per pid (Windows-only — POSIX path
    # returns ports as None and falls back to PID-only matching, which
    # is fine because preserve_ports only matters for the launcher's
    # adopted services).
    if os.name != "nt":
        return [(pid, None) for pid in pids]

    pid_to_port: dict[int, int] = {}
    try:
        proc = await asyncio.create_subprocess_exec(
            "netstat", "-ano", "-p", "TCP",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        stdout, _ = await proc.communicate()
        for line in stdout.decode("utf-8", errors="ignore").splitlines():
            parts = line.split()
            if len(parts) < 5 or "LISTENING" not in parts:
                continue
            try:
                pid = int(parts[-1])
            except ValueError:
                continue
            if pid not in pids or pid in pid_to_port:
                continue
            local = parts[1]
            try:
                port = int(local.rsplit(":", 1)[-1])
            except ValueError:
                continue
            pid_to_port[pid] = port
    except (OSError, FileNotFoundError):
        pass

    return [(pid, pid_to_port.get(pid)) for pid in pids]
