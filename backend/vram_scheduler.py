"""VRAM-aware model scheduler with per-tier slot cap + wait queue.

A single asyncio coordinator that tracks which model tiers are resident in
GPU memory, reference-counted by in-flight requests. When a new reservation
would exceed available VRAM (minus the configured headroom), it evicts
least-recently-used, unpinned tiers with refcount==0.

Slot cap + queue:
  Each loaded model has `slot_capacity = tier.parallel_slots`, matching the
  ``--parallel`` count llama-server was launched with. Refcount is capped
  at slot_capacity. Requests beyond the cap enter a per-tier FIFO wait
  queue driven by an `asyncio.Condition`. While queued, `acquire()` invokes
  the caller's `on_event` callback every
  `queue.position_update_interval_sec` so the SSE stream can surface
  progress. The queue itself is bounded by `queue.max_depth_per_tier` (over
  → QueueFull) and the total wait by `queue.max_wait_sec` (over →
  QueueTimeout).

Multi-worker note:
  With Uvicorn `--workers N`, each worker owns its own in-process registry.
  That's safe because the per-tier llama-server processes are the source of
  truth for what's actually loaded; ``LlamaCppClient.list_running()`` plus
  pynvml reconciliation on the sweeper poll cycle correct any drift.
  Cross-worker rate limiting is handled by the Redis-backed limiter (see
  backend/middleware/rate_limit.py).

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
from typing import Any, AsyncIterator, Awaitable, Callable

from .config import AppConfig, TierConfig


OnEventFn = Callable[[dict[str, Any]], Awaitable[None]]


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
    # Maximum concurrent requests this loaded model can serve. Set from
    # TierConfig.parallel_slots at load time. Refcount is capped at this
    # value; further requests queue on the tier's Condition.
    slot_capacity: int = 1
    # parallel_slots value the model was loaded with. If the tier config
    # changes (admin edit), the scheduler triggers an eviction so the next
    # reserve reloads with the new slot count. Tracked separately from
    # slot_capacity so a concurrent in-flight config edit doesn't confuse
    # in-flight acquires.
    loaded_slots: int = 1
    # Currently-active variant name (None = default / no variants declared).
    # Variant changes trigger eviction on next idle so the next reserve
    # reloads with the new variant — same pattern as a parallel_slots edit.
    variant: str | None = None

    def effective_cost(self) -> float:
        """Use max(estimate, observed) for eviction math to be safe."""
        if self.observed_cost_gb is None:
            return self.vram_estimate_gb
        return max(self.vram_estimate_gb, self.observed_cost_gb)


class VRAMExhausted(Exception):
    """Raised when a reservation cannot be satisfied even after eviction."""


class QueueFull(Exception):
    """Raised when the per-tier wait queue is already at max_depth_per_tier.

    The backend surfaces this as HTTP 503 + Retry-After.
    """


class QueueTimeout(Exception):
    """Raised when a queued reservation exceeds queue.max_wait_sec.

    The backend surfaces this as an SSE error event (stream already open) or
    HTTP 503 (pre-stream).
    """


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
        # Per-tier wait queue: Condition for notifying slot availability +
        # a waiter counter for position reporting and max_depth enforcement.
        self._gates: dict[str, asyncio.Condition] = {}
        self._waiters: dict[str, int] = {}
        self._sweeper_task: asyncio.Task | None = None
        self._observed_path = Path(self.vram.observed_costs.persist_path)
        self._load_observed_costs()

    def _gate(self, tier_id: str) -> asyncio.Condition:
        g = self._gates.get(tier_id)
        if g is None:
            g = asyncio.Condition()
            self._gates[tier_id] = g
        return g

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
    async def reserve(
        self,
        tier_id: str,
        on_event: OnEventFn | None = None,
        variant: str | None = None,
        live_user_text: str = "",
    ) -> AsyncIterator[str]:
        """Hold a slot on the tier's loaded model for the duration of the
        `with` block. If the model is not loaded, loads it. If all slots are
        taken, waits on the per-tier queue, emitting `{kind:"queued",...}`
        events through `on_event` every queue.position_update_interval_sec.

        Raises QueueFull if the queue is already at max_depth_per_tier and
        QueueTimeout if the total wait exceeds queue.max_wait_sec.

        `variant` selects a per-tier model variant (e.g. coding/30b vs
        coding/80b). When the currently-loaded variant differs from the
        requested one and the tier is idle (refcount=0), it's evicted so
        the new variant can spawn. When non-idle, the request waits via
        the standard queue — on busy variant churn this serializes naturally.
        """
        await self.acquire(
            tier_id, on_event, variant=variant, live_user_text=live_user_text,
        )
        try:
            yield tier_id
        finally:
            await self.release(tier_id)

    async def acquire(
        self,
        tier_id: str,
        on_event: OnEventFn | None = None,
        variant: str | None = None,
        live_user_text: str = "",
    ) -> None:
        if tier_id not in self.tiers:
            raise KeyError(f"Unknown tier: {tier_id}")
        tier = self.tiers[tier_id]
        # Resolve to the variant-effective tier so VRAM math + slot count
        # match what build_argv will actually spawn.
        effective_tier = tier.resolve_variant(variant)
        # The variant key we'll record on LoadedModel — falls back to the
        # tier's default_variant when the caller passed None so that
        # repeated default-requests don't churn against an explicit
        # default variant request.
        active_variant = variant or tier.default_variant
        slot_cap = max(1, int(getattr(effective_tier, "parallel_slots", 1)))
        qcfg = self.cfg.vram.queue
        deadline = time.time() + max(1, qcfg.max_wait_sec)
        entered_queue = False
        last_event_pos: int | None = None

        try:
            while True:
                # Step 1: Try to grab a slot under the lock. Decide if we
                # need to trigger a load, join the queue, or wait on an
                # in-flight load.
                load_needed = False
                wait_for_load: asyncio.Event | None = None
                async with self._lock:
                    m = self.loaded.get(tier_id)
                    if m and m.state == ModelState.RESIDENT:
                        # Variant mismatch is treated like a config change:
                        # the wrong variant is loaded, so evict and reload.
                        # If non-idle, fall through into the queue path.
                        variant_mismatch = (
                            active_variant is not None
                            and m.variant is not None
                            and m.variant != active_variant
                        )
                        # Config changed under us? Evict so we reload with
                        # the new slot count or variant.
                        if (m.loaded_slots != slot_cap or variant_mismatch) and m.refcount == 0:
                            await self._unload(m)
                            m = None
                        elif (
                            not variant_mismatch
                            and m.refcount < m.slot_capacity
                        ):
                            m.refcount += 1
                            m.last_used = time.time()
                            return
                    if m is None or m.state == ModelState.EVICTING:
                        # Not resident → we'll load it. Join queue so other
                        # concurrent reservers wait behind us.
                        if not entered_queue:
                            if self._waiters.get(tier_id, 0) >= qcfg.max_depth_per_tier:
                                raise QueueFull(
                                    f"Tier {tier_id!r} queue full "
                                    f"({qcfg.max_depth_per_tier})"
                                )
                            self._waiters[tier_id] = self._waiters.get(tier_id, 0) + 1
                            entered_queue = True
                        # Pre-eviction event for the chat UI so the
                        # user sees "Making room for <tier>..." while
                        # _make_room_for is unloading other models.
                        if on_event:
                            try:
                                await on_event({
                                    "type": "vram.making_room",
                                    "tier_id": tier_id,
                                    "needs_gb": tier.vram_estimate_gb,
                                })
                            except Exception:
                                logger.debug("on_event vram.making_room raised; continuing")
                        await self._make_room_for(effective_tier)
                        new_entry = LoadedModel(
                            tier_id=tier_id,
                            backend=effective_tier.backend,
                            model_tag=effective_tier.model_tag,
                            vram_estimate_gb=effective_tier.vram_estimate_gb,
                            observed_cost_gb=self._observed.get(tier_id),
                            state=ModelState.LOADING,
                            refcount=0,  # bumped after successful load
                            pinned=effective_tier.pinned,
                            slot_capacity=slot_cap,
                            loaded_slots=slot_cap,
                            variant=active_variant,
                        )
                        self.loaded[tier_id] = new_entry
                        load_needed = True
                    elif m.state == ModelState.LOADING:
                        # Someone else is loading. Wait on the event.
                        if not entered_queue:
                            if self._waiters.get(tier_id, 0) >= qcfg.max_depth_per_tier:
                                raise QueueFull(
                                    f"Tier {tier_id!r} queue full "
                                    f"({qcfg.max_depth_per_tier})"
                                )
                            self._waiters[tier_id] = self._waiters.get(tier_id, 0) + 1
                            entered_queue = True
                        wait_for_load = m.load_event
                    else:
                        # Resident but full → queue and wait
                        if not entered_queue:
                            if self._waiters.get(tier_id, 0) >= qcfg.max_depth_per_tier:
                                raise QueueFull(
                                    f"Tier {tier_id!r} queue full "
                                    f"({qcfg.max_depth_per_tier})"
                                )
                            self._waiters[tier_id] = self._waiters.get(tier_id, 0) + 1
                            entered_queue = True

                # Step 2: Perform load outside the lock.
                if load_needed:
                    # Emit a "loading" event so the chat UI can show
                    # "Loading <model> into VRAM..." while llama-server
                    # streams weights from disk → GPU. Without this the
                    # user sees a 5-30s silent gap between "Routing to..."
                    # and the first token.
                    if on_event:
                        try:
                            await on_event({
                                "type": "tier.loading",
                                "tier_id": tier_id,
                                "model_tag": effective_tier.model_tag,
                                "variant": active_variant,
                            })
                        except Exception:
                            logger.debug("on_event tier.loading raised; continuing")
                    before_free = self.probe.free_gb(self.vram.total_vram_gb)
                    try:
                        loader = self.loaders.get(effective_tier.backend)
                        if loader:
                            # Optional kwargs: free_vram_gb (residency
                            # planner), variant (per-tier model variant),
                            # and live_user_text (the latest user message,
                            # used by the residency planner for complexity
                            # estimation). Older loader signatures fall
                            # through the TypeError ladder.
                            try:
                                await loader(
                                    tier,
                                    free_vram_gb=before_free,
                                    variant=active_variant,
                                    live_user_text=live_user_text,
                                )
                            except TypeError:
                                try:
                                    await loader(
                                        tier,
                                        free_vram_gb=before_free,
                                        variant=active_variant,
                                    )
                                except TypeError:
                                    try:
                                        await loader(tier, free_vram_gb=before_free)
                                    except TypeError:
                                        await loader(tier)
                    except Exception:
                        async with self._lock:
                            new_entry.state = ModelState.EVICTING
                            self.loaded.pop(tier_id, None)
                            new_entry.load_event.set()
                        # Wake everyone so queued waiters can retry or bail
                        gate = self._gate(tier_id)
                        async with gate:
                            gate.notify_all()
                        raise
                    after_free = self.probe.free_gb(self.vram.total_vram_gb)
                    measured = max(0.0, before_free - after_free)
                    if measured > 0:
                        new_entry.observed_cost_gb = self._update_observed(
                            tier_id, measured,
                        )
                    async with self._lock:
                        new_entry.state = ModelState.RESIDENT
                        new_entry.last_used = time.time()
                        new_entry.load_event.set()
                    gate = self._gate(tier_id)
                    async with gate:
                        gate.notify_all()
                    # Loop back to grab a slot under the lock
                    continue

                # Step 3: Wait for an in-flight load to finish, then retry.
                if wait_for_load is not None:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        raise QueueTimeout(f"Queued wait exceeded max_wait_sec for {tier_id!r}")
                    try:
                        await asyncio.wait_for(wait_for_load.wait(), timeout=remaining)
                    except asyncio.TimeoutError:
                        raise QueueTimeout(
                            f"Queued wait exceeded max_wait_sec for {tier_id!r}",
                        )
                    continue

                # Step 4: Queued behind a full resident model. Emit a
                # progress event and wait on the condition.
                if on_event and entered_queue:
                    pos = self._waiters.get(tier_id, 0)
                    if pos != last_event_pos:
                        try:
                            await on_event({
                                "kind": "queued",
                                "tier": tier_id,
                                "position": pos,
                                "waited_sec": max(
                                    0,
                                    int(qcfg.max_wait_sec - (deadline - time.time())),
                                ),
                                "max_wait_sec": qcfg.max_wait_sec,
                            })
                            last_event_pos = pos
                        except Exception:
                            logger.debug("on_event callback raised; continuing")
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise QueueTimeout(f"Queued wait exceeded max_wait_sec for {tier_id!r}")
                wait_for = min(
                    max(0.1, float(qcfg.position_update_interval_sec)),
                    remaining,
                )
                gate = self._gate(tier_id)
                async with gate:
                    try:
                        await asyncio.wait_for(gate.wait(), timeout=wait_for)
                    except asyncio.TimeoutError:
                        pass  # re-emit position on next loop
        finally:
            if entered_queue:
                async with self._lock:
                    self._waiters[tier_id] = max(
                        0, self._waiters.get(tier_id, 0) - 1,
                    )

    async def release(self, tier_id: str) -> None:
        async with self._lock:
            m = self.loaded.get(tier_id)
            if not m:
                return
            m.refcount = max(0, m.refcount - 1)
            m.last_used = time.time()
        # Wake one waiter so it can grab the freed slot.
        gate = self._gate(tier_id)
        async with gate:
            gate.notify(1)

    # Backwards-compatible alias — some callers still use the underscore form.
    async def _release(self, tier_id: str) -> None:
        await self.release(tier_id)

    async def release_temporarily(self, tier_id: str) -> None:
        """Multi-agent helper: drop refcount to 0 so workers can evict this
        tier if they need the VRAM. Caller must re-acquire later."""
        await self.release(tier_id)

    async def mark_tier_dirty(self, tier_id: str) -> None:
        """Called by admin PATCH when a tier's parallel_slots (or other
        load-time parameter) changes. Evicts immediately if refcount==0;
        otherwise the next acquire will notice `loaded_slots != parallel_slots`
        once refcount reaches 0 and reload with the new value.
        """
        async with self._lock:
            m = self.loaded.get(tier_id)
            if m and m.state == ModelState.RESIDENT and m.refcount == 0:
                await self._unload(m)
        gate = self._gate(tier_id)
        async with gate:
            gate.notify_all()

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

        # NVML cross-check guards against orphan llama-server processes
        # (left from a previous crash / backend bounce / external VRAM
        # consumer) that reduce real free without touching `self.loaded`.
        # Trusting projection alone there causes a false "fits", spawn
        # proceeds, llama-server OOMs, scheduler thinks the tier loaded,
        # every chat thereafter 503s. We re-poll inside `_fits` so that
        # successful evictions (which the loop below performs) update
        # the actual-free reading — otherwise we'd spuriously raise
        # VRAMExhausted after evicting plenty.
        def _fits(projected: float) -> bool:
            # If the incoming tier is alone in our registry (no coexisting
            # models we manage), headroom is not enforced — just need <= total.
            # With coexisting models, projected + need + headroom must fit.
            other_used = max(0.0, projected)
            if other_used == 0:
                projection_ok = need <= total
            else:
                projection_ok = (other_used + need + headroom <= total)
            if not projection_ok:
                return False
            # NVML cross-check — only meaningful when pynvml is available
            # (free_gb returns the `total` sentinel when it isn't, which
            # makes this check trivially pass).
            actual_free = self.probe.free_gb(total)
            if actual_free >= total:
                return True
            return need + headroom <= actual_free

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
            actual_free_now = self.probe.free_gb(total)
            actual_used_now = max(0.0, total - actual_free_now) if actual_free_now <= total else 0.0
            untracked = max(0.0, actual_used_now - remaining)
            # Surface the NVML reading + the untracked-VRAM gap so the
            # message is actionable. The "pinned/in-use=0.0GB" reading
            # was misleading by itself — when it's near zero but the
            # spawn still fails, the cause is *untracked* VRAM (orphan
            # llama-server, another process holding the GPU, etc.) and
            # /admin/vram/probe will show the same drift.
            from .error_codes import format_error, VRAM_EXHAUSTED
            raise VRAMExhausted(format_error(
                VRAM_EXHAUSTED,
                f"need {need:.1f} GB + {headroom:.1f} GB headroom; "
                f"scheduler-tracked in-use {remaining:.1f} GB; "
                f"NVML actual free {actual_free_now:.1f} GB / total {total} GB; "
                f"untracked VRAM (orphan or external consumer) "
                f"{untracked:.1f} GB. "
                "Check GET /admin/vram/probe for orphans; "
                "POST /admin/vram/kill-orphans to reap them."
            ))

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
                    "slot_capacity": m.slot_capacity,
                    "waiters": self._waiters.get(m.tier_id, 0),
                    "vram_cost_gb": m.vram_estimate_gb,
                    "observed_cost_gb": m.observed_cost_gb,
                    "last_used_sec_ago": now - m.last_used,
                })
            projected = sum(
                m.effective_cost() for m in self.loaded.values()
                if m.state != ModelState.EVICTING
            )
            queue_snapshot = dict(self._waiters)
        actual_free = self.probe.free_gb(self.vram.total_vram_gb)
        return {
            "total_vram_gb": self.vram.total_vram_gb,
            "free_vram_gb_actual": actual_free,
            "free_vram_gb_projected": self.vram.total_vram_gb - projected,
            "headroom_gb": self.vram.headroom_gb,
            "loaded": loaded_list,
            "waiters_by_tier": queue_snapshot,
        }
