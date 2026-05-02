"""Tool registry — auto-discovers `tools/*.py` modules (the legacy
Open-WebUI tool format) and builds OpenAI-style tool schemas from each
method's signature + docstring.

Each legacy tool file defines a `class Tools` with callable methods. We
instantiate it once at startup and introspect its public, non-private
methods. Methods can take any typed args plus optional `__user__` /
`__event_emitter__` kwargs (which we drop from the schema but inject at
call time).

The registry is immutable after startup. Tools loaded from
config/tools.yaml have `default_enabled` respected. Disabled tools are
still dispatched if the model calls them (the gate is UI-level), but
they're excluded from the default schema list sent with each request.
"""

from __future__ import annotations

import importlib.util
import inspect
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml


logger = logging.getLogger(__name__)


# Params the legacy tool framework injects rather than receives from the
# model. We drop these from the JSON schema but supply them at call time.
_INJECTED_PARAMS = {"__user__", "__event_emitter__", "__metadata__", "__request__"}


# Top-level tier slugs. Tools whose `requires_service` starts with `host_`
# need direct host access (filesystem, processes, design software). Everything
# else either talks to local stack services (qdrant, redis, llama_cpp) or
# external HTTP APIs and so is "network-only".
TIER_HOST = "host"
TIER_NETWORK = "network"


def _classify_tier(requires_service: str | None) -> str:
    if requires_service and requires_service.startswith("host_"):
        return TIER_HOST
    return TIER_NETWORK


_TIER_TITLES = {
    TIER_HOST: "Host / System Access",
    TIER_NETWORK: "Network-only Tools",
}
_TIER_ORDER = {TIER_NETWORK: 10, TIER_HOST: 20}


# Map Python typing hints to JSON-schema types.
def _py_type_to_json(annotation: Any) -> dict[str, Any]:
    if annotation is inspect.Parameter.empty or annotation is None:
        return {"type": "string"}
    origin = getattr(annotation, "__origin__", None)
    if annotation is str:    return {"type": "string"}
    if annotation is int:    return {"type": "integer"}
    if annotation is float:  return {"type": "number"}
    if annotation is bool:   return {"type": "boolean"}
    if annotation is list or origin is list: return {"type": "array", "items": {"type": "string"}}
    if annotation is dict or origin is dict: return {"type": "object"}
    # Optional[T] / T | None -> unwrap
    args = getattr(annotation, "__args__", None)
    if args:
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return _py_type_to_json(non_none[0])
    return {"type": "string"}


# Minimal docstring parser. Supports numpy/Google style, plus :param lines.
_PARAM_RE = re.compile(r"^\s*:param\s+(\w+)\s*:\s*(.+)$", re.MULTILINE)


def _parse_doc(doc: str) -> tuple[str, dict[str, str]]:
    """Return (top-level description, {param: description})."""
    if not doc:
        return "", {}
    lines = doc.strip().split("\n")
    # First paragraph = description
    desc_lines = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith((":param", ":return", "Args:", "Returns:", "Parameters", "-----")):
            break
        desc_lines.append(line)
    description = " ".join(desc_lines).strip()

    params: dict[str, str] = {}
    for m in _PARAM_RE.finditer(doc):
        params[m.group(1)] = m.group(2).strip()

    return description, params


def method_to_schema(method: Callable, fallback_name: str | None = None) -> dict | None:
    """Build an OpenAI tool schema dict from a method. Returns None if
    the method has no public parameters worth surfacing (edge case)."""
    try:
        sig = inspect.signature(method)
    except (ValueError, TypeError):
        return None

    doc = inspect.getdoc(method) or ""
    description, param_docs = _parse_doc(doc)

    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, p in sig.parameters.items():
        if name == "self" or name in _INJECTED_PARAMS:
            continue
        schema = _py_type_to_json(p.annotation)
        if name in param_docs:
            schema["description"] = param_docs[name]
        properties[name] = schema
        if p.default is inspect.Parameter.empty:
            required.append(name)

    name = fallback_name or method.__name__
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description or name,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


