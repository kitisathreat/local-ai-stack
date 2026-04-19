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
