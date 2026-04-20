"""Unit tests for backend/model_residency.py — the partial-load planner
that chooses num_gpu / mmap / mlock per tier based on free VRAM + the
live request's complexity signal."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from backend.config import TierConfig
from backend.model_residency import (
    ResidencyMode,
    ResidencyPolicy,
    merge_into_options,
    plan_residency,
)


def _tier(name="fast", vram=7.0, tag="qwen3.5:9b", pinned=False) -> TierConfig:
    return TierConfig(
        name=name,
        description="t",
        backend="ollama",
        endpoint="http://ollama:11434",
        model_tag=tag,
        context_window=4096,
        vram_estimate_gb=vram,
        pinned=pinned,
    )


# ── FULL mode ──────────────────────────────────────────────────────────────

def test_full_when_headroom_and_complex():
    plan = plan_residency(
        _tier(vram=7.0),
        free_vram_gb=22.0,
        # Hits 2 complexity patterns ("analyze/refactor" + "walk me through"),
        # which crosses the 0.5 threshold even at short length.
        live_user_text=(
            "Please analyze this codebase and walk me through step by step "
            "how to refactor the auth layer."
        ),
    )
    assert plan.mode == ResidencyMode.FULL
    assert plan.num_gpu_layers == plan.total_layers
    assert plan.use_mmap is True
    assert plan.use_mlock is True


def test_full_with_pinned_tier_even_if_trivial():
    plan = plan_residency(
        _tier(vram=21.0, pinned=True),
        free_vram_gb=25.0,  # > 21 * 1.15 = 24.15, so fits_full holds
        live_user_text="hi",
    )
    # With headroom+pinned, planner stays FULL (doesn't shave trivial-turn layers)
    assert plan.mode == ResidencyMode.FULL


# ── Trivial turn with headroom shaves some layers ──────────────────────────

def test_trivial_turn_with_headroom_shaves_layers():
    plan = plan_residency(
        _tier(vram=7.0),
        free_vram_gb=22.0,
        live_user_text="hi",
    )
    assert plan.mode == ResidencyMode.PARTIAL
    assert plan.num_gpu_layers < plan.total_layers


# ── PARTIAL under pressure ─────────────────────────────────────────────────

def test_partial_when_tight_vram():
    plan = plan_residency(
        _tier(vram=21.0),
        free_vram_gb=10.0,
        live_user_text="Please analyze this codebase carefully.",
    )
    assert plan.mode == ResidencyMode.PARTIAL
    assert plan.num_gpu_layers < plan.total_layers
    assert plan.use_mmap is True
    assert plan.use_mlock is False
    assert plan.projected_vram_gb < 21.0


# ── MINIMAL when severely tight ────────────────────────────────────────────

def test_minimal_when_severely_tight():
    plan = plan_residency(
        _tier(vram=24.0),
        free_vram_gb=2.0,
        live_user_text="complex proof walkthrough step by step",
    )
    assert plan.mode == ResidencyMode.MINIMAL
    assert plan.num_gpu_layers >= 1


# ── User preference override ───────────────────────────────────────────────

def test_user_preference_overrides_heuristics():
    plan = plan_residency(
        _tier(vram=7.0),
        free_vram_gb=22.0,
        live_user_text="complex analysis please",
        user_preference=ResidencyMode.MINIMAL,
    )
    assert plan.mode == ResidencyMode.MINIMAL
    assert plan.reason == "user-pref"


# ── Policy knobs ───────────────────────────────────────────────────────────

def test_policy_min_ratio_enforced():
    pol = ResidencyPolicy(partial_min_ratio=0.5, minimal_ratio=0.3)
    plan = plan_residency(
        _tier(vram=20.0),
        free_vram_gb=5.0,
        live_user_text="simple",
        policy=pol,
    )
    # With free/cost=0.25 and complexity low, would otherwise go below the
    # partial_min_ratio — falls through to MINIMAL (respecting minimal_ratio)
    assert plan.mode == ResidencyMode.MINIMAL
    ratio = plan.num_gpu_layers / plan.total_layers
    assert ratio >= pol.minimal_ratio - 1e-6


# ── merge_into_options ─────────────────────────────────────────────────────

def test_merge_into_options_caller_wins():
    plan = plan_residency(_tier(), free_vram_gb=22.0, live_user_text="analyze")
    out = merge_into_options({"num_gpu": 99}, plan)
    # Caller-provided value overrides the planner's suggestion
    assert out["num_gpu"] == 99
    # Flags the caller didn't set come from the plan
    assert "use_mmap" in out
    assert "use_mlock" in out


def test_merge_into_options_empty_caller():
    plan = plan_residency(_tier(), free_vram_gb=22.0, live_user_text="analyze")
    out = merge_into_options({}, plan)
    assert out["num_gpu"] == plan.num_gpu_layers
    assert out["use_mmap"] == plan.use_mmap