@dataclass
class ToolEntry:
    name: str
    module_name: str          # "calculator"
    method_name: str          # "calculate"
    schema: dict              # OpenAI tool schema
    handler: Callable         # bound method on the Tools instance
    default_enabled: bool = True
    requires_service: str | None = None
    group: str = "uncategorized"      # Topical taxonomy slug (config/tool_groups.yaml)
    subgroup: str = "general"         # Second-level taxonomy slug
    tier: str = TIER_NETWORK          # "host" or "network" — derived from requires_service


# Services considered safe to reach from an airgap environment because they
# are part of the local compose stack. Anything outside this list is treated
# as external and excluded from the tool schema while airgap mode is on.
_LOCAL_SERVICES: set[str] = {"qdrant", "redis", "llama_cpp"}


def _is_airgap_safe(entry: "ToolEntry") -> bool:
    """Conservative allow-list. A tool is airgap-safe only when it has no
    declared `requires_service` (and therefore no external network
    dependency declared), or when its declared service is part of the
    local compose stack."""
    svc = entry.requires_service
    if svc is None:
        # Many Open-WebUI tools fetch external HTTP APIs without declaring
        # a service. The safe default while airgap is on is to still
        # surface them — the model is local, it just won't get a real
        # response if the underlying network is blocked. Operators who
        # want stricter enforcement can annotate `requires_service` in
        # tools.yaml; those entries then get filtered here.
        return True
    return svc in _LOCAL_SERVICES


class ToolRegistry:
    def __init__(self):
        self.tools: dict[str, ToolEntry] = {}
        # Display config from tool_groups.yaml: {slug: {title, order, subgroups}}.
        self.groups: dict[str, Any] = {}

    def __contains__(self, name: str) -> bool:
        return name in self.tools

    def get(self, name: str) -> ToolEntry | None:
        return self.tools.get(name)

    def group_title(self, group: str, subgroup: str | None = None) -> str:
        """Resolve a group/subgroup slug to its display title, falling
        back to the slug itself when not declared in tool_groups.yaml."""
        g = self.groups.get(group, {})
        if subgroup is None:
            return (g or {}).get("title") or group.replace("_", " ").title()
        sg = (g.get("subgroups") or {}).get(subgroup, {})
        return sg.get("title") or subgroup.replace("_", " ").title()

    def group_order(self, group: str, subgroup: str | None = None) -> int:
        g = self.groups.get(group, {})
        if subgroup is None:
            return int((g or {}).get("order", 999))
        sg = (g.get("subgroups") or {}).get(subgroup, {})
        return int(sg.get("order", 999))

    @staticmethod
    def tier_title(tier: str) -> str:
        return _TIER_TITLES.get(tier, tier.title())

    @staticmethod
    def tier_order(tier: str) -> int:
        return _TIER_ORDER.get(tier, 999)

    def all_schemas(
        self, only_enabled: bool = True, *, airgap: bool = False,
        names: list[str] | set[str] | None = None,
    ) -> list[dict]:
        """Schemas to send to the model. When `airgap=True` we also drop
        every tool whose declared service is outside the local stack so
        the model never sees an offering it can't fulfil.

        When `names` is provided (e.g. from a chat request's
        `enabled_tools`), only those exact tool names are returned —
        ignoring `only_enabled` (the user explicitly opted in). Airgap
        still filters: a user-toggled tool that needs a remote service
        is silently dropped rather than offered-and-failing.
        """
        wanted: set[str] | None = set(names) if names is not None else None
        out: list[dict] = []
        for t in self.tools.values():
            if wanted is not None:
                if t.name not in wanted:
                    continue
            elif only_enabled and not t.default_enabled:
                continue
            if airgap and not _is_airgap_safe(t):
                continue
            out.append(t.schema)
        return out

    def enabled_names(self, *, airgap: bool = False) -> list[str]:
        return [
            t.name for t in self.tools.values()
            if t.default_enabled and (not airgap or _is_airgap_safe(t))
        ]

    def is_airgap_safe(self, name: str) -> bool:
        t = self.tools.get(name)
        return bool(t and _is_airgap_safe(t))


