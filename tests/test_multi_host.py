"""Tests for multi-backend host support: HostConfig schema, client factory,
and TierDispatcher failover with legacy fallback."""
from __future__ import annotations

import asyncio
import os

import httpx
import pytest
import yaml

from backend.backends import client_for
from backend.backends.llama_cpp import LlamaCppClient
from backend.backends.ollama import OllamaClient
from backend.backends.openai import OpenAIClient
from backend.config import (
    AppConfig,
    FailoverConfig,
    HostConfig,
    HostsConfig,
    TierConfig,
)
from backend.dispatch import (
    LEGACY_LLAMA_CPP,
    LEGACY_OLLAMA,
    AllHostsUnavailable,
    TierDispatcher,
)


# ── Schema ──────────────────────────────────────────────────────────────────

def test_host_config_defaults():
    h = HostConfig(kind="ollama", url="http://example:11434")
    assert h.location == "local"
    assert h.verify_tls is True
    assert h.enabled is True
    assert h.auth_env is None


def test_host_config_remote_with_auth():
    h = HostConfig(
        kind="openai",
        url="https://proxy.example.com/v1",
        location="remote",
        total_vram_gb=999,
        auth_env="MY_TOKEN",
        verify_tls=False,
        connect_timeout_sec=15,
        request_timeout_sec=600,
    )
    assert h.kind == "openai"
    assert h.location == "remote"
    assert h.auth_env == "MY_TOKEN"
    assert h.verify_tls is False


def test_failover_config_defaults():
    f = FailoverConfig()
    assert f.open_after == 3
    assert f.half_open_probe_sec == 30
    assert f.legacy_fallback_enabled is True


# ── Client factory ──────────────────────────────────────────────────────────

def test_client_for_returns_correct_class():
    assert isinstance(
        client_for(HostConfig(kind="ollama", url="http://x:1")),
        OllamaClient,
    )
    assert isinstance(
        client_for(HostConfig(kind="llama_cpp", url="http://x:2/v1")),
        LlamaCppClient,
    )
    # OpenAIClient subclasses LlamaCppClient; assert the concrete type.
    c = client_for(HostConfig(kind="openai", url="http://x:3/v1"))
    assert isinstance(c, OpenAIClient)
    assert type(c) is OpenAIClient


def test_client_for_unknown_kind_raises():
    with pytest.raises(ValueError):
        client_for(HostConfig(kind="mystery", url="http://x"))


def test_client_for_injects_bearer_from_env(monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "sk-abc-123")
    client = client_for(HostConfig(
        kind="ollama",
        url="https://remote.example.com",
        auth_env="MY_TOKEN",
    ))
    headers = client._session_kwargs.get("headers") or {}
    assert headers.get("Authorization") == "Bearer sk-abc-123"


def test_client_for_no_auth_env_means_no_header(monkeypatch):
    monkeypatch.delenv("MY_TOKEN", raising=False)
    client = client_for(HostConfig(
        kind="ollama",
        url="https://remote.example.com",
        auth_env="MY_TOKEN",   # declared but env unset → None token, no header
    ))
    headers = client._session_kwargs.get("headers") or {}
    assert "Authorization" not in headers


def test_client_for_verify_tls_flows_through():
    client = client_for(HostConfig(
        kind="ollama",
        url="https://x",
        verify_tls=False,
    ))
    assert client._session_kwargs["verify"] is False


# ── Dispatcher: candidate ordering + legacy floor ──────────────────────────

