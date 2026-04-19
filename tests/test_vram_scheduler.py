"""Unit tests for backend/vram_scheduler.py with mocked NVML and
mocked backend load/unload functions."""

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from backend.config import AppConfig
from backend.vram_scheduler import (
    GPUProbe,
    ModelState,
    QueueFull,
    QueueTimeout,
    VRAMExhausted,
    VRAMScheduler,
)


@pytest.fixture
def cfg(tmp_path, monkeypatch) -> AppConfig:
    # Persist observed costs to a tmp path so tests don't contaminate each other
    c = AppConfig.load(config_dir=ROOT / "config")
    c.vram.observed_costs.persist_path = str(tmp_path / "vram_observed.json")
    c.vram.eviction.min_residency_sec = 0  # disable anti-thrash for tests
    return c


class FakeProbe(GPUProbe):
    """Deterministic probe — tracks free VRAM by pretending each loaded
    model consumes a fixed amount."""

    def __init__(self, total_gb: float, loaded_costs: dict):
        self._total = total_gb
        self._loaded_costs = loaded_costs

    def free_gb(self, total_gb: float) -> float:
        used = sum(self._loaded_costs.values())
        return max(0.0, self._total - used)

    def used_gb(self) -> float:
        return sum(self._loaded_costs.values())


def make_scheduler(cfg: AppConfig, probe: FakeProbe) -> VRAMScheduler:
    # Track "loaded models" on the fake probe
    async def loader(tier):
        probe._loaded_costs[tier.model_tag] = tier.vram_estimate_gb

    async def unloader(tier):
        probe._loaded_costs.pop(tier.model_tag, None)

    sched = VRAMScheduler(
        config=cfg,
        loaders={"ollama": loader, "llama_cpp": loader},
        unloaders={"ollama": unloader, "llama_cpp": unloader},
        probe=probe,
    )
    return sched


@pytest.mark.asyncio
async def test_reserve_loads_model(cfg):
    probe = FakeProbe(total_gb=24.0, loaded_costs={})
    sched = make_scheduler(cfg, probe)
    async with sched.reserve("fast"):
        assert "fast" in sched.loaded
        assert sched.loaded["fast"].state == ModelState.RESIDENT
        assert sched.loaded["fast"].refcount == 1
    assert sched.loaded["fast"].refcount == 0


@pytest.mark.asyncio
async def test_reserve_twice_shares_loaded(cfg):
    probe = FakeProbe(total_gb=24.0, loaded_costs={})
    sched = make_scheduler(cfg, probe)

    async def hold():
        async with sched.reserve("fast"):
            await asyncio.sleep(0.05)

    await asyncio.gather(hold(), hold())
    # Both reservations drop to refcount 0 after exiting; only one load total
    assert sched.loaded["fast"].refcount == 0


@pytest.mark.asyncio
async def test_evict_lru_when_pressure(cfg):
    probe = FakeProbe(total_gb=24.0, loaded_costs={})
    sched = make_scheduler(cfg, probe)

    # Load Versatile (21GB), then request Highest Quality (24GB) → evicts
    async with sched.reserve("versatile"):
        pass
    assert "versatile" in sched.loaded
    assert sched.loaded["versatile"].refcount == 0

    async with sched.reserve("highest_quality"):
        assert "highest_quality" in sched.loaded
        assert "versatile" not in sched.loaded  # evicted


@pytest.mark.asyncio
async def test_vram_exhausted_when_cant_evict(cfg):
    """If another tier is actively in use (refcount > 0) and there's no
    room for the new request, raise VRAMExhausted."""
    probe = FakeProbe(total_gb=24.0, loaded_costs={})
    sched = make_scheduler(cfg, probe)

    async def hog():
        async with sched.reserve("highest_quality"):
            await asyncio.sleep(0.5)

    async def try_load_too_much():
        await asyncio.sleep(0.05)
        with pytest.raises(VRAMExhausted):
            async with sched.reserve("coding"):
                pass

    await asyncio.gather(hog(), try_load_too_much())


@pytest.mark.asyncio
async def test_pinned_not_evicted(cfg):
    """Vision is pinned — Coding request must fail rather than evict Vision."""
    probe = FakeProbe(total_gb=24.0, loaded_costs={})
    sched = make_scheduler(cfg, probe)

    async with sched.reserve("vision"):
        pass  # refcount drops to 0 but pinned=True prevents eviction

    # Vision is 21GB; Coding is 24GB; Vision pinned → can't fit Coding
    with pytest.raises(VRAMExhausted):
        async with sched.reserve("coding"):
            pass

    # Vision still loaded
    assert "vision" in sched.loaded


@pytest.mark.asyncio
async def test_status_reports_loaded(cfg):
    probe = FakeProbe(total_gb=24.0, loaded_costs={})
    sched = make_scheduler(cfg, probe)
    async with sched.reserve("fast"):
        status = await sched.status()
        assert status["total_vram_gb"] == 24.0
        loaded_ids = [m["tier_id"] for m in status["loaded"]]
        assert "fast" in loaded_ids


@pytest.mark.asyncio
async def test_release_temporarily_allows_eviction(cfg):
    """Multi-agent case: orchestrator reserved, released temporarily,
    workers can now claim the VRAM."""
    probe = FakeProbe(total_gb=24.0, loaded_costs={})
    sched = make_scheduler(cfg, probe)

    async with sched.reserve("versatile"):
        pass

    await sched.release_temporarily("versatile")
    # Should fit 3× Fast (7GB each = 21GB)
    async with sched.reserve("fast"):
        assert "fast" in sched.loaded


