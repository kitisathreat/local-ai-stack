"""CI tests for configuration files.

Native-mode layout (post-PR #96): docker-compose.yml is deliberately
absent; one assertion below guards against it being resurrected. All
checks are purely static — no running services needed.
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
    assert backend == "llama_cpp", (
        f"Tier '{tier}' must use llama_cpp backend (got '{backend}')"
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
    # vision is intentionally NOT pinned: at 21 GB on a 24 GB GPU,
    # pinning it leaves only ~2 GB free for chat tiers and blocks
    # versatile / highest_quality / coding from ever loading. Vision
    # cold-spawns on demand (auto-routed when an image is in the
    # message) and joins the LRU eviction pool. The runtime evicts
    # via the scheduler's _make_room_for path which DOES safely
    # unload llama.cpp tiers (the original "llama.cpp can't unload"
    # claim was about an earlier pre-VRAMScheduler design).
    assert not vision.get("pinned"), (
        "Vision tier must NOT be pinned — pinning the 21 GB tier blocks every "
        "other chat tier from loading on a 24 GB GPU."
    )


def test_models_yaml_orchestrator_flag():
    data = _load_yaml("config/models.yaml")
    orchestrators = [
        name for name, t in data.get("tiers", {}).items()
        if t.get("is_orchestrator")
    ]
    assert len(orchestrators) == 1, (
        f"Exactly one tier must be marked as is_orchestrator; found: {orchestrators}"
    )


def test_reasoning_max_tier_present_and_valid():
    """The optional GPT-OSS-120B tier must declare a unique port + model_tag."""
    data = _load_yaml("config/models.yaml")
    tiers = data.get("tiers", {})
    assert "reasoning_max" in tiers, "reasoning_max tier missing from models.yaml"
    rmax = tiers["reasoning_max"]
    assert rmax.get("model_tag") == "gpt-oss-120b"
    assert rmax.get("port") == 8014
    # Should NOT have a speculative draft (tokenizer incompatible with Qwen3)
    assert not rmax.get("draft_model_tag")


def test_all_chat_tier_ports_are_unique():
    data = _load_yaml("config/models.yaml")
    tiers = data.get("tiers", {})
    ports = [t.get("port") for t in tiers.values() if t.get("port")]
    assert len(ports) == len(set(ports)), f"Duplicate ports: {ports}"


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
    # Native mode never starts searxng / n8n containers, but the declarations
    # stay in tools.yaml so the tool registry can gate them at runtime. Keep
    # them in the allowlist so the validator doesn't fail.
    #
    # `host_*` are pseudo-services declared by tools that reach the host
    # machine (filesystem, OS processes). They are deliberately not part of
    # the local stack — the tool registry uses them as a flag to suppress
    # the tool when airgap mode is on.
    known_services = {
        None,
        "qdrant", "jupyter", "backend", "n8n", "searxng",
        "host_filesystem", "host_processes",
    }
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


# ── Removed: ollama-models.yaml (deleted in llama.cpp migration) ─────────────

def test_ollama_models_yaml_removed():
    """The Ollama catalog file should have been removed in the llama.cpp
    migration. Models are now described in config/model-sources.yaml using
    HuggingFace repos exclusively."""
    assert not (ROOT / "config" / "ollama-models.yaml").exists(), (
        "config/ollama-models.yaml should be deleted — Ollama is no longer used"
    )


# ── Native mode (no docker-compose, no searxng container) ────────────────────

def test_docker_compose_removed_in_native_branch():
    """docker-compose.yml, .dockerignore, and Dockerfiles are deliberately
    absent on the non-docker-dependent branch."""
    assert not (ROOT / "docker-compose.yml").exists()
    assert not (ROOT / "backend" / "Dockerfile").exists()
    assert not (ROOT / "frontend").exists()


def test_model_sources_yaml_exists():
    assert (ROOT / "config" / "model-sources.yaml").exists()


def test_model_sources_yaml_has_tiers():
    data = _load_yaml("config/model-sources.yaml")
    assert "tiers" in data and data["tiers"]


def test_launcher_script_at_root():
    """The single consolidated launcher sits at the repo root."""
    assert (ROOT / "LocalAIStack.ps1").exists()


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


# ── .gitignore ────────────────────────────────────────────────────────────────

def test_gitignore_exists():
    assert (ROOT / ".gitignore").exists()


# ── TierConfig: override_tensors field ────────────────────────────────────────

def test_tier_config_override_tensors_default_empty():
    """New override_tensors field defaults to [] and is omittable."""
    from backend.config import TierConfig
    tier = TierConfig(name="t", context_window=4096)
    assert tier.override_tensors == []


def test_tier_config_override_tensors_round_trip():
    """override_tensors deserializes from YAML-shaped dict."""
    from backend.config import TierConfig
    tier = TierConfig(
        name="t",
        context_window=4096,
        override_tensors=[".ffn_.*_exps.=CPU"],
    )
    assert tier.override_tensors == [".ffn_.*_exps.=CPU"]


def test_tier_config_override_tensors_multiple_patterns():
    """Multiple patterns are preserved in order."""
    from backend.config import TierConfig
    tier = TierConfig(
        name="t",
        context_window=4096,
        override_tensors=[
            ".ffn_.*_exps.=CPU",
            ".ffn_gate_inp.=CPU",
        ],
    )
    assert tier.override_tensors == [
        ".ffn_.*_exps.=CPU",
        ".ffn_gate_inp.=CPU",
    ]


# ── TierConfig: speculative-decode draft fields ───────────────────────────────

def test_tier_config_draft_fields_default_none():
    """Spec-decode fields default to None / sane values; no -md emitted."""
    from backend.config import TierConfig
    tier = TierConfig(name="t", context_window=4096)
    assert tier.draft_model_tag is None
    assert tier.draft_gguf_path is None
    assert tier.draft_n_gpu_layers == -1
    assert tier.draft_max == 8
    assert tier.draft_min == 4


def test_tier_config_draft_fields_round_trip():
    from backend.config import TierConfig
    tier = TierConfig(
        name="t",
        context_window=4096,
        draft_model_tag="qwen3-0.6b",
        draft_gguf_path="/tmp/draft.gguf",
        draft_n_gpu_layers=-1,
        draft_max=6,
        draft_min=3,
    )
    assert tier.draft_model_tag == "qwen3-0.6b"
    assert tier.draft_gguf_path == "/tmp/draft.gguf"
    assert tier.draft_max == 6
    assert tier.draft_min == 3


# ── TierConfig: per-tier variants ─────────────────────────────────────────────

def test_tier_variants_default_empty():
    from backend.config import TierConfig
    tier = TierConfig(name="t", context_window=4096)
    assert tier.variants == {}
    assert tier.default_variant is None


def test_tier_variants_round_trip():
    from backend.config import TierConfig, TierVariant
    tier = TierConfig(
        name="coding",
        context_window=131072,
        model_tag="qwen3-coder-30b-a3b",
        vram_estimate_gb=6.5,
        variants={
            "30b": TierVariant(model_tag="qwen3-coder-30b-a3b", vram_estimate_gb=6.5),
            "80b": TierVariant(model_tag="qwen3-coder-next-80b-a3b", vram_estimate_gb=14.5),
        },
        default_variant="30b",
    )
    assert set(tier.variants.keys()) == {"30b", "80b"}
    assert tier.default_variant == "30b"


def test_resolve_variant_applies_overrides():
    """resolve_variant returns a tier copy with the variant's fields applied."""
    from backend.config import TierConfig, TierVariant
    tier = TierConfig(
        name="coding",
        context_window=131072,
        model_tag="qwen3-coder-30b-a3b",
        vram_estimate_gb=6.5,
        variants={
            "80b": TierVariant(
                model_tag="qwen3-coder-next-80b-a3b",
                vram_estimate_gb=14.5,
            ),
        },
        default_variant=None,
    )
    big = tier.resolve_variant("80b")
    assert big.model_tag == "qwen3-coder-next-80b-a3b"
    assert big.vram_estimate_gb == 14.5
    # Untouched fields preserved
    assert big.context_window == 131072
    # Original tier not mutated
    assert tier.model_tag == "qwen3-coder-30b-a3b"


def test_resolve_variant_falls_through_to_self_when_unset():
    from backend.config import TierConfig
    tier = TierConfig(name="t", context_window=4096, model_tag="foo")
    assert tier.resolve_variant(None) is tier
    # Unknown variant name also falls through (loader is lenient)
    assert tier.resolve_variant("nonexistent") is tier


def test_resolve_variant_uses_default_when_arg_none():
    from backend.config import TierConfig, TierVariant
    tier = TierConfig(
        name="coding",
        context_window=131072,
        model_tag="qwen3-coder-30b-a3b",
        variants={
            "30b": TierVariant(model_tag="qwen3-coder-30b-a3b"),
            "80b": TierVariant(model_tag="qwen3-coder-next-80b-a3b"),
        },
        default_variant="30b",
    )
    resolved = tier.resolve_variant(None)
    assert resolved.model_tag == "qwen3-coder-30b-a3b"