def _make_cfg(
    *,
    tier_host: str = "colab",
    fallbacks: list[str] | None = None,
    allow_legacy: bool = True,
    legacy_enabled: bool = True,
    enabled_hosts: list[str] | None = None,
) -> AppConfig:
    """Tiny config factory for dispatcher tests."""
    hosts = {
        "colab": HostConfig(kind="ollama", url="http://colab", location="remote"),
        "aws":   HostConfig(kind="ollama", url="http://aws", location="remote"),
        LEGACY_OLLAMA: HostConfig(kind="ollama", url="http://legacy-ollama"),
        LEGACY_LLAMA_CPP: HostConfig(kind="llama_cpp", url="http://legacy-llama/v1"),
    }
    if enabled_hosts is not None:
        for name, cfg in hosts.items():
            cfg.enabled = name in enabled_hosts

    tier = TierConfig(
        name="Test",
        backend="ollama",
        endpoint="http://colab",
        host=tier_host,
        host_fallbacks=fallbacks or [],
        allow_legacy_fallback=allow_legacy,
        model_tag="x",
        context_window=4096,
    )
    from backend.config import ModelsConfig, RouterConfig, VRAMConfig

    return AppConfig(
        models=ModelsConfig(default="t", tiers={"t": tier}),
        router=RouterConfig(),
        vram=VRAMConfig(total_vram_gb=24),
        hosts=HostsConfig(
            default_local_host="colab",
            failover=FailoverConfig(legacy_fallback_enabled=legacy_enabled, open_after=2),
            hosts=hosts,
        ),
    )


def _dispatcher(cfg: AppConfig) -> TierDispatcher:
    # Use sentinel string clients to make assertions clearer.
    clients = {name: f"client:{name}" for name in cfg.hosts.hosts if cfg.hosts.hosts[name].enabled}
    return TierDispatcher(cfg, clients)  # type: ignore[arg-type]


def test_dispatcher_candidate_order_primary_then_fallbacks_then_legacy():
    cfg = _make_cfg(fallbacks=["aws"])
    d = _dispatcher(cfg)
    tier = next(iter(cfg.models.tiers.values()))
    assert d.candidates_for(tier) == ["colab", "aws", LEGACY_OLLAMA]


def test_dispatcher_respects_allow_legacy_fallback_false():
    cfg = _make_cfg(fallbacks=["aws"], allow_legacy=False)
    d = _dispatcher(cfg)
    tier = next(iter(cfg.models.tiers.values()))
    assert d.candidates_for(tier) == ["colab", "aws"]


def test_dispatcher_respects_global_legacy_kill_switch():
    cfg = _make_cfg(fallbacks=["aws"], legacy_enabled=False)
    d = _dispatcher(cfg)
    tier = next(iter(cfg.models.tiers.values()))
    assert d.candidates_for(tier) == ["colab", "aws"]


def test_dispatcher_skips_disabled_hosts():
    cfg = _make_cfg(
        fallbacks=["aws"],
        enabled_hosts=["aws", LEGACY_OLLAMA, LEGACY_LLAMA_CPP],  # colab disabled
    )
    d = _dispatcher(cfg)
    tier = next(iter(cfg.models.tiers.values()))
    assert d.candidates_for(tier) == ["aws", LEGACY_OLLAMA]


def test_dispatcher_picks_first_closed_circuit():
    cfg = _make_cfg(fallbacks=["aws"])
    d = _dispatcher(cfg)
    tier = next(iter(cfg.models.tiers.values()))

    # Trip colab's circuit (open_after=2 in _make_cfg).
    d.record_failure("colab", "boom")
    d.record_failure("colab", "boom")
    assert d.health["colab"].is_open
    choice = d.client_for_tier(tier)
    assert choice.host_name == "aws"


def test_dispatcher_half_open_ready_after_timeout(monkeypatch):
    cfg = _make_cfg(fallbacks=["aws"])
    d = _dispatcher(cfg)
    tier = next(iter(cfg.models.tiers.values()))

    d.record_failure("colab", "err1")
    d.record_failure("colab", "err2")
    # Fast-forward time past half_open_probe_sec (30).
    import time as _t
    monkeypatch.setattr(_t, "monotonic", lambda: d.health["colab"].opened_at + 31)
    # The dispatcher module took a local ref — patch there too.
    import backend.dispatch as disp_mod
    monkeypatch.setattr(disp_mod.time, "monotonic", lambda: d.health["colab"].opened_at + 31)

    choice = d.client_for_tier(tier)
    assert choice.host_name == "colab"    # half-open probe back to primary


def test_dispatcher_legacy_is_last_resort():
    """When every registered host is dead, legacy still answers."""
    cfg = _make_cfg(fallbacks=["aws"])
    d = _dispatcher(cfg)
    tier = next(iter(cfg.models.tiers.values()))

    for host in ("colab", "aws"):
        d.record_failure(host, "fail")
        d.record_failure(host, "fail")
    choice = d.client_for_tier(tier)
    assert choice.host_name == LEGACY_OLLAMA
    assert choice.client == "client:__legacy_ollama__"