def _import_tool_module(path: Path) -> Any | None:
    """Import a `tools/foo.py` file as a module, returning the instantiated
    `Tools` object. Returns None on import failure."""
    module_name = f"_lai_tools.{path.stem}"
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if not spec or not spec.loader:
            return None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = mod
        spec.loader.exec_module(mod)
    except Exception as e:
        logger.warning("Failed to import tool file %s: %s", path.name, e)
        return None
    Tools = getattr(mod, "Tools", None)
    if not Tools:
        logger.debug("No 'Tools' class in %s — skipping", path.name)
        return None
    try:
        return Tools()
    except Exception as e:
        logger.warning("Failed to instantiate Tools() in %s: %s", path.name, e)
        return None


def _load_tools_yaml(config_dir: Path) -> dict[str, dict]:
    """Read every top-level dict whose values look like tool entries and
    merge them into a single name -> entry map.

    Historically tools.yaml used `tools:` for the original ~15 tools and
    `additional_tools:` (etc.) for everything else as a documentation
    aid. The registry treats them all equivalently — the section name
    is just a comment-like grouping aid, never surfaced anywhere."""
    path = config_dir / "tools.yaml"
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    merged: dict[str, dict] = {}
    for key, value in data.items():
        if not isinstance(value, dict):
            continue
        # A "tool entries" dict has nested dicts whose `file:` paths point
        # under tools/. The `middleware:` block doesn't (its files live
        # under backend/middleware/) and is documentation-only.
        entries = [v for v in value.values() if isinstance(v, dict) and "file" in v]
        if not entries:
            continue
        if not all(str(v["file"]).startswith("tools/") for v in entries):
            continue
        for name, entry in value.items():
            if name in merged:
                logger.warning(
                    "tools.yaml: %s defined in multiple sections — last definition wins", name,
                )
            merged[name] = entry
    return merged


def _load_tool_groups_yaml(config_dir: Path) -> dict[str, Any]:
    """Read the optional config/tool_groups.yaml mapping group/subgroup
    slugs to display titles and ordering. Returns an empty dict if the
    file is absent — callers should fall back to slug-as-title."""
    path = config_dir / "tool_groups.yaml"
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("groups", {}) or {}


def build_registry(
    tools_dir: Path,
    config_dir: Path | None = None,
    name_prefix: str = "",
) -> ToolRegistry:
    """Discover tools and build the registry.

    Tool-call names are namespaced as `<module>.<method>` so multiple
    modules can share method names without clashing. A single-method
    module with the same module+method name collapses to just `<module>`.
    """
    reg = ToolRegistry()
    if not tools_dir.exists():
        logger.warning("Tools directory not found: %s", tools_dir)
        return reg

    cfg_dir = config_dir or tools_dir.parent / "config"
    yaml_entries = _load_tools_yaml(cfg_dir)
    reg.groups = _load_tool_groups_yaml(cfg_dir)

    for py in sorted(tools_dir.glob("*.py")):
        if py.name.startswith("_") or py.name == "__init__.py":
            continue
        instance = _import_tool_module(py)
        if instance is None:
            continue

        module = py.stem
        yaml_entry = yaml_entries.get(module, {})
        default_enabled = bool(yaml_entry.get("default_enabled", True))
        requires_service = yaml_entry.get("requires_service")
        group = yaml_entry.get("group", "uncategorized")
        subgroup = yaml_entry.get("subgroup", "general")

        for meth_name, meth in inspect.getmembers(instance, predicate=inspect.ismethod):
            if meth_name.startswith("_"):
                continue
            name = f"{name_prefix}{module}.{meth_name}"
            schema = method_to_schema(meth, fallback_name=name)
            if schema is None:
                continue
            reg.tools[name] = ToolEntry(
                name=name,
                module_name=module,
                method_name=meth_name,
                schema=schema,
                handler=meth,
                default_enabled=default_enabled,
                requires_service=requires_service,
                group=group,
                subgroup=subgroup,
                tier=_classify_tier(requires_service),
            )

    logger.info("Loaded %d tool methods from %s", len(reg.tools), tools_dir)
    return reg
