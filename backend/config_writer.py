"""YAML config-file persistence + whitelisted PATCH helpers.

Pulled out of `admin.py` (which had grown to 1,150+ lines mixing route
handlers and disk persistence). The PATCH /admin/config endpoint and
the per-section writers in here are the only code paths that mutate
the YAML files on disk; everything else reads through `AppConfig`.

Each `patch_*` function takes a request body fragment and an in-memory
YAML dict, mutates the dict in-place, and returns the list of
dotted-paths that changed (for the audit log). `patch_tiers` also
returns the set of tier names that need a scheduler respawn;
`patch_concurrency` returns whether the change requires a process
restart (workers_target / redis_url are set at uvicorn launch time).

Whitelisting is by-field: unknown fields are silently ignored so a
partial PATCH from a stale dashboard doesn't error.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import yaml


def atomic_write_yaml(path: Path, data: dict) -> None:
    """Write `data` to `path` via tmp-file + rename. Crash-safe: a
    half-written tmp file never replaces the live config."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=str(path.parent), delete=False, suffix=".tmp",
    ) as f:
        yaml.safe_dump(data, f, sort_keys=False, default_flow_style=False)
        tmp = Path(f.name)
    tmp.replace(path)


def load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def set_deep(obj: dict, path: list[str], value: Any) -> None:
    cur = obj
    for key in path[:-1]:
        if key not in cur or not isinstance(cur[key], dict):
            cur[key] = {}
        cur = cur[key]
    cur[path[-1]] = value


# ── Per-section patchers ────────────────────────────────────────────────
#
# Each handler mutates the in-memory YAML dict for one config file and
# returns the set of changes it made (for audit log). Unknown fields are
# ignored silently so a partial PATCH is safe.


def patch_vram(patch: dict, doc: dict) -> list[str]:
    changes: list[str] = []
    def _apply(key_in: str, yaml_path: list[str], caster=lambda x: x):
        if key_in in patch and patch[key_in] is not None:
            set_deep(doc, yaml_path, caster(patch[key_in]))
            changes.append("vram." + ".".join(yaml_path))
    _apply("total_vram_gb", ["total_vram_gb"], float)
    _apply("headroom_gb", ["headroom_gb"], float)
    _apply("poll_interval_sec", ["poll_interval_sec"], int)
    ev = patch.get("eviction") or {}
    if "policy" in ev: set_deep(doc, ["eviction", "policy"], str(ev["policy"])); changes.append("vram.eviction.policy")
    if "min_residency_sec" in ev: set_deep(doc, ["eviction", "min_residency_sec"], int(ev["min_residency_sec"])); changes.append("vram.eviction.min_residency_sec")
    if "pin_orchestrator" in ev: set_deep(doc, ["eviction", "pin_orchestrator"], bool(ev["pin_orchestrator"])); changes.append("vram.eviction.pin_orchestrator")
    if "pin_vision" in ev: set_deep(doc, ["eviction", "pin_vision"], bool(ev["pin_vision"])); changes.append("vram.eviction.pin_vision")
    q = patch.get("queue") or {}
    if "max_depth_per_tier" in q:
        v = max(0, min(int(q["max_depth_per_tier"]), 1000))
        set_deep(doc, ["queue", "max_depth_per_tier"], v)
        changes.append("vram.queue.max_depth_per_tier")
    if "max_wait_sec" in q:
        v = max(1, min(int(q["max_wait_sec"]), 600))
        set_deep(doc, ["queue", "max_wait_sec"], v)
        changes.append("vram.queue.max_wait_sec")
    if "position_update_interval_sec" in q:
        v = max(1, min(int(q["position_update_interval_sec"]), 30))
        set_deep(doc, ["queue", "position_update_interval_sec"], v)
        changes.append("vram.queue.position_update_interval_sec")
    return changes


def patch_router(patch: dict, doc: dict) -> list[str]:
    changes: list[str] = []
    ats = patch.get("auto_thinking_signals") or {}
    if "enable_when_any" in ats:
        doc.setdefault("auto_thinking_signals", {})["enable_when_any"] = [
            {"regex": r["regex"]} for r in ats["enable_when_any"]
            if isinstance(r, dict) and r.get("regex")
        ]
        changes.append("router.auto_thinking_signals.enable_when_any")
    if "disable_when_any" in ats:
        doc.setdefault("auto_thinking_signals", {})["disable_when_any"] = [
            {"regex": r["regex"]} for r in ats["disable_when_any"]
            if isinstance(r, dict) and r.get("regex")
        ]
        changes.append("router.auto_thinking_signals.disable_when_any")
    ma = patch.get("multi_agent") or {}
    if "max_workers" in ma:
        v = max(1, min(int(ma["max_workers"]), 8))
        doc.setdefault("multi_agent", {})["max_workers"] = v
        changes.append("router.multi_agent.max_workers")
    if "min_workers" in ma:
        v = max(1, min(int(ma["min_workers"]), 8))
        doc.setdefault("multi_agent", {})["min_workers"] = v
        changes.append("router.multi_agent.min_workers")
    if "worker_tier" in ma:
        doc.setdefault("multi_agent", {})["worker_tier"] = str(ma["worker_tier"])
        changes.append("router.multi_agent.worker_tier")
    if "orchestrator_tier" in ma:
        doc.setdefault("multi_agent", {})["orchestrator_tier"] = str(ma["orchestrator_tier"])
        changes.append("router.multi_agent.orchestrator_tier")
    if "reasoning_workers" in ma:
        doc.setdefault("multi_agent", {})["reasoning_workers"] = bool(ma["reasoning_workers"])
        changes.append("router.multi_agent.reasoning_workers")
    if "interaction_mode" in ma:
        mode = str(ma["interaction_mode"]).lower()
        if mode not in ("independent", "collaborative"):
            mode = "independent"
        doc.setdefault("multi_agent", {})["interaction_mode"] = mode
        changes.append("router.multi_agent.interaction_mode")
    if "interaction_rounds" in ma:
        v = max(0, min(int(ma["interaction_rounds"]), 4))
        doc.setdefault("multi_agent", {})["interaction_rounds"] = v
        changes.append("router.multi_agent.interaction_rounds")
    return changes


