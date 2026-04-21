"""CI tests for configuration files, docker-compose.yml, and static assets.

All checks are purely static — no Docker or running services needed.
"""

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parent.parent


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_yaml(rel_path: str) -> dict:
    path = ROOT / rel_path
    assert path.exists(), f"File not found: {rel_path}"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _find_all_file_refs(data, refs=None) -> list[str]:
    """Recursively collect all 'file' values from a nested dict."""
    if refs is None:
        refs = []
    if isinstance(data, dict):
        if "file" in data and isinstance(data["file"], str):
            refs.append(data["file"])
        for v in data.values():
            _find_all_file_refs(v, refs)
    return refs


# ── models.yaml ───────────────────────────────────────────────────────────────

REQUIRED_TIERS = ["highest_quality", "versatile", "fast", "coding", "vision"]

REQUIRED_TIER_KEYS = ["name", "backend", "model_tag", "context_window", "params", "vram_estimate_gb"]
REQUIRED_PARAM_KEYS = ["temperature", "top_p", "top_k"]

DEFAULT_TIER = "versatile"

# Backwards-compat aliases that MUST resolve to real tiers
REQUIRED_ALIASES = ["quality", "large", "balanced", "analyst", "creative", "roleplay", "summarizer"]


def test_models_yaml_exists():
    assert (ROOT / "config" / "models.yaml").exists()


def test_models_yaml_has_default_key():
    data = _load_yaml("config/models.yaml")
    assert "default" in data, "models.yaml missing top-level 'default' key"


def test_models_yaml_default_is_versatile():
    data = _load_yaml("config/models.yaml")
    assert data.get("default") == DEFAULT_TIER, (
        f"Expected default tier '{DEFAULT_TIER}', got '{data.get('default')}'"
    )


def test_models_yaml_has_tiers_section():
    data = _load_yaml("config/models.yaml")
    assert "tiers" in data and isinstance(data["tiers"], dict), (
        "models.yaml missing 'tiers:' section"
    )


@pytest.mark.parametrize("tier", REQUIRED_TIERS)
def test_models_yaml_tier_exists(tier):
    data = _load_yaml("config/models.yaml")
    tiers = data.get("tiers", {})
    assert tier in tiers, f"Tier '{tier}' missing from models.yaml"


@pytest.mark.parametrize("tier", REQUIRED_TIERS)
def test_models_yaml_tier_required_keys(tier):
    data = _load_yaml("config/models.yaml")
    tier_data = data.get("tiers", {}).get(tier, {})
    for key in REQUIRED_TIER_KEYS:
        assert key in tier_data, (
            f"Tier '{tier}' missing required key '{key}'"
        )


@pytest.mark.parametrize("tier", REQUIRED_TIERS)
def test_models_yaml_tier_backend_is_known(tier):
    data = _load_yaml("config/models.yaml")
    backend = data.get("tiers", {}).get(tier, {}).get("backend")
    assert backend in {"ollama", "llama_cpp"}, (
        f"Tier '{tier}' has unknown backend '{backend}'"
    )


@pytest.mark.parametrize("tier", REQUIRED_TIERS)
def test_models_yaml_tier_params_in_range(tier):
    data = _load_yaml("config/models.yaml")
    params = data.get("tiers", {}).get(tier, {}).get("params", {})
    temp = params.get("temperature")
    if temp is not None:
        assert 0.0 <= float(temp) <= 2.0, (
            f"Tier '{tier}' temperature {temp} out of range [0.0, 2.0]"
        )
    top_p = params.get("top_p")
    if top_p is not None:
        assert 0.0 < float(top_p) <= 1.0, (
            f"Tier '{tier}' top_p {top_p} out of range (0.0, 1.0]"
        )


def test_models_yaml_default_tier_exists():
    data = _load_yaml("config/models.yaml")
    default = data.get("default")
    tiers = data.get("tiers", {})
    assert default in tiers, (
        f"Default tier '{default}' not found in tiers section"
    )


