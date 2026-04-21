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

#27: cold-boot is dominated by importing every `tools/*.py` file at
startup (50ms × 90 files). We cache the built schema list to
`.tools_cache.json` keyed by source mtimes; on cache hit we skip every
import until a tool is actually dispatched. The lazy handler imports
the module on first call. Set `LAI_TOOL_CACHE=0` to disable.
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import logging
import os
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
    # Serializable copy of the handler's injected-param set. Populated
    # alongside the eager schema introspection so `_LazyHandler` can
    # surface the same info without re-importing the module (#27).
    injected_params: tuple[str, ...] = ()


class _LazyHandler:
    """Deferred import of a tool module.

    At startup (on a cache hit) we skip importing every `tools/*.py`; we
    only know the (module_path, method_name) pair. The first dispatch
    imports + instantiates + method-binds. Subsequent calls reuse the
    cached method. `injected_params` mirrors what the executor inspects
    so the lazy handler Just Works with it."""

    __slots__ = ("_path", "_method_name", "_injected_params", "_bound")

    def __init__(
        self,
        path: Path,
        method_name: str,
        injected_params: tuple[str, ...] = (),
    ):
        self._path = path
        self._method_name = method_name
        self._injected_params = injected_params
        self._bound: Callable | None = None

    def _load(self) -> Callable:
        if self._bound is not None:
            return self._bound
        instance = _import_tool_module(self._path)
        if instance is None:
            raise RuntimeError(
                f"Failed to import tool module {self._path.name} on first dispatch"
            )
        meth = getattr(instance, self._method_name, None)
        if meth is None:
            raise RuntimeError(
                f"Method {self._method_name!r} missing from {self._path.name}"
            )
        self._bound = meth
        return meth

    def __call__(self, *args, **kwargs):
        return self._load()(*args, **kwargs)

    # inspect.signature(handler) is used by the executor to decide on
    # injected params. Expose the cached set directly.
    @property
    def injected_params(self) -> tuple[str, ...]:
        return self._injected_params


# Services considered safe to reach from an airgap environment because they
# are part of the local compose stack. Anything outside this list is treated
# as external and excluded from the tool schema while airgap mode is on.
_LOCAL_SERVICES: set[str] = {"ollama", "qdrant", "redis", "llama_cpp"}


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

    def __contains__(self, name: str) -> bool:
        return name in self.tools

    def get(self, name: str) -> ToolEntry | None:
        return self.tools.get(name)

    def all_schemas(
        self, only_enabled: bool = True, *, airgap: bool = False,
    ) -> list[dict]:
        """Schemas to send to the model. When `airgap=True` we also drop
        every tool whose declared service is outside the local stack so
        the model never sees an offering it can't fulfil."""
        out: list[dict] = []
        for t in self.tools.values():
            if only_enabled and not t.default_enabled:
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
    path = config_dir / "tools.yaml"
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("tools", {}) or {}


# ── Registry cache (#27) ──────────────────────────────────────────────────

_CACHE_VERSION = 1


def _cache_key(tools_dir: Path, config_dir: Path | None) -> str:
    """Hash of (python filename, size, mtime_ns) for every tool file +
    tools.yaml. Invalidates automatically when anything under tools/
    changes on disk."""
    parts: list[str] = [f"v{_CACHE_VERSION}"]
    for py in sorted(tools_dir.glob("*.py")):
        if py.name.startswith("_"):
            continue
        st = py.stat()
        parts.append(f"{py.name}:{st.st_size}:{st.st_mtime_ns}")
    tools_yaml = (config_dir or tools_dir.parent / "config") / "tools.yaml"
    if tools_yaml.exists():
        st = tools_yaml.stat()
        parts.append(f"tools.yaml:{st.st_size}:{st.st_mtime_ns}")
    h = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return h[:16]


