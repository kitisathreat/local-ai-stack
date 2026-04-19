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
    worker_tier: str = "fast"
    worker_overrides: dict[str, Any] = Field(default_factory=dict)
    orchestrator_tier: str = "versatile"
    orchestrator_overrides: dict[str, Any] = Field(default_factory=dict)
    synthesis_overrides: dict[str, Any] = Field(default_factory=dict)
    specialist_routes: dict[str, str] = Field(default_factory=dict)


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


class VRAMConfig(BaseModel):
    total_vram_gb: float
    headroom_gb: float = 1.5
    poll_interval_sec: int = 5
    eviction: EvictionPolicy = Field(default_factory=EvictionPolicy)
    ollama: OllamaKeepAlive = Field(default_factory=OllamaKeepAlive)
    multi_agent: VRAMMultiAgent = Field(default_factory=VRAMMultiAgent)
    observed_costs: ObservedCosts = Field(default_factory=ObservedCosts)


class AppConfig(BaseModel):
    models: ModelsConfig
    router: RouterConfig
    vram: VRAMConfig

    @classmethod
    def load(cls, config_dir: Path | None = None) -> "AppConfig":
        d = config_dir or CONFIG_DIR
        return cls(
            models=ModelsConfig(**_read_yaml(d / "models.yaml")),
            router=RouterConfig(**_read_yaml(d / "router.yaml")),
            vram=VRAMConfig(**_read_yaml(d / "vram.yaml")),
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