def test_models_yaml_vision_tier_has_mmproj():
    data = _load_yaml("config/models.yaml")
    vision = data.get("tiers", {}).get("vision", {})
    assert vision.get("backend") == "llama_cpp", "Vision tier must use llama_cpp backend"
    assert vision.get("mmproj_path"), "Vision tier must declare mmproj_path"
    assert vision.get("pinned") is True, "Vision tier must be pinned (llama.cpp can't unload)"


def test_models_yaml_orchestrator_flag():
    data = _load_yaml("config/models.yaml")
    orchestrators = [
        name for name, t in data.get("tiers", {}).items()
        if t.get("is_orchestrator")
    ]
    assert len(orchestrators) == 1, (
        f"Exactly one tier must be marked as is_orchestrator; found: {orchestrators}"
    )


@pytest.mark.parametrize("alias", REQUIRED_ALIASES)
def test_models_yaml_aliases_resolve_to_real_tiers(alias):
    data = _load_yaml("config/models.yaml")
    target = data.get("aliases", {}).get(alias)
    assert target, f"Alias '{alias}' missing from models.yaml"
    assert target in data.get("tiers", {}), (
        f"Alias '{alias}' -> '{target}' which is not a real tier"
    )


# ── router.yaml ───────────────────────────────────────────────────────────────

def test_router_yaml_exists():
    assert (ROOT / "config" / "router.yaml").exists()


def test_router_yaml_valid():
    data = _load_yaml("config/router.yaml")
    assert "auto_thinking_signals" in data
    assert "multi_agent" in data
    assert "slash_commands" in data


def test_router_yaml_all_regexes_compile():
    """Every regex in router.yaml must compile."""
    data = _load_yaml("config/router.yaml")
    buckets = [
        data.get("auto_thinking_signals", {}).get("enable_when_any", []),
        data.get("auto_thinking_signals", {}).get("disable_when_any", []),
        data.get("multi_agent", {}).get("trigger_when_any", []),
    ]
    for bucket in buckets:
        for rule in bucket:
            if isinstance(rule, dict) and "regex" in rule:
                try:
                    re.compile(rule["regex"])
                except re.error as e:
                    pytest.fail(f"Regex failed to compile: {rule['regex']} — {e}")


def test_router_yaml_multi_agent_references_real_tier():
    data = _load_yaml("config/router.yaml")
    models = _load_yaml("config/models.yaml")
    tiers = models.get("tiers", {})
    ma = data.get("multi_agent", {})
    for key in ("worker_tier", "orchestrator_tier"):
        tier_name = ma.get(key)
        assert tier_name in tiers, f"router.yaml {key}={tier_name!r} not a real tier"
    for _cond, target_tier in ma.get("specialist_routes", {}).items():
        assert target_tier in tiers, (
            f"router.yaml specialist_routes target '{target_tier}' not a real tier"
        )


# ── vram.yaml ─────────────────────────────────────────────────────────────────

def test_vram_yaml_exists():
    assert (ROOT / "config" / "vram.yaml").exists()


def test_vram_yaml_valid():
    data = _load_yaml("config/vram.yaml")
    assert data.get("total_vram_gb", 0) > 0
    assert data.get("headroom_gb", 0) >= 0
    assert data.get("total_vram_gb") > data.get("headroom_gb")


# ── tools.yaml ────────────────────────────────────────────────────────────────

def test_tools_yaml_exists():
    assert (ROOT / "config" / "tools.yaml").exists()


def test_tools_yaml_valid_yaml():
    data = _load_yaml("config/tools.yaml")
    assert isinstance(data, dict)


def test_tools_yaml_has_tools_section():
    data = _load_yaml("config/tools.yaml")
    assert "tools" in data, "tools.yaml missing 'tools:' section"