def _cache_path(tools_dir: Path) -> Path:
    # Stashed inside `data/` so it's persisted across container restarts
    # but outside any read-only mount of tools_dir.
    base = Path(os.environ.get("LAI_DB_PATH", "/app/data/lai.db")).parent
    base.mkdir(parents=True, exist_ok=True)
    return base / ".tools_cache.json"


def _cache_enabled() -> bool:
    return os.environ.get("LAI_TOOL_CACHE", "1").lower() not in {"0", "false", "no"}


def _try_load_cache(tools_dir: Path, config_dir: Path | None) -> dict | None:
    if not _cache_enabled():
        return None
    path = _cache_path(tools_dir)
    if not path.exists():
        return None
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if blob.get("key") != _cache_key(tools_dir, config_dir):
        return None
    return blob


def _save_cache(
    tools_dir: Path, config_dir: Path | None, entries: list[dict],
) -> None:
    if not _cache_enabled():
        return
    path = _cache_path(tools_dir)
    blob = {
        "key": _cache_key(tools_dir, config_dir),
        "entries": entries,
    }
    try:
        path.write_text(json.dumps(blob), encoding="utf-8")
    except OSError as e:
        logger.debug("Failed to write tool cache %s: %s", path, e)


def _injected_params_of(method: Callable) -> tuple[str, ...]:
    """Return the subset of _INJECTED_PARAMS that the method declares."""
    try:
        sig = inspect.signature(method)
    except (ValueError, TypeError):
        return ()
    return tuple(p for p in sig.parameters if p in _INJECTED_PARAMS)


def build_registry(
    tools_dir: Path,
    config_dir: Path | None = None,
    name_prefix: str = "",
) -> ToolRegistry:
    """Discover tools and build the registry.

    Tool-call names are namespaced as `<module>.<method>` so multiple
    modules can share method names without clashing. A single-method
    module with the same module+method name collapses to just `<module>`.

    Lazy-load path (#27): when `.tools_cache.json` is present and its
    key matches (filenames + mtimes), we skip every `tools/*.py` import
    at startup and hydrate the registry with `_LazyHandler` handlers that
    import on first dispatch. Falls through to eager discovery on a
    cache miss — which then writes a fresh cache for next time.
    """
    reg = ToolRegistry()
    if not tools_dir.exists():
        logger.warning("Tools directory not found: %s", tools_dir)
        return reg

    cached = _try_load_cache(tools_dir, config_dir)
    if cached is not None:
        for e in cached.get("entries", []):
            try:
                path = tools_dir / f"{e['module_name']}.py"
                reg.tools[e["name"]] = ToolEntry(
                    name=e["name"],
                    module_name=e["module_name"],
                    method_name=e["method_name"],
                    schema=e["schema"],
                    handler=_LazyHandler(
                        path, e["method_name"], tuple(e.get("injected_params") or ()),
                    ),
                    default_enabled=bool(e.get("default_enabled", True)),
                    requires_service=e.get("requires_service"),
                    injected_params=tuple(e.get("injected_params") or ()),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("tool cache entry %r invalid: %s", e.get("name"), exc)
        logger.info(
            "Loaded %d tool methods from cache (lazy)", len(reg.tools),
        )
        return reg

    yaml_entries = _load_tools_yaml(config_dir or tools_dir.parent / "config")
    cache_entries: list[dict] = []

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
            injected = _injected_params_of(meth)
            reg.tools[name] = ToolEntry(
                name=name,
                module_name=module,
                method_name=meth_name,
                schema=schema,
                handler=meth,
                default_enabled=default_enabled,
                requires_service=requires_service,
                injected_params=injected,
            )
            cache_entries.append({
                "name": name,
                "module_name": module,
                "method_name": meth_name,
                "schema": schema,
                "default_enabled": default_enabled,
                "requires_service": requires_service,
                "injected_params": list(injected),
            })

    _save_cache(tools_dir, config_dir, cache_entries)
    logger.info("Loaded %d tool methods from %s", len(reg.tools), tools_dir)
    return reg
