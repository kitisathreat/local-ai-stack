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


CONFIG_DIR = Path(os.getenv("LAI_CONFIG_DIR", "/app/config"))


class TierConfig(BaseModel):
    """One entry from config/models.yaml `tiers:`."""

    name: str
    description: str = ""
    backend: str                          # "ollama" | "llama_cpp"
    endpoint: str
    model_tag: str
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
    # Ollama's num_parallel and the scheduler's slot cap. The effective
    # per-request KV budget is context_window (passed as num_ctx) and the
    # model's total KV allocation is roughly parallel_slots * num_ctx.
    parallel_slots: int = 1
    # Primary host name (key into HostsConfig.hosts). When None, the loader
    # fills this in by synthesising a host from the legacy `backend` +
    # `endpoint` fields so pre-multi-host configs keep working.
    host: str | None = None
    # Ordered list of alternate host names the dispatcher should try before
    # falling back to the legacy client.
    host_fallbacks: list[str] = Field(default_factory=list)
    # Per-tier opt-out from the always-on legacy (OLLAMA_URL/LLAMACPP_URL)
    # clients. Useful for tiers that only make sense on a specific cloud GPU.
    allow_legacy_fallback: bool = True


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


class OllamaKeepAlive(BaseModel):
    keep_alive_default: str = "30m"
    keep_alive_pinned: int = -1


class VRAMMultiAgent(BaseModel):
    release_orchestrator_during_workers: bool = True
    synthesis_reload_timeout_sec: int = 60


class ObservedCosts(BaseModel):
    persist_path: str = "/app/data/vram_observed.json"
    learning_rate: float = 0.1


class QueueConfig(BaseModel):
    """Per-tier wait queue for requests beyond the current slot capacity."""

    max_depth_per_tier: int = 10
    max_wait_sec: int = 60
    position_update_interval_sec: int = 2


class VRAMConfig(BaseModel):
    total_vram_gb: float
    headroom_gb: float = 1.5
    poll_interval_sec: int = 5
    eviction: EvictionPolicy = Field(default_factory=EvictionPolicy)
    ollama: OllamaKeepAlive = Field(default_factory=OllamaKeepAlive)
    multi_agent: VRAMMultiAgent = Field(default_factory=VRAMMultiAgent)
    observed_costs: ObservedCosts = Field(default_factory=ObservedCosts)
    queue: QueueConfig = Field(default_factory=QueueConfig)


class MagicLinkConfig(BaseModel):
    expiry_minutes: int = 15
    email_subject: str = "Sign in to Local AI Stack"
    email_from: str = "noreply@localaistack.local"
    email_body_template: str = ""


class SessionCookieConfig(BaseModel):
    cookie_name: str = "lai_session"
    cookie_ttl_days: int = 30
    cookie_secure: bool = True
    cookie_samesite: str = "lax"
    jwt_algorithm: str = "HS256"


class AuthRateLimits(BaseModel):
    requests_per_hour_per_email: int = 5
    requests_per_hour_per_ip: int = 30
    # Chat-endpoint throttles applied per authenticated user (or per IP for
    # anonymous callers). 0 disables the window.
    requests_per_minute_per_user: int = 30
    requests_per_day_per_user: int = 500


class AuthConfig(BaseModel):
    magic_link: MagicLinkConfig = Field(default_factory=MagicLinkConfig)
    session: SessionCookieConfig = Field(default_factory=SessionCookieConfig)
    allowed_email_domains: list[str] = Field(default_factory=list)
    rate_limits: AuthRateLimits = Field(default_factory=AuthRateLimits)


class FailoverConfig(BaseModel):
    """Circuit-breaker + legacy-fallback policy for the TierDispatcher."""

    open_after: int = 3                   # consecutive failures before circuit opens
    half_open_probe_sec: float = 30.0     # re-probe cadence while open
    legacy_fallback_enabled: bool = True  # global kill-switch for legacy clients


class HostConfig(BaseModel):
    """One entry under config/hosts.yaml `hosts:`.

    A host describes *where* to reach an inference backend. It is decoupled
    from which tier runs there — tiers reference hosts by name.
    """

    kind: str                             # "ollama" | "llama_cpp" | "openai"
    url: str
    location: str = "local"               # "local" | "remote"
    # VRAM bookkeeping. For location=local this is the physical GPU size
    # (pynvml reports the truth and corrects). For location=remote we trust
    # this value — remote VRAM isn't observable.
    total_vram_gb: float = 0.0
    headroom_gb: float = 1.5
    # Hosts that share a physical accelerator and therefore must share an
    # eviction pool. Names reference other entries in `hosts:`.
    shared_vram_with: list[str] = Field(default_factory=list)
    # Optional bearer token. The backend reads this env var at startup and
    # resolve-time; if unset, no Authorization header is attached.
    auth_env: str | None = None
    verify_tls: bool = True
    connect_timeout_sec: float = 10.0
    request_timeout_sec: float = 300.0
    enabled: bool = True