def test_tools_yaml_all_referenced_files_exist():
    """Every 'file:' path listed in tools.yaml must exist in the repo."""
    data = _load_yaml("config/tools.yaml")
    refs = _find_all_file_refs(data)
    missing = [r for r in refs if not (ROOT / r).exists()]
    assert not missing, (
        f"tools.yaml references files that don't exist:\n" + "\n".join(f"  {m}" for m in missing)
    )


def test_tools_yaml_service_refs_are_valid():
    """requires_service must be null or a known service name."""
    data = _load_yaml("config/tools.yaml")
    known_services = {None, "searxng", "qdrant", "ollama", "n8n", "jupyter", "backend"}
    all_tool_entries = {}
    for section_val in data.values():
        if isinstance(section_val, dict):
            for k, v in section_val.items():
                if isinstance(v, dict):
                    all_tool_entries[k] = v

    for name, entry in all_tool_entries.items():
        if "requires_service" in entry:
            svc = entry["requires_service"]
            assert svc in known_services, (
                f"Tool/pipeline '{name}' references unknown service '{svc}'"
            )


# ── ollama-models.yaml ────────────────────────────────────────────────────────

def test_ollama_models_yaml_exists():
    assert (ROOT / "config" / "ollama-models.yaml").exists()


def test_ollama_models_yaml_has_auto_pull():
    data = _load_yaml("config/ollama-models.yaml")
    assert "auto_pull" in data, "ollama-models.yaml missing 'auto_pull' section"
    assert isinstance(data["auto_pull"], list), "'auto_pull' must be a list"
    assert len(data["auto_pull"]) > 0, "'auto_pull' list is empty"


def test_ollama_models_yaml_has_tier_group():
    data = _load_yaml("config/ollama-models.yaml")
    groups = data.get("groups", {})
    assert "tiers" in groups, "ollama-models.yaml must define a 'tiers' group"


# ── docker-compose.yml ────────────────────────────────────────────────────────

REQUIRED_SERVICES = [
    "backend", "frontend", "jupyter", "qdrant", "searxng", "ollama", "llama-server", "n8n",
    # Phase 6: cloudflared is profile-gated (only starts with `--profile public`)
    # but must be declared as a service for compose config validity.
    "cloudflared",
]


def test_docker_compose_exists():
    assert (ROOT / "docker-compose.yml").exists()


def test_docker_compose_valid_yaml():
    data = _load_yaml("docker-compose.yml")
    assert "services" in data, "docker-compose.yml missing 'services' key"


@pytest.mark.parametrize("service", REQUIRED_SERVICES)
def test_docker_compose_has_service(service):
    data = _load_yaml("docker-compose.yml")
    assert service in data["services"], (
        f"docker-compose.yml missing required service: '{service}'"
    )


def test_docker_compose_ollama_uses_gpu():
    """Ollama (not the backend) holds the GPU reservation — the backend runs
    CPU-only inside Docker so Docker Desktop GPU management can't restart it."""
    data = _load_yaml("docker-compose.yml")
    ollama = data["services"].get("ollama", {})
    deploy = ollama.get("deploy", {})
    devices = str(deploy.get("resources", {}).get("reservations", {}).get("devices", ""))
    assert "nvidia" in devices, "ollama service must reserve NVIDIA GPUs"


def test_docker_compose_backend_routes_ollama():
    data = _load_yaml("docker-compose.yml")
    backend = data["services"].get("backend", {})
    env = str(backend.get("environment", ""))
    assert "OLLAMA_URL" in env, "backend must set OLLAMA_URL"


def test_docker_compose_frontend_proxies_backend():
    """Frontend service uses the built nginx image; nginx.conf proxies /api/
    to backend:8000. We validate both the service exists with correct port
    and the nginx.conf reference to backend:8000."""
    data = _load_yaml("docker-compose.yml")
    frontend = data["services"].get("frontend", {})
    ports = str(frontend.get("ports", ""))
    assert "3000" in ports, "frontend must expose port 3000"
    nginx_conf = (ROOT / "frontend" / "nginx.conf").read_text(encoding="utf-8")
    assert "backend:8000" in nginx_conf, "frontend/nginx.conf must proxy to backend:8000"


