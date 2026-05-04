"""Tests for backend/plugins/registry.py — the plugin manifest loader."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def _write_plugin(path: Path, name: str, tools: list, skills: list, **extra) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"name: {name}", f"title: {name.title()}"]
    if tools:
        lines.append("tools:")
        lines.extend(f"  - {t}" for t in tools)
    if skills:
        lines.append("skills:")
        lines.extend(f"  - {s}" for s in skills)
    for k, v in extra.items():
        lines.append(f"{k}: {v}")
    path.write_text("\n".join(lines))


def test_loads_plugin_manifests(tmp_path):
    from backend.plugins.registry import build_plugin_registry
    _write_plugin(tmp_path / "a.yaml", "a", tools=["foo", "bar"], skills=["s1"])
    _write_plugin(tmp_path / "b.yaml", "b", tools=["baz"], skills=[])
    reg = build_plugin_registry(tmp_path)
    assert {"a", "b"}.issubset(set(reg.plugins))
    a = reg.get("a")
    assert a.tools == ["foo", "bar"]
    assert a.skills == ["s1"]


def test_skips_dotfiles_and_underscores(tmp_path):
    from backend.plugins.registry import build_plugin_registry
    _write_plugin(tmp_path / ".hidden.yaml", "hidden", tools=["x"], skills=[])
    _write_plugin(tmp_path / "_template.yaml", "template", tools=["x"], skills=[])
    _write_plugin(tmp_path / "real.yaml", "real", tools=["x"], skills=[])
    reg = build_plugin_registry(tmp_path)
    assert list(reg.plugins.keys()) == ["real"]


def test_real_plugins_directory_loads():
    from backend.plugins.registry import build_plugin_registry
    reg = build_plugin_registry(ROOT / "plugins")
    expected = {
        "productivity", "design", "data", "finance",
        "engineering", "pdf-viewer", "bio-research", "zoom",
    }
    assert expected.issubset(set(reg.plugins))


def test_apply_to_tools_flips_default_enabled(tmp_path, monkeypatch):
    """Toggle a plugin and confirm every method on every member tool flips."""
    from backend.plugins.registry import build_plugin_registry
    from backend.tools.registry import build_registry as build_tool_registry

    # Use the real tools dir (includes the new connectors).
    tool_reg = build_tool_registry(tools_dir=ROOT / "tools", config_dir=ROOT / "config")

    _write_plugin(
        tmp_path / "design.yaml", "design",
        tools=["figma", "canva"], skills=[],
    )
    plugins = build_plugin_registry(tmp_path)

    # Snapshot baseline state
    before = {
        n: t.default_enabled for n, t in tool_reg.tools.items()
        if t.module_name in {"figma", "canva"}
    }
    assert before, "expected figma/canva methods to be discovered"

    n = plugins.apply_to_tools("design", True, tool_reg)
    assert n == len(before), f"flipped {n}, expected {len(before)}"

    after = {
        n: t.default_enabled for n, t in tool_reg.tools.items()
        if t.module_name in {"figma", "canva"}
    }
    assert all(after.values()), "every figma/canva method should be enabled"

    plugins.apply_to_tools("design", False, tool_reg)
    after_off = {
        n: t.default_enabled for n, t in tool_reg.tools.items()
        if t.module_name in {"figma", "canva"}
    }
    assert not any(after_off.values()), "every figma/canva method should be disabled"


def test_members_for_filters_unknown_entries(tmp_path):
    """Plugins can ship referencing tools not yet present — `members_for`
    must not blow up."""
    from backend.plugins.registry import build_plugin_registry
    from backend.skills.registry import SkillRegistry
    from backend.tools.registry import ToolRegistry

    _write_plugin(
        tmp_path / "ghost.yaml", "ghost",
        tools=["never_built", "still_pending"],
        skills=["nope"],
    )
    plugins = build_plugin_registry(tmp_path)
    members = plugins.members_for("ghost", ToolRegistry(), SkillRegistry())
    assert members == {"tools": [], "skills": []}


def test_members_for_unknown_plugin_returns_empty():
    from backend.plugins.registry import PluginRegistry
    from backend.skills.registry import SkillRegistry
    from backend.tools.registry import ToolRegistry
    reg = PluginRegistry()
    assert reg.members_for("ghost", ToolRegistry(), SkillRegistry()) == {"tools": [], "skills": []}
