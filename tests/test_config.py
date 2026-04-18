"""
CI tests for configuration files, docker-compose.yml, and static assets.

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

REQUIRED_PROFILES = [
    "fast", "quality", "coding", "large",           # original 4
    "creative", "analyst", "balanced", "roleplay", "summarizer",  # new connector profiles
]

REQUIRED_PROFILE_KEYS = ["id", "gpu", "context", "parallel", "description"]
REQUIRED_INFERENCE_KEYS = ["temperature", "top_p", "top_k", "repeat_penalty", "max_tokens"]

DEFAULT_PROFILE = "quality"


def test_models_yaml_exists():
    assert (ROOT / "config" / "models.yaml").exists()


def test_models_yaml_has_default_key():
    data = _load_yaml("config/models.yaml")
    assert "default" in data, "models.yaml missing top-level 'default' key"


def test_models_yaml_default_is_quality():
    data = _load_yaml("config/models.yaml")
    assert data.get("default") == DEFAULT_PROFILE, (
        f"Expected default profile '{DEFAULT_PROFILE}', got '{data.get('default')}'"
    )


def test_models_yaml_has_models_section():
    data = _load_yaml("config/models.yaml")
    assert "models" in data and isinstance(data["models"], dict), (
        "models.yaml missing 'models:' section"
    )


@pytest.mark.parametrize("profile", REQUIRED_PROFILES)
def test_models_yaml_profile_exists(profile):
    data = _load_yaml("config/models.yaml")
    profiles = data.get("models", {})
    assert profile in profiles, f"Profile '{profile}' missing from models.yaml"


@pytest.mark.parametrize("profile", REQUIRED_PROFILES)
def test_models_yaml_profile_required_keys(profile):
    data = _load_yaml("config/models.yaml")
    profile_data = data.get("models", {}).get(profile, {})
    for key in REQUIRED_PROFILE_KEYS:
        assert key in profile_data, (
            f"Profile '{profile}' missing required key '{key}'"
        )


@pytest.mark.parametrize("profile", REQUIRED_PROFILES)
def test_models_yaml_profile_inference_keys(profile):
    data = _load_yaml("config/models.yaml")
    profile_data = data.get("models", {}).get(profile, {})
    for key in REQUIRED_INFERENCE_KEYS:
        assert key in profile_data, (
            f"Profile '{profile}' missing inference key '{key}'"
        )


@pytest.mark.parametrize("profile", REQUIRED_PROFILES)
def test_models_yaml_profile_temperature_in_range(profile):
    data = _load_yaml("config/models.yaml")
    profile_data = data.get("models", {}).get(profile, {})
    temp = profile_data.get("temperature")
    if temp is not None:
        assert 0.0 <= float(temp) <= 2.0, (
            f"Profile '{profile}' temperature {temp} out of range [0.0, 2.0]"
        )


def test_models_yaml_default_profile_exists_in_models():
    data = _load_yaml("config/models.yaml")
    default = data.get("default")
    profiles = data.get("models", {})
    assert default in profiles, (
        f"Default profile '{default}' not found in models section"
    )


def test_models_yaml_no_duplicate_profiles():
    # Re-parse with a loader that tracks duplicates
    raw = (ROOT / "config" / "models.yaml").read_text(encoding="utf-8")

    class DuplicateKeyLoader(yaml.SafeLoader):
        pass

    seen_keys = []

    def construct_mapping(loader, node):
        pairs = loader.construct_pairs(node)
        keys = [k for k, _ in pairs]
        for k in keys:
            if k in seen_keys:
                pass  # Just track, warn in assertion
        seen_keys.extend(keys)
        return dict(pairs)

    DuplicateKeyLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping
    )
    yaml.load(raw, Loader=DuplicateKeyLoader)

    profile_keys = [k for k in seen_keys if k in REQUIRED_PROFILES]
    duplicates = [k for k in profile_keys if profile_keys.count(k) > 1]
    assert not duplicates, f"Duplicate profile keys in models.yaml: {set(duplicates)}"


# ── tools.yaml ────────────────────────────────────────────────────────────────

def test_tools_yaml_exists():
    assert (ROOT / "config" / "tools.yaml").exists()


def test_tools_yaml_valid_yaml():
    data = _load_yaml("config/tools.yaml")
    assert isinstance(data, dict)


def test_tools_yaml_has_tools_section():
    data = _load_yaml("config/tools.yaml")
    assert "tools" in data, "tools.yaml missing 'tools:' section"


def test_tools_yaml_has_pipelines_section():
    data = _load_yaml("config/tools.yaml")
    assert "pipelines" in data, "tools.yaml missing 'pipelines:' section"


def test_tools_yaml_all_referenced_files_exist():
    """Every 'file:' path listed in tools.yaml must exist in the repo."""
    data = _load_yaml("config/tools.yaml")
    refs = _find_all_file_refs(data)
    missing = [r for r in refs if not (ROOT / r).exists()]
    assert not missing, (
        f"tools.yaml references files that don't exist:\n" + "\n".join(f"  {m}" for m in missing)
    )


def test_tools_yaml_pipeline_types_are_valid():
    data = _load_yaml("config/tools.yaml")
    valid_types = {"filter", "pipe"}
    pipelines = data.get("pipelines", {})
    for name, entry in pipelines.items():
        if isinstance(entry, dict) and "type" in entry:
            assert entry["type"] in valid_types, (
                f"Pipeline '{name}' has invalid type '{entry['type']}' (must be: {valid_types})"
            )


def test_tools_yaml_service_refs_are_valid():
    """requires_service must be null or a known service name."""
    data = _load_yaml("config/tools.yaml")
    known_services = {None, "searxng", "pipelines", "qdrant", "ollama", "n8n", "jupyter"}
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


# ── docker-compose.yml ────────────────────────────────────────────────────────

REQUIRED_SERVICES = [
    "open-webui", "jupyter", "pipelines", "qdrant", "searxng", "ollama", "n8n",
]

REQUIRED_OPEN_WEBUI_ENV = [
    "WEBUI_AUTH",
    "OPENAI_API_BASE_URLS",
    "OLLAMA_BASE_URL",
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


def test_docker_compose_webui_auth_disabled():
    data = _load_yaml("docker-compose.yml")
    webui = data["services"].get("open-webui", {})
    env = webui.get("environment", [])
    env_str = str(env)
    assert "WEBUI_AUTH" in env_str, "WEBUI_AUTH not found in open-webui environment"
    assert "False" in env_str, "WEBUI_AUTH is not set to False in open-webui environment"


def test_docker_compose_jupyter_token_set():
    data = _load_yaml("docker-compose.yml")
    jupyter = data["services"].get("jupyter", {})
    env = str(jupyter.get("environment", ""))
    assert "JUPYTER_TOKEN" in env, "JUPYTER_TOKEN not configured in jupyter service"


def test_docker_compose_open_webui_port():
    data = _load_yaml("docker-compose.yml")
    webui = data["services"].get("open-webui", {})
    ports = str(webui.get("ports", ""))
    assert "3000" in ports, "open-webui not exposing port 3000"


def test_docker_compose_all_services_have_image():
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
    assert "json" in raw, "searxng settings.yml does not mention 'json' format"


@pytest.mark.parametrize("engine", REQUIRED_SEARXNG_ENGINES)
def test_searxng_has_academic_engine(engine):
    raw = (ROOT / "config" / "searxng" / "settings.yml").read_text(encoding="utf-8").lower()
    assert engine.lower() in raw, f"SearXNG settings missing academic engine: '{engine}'"


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
    assert domain in raw, f"knowledge/sources.yaml missing domain: '{domain}'"


# ── Static assets ─────────────────────────────────────────────────────────────

def test_squarespace_embed_exists():
    assert (ROOT / "squarespace-embed.html").exists(), (
        "squarespace-embed.html not found"
    )


def test_squarespace_embed_has_tailscale_hostname():
    html = (ROOT / "squarespace-embed.html").read_text(encoding="utf-8")
    assert re.search(r"desktop-j4g42gi\.taila2838f\.ts\.net", html), (
        "squarespace-embed.html missing or has wrong Tailscale hostname"
    )


def test_squarespace_embed_has_error_fallback():
    html = (ROOT / "squarespace-embed.html").read_text(encoding="utf-8")
    assert "ai-error" in html, "squarespace-embed.html missing error fallback UI"


# ── .gitignore ────────────────────────────────────────────────────────────────

def test_gitignore_excludes_env_local():
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".env.local" in gi, ".gitignore must exclude .env.local"


def test_gitignore_exists():
    assert (ROOT / ".gitignore").exists()