# ── Slot cap + wait queue ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_slot_cap_caps_concurrent_holders(cfg):
    """Fast has parallel_slots=4; first 4 concurrent reserves succeed
    immediately, the 5th queues until one releases."""
    probe = FakeProbe(total_gb=24.0, loaded_costs={})
    sched = make_scheduler(cfg, probe)

    started = [asyncio.Event() for _ in range(5)]
    release_gates = [asyncio.Event() for _ in range(5)]

    async def worker(i: int):
        async with sched.reserve("fast"):
            started[i].set()
            await release_gates[i].wait()

    tasks = [asyncio.create_task(worker(i)) for i in range(5)]

    # Wait for the first 4 to acquire
    await asyncio.wait_for(
        asyncio.gather(*(started[i].wait() for i in range(4))),
        timeout=1.0,
    )
    # 5th must not have started yet
    assert not started[4].is_set()
    assert sched.loaded["fast"].refcount == 4

    # Release one → 5th should wake up
    release_gates[0].set()
    await asyncio.wait_for(started[4].wait(), timeout=1.0)

    # Clean up
    for g in release_gates:
        g.set()
    await asyncio.gather(*tasks)
    assert sched.loaded["fast"].refcount == 0


@pytest.mark.asyncio
async def test_queue_full_rejects_beyond_max_depth(cfg):
    """When max_depth_per_tier=1, a second waiter is rejected with QueueFull."""
    cfg.vram.queue.max_depth_per_tier = 1
    cfg.vram.queue.max_wait_sec = 5
    probe = FakeProbe(total_gb=24.0, loaded_costs={})
    sched = make_scheduler(cfg, probe)

    held = asyncio.Event()
    release = asyncio.Event()

    async def hog():
        # Parallel slots=4 for fast; hog all 4.
        async def one():
            async with sched.reserve("fast"):
                held.set()
                await release.wait()
        await asyncio.gather(*[one() for _ in range(4)])

    hog_task = asyncio.create_task(hog())
    await asyncio.wait_for(held.wait(), timeout=1.0)

    # First waiter (5th overall) occupies the queue slot.
    async def wait_for_slot():
        async with sched.reserve("fast"):
            pass

    waiter = asyncio.create_task(wait_for_slot())
    # Give the waiter a moment to register.
    await asyncio.sleep(0.05)

    # A second waiter must be rejected with QueueFull.
    with pytest.raises(QueueFull):
        async with sched.reserve("fast"):
            pass

    # Let things drain.
    release.set()
    await asyncio.gather(waiter, hog_task)


@pytest.mark.asyncio
async def test_queue_timeout_on_long_wait(cfg):
    """With max_wait_sec=0.2, a queued reserve times out if no slot opens."""
    cfg.vram.queue.max_wait_sec = 1  # minimum allowed is 1
    cfg.vram.queue.position_update_interval_sec = 1
    cfg.vram.queue.max_depth_per_tier = 10
    probe = FakeProbe(total_gb=24.0, loaded_costs={})
    sched = make_scheduler(cfg, probe)

    held = asyncio.Event()
    release = asyncio.Event()

    async def hog_one():
        async with sched.reserve("fast"):
            held.set()
            await release.wait()

    hogs = [asyncio.create_task(hog_one()) for _ in range(4)]
    await asyncio.wait_for(held.wait(), timeout=1.0)

    with pytest.raises(QueueTimeout):
        async with sched.reserve("fast"):
            pass

    release.set()
    await asyncio.gather(*hogs)


@pytest.mark.asyncio
async def test_on_event_emits_queue_progress(cfg):
    """The on_event callback is invoked with {kind:"queued",...} while waiting."""
    cfg.vram.queue.position_update_interval_sec = 1
    cfg.vram.queue.max_wait_sec = 5
    probe = FakeProbe(total_gb=24.0, loaded_costs={})
    sched = make_scheduler(cfg, probe)

    held = asyncio.Event()
    release = asyncio.Event()

    async def hog_one():
        async with sched.reserve("fast"):
            held.set()
            await release.wait()

    hogs = [asyncio.create_task(hog_one()) for _ in range(4)]
    await asyncio.wait_for(held.wait(), timeout=1.0)

    events: list[dict] = []

    async def on_event(ev: dict):
        events.append(ev)

    async def wait_for_slot():
        async with sched.reserve("fast", on_event=on_event):
            pass

    waiter = asyncio.create_task(wait_for_slot())
    # Allow one interval cycle of queued events.
    await asyncio.sleep(1.2)
    assert any(e.get("kind") == "queued" for e in events)

    release.set()
    await asyncio.gather(waiter, *hogs)


@pytest.mark.asyncio
async def test_mark_tier_dirty_evicts_idle(cfg):
    """Changing parallel_slots should evict the model if idle so it reloads
    with the new num_parallel value."""
    probe = FakeProbe(total_gb=24.0, loaded_costs={})
    sched = make_scheduler(cfg, probe)

    async with sched.reserve("fast"):
        pass
    assert "fast" in sched.loaded
    assert sched.loaded["fast"].refcount == 0

    await sched.mark_tier_dirty("fast")
    # Evicted since refcount==0
    assert "fast" not in sched.loaded
