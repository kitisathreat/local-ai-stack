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


class ToolRegistry:
    def __init__(self):
        self.tools: dict[str, ToolEntry] = {}

    def __contains__(self, name: str) -> bool:
        return name in self.tools

    def get(self, name: str) -> ToolEntry | None:
        return self.tools.get(name)

    def all_schemas(self, only_enabled: bool = True) -> list[dict]:
        return [t.schema for t in self.tools.values() if (not only_enabled or t.default_enabled)]

    def enabled_names(self) -> list[str]:
        return [t.name for t in self.tools.values() if t.default_enabled]


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
    path = config_dir / "tools.yaml"
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("tools", {}) or {}


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

    yaml_entries = _load_tools_yaml(config_dir or tools_dir.parent / "config")

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
            )

    logger.info("Loaded %d tool methods from %s", len(reg.tools), tools_dir)
    return reg