def test_docker_compose_jupyter_token_set():
    data = _load_yaml("docker-compose.yml")
    jupyter = data["services"].get("jupyter", {})
    env = str(jupyter.get("environment", ""))
    assert "JUPYTER_TOKEN" in env


def test_docker_compose_backend_port():
    data = _load_yaml("docker-compose.yml")
    backend = data["services"].get("backend", {})
    ports = str(backend.get("ports", ""))
    assert "8000" in ports


def test_docker_compose_no_open_webui_service():
    """Open WebUI was replaced by the custom frontend in Phase 4."""
    data = _load_yaml("docker-compose.yml")
    assert "open-webui" not in data["services"], (
        "open-webui service should be removed in Phase 4; frontend replaces it"
    )


def test_docker_compose_all_services_have_image_or_build():
    data = _load_yaml("docker-compose.yml")
    for name, svc in data["services"].items():
        assert "image" in svc or "build" in svc, (
            f"Service '{name}' has neither 'image' nor 'build'"
        )


# ── searxng settings ──────────────────────────────────────────────────────────

REQUIRED_SEARXNG_ENGINES = [
    "pubmed", "semantic scholar", "paperswithcode", "openaire", "biorxiv",
]


def test_searxng_settings_exists():
    assert (ROOT / "config" / "searxng" / "settings.yml").exists()


def test_searxng_settings_json_format_enabled():
    raw = (ROOT / "config" / "searxng" / "settings.yml").read_text(encoding="utf-8")
    assert "json" in raw


@pytest.mark.parametrize("engine", REQUIRED_SEARXNG_ENGINES)
def test_searxng_has_academic_engine(engine):
    raw = (ROOT / "config" / "searxng" / "settings.yml").read_text(encoding="utf-8").lower()
    assert engine.lower() in raw


# ── knowledge/sources.yaml ────────────────────────────────────────────────────

REQUIRED_KNOWLEDGE_DOMAINS = [
    "biomedical", "physics", "chemistry", "mathematics",
    "computer_science", "social_sciences", "open_data",
]


def test_knowledge_sources_exists():
    assert (ROOT / "knowledge" / "sources.yaml").exists()


@pytest.mark.parametrize("domain", REQUIRED_KNOWLEDGE_DOMAINS)
def test_knowledge_has_domain(domain):
    raw = (ROOT / "knowledge" / "sources.yaml").read_text(encoding="utf-8")
    assert domain in raw


# ── Static assets ─────────────────────────────────────────────────────────────

def test_squarespace_embed_exists():
    assert (ROOT / "squarespace-embed.html").exists()


def test_squarespace_embed_has_error_fallback():
    html = (ROOT / "squarespace-embed.html").read_text(encoding="utf-8")
    assert "ai-error" in html


def test_squarespace_embed_uses_cloudflare_placeholder():
    """Phase 6: Tailscale hostname replaced with a placeholder substituted
    at render time via scripts/render-embed.sh."""
    html = (ROOT / "squarespace-embed.html").read_text(encoding="utf-8")
    assert "__CLOUDFLARE_HOSTNAME__" in html, (
        "embed should use __CLOUDFLARE_HOSTNAME__ placeholder; "
        "run scripts/render-embed.sh to substitute before pasting into Squarespace"
    )
    assert "taila2838f.ts.net" not in html, "embed still references Tailscale hostname"
    assert "tailscale://" not in html, "embed still has tailscale:// link"


# ── .gitignore ────────────────────────────────────────────────────────────────

def test_gitignore_exists():
    assert (ROOT / ".gitignore").exists()


def test_gitignore_excludes_env_local():
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".env.local" in gi
