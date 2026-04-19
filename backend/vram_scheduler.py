"""VRAM-aware model scheduler.

A single asyncio coordinator that tracks which model tiers are resident in
GPU memory, reference-counted by in-flight requests. When a new reservation
would exceed available VRAM (minus the configured headroom), it evicts
least-recently-used, unpinned tiers with refcount==0.

Design notes:
  - `pynvml` is optional at import time: tests mock the NVML calls, and the
    scheduler falls back to the registry-based projection when NVML isn't
    available (e.g., on non-NVIDIA hosts during development).
  - Observed VRAM costs are measured after each successful load (delta of
    free VRAM) and EMA-smoothed. Persisted to disk so process restarts don't
    lose tuning.
  - A background sweeper periodically reconciles the registry with the true
    free-VRAM reading, correcting for external GPU consumers.
  - The Vision tier is pinned (llama.cpp can't unload without restart), so
    it's never an eviction candidate.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import AsyncIterator, Callable

from .config import AppConfig, TierConfig


logger = logging.getLogger(__name__)


class ModelState(str, Enum):
    LOADING = "loading"
    RESIDENT = "resident"
    EVICTING = "evicting"


@dataclass
class LoadedModel:
    tier_id: str
    backend: str
    model_tag: str
    vram_estimate_gb: float
    observed_cost_gb: float | None = None
    state: ModelState = ModelState.LOADING
    refcount: int = 0
    last_used: float = field(default_factory=time.time)
    load_event: asyncio.Event = field(default_factory=asyncio.Event)
    pinned: bool = False

    def effective_cost(self) -> float:
        """Use max(estimate, observed) for eviction math to be safe."""
        if self.observed_cost_gb is None:
            return self.vram_estimate_gb
        return max(self.vram_estimate_gb, self.observed_cost_gb)


class VRAMExhausted(Exception):
    """Raised when a reservation cannot be satisfied even after eviction."""


class GPUProbe:
    """Wraps pynvml with a test-friendly interface."""

    def __init__(self):
        self._handle = None
        try:
            import pynvml  # type: ignore
            pynvml.nvmlInit()
            self._handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            self._pynvml = pynvml
        except Exception as e:
            logger.warning("pynvml unavailable (%s) — using registry-only math", e)
            self._pynvml = None

    def free_gb(self, total_gb: float) -> float:
        """Actual free VRAM in GB. If NVML unavailable, caller must fall
        back to registry-based projection — returns `total_gb` sentinel to
        signal "unknown, trust registry"."""
        if self._pynvml is None or self._handle is None:
            return total_gb
        info = self._pynvml.nvmlDeviceGetMemoryInfo(self._handle)
        return info.free / (1024 ** 3)

    def used_gb(self) -> float:
        if self._pynvml is None or self._handle is None:
            return 0.0
        info = self._pynvml.nvmlDeviceGetMemoryInfo(self._handle)
        return info.used / (1024 ** 3)


LoadFn = Callable[[TierConfig], "asyncio.Future | asyncio.Task | None"]
UnloadFn = Callable[[TierConfig], "asyncio.Future | asyncio.Task | None"]


class VRAMScheduler:
    """Primary public API: `async with scheduler.reserve(tier_id): ...`."""

    def __init__(
        self,
        config: AppConfig,
        loaders: dict[str, LoadFn],       # backend -> async load fn
        unloaders: dict[str, UnloadFn],   # backend -> async unload fn
        probe: GPUProbe | None = None,
    ):
        self.cfg = config
        self.vram = config.vram
        self.tiers = config.models.tiers
        self.loaders = loaders
        self.unloaders = unloaders
        self.probe = probe or GPUProbe()
        self.loaded: dict[str, LoadedModel] = {}
        self._lock = asyncio.Lock()
        self._sweeper_task: asyncio.Task | None = None
        self._observed_path = Path(self.vram.observed_costs.persist_path)
        self._load_observed_costs()

    # ── Observed cost persistence ────────────────────────────────────────

    def _load_observed_costs(self) -> None:
        if not self._observed_path.exists():
            self._observed = {}
            return
        try:
            self._observed = json.loads(self._observed_path.read_text())
        except (json.JSONDecodeError, OSError):
            self._observed = {}

    def _persist_observed_costs(self) -> None:
        try:
            self._observed_path.parent.mkdir(parents=True, exist_ok=True)
            self._observed_path.write_text(json.dumps(self._observed, indent=2))
        except OSError as e:
            logger.warning("Failed to persist observed VRAM costs: %s", e)

    def _update_observed(self, tier_id: str, measured_gb: float) -> float:
        prior = self._observed.get(tier_id)
        lr = self.vram.observed_costs.learning_rate
        new_val = measured_gb if prior is None else (prior * (1 - lr) + measured_gb * lr)
        self._observed[tier_id] = new_val
        self._persist_observed_costs()
        return new_val

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the background sweeper. Call from FastAPI lifespan."""
        if self._sweeper_task is None:
            self._sweeper_task = asyncio.create_task(self._sweeper())

    async def stop(self) -> None:
        if self._sweeper_task:
            self._sweeper_task.cancel()
            try:
                await self._sweeper_task
            except asyncio.CancelledError:
                pass
            self._sweeper_task = None

    # ── Core reservation API ─────────────────────────────────────────────

    @asynccontextmanager
    async def reserve(self, tier_id: str) -> AsyncIterator[str]:
        """Hold the model in VRAM for the duration of the `with` block.
        Blocks until the model is resident. Yields the canonical tier id."""
        await self._acquire(tier_id)
        try:
            yield tier_id
        finally:
            await self._release(tier_id)

    async def _acquire(self, tier_id: str) -> None:
        if tier_id not in self.tiers:
            raise KeyError(f"Unknown tier: {tier_id}")
        tier = self.tiers[tier_id]

        # First-pass fast path: already resident, just bump refcount
        async with self._lock:
            existing = self.loaded.get(tier_id)
            if existing and existing.state == ModelState.RESIDENT:
                existing.refcount += 1
                existing.last_used = time.time()
                return
            if existing and existing.state == ModelState.LOADING:
                ev = existing.load_event
                # Release the lock while we wait on another coroutine's load
            else:
                ev = None

        if ev is not None:
            await ev.wait()
            async with self._lock:
                m = self.loaded.get(tier_id)
                if m and m.state == ModelState.RESIDENT:
                    m.refcount += 1
                    m.last_used = time.time()
                    return
                # Load failed or was evicted; fall through to fresh load below

        async with self._lock:
            # Re-check under lock (another coroutine may have loaded it)
            existing = self.loaded.get(tier_id)
            if existing and existing.state == ModelState.RESIDENT:
                existing.refcount += 1
                existing.last_used = time.time()
                return

            await self._make_room_for(tier)
            new_entry = LoadedModel(
                tier_id=tier_id,
                backend=tier.backend,
                model_tag=tier.model_tag,
                vram_estimate_gb=tier.vram_estimate_gb,
                observed_cost_gb=self._observed.get(tier_id),
                state=ModelState.LOADING,
                refcount=1,
                pinned=tier.pinned,
            )
            self.loaded[tier_id] = new_entry

        # Do the actual load outside the lock — it can take 5-15s
        before_free = self.probe.free_gb(self.vram.total_vram_gb)
        try:
            loader = self.loaders.get(tier.backend)
            if loader:
                await loader(tier)
        except Exception:
            async with self._lock:
                new_entry.state = ModelState.EVICTING
                self.loaded.pop(tier_id, None)
                new_entry.load_event.set()
            raise

        after_free = self.probe.free_gb(self.vram.total_vram_gb)
        measured = max(0.0, before_free - after_free)
        if measured > 0:
            new_entry.observed_cost_gb = self._update_observed(tier_id, measured)

        async with self._lock:
            new_entry.state = ModelState.RESIDENT
            new_entry.last_used = time.time()
            new_entry.load_event.set()

    async def _release(self, tier_id: str) -> None:
        async with self._lock:
            m = self.loaded.get(tier_id)
            if not m:
                return
            m.refcount = max(0, m.refcount - 1)
            m.last_used = time.time()

    async def release_temporarily(self, tier_id: str) -> None:
        """Multi-agent helper: drop refcount to 0 so workers can evict this
        tier if they need the VRAM. Caller must re-acquire later."""
        await self._release(tier_id)

    # ── Eviction ─────────────────────────────────────────────────────────

    async def _make_room_for(self, tier: TierConfig) -> None:
        """Must be called with `self._lock` held.

        Headroom is applied only when the new tier would coexist with other
        loaded models. A "fills-the-card" tier (need >= total - headroom) is
        allowed to consume the full card alone — otherwise tiers whose
        estimate approaches total_vram_gb (like Qwen3 72B at 24GB on a 24GB
        card) could never load.
        """
        # For the incoming tier, need = max(estimate, observed-if-known).
        tier_id = next(
            (tid for tid, t in self.tiers.items() if t is tier),
            None,
        )
        observed = self._observed.get(tier_id) if tier_id else None
        need = max(tier.vram_estimate_gb, observed or 0.0)
        headroom = self.vram.headroom_gb
        total = self.vram.total_vram_gb

        def _fits(projected: float) -> bool:
            # If the incoming tier is alone (no coexisting models), headroom
            # is not enforced — just need <= total. With coexisting models,
            # projected + need + headroom must fit.
            other_used = max(0.0, projected)
            if other_used == 0:
                return need <= total
            return other_used + need + headroom <= total

        def _current_projected() -> float:
            return sum(
                m.effective_cost() for m in self.loaded.values()
                if m.state != ModelState.EVICTING
            )

        if _fits(_current_projected()):
            return

        # Evict LRU unpinned refcount==0 until fits
        now = time.time()
        candidates = [
            m for m in self.loaded.values()
            if m.state == ModelState.RESIDENT
            and m.refcount == 0
            and not m.pinned
            and (now - m.last_used) >= self.vram.eviction.min_residency_sec
        ]
        candidates.sort(key=lambda m: m.last_used)  # LRU first

        for victim in candidates:
            if _fits(_current_projected()):
                break
            await self._unload(victim)

        if _fits(_current_projected()):
            return

        # Relax the min_residency guard as a last resort
        late_candidates = [
            m for m in self.loaded.values()
            if m.state == ModelState.RESIDENT
            and m.refcount == 0
            and not m.pinned
            and m not in candidates
        ]
        late_candidates.sort(key=lambda m: m.last_used)
        for victim in late_candidates:
            if _fits(_current_projected()):
                break
            await self._unload(victim)

        if not _fits(_current_projected()):
            remaining = _current_projected()
            raise VRAMExhausted(
                f"Cannot fit tier needing {need:.1f}GB "
                f"(pinned/in-use={remaining:.1f}GB, total={total}GB)"
            )

    async def _unload(self, model: LoadedModel) -> None:
        """Must be called with `self._lock` held."""
        model.state = ModelState.EVICTING
        tier = self.tiers.get(model.tier_id)
        if tier:
            unloader = self.unloaders.get(tier.backend)
            if unloader:
                try:
                    # Release lock briefly; unloader may be slow
                    self._lock.release()
                    try:
                        await unloader(tier)
                    finally:
                        await self._lock.acquire()
                except Exception as e:
                    logger.warning("Unload failed for %s: %s", model.tier_id, e)
        self.loaded.pop(model.tier_id, None)

    # ── Background sweeper ──────────────────────────────────────────────

    async def _sweeper(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.vram.poll_interval_sec)
                await self._sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Sweeper error: %s", e)

    async def _sweep_once(self) -> None:
        free = self.probe.free_gb(self.vram.total_vram_gb)
        if free >= self.vram.headroom_gb:
            return
        logger.info("VRAM pressure: only %.2fGB free, evicting idle tiers", free)
        async with self._lock:
            candidates = [
                m for m in self.loaded.values()
                if m.state == ModelState.RESIDENT
                and m.refcount == 0
                and not m.pinned
            ]
            candidates.sort(key=lambda m: m.last_used)
            for victim in candidates:
                await self._unload(victim)
                free = self.probe.free_gb(self.vram.total_vram_gb)
                if free >= self.vram.headroom_gb:
                    break

    # ── Introspection ────────────────────────────────────────────────────

    async def status(self) -> dict:
        async with self._lock:
            loaded_list = []
            now = time.time()
            for m in self.loaded.values():
                loaded_list.append({
                    "tier_id": m.tier_id,
                    "model_tag": m.model_tag,
                    "backend": m.backend,
                    "state": m.state.value,
                    "refcount": m.refcount,
                    "vram_cost_gb": m.vram_estimate_gb,
                    "observed_cost_gb": m.observed_cost_gb,
                    "last_used_sec_ago": now - m.last_used,
                })
            projected = sum(
                m.effective_cost() for m in self.loaded.values()
                if m.state != ModelState.EVICTING
            )
        actual_free = self.probe.free_gb(self.vram.total_vram_gb)
        return {
            "total_vram_gb": self.vram.total_vram_gb,
            "free_vram_gb_actual": actual_free,
            "free_vram_gb_projected": self.vram.total_vram_gb - projected,
            "headroom_gb": self.vram.headroom_gb,
            "loaded": loaded_list,
        }