def test_dispatcher_raises_when_no_candidates():
    cfg = _make_cfg(fallbacks=[], allow_legacy=False)
    # Disable even the primary
    cfg.hosts.hosts["colab"].enabled = False
    d = _dispatcher(cfg)
    tier = next(iter(cfg.models.tiers.values()))
    with pytest.raises(AllHostsUnavailable):
        d.client_for_tier(tier)


# ── execute(): same-request failover ────────────────────────────────────────

def test_execute_rotates_on_connect_error():
    cfg = _make_cfg(fallbacks=["aws"])
    d = _dispatcher(cfg)
    tier = next(iter(cfg.models.tiers.values()))

    call_log: list[str] = []

    async def fake_fn(client):
        call_log.append(client)
        # Every host fails so we can verify full rotation through to legacy.
        raise httpx.ConnectError("nope")

    async def _run():
        with pytest.raises(AllHostsUnavailable):
            await d.execute(tier, fake_fn)

    asyncio.run(_run())
    # Tried colab → aws → legacy-ollama before giving up.
    assert call_log == [
        "client:colab",
        "client:aws",
        "client:__legacy_ollama__",
    ]


def test_execute_returns_on_first_success():
    cfg = _make_cfg(fallbacks=["aws"])
    d = _dispatcher(cfg)
    tier = next(iter(cfg.models.tiers.values()))

    async def fake_fn(client):
        if client == "client:colab":
            raise httpx.ReadTimeout("slow")
        return ("ok", client)

    async def _run():
        return await d.execute(tier, fake_fn)

    result = asyncio.run(_run())
    assert result == ("ok", "client:aws")
    assert d.health["colab"].consecutive_failures == 1
    assert d.health["aws"].consecutive_failures == 0


def test_execute_does_not_rotate_on_4xx():
    """HTTP 404 is a client bug, not a server-health signal."""
    cfg = _make_cfg(fallbacks=["aws"])
    d = _dispatcher(cfg)
    tier = next(iter(cfg.models.tiers.values()))

    async def fake_fn(client):
        req = httpx.Request("POST", "http://x/y")
        resp = httpx.Response(404, request=req)
        raise httpx.HTTPStatusError("not found", request=req, response=resp)

    async def _run():
        await d.execute(tier, fake_fn)

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(_run())
    assert d.health["colab"].consecutive_failures == 0


# ── Backward compat: missing hosts.yaml synthesises legacy hosts ───────────

def test_appconfig_reconcile_synthesises_missing_hosts(tmp_path, monkeypatch):
    (tmp_path / "models.yaml").write_text(yaml.safe_dump({
        "default": "fast",
        "tiers": {
            "fast": {
                "name": "Fast",
                "backend": "ollama",
                "endpoint": "http://ollama:11434",
                "model_tag": "x",
                "context_window": 2048,
                "params": {"temperature": 0.7, "top_p": 0.9, "top_k": 20},
                "vram_estimate_gb": 4,
            },
        },
    }))
    (tmp_path / "router.yaml").write_text("auto_thinking_signals: {}\nmulti_agent: {}\n")
    (tmp_path / "vram.yaml").write_text("total_vram_gb: 24\nheadroom_gb: 1.5\n")
    # no hosts.yaml written

    monkeypatch.setenv("OLLAMA_URL", "http://legacy-ollama:11434")
    monkeypatch.setenv("LLAMACPP_URL", "http://legacy-llama:8001/v1")

    cfg = AppConfig.load(config_dir=tmp_path)

    # Legacy hosts always present.
    assert LEGACY_OLLAMA in cfg.hosts.hosts
    assert LEGACY_LLAMA_CPP in cfg.hosts.hosts
    # Tier got auto-assigned a host that matches its legacy endpoint.
    tier = cfg.models.tiers["fast"]
    assert tier.host is not None
    assert tier.host in cfg.hosts.hosts
    assert cfg.hosts.hosts[tier.host].url.rstrip("/") == "http://ollama:11434"
