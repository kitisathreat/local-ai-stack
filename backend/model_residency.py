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
    "qwen3-coder-next-80b-a3b": 64,      # 80B-A3B coder, MoE w/ standard attention
    "gpt-oss-120b": 80,                  # 117 B / 5.1 B active MoE, standard attention
    "qwen3-0.6b": 28,                    # The universal speculative-decode draft
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
    # Cascade outputs — only populated when the layer-offload plan
    # alone wasn't enough to fit. See `_tighten_for_fit`.
    kv_offload: bool = False             # llama-server --no-kv-offload (KV → CPU RAM)
    context_window: int | None = None    # override for tier.context_window when shrunk

    def to_backend_options(self) -> dict[str, Any]:
        """Spawn-time argv contributions consumed by
        ``backends/llama_cpp.py`` when launching a per-tier llama-server."""
        opts: dict[str, Any] = {
            "n_gpu_layers": self.num_gpu_layers,
            "use_mmap": self.use_mmap,
            "use_mlock": self.use_mlock,
            "kv_offload": self.kv_offload,
        }
        if self.context_window is not None:
            opts["context_window"] = self.context_window
        return opts


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
    # Fitting cascade — applied AFTER the layer-offload decision when
    # the resulting plan still doesn't fit free VRAM.
    enable_kv_offload: bool = True
    enable_ctx_shrink: bool = True
    min_context_window: int = 4096


# Per-element bytes for the supported KV cache quantizations. q4/q5/q8
# include a small per-block scale + zero-point so they're slightly
# heavier than the raw type bits / 8 would suggest.
_KV_BYTES_PER_ELEM = {
    "q4_0": 0.625, "q4_1": 0.75, "q5_0": 0.75, "q5_1": 0.875,
    "q8_0": 1.125, "f16": 2.0, "f32": 4.0, "bf16": 2.0,
}


def _kv_per_token_gb(tier: TierConfig) -> float:
    """Rough VRAM-per-token-of-KV-cache estimate, intentionally a small
    overestimate for plan-cascade ordering. Capped by `_projected_kv_gb`
    against the tier's reported vram_estimate so hybrid-attention models
    (where most layers don't carry KV) don't get a wildly wrong figure."""
    bytes_k = _KV_BYTES_PER_ELEM.get((tier.cache_type_k or "f16").lower(), 2.0)
    bytes_v = _KV_BYTES_PER_ELEM.get((tier.cache_type_v or "f16").lower(), 2.0)
    layers = _layer_hint(tier)
    # Generic dense Qwen3-class shape — 32 attn heads × 128 head_dim.
    # Hybrid Qwen3-Next would be much smaller; the cap below corrects.
    elements_per_layer = 32 * 128
    bytes_per_token = layers * elements_per_layer * (bytes_k + bytes_v)
    return bytes_per_token / (1024 ** 3)


def _projected_kv_gb(tier: TierConfig, ctx: int) -> float:
    """Estimate VRAM held by the KV cache at `ctx` tokens, capped by the
    tier's reported full-load vram_estimate.

    Why the cap: the dense-model heuristic in `_kv_per_token_gb`
    assumes every layer carries KV, which over-counts hybrid-attention
    families (Qwen3-Next has ~25% full-attn layers, the rest are GDN
    / Mamba which don't allocate KV). The YAML's vram_estimate at
    native context already encodes the real shape; treating it as a
    ceiling for KV-at-native-ctx prevents the cascade from making a
    wrong "won't fit" call on those models.
    """
    naive = ctx * _kv_per_token_gb(tier) * max(1, tier.parallel_slots)
    native = tier.context_window or 0
    if native <= 0 or tier.vram_estimate_gb <= 0:
        return naive
    # At native ctx, assume KV is at most ~80% of the tier's reported
    # vram_estimate (the rest is weights + activations + embeddings +
    # overhead). Scale linearly with ctx from that anchor.
    ceiling_at_native = tier.vram_estimate_gb * 0.8
    scaled = ceiling_at_native * (ctx / native)
    return min(naive, scaled)