def patch_auth(patch: dict, doc: dict) -> list[str]:
    changes: list[str] = []
    if "allowed_email_domains" in patch:
        val = patch["allowed_email_domains"]
        if isinstance(val, str):
            val = [d.strip() for d in val.split(",") if d.strip()]
        doc["allowed_email_domains"] = [str(d).lower() for d in val]
        changes.append("auth.allowed_email_domains")
    rl = patch.get("rate_limits") or {}
    if "requests_per_hour_per_ip" in rl:
        doc.setdefault("rate_limits", {})["requests_per_hour_per_ip"] = int(rl["requests_per_hour_per_ip"])
        changes.append("auth.rate_limits.requests_per_hour_per_ip")
    if "requests_per_minute_per_user" in rl:
        v = max(0, min(int(rl["requests_per_minute_per_user"]), 10_000))
        doc.setdefault("rate_limits", {})["requests_per_minute_per_user"] = v
        changes.append("auth.rate_limits.requests_per_minute_per_user")
    if "requests_per_day_per_user" in rl:
        v = max(0, min(int(rl["requests_per_day_per_user"]), 1_000_000))
        doc.setdefault("rate_limits", {})["requests_per_day_per_user"] = v
        changes.append("auth.rate_limits.requests_per_day_per_user")
    ses = patch.get("session") or {}
    if "cookie_ttl_days" in ses:
        doc.setdefault("session", {})["cookie_ttl_days"] = int(ses["cookie_ttl_days"])
        changes.append("auth.session.cookie_ttl_days")
    return changes


def patch_tiers(patch: dict, doc: dict) -> tuple[list[str], set[str]]:
    """Patch a subset of fields on existing tiers in models.yaml.

    Only allows edits to fields the dashboard shows: context_window,
    think_default, vram_estimate_gb, description, parallel_slots, plus
    llama.cpp spawn-time knobs (n_gpu_layers, flash_attention,
    cache_type_k, cache_type_v) and a flat `params` dict
    (temperature/top_p/top_k/num_predict). New tiers cannot be created
    this way.

    Returns (changes, dirty_tiers). `dirty_tiers` is the set of tier names
    whose load-time parameters changed — the caller calls
    `scheduler.mark_tier_dirty()` on each so the scheduler respawns them on
    next reserve.
    """
    changes: list[str] = []
    dirty: set[str] = set()
    tiers_doc = doc.get("tiers") or {}
    for name, body in (patch or {}).items():
        if name not in tiers_doc or not isinstance(body, dict):
            continue
        t = tiers_doc[name]
        for k, caster in (
            ("description", str), ("context_window", int),
            ("think_default", bool), ("vram_estimate_gb", float),
        ):
            if k in body:
                t[k] = caster(body[k])
                changes.append(f"models.tiers.{name}.{k}")
                if k == "context_window":
                    dirty.add(name)
        if "parallel_slots" in body:
            v = max(1, min(int(body["parallel_slots"]), 16))
            if t.get("parallel_slots") != v:
                t["parallel_slots"] = v
                changes.append(f"models.tiers.{name}.parallel_slots")
                dirty.add(name)
        # llama.cpp spawn-time knobs — change forces process respawn.
        for k, caster in (
            ("n_gpu_layers", int),
            ("flash_attention", bool),
            ("cache_type_k", str),
            ("cache_type_v", str),
        ):
            if k in body:
                t[k] = caster(body[k])
                changes.append(f"models.tiers.{name}.{k}")
                dirty.add(name)
        if "params" in body and isinstance(body["params"], dict):
            t.setdefault("params", {})
            for pk, pv in body["params"].items():
                if pv is None:
                    t["params"].pop(pk, None)
                    changes.append(f"models.tiers.{name}.params.{pk}=null")
                else:
                    t["params"][pk] = pv
                    changes.append(f"models.tiers.{name}.params.{pk}")
    return changes, dirty


def patch_concurrency(patch: dict, doc: dict) -> tuple[list[str], bool]:
    """Patch runtime.yaml (workers_target, redis_url). Returns (changes,
    requires_restart). Workers and redis_url need a container restart to
    take effect because Uvicorn is launched with --workers at startup."""
    changes: list[str] = []
    requires_restart = False
    if "workers_target" in patch and patch["workers_target"] is not None:
        v = max(1, min(int(patch["workers_target"]), 16))
        if doc.get("workers_target") != v:
            doc["workers_target"] = v
            changes.append("concurrency.workers_target")
            requires_restart = True
    if "redis_url" in patch:
        val = patch["redis_url"]
        if val is None or (isinstance(val, str) and not val.strip()):
            if doc.get("redis_url"):
                requires_restart = True
            doc["redis_url"] = None
        else:
            val = str(val).strip()
            if doc.get("redis_url") != val:
                doc["redis_url"] = val
                requires_restart = True
        changes.append("concurrency.redis_url")
    return changes, requires_restart
