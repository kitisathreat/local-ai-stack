"""Configuration loader — reads YAML files from /app/config/ (or a path
override from env) and exposes typed Pydantic models for the rest of the
backend to consume.

All YAML files are loaded once at startup. A `/api/reload` endpoint (added
later) will re-invoke `Config.load()` for hot-reloading router heuristics
without restarting the process.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


# Prefer explicit override; otherwise auto-detect relative to this file so
# native mode (no Docker, no LAI_CONFIG_DIR) finds config/ at the repo root.
CONFIG_DIR = Path(
    os.getenv("LAI_CONFIG_DIR")
    or (Path(__file__).parent.parent / "config")
)


class RopeScaling(BaseModel):
    """YaRN-style rope scaling, mapped to llama-server flags
    --rope-scaling/--rope-scale/--yarn-orig-ctx."""

    type: str = "yarn"
    factor: float = 1.0
    orig_ctx: int | None = None


class TierConfig(BaseModel):
    """One entry from config/models.yaml `tiers:`."""

    name: str
    description: str = ""
    backend: str = "llama_cpp"            # "llama_cpp" — only llama.cpp is supported
    endpoint: str | None = None           # derived from `port` when omitted
    model_tag: str = ""
    fallback_tag: str | None = None
    context_window: int
    think_default: bool = False
    think_supported: bool = True
    preserve_thinking: bool = False
    params: dict[str, Any] = Field(default_factory=dict)
    vram_estimate_gb: float = 0.0
    is_orchestrator: bool = False
    parallel_workers_max: int = 1
    pinned: bool = False
    mmproj_path: str | None = None
    chat_template_kwargs: dict[str, Any] = Field(default_factory=dict)
    reasoning_format: str | None = None
    # How many concurrent requests can share this loaded model. Drives
    # llama-server's --parallel and the scheduler's slot cap. The effective
    # per-request KV budget is context_window (passed as --ctx-size) and the
    # model's total KV allocation is roughly parallel_slots * context_window.
    parallel_slots: int = 1

    # llama.cpp process configuration. Each tier runs its own llama-server
    # subprocess on a dedicated port; the backend's VRAMScheduler manages the
    # lifecycle for non-pinned tiers (vision + embedding are pre-spawned by
    # the launcher and pinned).
    role: str = "chat"                    # "chat" | "embedding"
    # UI grouping label. When set, the chat UI's tier dropdown renders
    # this tier inside an <optgroup label="<category>"> so related
    # tiers cluster together. Recommended values: "Reasoning" for the
    # heavy-reasoning family (highest_quality / reasoning_max /
    # reasoning_xl / frontier), "Coding" for the coding tiers, etc.
    # Empty string means the tier renders at the top level outside any
    # optgroup.
    category: str = ""
    # When true, this tier's working set (weights + KV cache) is
    # expected to exceed system RAM, so cold expert pages are mmap-
    # spilled to NVMe at inference time. Implies a hard requirement
    # on `use_mmap=true, use_mlock=false`, plus the GGUF being on a
    # fast SSD/NVMe. Surfaced by:
    #   - startup diagnostics (warn if the models dir is on a slow disk)
    #   - the chat UI tier dropdown (badge tiers as "(NVMe)")
    #   - README ops table (so an operator sizes their hardware right)
    # Pure-informational today; a future PR can also add hard config
    # validation that refuses use_mlock=true when this is set.
    requires_nvme_spillover: bool = False
    gguf_path: str | None = None          # filled in from data/resolved-models.json
    port: int | None = None               # required at runtime
    n_gpu_layers: int = -1                # -1 = offload all
    flash_attention: bool = True
    cache_type_k: str = "q8_0"            # k/v KV cache quantization
    cache_type_v: str = "q8_0"
    rope_scaling: RopeScaling | None = None
    extra_args: list[str] = Field(default_factory=list)
    # Tensor-level offload override. When set, each pattern is passed to
    # llama-server as a separate `-ot <pattern>` flag. The canonical MoE
    # spillover pattern is ".ffn_.*_exps.=CPU" — keeps attention, KV
    # cache, embeddings, and non-expert MLP on the GPU; offloads only
    # the routed-expert tensors to system RAM. Use with n_gpu_layers: -1
    # (everything to GPU as the base, then -ot selectively pulls experts
    # back). Combine with flash_attention: true — FA stays correct
    # because every attention layer is GPU-resident.
    #
    # For dense models (no expert tensors), leave this empty and control
    # spillover via n_gpu_layers / use_mmap / use_mlock as before.
    override_tensors: list[str] = Field(default_factory=list)
    use_mmap: bool = True
    use_mlock: bool = False
    spawn_timeout_sec: int = 180
    # When true, llama-server is launched with --no-kv-offload, keeping
    # the entire KV cache in CPU RAM. Costs throughput (CPU↔GPU traffic
    # in attention) but frees a chunk of VRAM proportional to ctx-size.
    # The residency planner toggles this automatically as a fitting
    # cascade step before resorting to shrinking context_window.
    kv_offload: bool = False

    # ── Speculative decoding ───────────────────────────────────────────
    # When `draft_gguf_path` is set at runtime (resolved from
    # `draft_model_tag` via model-sources.yaml), build_argv emits
    # `-md / -ngld / --draft-max / --draft-min` flags so llama-server
    # runs speculative decoding against the draft model. The algorithm
    # is the standard Leviathan et al. 2023 rejection-sampling variant
    # implemented in llama.cpp — mathematically equivalent to sampling
    # from the target alone, so output quality is unchanged. Speedup
    # comes from amortizing memory bandwidth across `--draft-max`
    # parallel-evaluated tokens per target step.
    #
    # Tokenizer compatibility is REQUIRED: the draft and target must
    # share a tokenizer (same vocab + merges) or rejection sampling is
    # undefined. Qwen3-0.6B is the universal draft for the Qwen3 family.
    draft_model_tag: str | None = None
    draft_gguf_path: str | None = None
    draft_n_gpu_layers: int = -1
    draft_max: int = 8
    draft_min: int = 4

    # ── Per-tier model variants (e.g. coding 30B vs 80B) ───────────────
    # When a tier defines `variants:`, each entry overrides a small set
    # of fields (model_tag/gguf_path/vram/draft_*). At request time the
    # router can set a variant override (slash command); the loader
    # calls `resolve_variant(name)` to obtain a TierConfig copy with
    # the variant fields applied before spawning. The base TierConfig
    # itself acts as the fallback when no variant is requested AND
    # `default_variant` is None.
    variants: dict[str, "TierVariant"] = Field(default_factory=dict)
    default_variant: str | None = None

    def resolved_endpoint(self) -> str:
        """Return the OpenAI-compatible base URL for this tier.

        Prefers the explicit `endpoint` if set, otherwise composes one from
        `port`. Endpoints always include the /v1 suffix expected by the
        LlamaCppClient HTTP layer.
        """
        if self.endpoint:
            base = self.endpoint.rstrip("/")
            return base if base.endswith("/v1") else f"{base}/v1"
        if self.port is None:
            raise ValueError(f"tier {self.name!r} has no port or endpoint")
        return f"http://127.0.0.1:{self.port}/v1"

    def resolve_variant(self, variant: str | None) -> "TierConfig":
        """Return a TierConfig with the requested variant's overrides applied.

        Selection order:
          1. explicit `variant` arg
          2. `self.default_variant`
          3. fall through to `self` unchanged
        """
        chosen = variant or self.default_variant
        if not chosen or chosen not in self.variants:
            return self
        v = self.variants[chosen]
        updates: dict[str, Any] = {}
        for field in (
            "model_tag", "gguf_path", "vram_estimate_gb",
            "draft_model_tag", "draft_gguf_path", "draft_max", "draft_min",
            "context_window", "rope_scaling", "override_tensors",
        ):
            value = getattr(v, field, None)
            if value is not None and value != [] and value != {}:
                updates[field] = value
        return self.model_copy(update=updates)


class TierVariant(BaseModel):
    """A named override set for a TierConfig (e.g. coding tier's 30B vs 80B).

    All fields are optional — only the ones explicitly set are applied.
    Keep this surface narrow: variants exist to swap the model + its
    immediate runtime sizing, not to reconfigure the tier wholesale.

    `source` names an entry in model-sources.yaml whose resolved gguf_path
    is copied into this variant's gguf_path at config load time. Lets
    each variant point at its own GGUF without duplicating model_resolver
    plumbing.
    """

    # Display label used by the chat UI's variant sub-selector. Falls
    # back to model_tag / id when None.
    name: str | None = None
    source: str | None = None
    model_tag: str | None = None
    gguf_path: str | None = None
    vram_estimate_gb: float | None = None
    draft_model_tag: str | None = None
    draft_gguf_path: str | None = None
    draft_max: int | None = None
    draft_min: int | None = None
    context_window: int | None = None
    # YaRN / rope-scaling override. When the variant changes
    # context_window, the rope scaling factor must move with it
    # (factor = ctx / orig_ctx). Without this field a long-context
    # variant would use the parent tier's factor and produce garbled
    # output past the parent's ctx.
    rope_scaling: RopeScaling | None = None
    override_tensors: list[str] = Field(default_factory=list)


# Forward-ref resolution: TierConfig.variants references TierVariant.
TierConfig.model_rebuild()


class ModelsConfig(BaseModel):
    default: str
    tiers: dict[str, TierConfig]
    aliases: dict[str, str] = Field(default_factory=dict)

    def resolve(self, tier_or_alias: str) -> tuple[str, TierConfig]:
        """Resolve a tier name (or alias). Returns (canonical_name, tier)."""
        name = tier_or_alias
        if name.startswith("tier."):
            name = name[5:]
        name = self.aliases.get(name, name)
        if name not in self.tiers:
            raise KeyError(f"Unknown tier or alias: {tier_or_alias!r}")
        return name, self.tiers[name]


class SignalRule(BaseModel):
    regex: str | None = None
    keyword_count: dict[str, Any] | None = None
    min_question_marks: int | None = None
    estimated_output_tokens_gt: int | None = None


class AutoThinking(BaseModel):
    enable_when_any: list[SignalRule] = Field(default_factory=list)
    disable_when_any: list[SignalRule] = Field(default_factory=list)


class MultiAgentConfig(BaseModel):
    trigger_when_any: list[SignalRule] = Field(default_factory=list)
    max_workers: int = 3
    min_workers: int = 2
    worker_tier: str = "fast"
    worker_overrides: dict[str, Any] = Field(default_factory=dict)
    orchestrator_tier: str = "versatile"
    orchestrator_overrides: dict[str, Any] = Field(default_factory=dict)
    synthesis_overrides: dict[str, Any] = Field(default_factory=dict)
    specialist_routes: dict[str, str] = Field(default_factory=dict)
    # Whether parallel workers reason (think mode) on by default. Trades VRAM
    # and latency for output rigor; admins typically leave off and let users
    # opt in per-chat.
    reasoning_workers: bool = False
    # "independent": classic decompose → parallel → synthesize.
    # "collaborative": after the initial round, each worker sees the other
    # workers' drafts and refines its own answer for `interaction_rounds`
    # additional turns before synthesis. Higher rigor, higher cost.
    interaction_mode: str = "independent"
    interaction_rounds: int = 1


class RouterConfig(BaseModel):
    auto_thinking_signals: AutoThinking = Field(default_factory=AutoThinking)
    multi_agent: MultiAgentConfig = Field(default_factory=MultiAgentConfig)
    slash_commands: dict[str, dict[str, Any]] = Field(default_factory=dict)


class EvictionPolicy(BaseModel):
    policy: str = "lru"
    min_residency_sec: int = 30
    pin_orchestrator: bool = False
    pin_vision: bool = True
    # Proactive idle eviction. The sweeper drops any non-pinned, idle
    # (refcount == 0) tier whose `last_used` is older than this, even
    # when there is no VRAM pressure. Frees the GPU for other workloads
    # (other apps, headless tests, eventual second-user scenarios) and
    # forces a fresh observed-cost measurement on the next acquire,
    # which neutralises the "stale 18 GB observed clings forever in
    # process memory" failure mode. Set to 0 to disable proactive
    # eviction (legacy behaviour: only evict under VRAM pressure).
    idle_evict_after_sec: int = 1800       # 30 min

    # Single-chat-tier cap. When true, acquiring a new chat tier
    # evicts any other resident chat tier (refcount==0, non-pinned)
    # immediately. Pinned tiers (vision, embedding, reranker) are
    # always preserved. See vram.yaml for the operator-facing prose.
    single_tier_cap: bool = False


class VRAMMultiAgent(BaseModel):
    release_orchestrator_during_workers: bool = True
    synthesis_reload_timeout_sec: int = 60


def _default_observed_path() -> str:
    """Resolve ``data/vram_observed.json`` relative to the repo root,
    honouring ``LAI_DATA_DIR`` when set. The previous hardcoded
    ``/app/data/...`` default was a dead Docker-era path on Windows-
    native, so the EMA observed-cost cache silently never persisted."""
    import os as _os
    env = _os.getenv("LAI_DATA_DIR")
    base = Path(env) if env else (Path(__file__).resolve().parent.parent / "data")
    return str(base / "vram_observed.json")


class ObservedCosts(BaseModel):
    persist_path: str = Field(default_factory=_default_observed_path)
    learning_rate: float = 0.1


class QueueConfig(BaseModel):
    """Per-tier wait queue for requests beyond the current slot capacity."""

    max_depth_per_tier: int = 10
    max_wait_sec: int = 60
    position_update_interval_sec: int = 2


class KVCacheWeights(BaseModel):
    recency: float = 0.45
    relevance: float = 0.30
    role_prior: float = 0.15
    size_penalty: float = 0.10
    hot_window: int = 4


class KVCacheConfig(BaseModel):
    """KV-pressure manager (see backend/kv_cache_manager.py).

    The manager prunes low-importance context segments before dispatch
    when the live request would push a tier's KV allocation into RAM
    spillover. `spill_trigger_fraction` defines how early we act —
    below 1.0 so we never actually engage llama.cpp's page-fault path.
    """

    enable: bool = True
    spill_trigger_fraction: float = 0.92
    reserve_output_tokens: int = 512
    max_spill_entries_per_conv: int = 256
    weights: KVCacheWeights = Field(default_factory=KVCacheWeights)


class ResidencyPolicyConfig(BaseModel):
    """Per-model partial residency planner (backend/model_residency.py)."""

    enable: bool = True
    full_headroom_multiplier: float = 1.15
    partial_min_ratio: float = 0.35
    minimal_ratio: float = 0.15
    low_complexity_savings: float = 0.15
    mlock_full_mode: bool = True
    mlock_partial_mode: bool = False
    # Fitting cascade — applied after the layer-offload decision when
    # the resulting plan still doesn't fit free VRAM. Order:
    #   1. shrink GPU layers (existing PARTIAL/MINIMAL behaviour)
    #   2. flip --no-kv-offload (KV cache → CPU RAM)
    #   3. shrink context_window in half-steps until it fits or hits min
    # Both steps 2 and 3 are gated by these knobs so an operator can opt
    # out of either (e.g. if their RAM bandwidth makes KV-on-CPU too slow
    # in their workload, set enable_kv_offload=false to skip straight to
    # ctx shrink).
    enable_kv_offload: bool = True
    enable_ctx_shrink: bool = True
    min_context_window: int = 4096


class VRAMConfig(BaseModel):
    total_vram_gb: float
    headroom_gb: float = 1.5
    poll_interval_sec: int = 5
    eviction: EvictionPolicy = Field(default_factory=EvictionPolicy)
    multi_agent: VRAMMultiAgent = Field(default_factory=VRAMMultiAgent)
    observed_costs: ObservedCosts = Field(default_factory=ObservedCosts)
    queue: QueueConfig = Field(default_factory=QueueConfig)
    kv_cache: KVCacheConfig = Field(default_factory=KVCacheConfig)
    residency: ResidencyPolicyConfig = Field(default_factory=ResidencyPolicyConfig)


class SessionCookieConfig(BaseModel):
    cookie_name: str = "lai_session"
    cookie_ttl_days: int = 30
    cookie_secure: bool = True
    cookie_samesite: str = "lax"
    jwt_algorithm: str = "HS256"


class AuthRateLimits(BaseModel):
    # Password-login rate limits (replacing the old magic-link throttle).
    requests_per_hour_per_ip: int = 30
    # Chat-endpoint throttles applied per authenticated user (or per IP for
    # anonymous callers). 0 disables the window.
    requests_per_minute_per_user: int = 30
    requests_per_day_per_user: int = 500


class AuthConfig(BaseModel):
    session: SessionCookieConfig = Field(default_factory=SessionCookieConfig)
    allowed_email_domains: list[str] = Field(default_factory=list)
    rate_limits: AuthRateLimits = Field(default_factory=AuthRateLimits)


class ConcurrencyConfig(BaseModel):
    """Backend-wide concurrency knobs (Uvicorn workers, Redis coordination).

    `workers_target` is the desired Uvicorn worker count. The running count is
    set by the `BACKEND_WORKERS` env var at container start and therefore
    can't be hot-reloaded — saving this value rewrites runtime.yaml and is
    picked up on the next restart.
    """

    workers_target: int = 1
    redis_url: str | None = None


class AppConfig(BaseModel):
    models: ModelsConfig
    router: RouterConfig
    vram: VRAMConfig
    auth: AuthConfig = Field(default_factory=AuthConfig)
    concurrency: ConcurrencyConfig = Field(default_factory=ConcurrencyConfig)

    @classmethod
    def load(cls, config_dir: Path | None = None) -> "AppConfig":
        d = config_dir or CONFIG_DIR
        models_cfg = ModelsConfig(**_read_yaml(d / "models.yaml"))
        _apply_resolved_models(models_cfg)
        kwargs = dict(
            models=models_cfg,
            router=RouterConfig(**_read_yaml(d / "router.yaml")),
            vram=VRAMConfig(**_read_yaml(d / "vram.yaml")),
        )
        auth_path = d / "auth.yaml"
        if auth_path.exists():
            kwargs["auth"] = AuthConfig(**_read_yaml(auth_path))
        runtime_path = d / "runtime.yaml"
        if runtime_path.exists():
            kwargs["concurrency"] = ConcurrencyConfig(**_read_yaml(runtime_path))
        # Env overrides the YAML — it's what Uvicorn actually runs with.
        import os as _os
        env_workers = _os.getenv("BACKEND_WORKERS")
        env_redis = _os.getenv("REDIS_URL")
        if env_workers or env_redis:
            c = kwargs.get("concurrency") or ConcurrencyConfig()
            if env_workers:
                try:
                    c = c.model_copy(update={"workers_target": int(env_workers)})
                except ValueError:
                    pass
            if env_redis:
                c = c.model_copy(update={"redis_url": env_redis})
            kwargs["concurrency"] = c
        return cls(**kwargs)

    def compile_signals(self) -> "CompiledSignals":
        """Pre-compile all regexes so hot-path evaluation is cheap."""
        return CompiledSignals.build(self)


class CompiledSignals(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    enable_thinking: list[re.Pattern]
    disable_thinking: list[re.Pattern]
    multi_agent_triggers: list[re.Pattern]
    think_keyword_rules: list[dict[str, Any]]
    multi_agent_question_mark_min: int | None
    multi_agent_token_gt: int | None

    @classmethod
    def build(cls, cfg: AppConfig) -> "CompiledSignals":
        def _compile(rules: list[SignalRule]) -> list[re.Pattern]:
            return [re.compile(r.regex, re.IGNORECASE) for r in rules if r.regex]

        keyword_rules = [
            r.keyword_count for r in cfg.router.auto_thinking_signals.enable_when_any
            if r.keyword_count
        ]

        qm = next(
            (r.min_question_marks for r in cfg.router.multi_agent.trigger_when_any
             if r.min_question_marks is not None),
            None,
        )
        tok = next(
            (r.estimated_output_tokens_gt for r in cfg.router.multi_agent.trigger_when_any
             if r.estimated_output_tokens_gt is not None),
            None,
        )

        return cls(
            enable_thinking=_compile(cfg.router.auto_thinking_signals.enable_when_any),
            disable_thinking=_compile(cfg.router.auto_thinking_signals.disable_when_any),
            multi_agent_triggers=_compile(cfg.router.multi_agent.trigger_when_any),
            think_keyword_rules=keyword_rules,
            multi_agent_question_mark_min=qm,
            multi_agent_token_gt=tok,
        )


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _apply_resolved_models(models_cfg: "ModelsConfig") -> None:
    """Overlay per-tier `gguf_path` from data/resolved-models.json.

    The model_resolver writes this file at -Setup time; it carries the actual
    on-disk paths for downloaded GGUFs. When present, it overrides the static
    defaults in models.yaml so the launcher can rename/relocate files without
    editing configs.
    """
    import json

    data_dir = Path(os.getenv("LAI_DATA_DIR") or (Path(__file__).parent.parent / "data"))
    manifest = data_dir / "resolved-models.json"
    if not manifest.exists():
        return
    try:
        doc = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    tiers = (doc or {}).get("tiers") or {}
    for tier_name, info in tiers.items():
        tier = models_cfg.tiers.get(tier_name)
        if not tier:
            continue
        gguf = (info or {}).get("gguf_path")
        if gguf:
            tier.gguf_path = gguf
        mmproj = (info or {}).get("mmproj_path")
        if mmproj:
            tier.mmproj_path = mmproj

    # Speculative-decode drafts are resolved as ordinary entries in
    # model-sources.yaml (e.g. `draft_qwen3_06b`). For every chat tier
    # that declares `draft_model_tag`, look up that name in the resolved
    # manifest and copy its gguf_path into the tier's `draft_gguf_path`.
    # Done here rather than in the resolver so a missing draft (offline,
    # not-yet-downloaded) doesn't block tier setup — build_argv only
    # emits -md when the path is set, so absent drafts cleanly disable
    # spec decode without breaking startup.
    for tier in models_cfg.tiers.values():
        if not tier.draft_model_tag or tier.draft_gguf_path:
            continue
        draft_info = tiers.get(tier.draft_model_tag) or {}
        path = draft_info.get("gguf_path")
        if path:
            tier.draft_gguf_path = path

    # Variant gguf_paths: each TierVariant.source names a model-sources
    # entry; we copy that entry's resolved gguf_path into the variant.
    # Variants without a `source` keep whatever gguf_path was declared in
    # YAML (typically empty for the default variant — it inherits from
    # the parent tier).
    for tier in models_cfg.tiers.values():
        for variant in tier.variants.values():
            if not variant.source or variant.gguf_path:
                continue
            src_info = tiers.get(variant.source) or {}
            path = src_info.get("gguf_path")
            if path:
                variant.gguf_path = path


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    return AppConfig.load()