class HostsConfig(BaseModel):
    default_local_host: str = "local-ollama"
    failover: FailoverConfig = Field(default_factory=FailoverConfig)
    hosts: dict[str, HostConfig] = Field(default_factory=dict)


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
    hosts: HostsConfig = Field(default_factory=HostsConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    concurrency: ConcurrencyConfig = Field(default_factory=ConcurrencyConfig)

    @classmethod
    def load(cls, config_dir: Path | None = None) -> "AppConfig":
        d = config_dir or CONFIG_DIR
        kwargs = dict(
            models=ModelsConfig(**_read_yaml(d / "models.yaml")),
            router=RouterConfig(**_read_yaml(d / "router.yaml")),
            vram=VRAMConfig(**_read_yaml(d / "vram.yaml")),
        )
        auth_path = d / "auth.yaml"
        if auth_path.exists():
            kwargs["auth"] = AuthConfig(**_read_yaml(auth_path))
        runtime_path = d / "runtime.yaml"
        if runtime_path.exists():
            kwargs["concurrency"] = ConcurrencyConfig(**_read_yaml(runtime_path))
        hosts_path = d / "hosts.yaml"
        if hosts_path.exists():
            kwargs["hosts"] = HostsConfig(**_read_yaml(hosts_path))
        else:
            kwargs["hosts"] = HostsConfig()
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
        cfg = cls(**kwargs)
        cfg._reconcile_hosts()
        return cfg

    def _reconcile_hosts(self) -> None:
        """Ensure every tier has a resolvable `host`.

        For backward compat: when `config/hosts.yaml` is absent or a tier
        omits the new `host` field, we synthesise hosts from the legacy
        `tier.backend` + `tier.endpoint` pair. This keeps pre-multi-host
        deployments working with zero config changes.
        """
        import os as _os

        # Populate an auto-synth host for each unique (backend, endpoint)
        # still missing from the hosts registry.
        synth_by_url: dict[str, str] = {
            cfg.url.rstrip("/"): name
            for name, cfg in self.hosts.hosts.items()
        }
        vram_total = self.vram.total_vram_gb
        vram_head = self.vram.headroom_gb
        for tier_name, tier in self.models.tiers.items():
            if tier.host and tier.host in self.hosts.hosts:
                continue
            # First, try to match by URL.
            ep = (tier.endpoint or "").rstrip("/")
            matched = synth_by_url.get(ep)
            if matched is None:
                # Synthesise. Prefer a stable name so it's visible in /admin/hosts.
                matched = f"auto-{tier.backend}"
                i = 2
                while matched in self.hosts.hosts:
                    matched = f"auto-{tier.backend}-{i}"
                    i += 1
                self.hosts.hosts[matched] = HostConfig(
                    kind=tier.backend,
                    url=tier.endpoint,
                    location="local",
                    total_vram_gb=vram_total,
                    headroom_gb=vram_head,
                    enabled=True,
                )
                synth_by_url[ep] = matched
            tier.host = matched

        # Always ensure the two always-on legacy hosts exist in the registry
        # (even when they don't correspond to any tier) so the dispatcher can
        # fall back to them on request failure.
        legacy_pairs = [
            ("__legacy_ollama__", "ollama",
             _os.getenv("OLLAMA_URL", "http://ollama:11434")),
            ("__legacy_llama_cpp__", "llama_cpp",
             _os.getenv("LLAMACPP_URL", "http://llama-server:8001/v1")),
        ]
        for name, kind, url in legacy_pairs:
            if name not in self.hosts.hosts:
                self.hosts.hosts[name] = HostConfig(
                    kind=kind,
                    url=url,
                    location="local",
                    total_vram_gb=vram_total,
                    headroom_gb=vram_head,
                    enabled=True,
                )

        # Validate host_fallbacks references after synthesis is done.
        for tier_name, tier in self.models.tiers.items():
            if tier.host not in self.hosts.hosts:
                raise ValueError(
                    f"Tier {tier_name!r} references host {tier.host!r} "
                    "which is not defined in hosts.yaml"
                )
            for fb in tier.host_fallbacks:
                if fb not in self.hosts.hosts:
                    raise ValueError(
                        f"Tier {tier_name!r} host_fallback {fb!r} "
                        "is not defined in hosts.yaml"
                    )

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


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    return AppConfig.load()
