"""Per-model partial residency planner.

Complements `vram_scheduler.py` (which decides *whether* a tier is loaded)
by deciding *how much of it* is loaded. Rather than always pushing every
transformer block into VRAM, we choose a `n_gpu_layers` count and
mmap/mlock posture appropriate to:

  - the current free VRAM headroom,
  - the caller's explicit preference (if any),
  - the tier's own vram_estimate vs. the free budget,
  - a simple request-complexity signal (live user text) so a trivial
    "what's 2+2?" turn doesn't need the full 80B coder on the GPU while
    something vision-heavy or long-reasoning does.

Output is a `ResidencyPlan` plus a dict of llama-server CLI argv
contributions (``--n-gpu-layers``, ``--no-mmap``, ``--mlock``) that the
spawn path in ``backends/llama_cpp.py`` merges into the per-tier argv.
Mode changes mid-life require a process respawn — encoded by the
scheduler via ``mark_tier_dirty``.

The planner is intentionally conservative: unless VRAM is tight or the
user asks for partial residency, it returns the "full" plan so existing
behavior is preserved.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .config import TierConfig


logger = logging.getLogger(__name__)


# Typical layer counts per architecture family. The planner doesn't need
# exact numbers — Ollama clamps `num_gpu` to the model's real layer count
# — but picking a sensible default helps us scale the offload ratio.
_LAYER_HINTS = {
    # Current chat tiers
    "qwen3-next-80b-a3b": 48,            # Hybrid: ~25% full-attn + ~75% GDN/Mamba
    "qwen3.6-35b-a3b": 64,
    "qwen3.5-9b": 40,
    "qwen3-coder-30b-a3b": 48,
    # Legacy entries kept for graceful fallback during migration
    "qwen3:72b": 80,
    "qwen3-coder-next:80b": 80,
    "qwen2.5-coder:32b": 64,
}


def _layer_hint(tier: TierConfig) -> int:
    tag = (tier.model_tag or "").lower()
    for key, n in _LAYER_HINTS.items():
        if key in tag:
            return n
    # Generic fallback — most 7-13B models fit in ~32-40 blocks, bigger
    # ones in ~64-80. Pick by VRAM estimate as a proxy.
    if tier.vram_estimate_gb >= 20:
        return 80
    if tier.vram_estimate_gb >= 10:
        return 60
    return 40


class ResidencyMode(str, Enum):
    FULL = "full"                        # all layers on GPU, mlock
    PARTIAL = "partial"                  # some layers CPU-offloaded via mmap
    MINIMAL = "minimal"                  # smallest GPU footprint that still runs


@dataclass
class ResidencyPlan:
    tier_id: str
    mode: ResidencyMode
    num_gpu_layers: int                  # total transformer blocks targeted for GPU
    total_layers: int
    use_mmap: bool                       # lazy-page weights from disk
    use_mlock: bool                      # pin mapped pages in RAM
    reason: str                          # short human-readable justification
    projected_vram_gb: float             # estimated VRAM cost at this offload ratio

    def to_backend_options(self) -> dict[str, Any]:
        """Spawn-time argv contributions consumed by
        ``backends/llama_cpp.py`` when launching a per-tier llama-server."""
        return {
            "n_gpu_layers": self.num_gpu_layers,
            "use_mmap": self.use_mmap,
            "use_mlock": self.use_mlock,
        }


# ── Signals ─────────────────────────────────────────────────────────────────

_COMPLEXITY_PATTERNS = (
    re.compile(r"```"),                  # code fences
    re.compile(r"\b(proof|derive|analy[sz]e|optimi[sz]e|refactor)\b", re.I),
    re.compile(r"\b(explain|walk me through|step[- ]by[- ]step)\b", re.I),
    re.compile(r"[?].*[?]"),              # multi-question turn
)


def _complexity_score(live_text: str) -> float:
    """0.0 (trivial) → 1.0 (heavy). Crude but cheap — the router already
    handles real intent classification; this is just enough to bias the
    layer count when we're on the edge of spilling."""
    if not live_text:
        return 0.3
    length = min(1.0, len(live_text) / 2000.0)
    pattern_hits = sum(1 for p in _COMPLEXITY_PATTERNS if p.search(live_text))
    hit_score = min(1.0, pattern_hits / 3.0)
    return max(length, hit_score)


# ── Planner ─────────────────────────────────────────────────────────────────

@dataclass
class ResidencyPolicy:
    """Knobs that gate when the planner steps down from FULL."""

    # If free VRAM exceeds (tier_cost * this factor), prefer FULL. At
    # 1.0 we always go FULL when there's exactly enough room; at 1.15 we
    # insist on 15% slack so the KV cache can grow.
    full_headroom_multiplier: float = 1.15
    # Minimum fraction of layers we'll keep on the GPU in PARTIAL mode.
    # Below this the speedup is negative (CPU bottleneck dominates), so
    # MINIMAL just sets a floor without pretending to still be "partial".
    partial_min_ratio: float = 0.35
    minimal_ratio: float = 0.15
    # When the caller marks a request as low-complexity, we're willing
    # to drop this many extra percentage points of layers.
    low_complexity_savings: float = 0.15
    # When mmap is on we expect `num_gpu` fewer layers resident; the CPU
    # side serves the rest via page cache. Pinning those pages (mlock)
    # trades RAM residency for eliminated page faults.
    mlock_full_mode: bool = True
    mlock_partial_mode: bool = False


