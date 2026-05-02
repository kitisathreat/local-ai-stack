"""Unit tests for backend/tools/registry.py.

Build the registry against the real tools/ directory so we also get a
smoke-test that every legacy tool still imports cleanly under Phase 5
auto-discovery.
"""

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="module")
def registry():
    from backend.tools.registry import build_registry
    return build_registry(tools_dir=ROOT / "tools", config_dir=ROOT / "config")


# ── Coverage ────────────────────────────────────────────────────────────

def test_registry_discovers_tools(registry):
    # Expect at least the simple, dep-free tools to be discovered.
    required = [
        "calculator.calculate",
        "datetime_tool",
    ]
    # Collapse module-only names and check at least one method of each module.
    names = list(registry.tools.keys())
    assert len(names) > 10, f"Expected many tools, got {len(names)}"
    assert any(n.startswith("calculator.") for n in names), (
        f"calculator.* missing. Sample: {names[:20]}"
    )
    assert any(n.startswith("datetime_tool.") for n in names), (
        f"datetime_tool.* missing."
    )


# ── Schema shape ────────────────────────────────────────────────────────

def test_tool_schemas_are_openai_shaped(registry):
    sample_name = next(iter(registry.tools))
    schema = registry.tools[sample_name].schema
    assert schema["type"] == "function"
    fn = schema["function"]
    assert "name" in fn and "description" in fn
    params = fn["parameters"]
    assert params["type"] == "object"
    assert "properties" in params and "required" in params


def test_schemas_exclude_injected_params(registry):
    for name, t in registry.tools.items():
        props = t.schema["function"]["parameters"]["properties"]
        assert "__user__" not in props, f"{name} schema leaked __user__"
        assert "__event_emitter__" not in props, f"{name} leaked __event_emitter__"
        assert "self" not in props


# ── Dispatch ────────────────────────────────────────────────────────────

def test_dispatch_calculator(registry):
    from backend.tools.executor import dispatch_tool_call
    call = {
        "id": "call_1",
        "type": "function",
        "function": {
            "name": "calculator.calculate",
            "arguments": '{"expression": "2 + 2"}',
        },
    }
    result = asyncio.run(dispatch_tool_call(call, registry))
    assert result["role"] == "tool"
    assert result["tool_call_id"] == "call_1"
    assert "4" in result["content"]


def test_dispatch_unknown_tool(registry):
    from backend.tools.executor import dispatch_tool_call
    call = {
        "id": "call_bad",
        "type": "function",
        "function": {"name": "nonexistent.foo", "arguments": "{}"},
    }
    result = asyncio.run(dispatch_tool_call(call, registry))
    assert "Unknown tool" in result["content"]


def test_dispatch_bad_json(registry):
    from backend.tools.executor import dispatch_tool_call
    call = {
        "id": "call_bad_json",
        "type": "function",
        "function": {
            "name": "calculator.calculate",
            "arguments": "{not-json",
        },
    }
    result = asyncio.run(dispatch_tool_call(call, registry))
    assert "Invalid JSON" in result["content"]


def test_dispatch_many(registry):
    from backend.tools.executor import dispatch_many
    calls = [
        {"id": "a", "function": {"name": "calculator.calculate", "arguments": '{"expression": "1+1"}'}},
        {"id": "b", "function": {"name": "calculator.calculate", "arguments": '{"expression": "3*3"}'}},
    ]
    results = asyncio.run(dispatch_many(calls, registry))
    assert len(results) == 2
    assert "2" in results[0]["content"]
    assert "9" in results[1]["content"]


# ── Taxonomy (group / subgroup / tier) ─────────────────────────────────

def test_groups_yaml_loaded(registry):
    """tool_groups.yaml should populate the registry's display map."""
    assert registry.groups, "Expected tool_groups.yaml to populate ToolRegistry.groups"
    # Spot-check a few canonical groups exist with display titles.
    for slug in ("research", "finance", "entertainment", "desktop"):
        assert slug in registry.groups, f"Missing group {slug!r} in tool_groups.yaml"
    assert registry.group_title("desktop") == "Desktop Integration"
    assert registry.group_title("entertainment", "torrents") == "Torrents"


def test_every_tool_has_group(registry):
    """Every method registered must carry a group/subgroup. Tools without
    yaml entries fall back to ('uncategorized', 'general')."""
    for name, t in registry.tools.items():
        assert t.group, f"{name}: empty group"
        assert t.subgroup, f"{name}: empty subgroup"


def test_uncategorized_floor_is_low(registry):
    """If most tools end up in the 'uncategorized' bucket the YAML
    annotations have drifted out of sync with the file system. Keep the
    bar at <5% so a regression here is loud."""
    total = len(registry.tools)
    uncat = sum(1 for t in registry.tools.values() if t.group == "uncategorized")
    assert uncat / total < 0.05, (
        f"{uncat}/{total} tool methods are uncategorized — "
        "add their module name to config/tools.yaml with group/subgroup."
    )


def test_tier_classification(registry):
    """Tools with requires_service starting `host_` are host-tier; rest network."""
    from backend.tools.registry import TIER_HOST, TIER_NETWORK
    sample = {
        "filesystem.list_directory":  TIER_HOST,
        "kicad.run_erc":              TIER_HOST,
        "qbittorrent.list_torrents":  TIER_HOST,
        "spotify.search":             TIER_NETWORK,
        "calculator.calculate":       TIER_NETWORK,
        "torrent_search.search_movies": TIER_NETWORK,
    }
    for name, expected in sample.items():
        t = registry.get(name)
        if t is None:
            continue   # Tool may have failed to import in a constrained env.
        assert t.tier == expected, f"{name}: tier={t.tier!r}, expected {expected!r}"


def test_tier_titles_resolve(registry):
    from backend.tools.registry import TIER_HOST, TIER_NETWORK, ToolRegistry
    assert ToolRegistry.tier_title(TIER_HOST) == "Host / System Access"
    assert ToolRegistry.tier_title(TIER_NETWORK) == "Network-only Tools"
    # Network is shown above host in the UI.
    assert ToolRegistry.tier_order(TIER_NETWORK) < ToolRegistry.tier_order(TIER_HOST)


def test_tools_yaml_merges_all_sections(registry):
    """The original tools.yaml split tools across `tools:` and
    `additional_tools:` sections. Both must be honoured by the loader."""
    # finance is under additional_tools historically; filesystem under tools.
    finance = registry.get("finance.stock_quote") or next(
        (t for n, t in registry.tools.items() if n.startswith("finance.")), None,
    )
    if finance is not None:
        # If the additional_tools section is being read, finance has a
        # group set. If it's being silently ignored, group falls back
        # to "uncategorized".
        assert finance.group != "uncategorized", (
            "additional_tools: section is not being read by the loader"
        )