def _tighten_for_fit(
    plan: ResidencyPlan,
    tier: TierConfig,
    free_vram_gb: float,
    pol: ResidencyPolicy,
) -> ResidencyPlan:
    """Apply the cascade: KV→CPU first, ctx shrink last.

    The user policy is "shrink ctx by offloading as much as possible to
    system RAM without degrading performance, then shrink overall ctx".
    Layer offload is already done by the caller (it's the lowest-impact
    step on throughput for MoE / partial-attention models). KV→CPU is
    next: a measurable but tolerable hit to attention bandwidth that
    typically frees several GB. Only when that's still not enough do we
    shrink the context window — and we shrink in halving steps so we
    don't gratuitously cut the user's working memory in half when 30%
    would have done.
    """
    ctx = plan.context_window or tier.context_window
    layer_vram = plan.projected_vram_gb
    kv_vram = _projected_kv_gb(tier, ctx)
    total = layer_vram + kv_vram

    # Already fits with everything on GPU at full ctx? Done.
    if total <= free_vram_gb:
        plan.projected_vram_gb = total
        logger.info(
            "residency: %s fits at full ctx %d (need %.2fG, free %.2fG, mode=%s)",
            tier.name, ctx, total, free_vram_gb, plan.mode.value,
        )
        return plan

    reasons: list[str] = [plan.reason] if plan.reason else []

    # Step 2: KV → CPU.
    if pol.enable_kv_offload:
        plan.kv_offload = True
        logger.info(
            "residency: %s KV→CPU (layer=%.2fG + kv=%.2fG > free=%.2fG)",
            tier.name, layer_vram, kv_vram, free_vram_gb,
        )
        kv_vram = 0.0
        reasons.append("kv→cpu")
        total = layer_vram + kv_vram
        if total <= free_vram_gb:
            plan.projected_vram_gb = total
            plan.reason = "+".join(reasons)
            return plan

    # Step 3: shrink ctx in halving steps until it fits.
    if pol.enable_ctx_shrink:
        candidate = ctx
        while candidate > pol.min_context_window:
            candidate = max(pol.min_context_window, candidate // 2)
            kv_vram = (
                0.0 if plan.kv_offload
                else _projected_kv_gb(tier, candidate)
            )
            if layer_vram + kv_vram <= free_vram_gb:
                plan.context_window = candidate
                plan.projected_vram_gb = layer_vram + kv_vram
                reasons.append(f"ctx→{candidate}")
                plan.reason = "+".join(reasons)
                logger.info(
                    "residency: %s ctx shrunk %d→%d (layer=%.2fG + kv=%.2fG ≤ free=%.2fG)",
                    tier.name, ctx, candidate, layer_vram, kv_vram, free_vram_gb,
                )
                return plan
            if candidate <= pol.min_context_window:
                break
        # Fell out at the floor — record the smallest ctx and live with it.
        plan.context_window = pol.min_context_window
        plan.projected_vram_gb = (
            layer_vram + (
                0.0 if plan.kv_offload
                else _projected_kv_gb(tier, pol.min_context_window)
            )
        )
        reasons.append(f"ctx→{pol.min_context_window}(floor)")
        logger.warning(
            "residency: %s hit ctx floor %d — projected %.2fG, free %.2fG",
            tier.name, pol.min_context_window, plan.projected_vram_gb, free_vram_gb,
        )

    plan.reason = "+".join(reasons) if reasons else plan.reason
    return plan


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
        plan = _plan_for_mode(tier, user_preference, total, cost, pol,
                              reason="user-pref")
        return _tighten_for_fit(plan, tier, free_vram_gb, pol)

    # If there's plenty of room and the user's turn looks involved, give
    # the whole model the GPU.
    complexity = _complexity_score(live_user_text)
    fits_full = free_vram_gb >= cost * pol.full_headroom_multiplier
    if fits_full and complexity >= 0.5:
        plan = _plan_for_mode(tier, ResidencyMode.FULL, total, cost, pol,
                              reason="headroom-ok+complex")
    elif fits_full and tier.pinned:
        # Pinned tiers (e.g. vision on llama.cpp, which can't unload
        # without a restart) shouldn't be partially offloaded on a whim —
        # if the card fits them, keep them whole.
        plan = _plan_for_mode(tier, ResidencyMode.FULL, total, cost, pol,
                              reason="headroom-ok+pinned")
    elif fits_full:
        # Fits, but the turn looks trivial — shave off a bit to leave VRAM
        # for other tiers to share the card without eviction.
        ratio = max(pol.partial_min_ratio,
                    1.0 - pol.low_complexity_savings)
        plan = _custom_plan(tier, ratio, total, cost, pol,
                            reason="headroom-ok+trivial")
    elif cost <= 0 or free_vram_gb <= 0:
        plan = _plan_for_mode(tier, ResidencyMode.MINIMAL, total, cost, pol,
                              reason="unknown-cost")
    else:
        # Tight: pick the offload ratio that projects under free_vram.
        ratio = max(pol.minimal_ratio, min(1.0, free_vram_gb / cost))
        if ratio < pol.partial_min_ratio:
            plan = _plan_for_mode(tier, ResidencyMode.MINIMAL, total, cost, pol,
                                  reason="pressure-high")
        else:
            if complexity < 0.3:
                ratio = max(pol.partial_min_ratio,
                            ratio - pol.low_complexity_savings)
            plan = _custom_plan(tier, ratio, total, cost, pol,
                                reason="pressure-partial")

    # Final cascade: KV→CPU first, then ctx-shrink. The layer-offload
    # decision above is the cheapest VRAM lever for MoE / partial-attn
    # models; the cascade here covers what's left when it isn't enough.
    return _tighten_for_fit(plan, tier, free_vram_gb, pol)


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