def plan_residency(
    tier: TierConfig,
    *,
    free_vram_gb: float,
    live_user_text: str = "",
    user_preference: ResidencyMode | None = None,
    policy: ResidencyPolicy | None = None,
) -> ResidencyPlan:
    """Pick a residency mode + layer count for a single tier.

    Parameters
    ----------
    tier : the tier we're about to load / serve.
    free_vram_gb : current free VRAM (from `VRAMScheduler.probe.free_gb`).
    live_user_text : the active user turn — used for complexity bias.
    user_preference : hard override (e.g. set by the frontend's per-chat
        panel). If provided, we honour it verbatim apart from enforcing
        the minimal-ratio floor so the model still runs.
    """
    pol = policy or ResidencyPolicy()
    total = _layer_hint(tier)
    cost = tier.vram_estimate_gb

    # Caller override wins — but we still compute a sane layer count.
    if user_preference is not None:
        return _plan_for_mode(tier, user_preference, total, cost, pol,
                              reason="user-pref")

    # If there's plenty of room and the user's turn looks involved, give
    # the whole model the GPU.
    complexity = _complexity_score(live_user_text)
    fits_full = free_vram_gb >= cost * pol.full_headroom_multiplier
    if fits_full and complexity >= 0.5:
        return _plan_for_mode(tier, ResidencyMode.FULL, total, cost, pol,
                              reason="headroom-ok+complex")
    if fits_full and tier.pinned:
        # Pinned tiers (e.g. vision on llama.cpp, which can't unload
        # without a restart) shouldn't be partially offloaded on a whim —
        # if the card fits them, keep them whole.
        return _plan_for_mode(tier, ResidencyMode.FULL, total, cost, pol,
                              reason="headroom-ok+pinned")
    if fits_full:
        # Fits, but the turn looks trivial — shave off a bit to leave VRAM
        # for other tiers to share the card without eviction.
        ratio = max(pol.partial_min_ratio,
                    1.0 - pol.low_complexity_savings)
        return _custom_plan(tier, ratio, total, cost, pol,
                            reason="headroom-ok+trivial")

    # Tight: pick the offload ratio that projects under free_vram.
    if cost <= 0 or free_vram_gb <= 0:
        return _plan_for_mode(tier, ResidencyMode.MINIMAL, total, cost, pol,
                              reason="unknown-cost")
    ratio = max(pol.minimal_ratio, min(1.0, free_vram_gb / cost))
    if ratio < pol.partial_min_ratio:
        return _plan_for_mode(tier, ResidencyMode.MINIMAL, total, cost, pol,
                              reason="pressure-high")
    if complexity < 0.3:
        ratio = max(pol.partial_min_ratio, ratio - pol.low_complexity_savings)
    return _custom_plan(tier, ratio, total, cost, pol,
                        reason="pressure-partial")


def _plan_for_mode(
    tier: TierConfig,
    mode: ResidencyMode,
    total: int,
    cost: float,
    pol: ResidencyPolicy,
    *,
    reason: str,
) -> ResidencyPlan:
    if mode == ResidencyMode.FULL:
        return ResidencyPlan(
            tier_id=tier.name,
            mode=mode,
            num_gpu_layers=total,
            total_layers=total,
            use_mmap=True,                       # mmap+mlock is cheap and avoids cold-load stalls
            use_mlock=pol.mlock_full_mode,
            reason=reason,
            projected_vram_gb=cost,
        )
    if mode == ResidencyMode.MINIMAL:
        layers = max(1, int(total * pol.minimal_ratio))
        return ResidencyPlan(
            tier_id=tier.name,
            mode=mode,
            num_gpu_layers=layers,
            total_layers=total,
            use_mmap=True,
            use_mlock=False,
            reason=reason,
            projected_vram_gb=cost * (layers / total),
        )
    # PARTIAL: pick mid-ratio as a sensible default
    ratio = max(pol.partial_min_ratio, 0.65)
    return _custom_plan(tier, ratio, total, cost, pol, reason=reason)


def _custom_plan(
    tier: TierConfig,
    ratio: float,
    total: int,
    cost: float,
    pol: ResidencyPolicy,
    *,
    reason: str,
) -> ResidencyPlan:
    ratio = max(pol.minimal_ratio, min(1.0, ratio))
    layers = max(1, int(round(total * ratio)))
    mode = (ResidencyMode.FULL if layers >= total
            else ResidencyMode.MINIMAL if ratio <= pol.partial_min_ratio + 1e-6
            else ResidencyMode.PARTIAL)
    return ResidencyPlan(
        tier_id=tier.name,
        mode=mode,
        num_gpu_layers=layers,
        total_layers=total,
        use_mmap=True,
        use_mlock=(pol.mlock_full_mode if mode == ResidencyMode.FULL
                   else pol.mlock_partial_mode),
        reason=reason,
        projected_vram_gb=cost * (layers / total),
    )


# ── Convenience: merge a plan into Ollama options ───────────────────────────

def merge_into_options(
    options: dict[str, Any],
    plan: ResidencyPlan,
) -> dict[str, Any]:
    """Non-destructive merge: caller-supplied options win over planner
    defaults (so an explicit `num_gpu` in `tier.params` isn't clobbered)."""
    merged = dict(plan.to_backend_options())
    merged.update(options or {})
    return merged
