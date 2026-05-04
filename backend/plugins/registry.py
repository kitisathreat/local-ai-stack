"""Plugin manifest loader.

Each plugin lives at `plugins/<slug>.yaml`:

    name: productivity
    title: Productivity
    description: Email, calendar, docs and notes.
    icon: zap
    tools:
      - gmail
      - google_calendar
      - google_drive
      - notion
    skills:
      - doc-coauthoring
    enabled_by_default: false

Toggling a plugin in the admin UI is equivalent to flipping
``default_enabled`` on every named tool and ``enabled`` on every named
skill. The registry doesn't *enforce* the toggle itself — it just
exposes the membership graph and the current desired state, which
admin endpoints apply to the underlying tool / skill registries.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..skills.registry import SkillRegistry
from ..tools.registry import ToolRegistry


logger = logging.getLogger(__name__)


@dataclass
class Plugin:
    slug: str
    title: str
    description: str = ""
    icon: str = ""               # opaque string, rendered by the UI
    tools: list[str] = field(default_factory=list)        # tool *module* names
    skills: list[str] = field(default_factory=list)       # skill slugs
    enabled_by_default: bool = False
    path: Path | None = None

    def tool_method_names(self, tool_registry: ToolRegistry) -> list[str]:
        """Expand `tools: [foo]` to every method-level entry the tool
        registry surfaces (`foo.method_a`, `foo.method_b`, ...). The
        plugin manifest is module-level for ergonomics; the registry is
        method-level for granularity."""
        wanted = set(self.tools)
        return [
            t.name for t in tool_registry.tools.values()
            if t.module_name in wanted
        ]


class PluginRegistry:
    def __init__(self) -> None:
        self.plugins: dict[str, Plugin] = {}

    def __contains__(self, slug: str) -> bool:
        return slug in self.plugins

    def __len__(self) -> int:
        return len(self.plugins)

    def get(self, slug: str) -> Plugin | None:
        return self.plugins.get(slug)

    def all(self) -> list[Plugin]:
        return sorted(self.plugins.values(), key=lambda p: p.title.lower())

    def members_for(
        self,
        slug: str,
        tools: ToolRegistry,
        skills: SkillRegistry,
    ) -> dict[str, list[str]]:
        """Return the resolved members of a plugin so the UI can render
        the affected toggles. Unknown tools / skills are filtered out
        rather than erroring so a plugin can ship before every member
        connector lands."""
        plugin = self.get(slug)
        if not plugin:
            return {"tools": [], "skills": []}
        present_tools = plugin.tool_method_names(tools)
        present_skills = [s for s in plugin.skills if s in skills]
        return {"tools": present_tools, "skills": present_skills}

    def apply_to_tools(self, slug: str, enabled: bool, tools: ToolRegistry) -> int:
        """Flip default_enabled on every tool method in the plugin.
        Returns the number of methods affected."""
        plugin = self.get(slug)
        if not plugin:
            return 0
        names = set(plugin.tool_method_names(tools))
        count = 0
        for entry in tools.tools.values():
            if entry.name in names:
                entry.default_enabled = enabled
                count += 1
        return count

    def apply_to_skills(self, slug: str, enabled: bool, skills: SkillRegistry) -> int:
        plugin = self.get(slug)
        if not plugin:
            return 0
        count = 0
        for s in plugin.skills:
            sk = skills.get(s)
            if sk:
                sk.enabled = enabled
                count += 1
        return count


def _load_plugin(path: Path) -> Plugin | None:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        logger.warning("Failed to load plugin %s: %s", path, e)
        return None
    if not isinstance(data, dict):
        return None
    slug = str(data.get("name") or path.stem).strip()
    if not slug:
        return None
    return Plugin(
        slug=slug,
        title=str(data.get("title") or slug.replace("-", " ").title()).strip(),
        description=str(data.get("description") or "").strip(),
        icon=str(data.get("icon") or "").strip(),
        tools=[str(t).strip() for t in (data.get("tools") or []) if str(t).strip()],
        skills=[str(s).strip() for s in (data.get("skills") or []) if str(s).strip()],
        enabled_by_default=bool(data.get("enabled_by_default", False)),
        path=path,
    )


def build_plugin_registry(plugins_dir: Path) -> PluginRegistry:
    reg = PluginRegistry()
    if not plugins_dir.exists():
        logger.info("Plugins dir not found: %s — registry will be empty", plugins_dir)
        return reg
    for path in sorted(plugins_dir.glob("*.yaml")):
        if path.name.startswith((".", "_")):
            continue
        plugin = _load_plugin(path)
        if plugin:
            reg.plugins[plugin.slug] = plugin
    logger.info("Loaded %d plugins from %s", len(reg.plugins), plugins_dir)
    return reg
