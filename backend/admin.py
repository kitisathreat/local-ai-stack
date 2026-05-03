"""Admin dashboard API.

Access model (Phase 3, password auth):
    - Users have an `is_admin` boolean column. The `require_admin`
      dependency gates every /admin/* route by that flag.
    - "Admin configured" means at least one is_admin=1 user exists.
      If none do, the Qt admin window prompts for first-run creation
      via `backend.seed_admin`.

Endpoints mounted under /admin:
    GET    /admin/me                 - {username, email, is_admin, admin_configured}
    GET    /admin/overview           - counters + totals
    GET    /admin/usage              - bucketed time series
    GET    /admin/usage/by_tier
    GET    /admin/usage/by_user
    GET    /admin/errors             - recent error events
    GET    /admin/users              - full user list
    POST   /admin/users              - create a new user (admin-only)
    PATCH  /admin/users/{id}         - edit username/email/password/is_admin
    DELETE /admin/users/{id}         - hard-delete a user (cascades)
    GET    /admin/config             - current config snapshot
    PATCH  /admin/config             - apply patches, write YAML, hot-reload
    GET    /admin/tools              - tool registry with enabled flags
    PATCH  /admin/tools/{name}       - enable/disable a tool (memory-only)
    POST   /admin/reload             - force reload config from disk
    GET    /admin/airgap             - airgap state
    PATCH  /admin/airgap             - toggle airgap mode

Config writes are guarded: only a whitelisted set of YAML paths can change,
and each file is rewritten atomically (tmp-file + rename).
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

import aiosqlite
import yaml
from fastapi import APIRouter, Depends, HTTPException, Request

from . import airgap, auth, db, metrics, passwords
from .config import AppConfig, CONFIG_DIR
from .schemas import CreateUserRequest, UpdateUserRequest


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


# ── Role gate ────────────────────────────────────────────────────────────

async def require_admin(user: dict = Depends(auth.current_user)) -> dict:
    if not user.get("is_admin"):
        raise HTTPException(403, "Not an admin account")
    return user


# ── Me ──────────────────────────────────────────────────────────────────

@router.get("/me")
async def admin_me(user: dict = Depends(auth.current_user)):
    admin_count = await db.count_admins()
    return {
        "username": user.get("username", ""),
        "email": user["email"],
        "is_admin": bool(user.get("is_admin")),
        "admin_configured": admin_count > 0,
    }


# ── Metrics ─────────────────────────────────────────────────────────────

@router.get("/overview")
async def overview(
    window: int = 86400,
    _: dict = Depends(require_admin),
):
    data = await metrics.overview(window_seconds=window)
    return data


# ── Model pull progress ──────────────────────────────────────────────────
#
# The admin GUI's Models tab renders a progress bar per tier. Source of
# truth for "expected size" is the HuggingFace API (siblings.size on the
# repo). Source for "downloaded" is whichever is bigger between:
#   1. The on-disk <tier>.gguf (after symlink resolution), or
#   2. The largest blob (or .incomplete blob) in the HF cache directory
#      for that repo — that's where huggingface_hub streams partial
#      downloads before promoting them to a final symlink.
#
# Expected sizes are looked up once per repo and cached in-process to
# avoid hammering HF on every poll.

_expected_size_cache: dict[tuple[str, str], int | None] = {}


def _resolved_models_manifest() -> dict[str, dict]:
    data_dir = Path(os.getenv("LAI_DATA_DIR") or
                    Path(__file__).resolve().parent.parent / "data")
    path = data_dir / "resolved-models.json"
    if not path.exists():
        return {}
    try:
        import json
        return (json.loads(path.read_text(encoding="utf-8")).get("tiers") or {})
    except (OSError, ValueError):
        return {}


def _hf_expected_size(repo: str, filename: str) -> int | None:
    """Lookup expected file size from HF API. Cached after first call."""
    key = (repo, filename)
    if key in _expected_size_cache:
        return _expected_size_cache[key]
    size: int | None = None
    try:
        from huggingface_hub import HfApi
        info = HfApi().model_info(repo, files_metadata=True)
        for s in (info.siblings or []):
            if getattr(s, "rfilename", None) == filename:
                size = getattr(s, "size", None) or getattr(s, "lfs", {}).get("size")
                break
    except Exception:
        size = None
    _expected_size_cache[key] = size
    return size


def _hf_cache_partial_size(repo: str) -> int:
    """Largest file in the HF cache blobs dir for this repo. Reflects an
    in-flight download even before it's symlinked into snapshots/."""
    try:
        cache = Path.home() / ".cache" / "huggingface" / "hub"
        # huggingface_hub mangles repo ids: "lmstudio-community/Qwen3.6-35B-A3B-GGUF"
        # → "models--lmstudio-community--Qwen3.6-35B-A3B-GGUF"
        repo_dir = cache / f"models--{repo.replace('/', '--')}"
        blobs = repo_dir / "blobs"
        if not blobs.exists():
            return 0
        biggest = 0
        for f in blobs.iterdir():
            if f.is_file():
                try:
                    biggest = max(biggest, f.stat().st_size)
                except OSError:
                    pass
        return biggest
    except OSError:
        return 0


@router.get("/model-pull-status")
async def model_pull_status(_: dict = Depends(require_admin)):
    """Per-tier download progress for the admin GUI Models tab.

    Returns a map of tier name → {downloaded_bytes, expected_bytes,
    percent, complete, in_progress, repo, filename}. The chat dropdown's
    `available` flag and this endpoint are independent: a tier is
    `available` only when its <tier>.gguf is on disk; `in_progress` is
    inferred from a non-trivial partial blob in the HF cache."""
    from backend import state as _state
    cfg = _state.config
    manifest = _resolved_models_manifest()
    out: dict[str, dict] = {}
    data_dir = Path(os.getenv("LAI_DATA_DIR") or
                    Path(__file__).resolve().parent.parent / "data")
    for tier_name in (cfg.models.tiers or {}).keys():
        info = manifest.get(tier_name) or {}
        repo = info.get("repo") or ""
        filename = info.get("filename") or ""
        expected = _hf_expected_size(repo, filename) if repo and filename else None
        # Disk file (final or symlink target)
        on_disk = 0
        target = data_dir / "models" / f"{tier_name}.gguf"
        try:
            if target.exists():
                # Resolve symlink to actual blob to get the real size.
                real = target.resolve()
                if real.exists():
                    on_disk = real.stat().st_size
                else:
                    on_disk = target.stat().st_size
        except OSError:
            pass
        partial = _hf_cache_partial_size(repo) if repo else 0
        downloaded = max(on_disk, partial)
        complete = bool(expected) and downloaded >= expected
        # Treat anything > 100 MB as a real download (filters out the
        # tiny resolved-models.json manifest from being mistaken for a
        # GGUF if naming is off).
        in_progress = (not complete) and downloaded > 0
        percent: float | None = None
        if expected and expected > 0:
            percent = min(100.0, (downloaded / expected) * 100.0)
        out[tier_name] = {
            "downloaded_bytes": downloaded,
            "expected_bytes": expected,
            "percent": percent,
            "complete": complete,
            "in_progress": in_progress,
            "repo": repo,
            "filename": filename,
        }
    return out


@router.get("/usage")
async def usage(
    window: int = 86400,
    buckets: int = 48,
    _: dict = Depends(require_admin),
):
    buckets = max(6, min(buckets, 240))
    return await metrics.timeseries(window_seconds=window, buckets=buckets)


@router.get("/usage/by_tier")
async def usage_by_tier(window: int = 86400, _: dict = Depends(require_admin)):
    return {"data": await metrics.by_tier(window_seconds=window)}


@router.get("/usage/by_user")
async def usage_by_user(window: int = 86400, limit: int = 50,
                       _: dict = Depends(require_admin)):
    return {"data": await metrics.by_user(window_seconds=window, limit=limit)}


@router.get("/errors")
async def errors(limit: int = 25, _: dict = Depends(require_admin)):
    return {"data": await metrics.recent_errors(limit=limit)}


# ── Users ───────────────────────────────────────────────────────────────

@router.get("/users")
async def users(_: dict = Depends(require_admin)):
    async with db.get_conn() as c:
        rows = await (await c.execute(
            "SELECT u.id, u.username, u.email, u.is_admin, u.created_at, u.last_login_at, "
            "       (SELECT COUNT(*) FROM conversations c WHERE c.user_id = u.id) AS conversations, "
            "       (SELECT COUNT(*) FROM memories m WHERE m.user_id = u.id) AS memories, "
            "       (SELECT COUNT(*) FROM rag_docs r WHERE r.user_id = u.id) AS rag_docs "
            "FROM users u ORDER BY u.last_login_at DESC NULLS LAST",
        )).fetchall()
    data = []
    for r in rows:
        d = dict(r)
        d["is_admin"] = bool(d.get("is_admin"))
        data.append(d)
    return {"data": data}


@router.post("/users")
async def create_user(body: CreateUserRequest, _: dict = Depends(require_admin)):
    if not body.password or len(body.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    if not body.username.strip():
        raise HTTPException(400, "Username must not be empty")
    try:
        user = await db.create_user(
            username=body.username,
            email=body.email,
            password_hash=passwords.hash_password(body.password),
            is_admin=body.is_admin,
        )
    except aiosqlite.IntegrityError:
        raise HTTPException(409, "Username or email already exists")
    # Strip the hash from the response.
    user.pop("password_hash", None)
    return user


@router.patch("/users/{user_id}")
async def patch_user(
    user_id: int, body: UpdateUserRequest, actor: dict = Depends(require_admin),
):
    if user_id == actor["id"] and body.is_admin is False:
        raise HTTPException(400, "Refusing to revoke your own admin privileges")
    existing = await db.get_user(user_id)
    if not existing:
        raise HTTPException(404, "User not found")
    if body.username is not None or body.email is not None:
        try:
            await db.update_user_fields(
                user_id, username=body.username, email=body.email,
            )
        except aiosqlite.IntegrityError:
            raise HTTPException(409, "Username or email already exists")
    if body.password:
        if len(body.password) < 8:
            raise HTTPException(400, "Password must be at least 8 characters")
        await db.set_user_password(user_id, passwords.hash_password(body.password))
    if body.is_admin is not None:
        await db.set_user_admin(user_id, body.is_admin)
    user = await db.get_user(user_id)
    if user:
        user.pop("password_hash", None)
    return user


@router.delete("/users/{user_id}")
async def delete_user(user_id: int, actor: dict = Depends(require_admin)):
    if user_id == actor["id"]:
        raise HTTPException(400, "Refusing to delete your own admin account")
    ok = await db.delete_user(user_id)
    if not ok:
        raise HTTPException(404, "User not found")
    return {"ok": True}


# ── VRAM + tools passthrough (admin view) ───────────────────────────────

@router.get("/vram")
async def vram_status(request: Request, _: dict = Depends(require_admin)):
    scheduler = request.app.state.scheduler if hasattr(request.app.state, "scheduler") else None
    # main.py holds scheduler on the module-level `state`, not app.state.
    # Fall back to importing it:
    from . import main as backend_main
    return await backend_main.state.scheduler.status()


@router.get("/vram/probe")
async def vram_probe(_: dict = Depends(require_admin)):
    """Diagnostic snapshot comparing the scheduler's VRAM bookkeeping
    with the GPU's actual allocation. The interesting field is
    ``orphan_drift_gb``: if it's > 0.5 GB on a quiet system, something
    other than this backend is holding VRAM (orphan llama-server, an
    external process, a leaked allocation). The startup orphan-sweep
    handles the common case; this endpoint is what you hit when the
    scheduler's "won't fit" decision disagrees with what nvidia-smi
    shows."""
    from . import main as backend_main
    sched = backend_main.state.scheduler
    total = sched.vram.total_vram_gb
    actual_free = sched.probe.free_gb(total)
    actual_used = max(0.0, total - actual_free) if actual_free <= total else 0.0
    async with sched._lock:
        tracked_used = sum(
            m.effective_cost() for m in sched.loaded.values()
            if m.state.value != "evicting"
        )
        loaded_summary = [
            {
                "tier_id": m.tier_id,
                "state": m.state.value,
                "refcount": m.refcount,
                "estimate_gb": m.vram_estimate_gb,
                "observed_gb": m.observed_cost_gb,
            }
            for m in sched.loaded.values()
        ]
    drift = round(actual_used - tracked_used, 3)
    # Best-effort orphan listing — purely diagnostic, never kills.
    try:
        from .backends.llama_cpp import _list_llama_server_pids
        all_llama = await _list_llama_server_pids()
        tracked_pids = {
            p.popen.pid for p in backend_main.state.llama_cpp.processes.values()
            if p.popen is not None and p.popen.poll() is None
        }
        orphans = [
            {"pid": pid, "port": port}
            for pid, port in all_llama if pid not in tracked_pids
        ]
    except Exception:
        orphans = []
    return {
        "total_vram_gb": total,
        "nvml_free_gb": round(actual_free, 3),
        "nvml_used_gb": round(actual_used, 3),
        "scheduler_tracked_used_gb": round(tracked_used, 3),
        "orphan_drift_gb": drift,
        "loaded": loaded_summary,
        "orphan_llama_server_pids": orphans,
        "headroom_gb": sched.vram.headroom_gb,
    }


@router.post("/vram/kill-orphans")
async def vram_kill_orphans(_: dict = Depends(require_admin)):
    """Force a sweep of orphan llama-server processes. Same logic that
    runs at startup, exposed for after-the-fact cleanup when you can
    see drift in /admin/vram/probe but the backend hasn't bounced."""
    from . import main as backend_main
    preserve = set()
    for tier in backend_main.state.config.models.tiers.values():
        if getattr(tier, "pinned", False) and getattr(tier, "port", None):
            preserve.add(int(tier.port))
    preserve.add(8091)
    killed = await backend_main.state.llama_cpp.kill_orphans(preserve_ports=preserve)
    return {"killed_pids": killed, "preserved_ports": sorted(preserve)}


@router.get("/tools")
async def list_tools(_: dict = Depends(require_admin)):
    from . import main as backend_main
    reg = backend_main.state.tools
    return {
        "groups": backend_main._serialize_taxonomy(reg),
        "data": [
            {
                "name": t.name,
                "description": t.schema.get("function", {}).get("description", ""),
                "default_enabled": t.default_enabled,
                "enabled": t.default_enabled,
                "requires_service": t.requires_service,
                "tier": t.tier,
                "tier_title": reg.tier_title(t.tier),
                "group": t.group,
                "group_title": reg.group_title(t.group),
                "subgroup": t.subgroup,
                "subgroup_title": reg.group_title(t.group, t.subgroup),
            }
            for t in reg.tools.values()
        ],
    }


@router.patch("/tools/{name}")
async def toggle_tool(name: str, body: dict, _: dict = Depends(require_admin)):
    from . import main as backend_main
    tool = backend_main.state.tools.tools.get(name)
    if not tool:
        raise HTTPException(404, f"Tool not found: {name}")
    enabled = bool(body.get("enabled", True))
    tool.default_enabled = enabled
    return {"ok": True, "name": name, "enabled": enabled}


@router.patch("/tools")
async def bulk_toggle_tools(body: dict, _: dict = Depends(require_admin)):
    """Enable or disable multiple tools at once. The body accepts either
    `names` (an explicit list) or `tier` + `group` + `subgroup` filters
    (any subset). All tools matching the filter set are flipped to
    `enabled`. Returns the list of names that were changed."""
    from . import main as backend_main
    reg = backend_main.state.tools
    enabled = bool(body.get("enabled", True))

    names = set(body.get("names") or [])
    tier = body.get("tier")
    group = body.get("group")
    subgroup = body.get("subgroup")

    matches: list[str] = []
    for t in reg.tools.values():
        if names:
            if t.name not in names:
                continue
        else:
            if tier and t.tier != tier:
                continue
            if group and t.group != group:
                continue
            if subgroup and t.subgroup != subgroup:
                continue
        t.default_enabled = enabled
        matches.append(t.name)

    return {"ok": True, "enabled": enabled, "changed": matches, "count": len(matches)}


# ── Config GET ──────────────────────────────────────────────────────────

@router.get("/config")
async def get_config(_: dict = Depends(require_admin)):
    from . import main as backend_main
    cfg: AppConfig = backend_main.state.config
    redis_client = getattr(backend_main.state, "redis", None)
    redis_healthy = False
    if redis_client is not None:
        try:
            await redis_client.ping()
            redis_healthy = True
        except Exception:
            redis_healthy = False
    return {
        "vram": {
            "total_vram_gb": cfg.vram.total_vram_gb,
            "headroom_gb": cfg.vram.headroom_gb,
            "poll_interval_sec": cfg.vram.poll_interval_sec,
            "eviction": {
                "policy": cfg.vram.eviction.policy,
                "min_residency_sec": cfg.vram.eviction.min_residency_sec,
                "pin_orchestrator": cfg.vram.eviction.pin_orchestrator,
                "pin_vision": cfg.vram.eviction.pin_vision,
            },
            "queue": {
                "max_depth_per_tier": cfg.vram.queue.max_depth_per_tier,
                "max_wait_sec": cfg.vram.queue.max_wait_sec,
                "position_update_interval_sec": cfg.vram.queue.position_update_interval_sec,
            },
        },
        "concurrency": {
            "workers_target": cfg.concurrency.workers_target,
            "workers_running": int(os.getenv("BACKEND_WORKERS", "1")),
            "redis_url_set": bool(cfg.concurrency.redis_url),
            "redis_healthy": redis_healthy,
        },
        "router": {
            "auto_thinking_signals": {
                "enable_when_any": [
                    {"regex": r.regex} for r in cfg.router.auto_thinking_signals.enable_when_any
                    if r.regex
                ],
                "disable_when_any": [
                    {"regex": r.regex} for r in cfg.router.auto_thinking_signals.disable_when_any
                    if r.regex
                ],
            },
            "multi_agent": {
                "max_workers": cfg.router.multi_agent.max_workers,
                "min_workers": cfg.router.multi_agent.min_workers,
                "worker_tier": cfg.router.multi_agent.worker_tier,
                "orchestrator_tier": cfg.router.multi_agent.orchestrator_tier,
                "reasoning_workers": cfg.router.multi_agent.reasoning_workers,
                "interaction_mode": cfg.router.multi_agent.interaction_mode,
                "interaction_rounds": cfg.router.multi_agent.interaction_rounds,
            },
        },
        "auth": {
            "allowed_email_domains": list(cfg.auth.allowed_email_domains),
            "rate_limits": {
                "requests_per_hour_per_ip": cfg.auth.rate_limits.requests_per_hour_per_ip,
                "requests_per_minute_per_user": cfg.auth.rate_limits.requests_per_minute_per_user,
                "requests_per_day_per_user": cfg.auth.rate_limits.requests_per_day_per_user,
            },
            "session": {
                "cookie_ttl_days": cfg.auth.session.cookie_ttl_days,
            },
        },
        "tiers": {
            name: {
                "name": t.name,
                "description": t.description,
                "backend": t.backend,
                "model_tag": t.model_tag,
                "context_window": t.context_window,
                "think_default": t.think_default,
                "vram_estimate_gb": t.vram_estimate_gb,
                "parallel_slots": getattr(t, "parallel_slots", 1),
                "n_gpu_layers": getattr(t, "n_gpu_layers", -1),
                "flash_attention": getattr(t, "flash_attention", True),
                "cache_type_k": getattr(t, "cache_type_k", "q8_0"),
                "cache_type_v": getattr(t, "cache_type_v", "q8_0"),
                "port": getattr(t, "port", None),
                "role": getattr(t, "role", "chat"),
                "params": dict(t.params),
            }
            for name, t in cfg.models.tiers.items()
        },
    }


# ── Config PATCH ────────────────────────────────────────────────────────
#
# Whitelisted paths. The key is the JSON-shape path in the PATCH payload;
# the value is (yaml_filename, dot-path inside the YAML). A tier entry's
# leaf field lands at tiers/<tier_name>/<field>.

def _atomic_write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=str(path.parent), delete=False, suffix=".tmp",
    ) as f:
        yaml.safe_dump(data, f, sort_keys=False, default_flow_style=False)
        tmp = Path(f.name)
    tmp.replace(path)


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _set_deep(obj: dict, path: list[str], value: Any) -> None:
    cur = obj
    for key in path[:-1]:
        if key not in cur or not isinstance(cur[key], dict):
            cur[key] = {}
        cur = cur[key]
    cur[path[-1]] = value


# Each handler mutates the in-memory YAML dict for one config file and
# returns the set of changes it made (for audit log). Unknown fields are
# ignored silently so a partial PATCH is safe.

def _patch_vram(patch: dict, doc: dict) -> list[str]:
    changes: list[str] = []
    def _apply(key_in: str, yaml_path: list[str], caster=lambda x: x):
        if key_in in patch and patch[key_in] is not None:
            _set_deep(doc, yaml_path, caster(patch[key_in]))
            changes.append("vram." + ".".join(yaml_path))
    _apply("total_vram_gb", ["total_vram_gb"], float)
    _apply("headroom_gb", ["headroom_gb"], float)
    _apply("poll_interval_sec", ["poll_interval_sec"], int)
    ev = patch.get("eviction") or {}
    if "policy" in ev: _set_deep(doc, ["eviction", "policy"], str(ev["policy"])); changes.append("vram.eviction.policy")
    if "min_residency_sec" in ev: _set_deep(doc, ["eviction", "min_residency_sec"], int(ev["min_residency_sec"])); changes.append("vram.eviction.min_residency_sec")
    if "pin_orchestrator" in ev: _set_deep(doc, ["eviction", "pin_orchestrator"], bool(ev["pin_orchestrator"])); changes.append("vram.eviction.pin_orchestrator")
    if "pin_vision" in ev: _set_deep(doc, ["eviction", "pin_vision"], bool(ev["pin_vision"])); changes.append("vram.eviction.pin_vision")
    q = patch.get("queue") or {}
    if "max_depth_per_tier" in q:
        v = max(0, min(int(q["max_depth_per_tier"]), 1000))
        _set_deep(doc, ["queue", "max_depth_per_tier"], v)
        changes.append("vram.queue.max_depth_per_tier")
    if "max_wait_sec" in q:
        v = max(1, min(int(q["max_wait_sec"]), 600))
        _set_deep(doc, ["queue", "max_wait_sec"], v)
        changes.append("vram.queue.max_wait_sec")
    if "position_update_interval_sec" in q:
        v = max(1, min(int(q["position_update_interval_sec"]), 30))
        _set_deep(doc, ["queue", "position_update_interval_sec"], v)
        changes.append("vram.queue.position_update_interval_sec")
    return changes


def _patch_router(patch: dict, doc: dict) -> list[str]:
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


def _patch_auth(patch: dict, doc: dict) -> list[str]:
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


def _patch_tiers(patch: dict, doc: dict) -> tuple[list[str], set[str]]:
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


def _patch_concurrency(patch: dict, doc: dict) -> tuple[list[str], bool]:
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


@router.patch("/config")
async def patch_config(body: dict, actor: dict = Depends(require_admin)):
    from . import main as backend_main

    all_changes: list[str] = []
    dirty_tiers: set[str] = set()
    requires_restart = False
    config_dir = Path(os.getenv("LAI_CONFIG_DIR", str(CONFIG_DIR)))

    # vram.yaml
    if "vram" in body and isinstance(body["vram"], dict):
        p = config_dir / "vram.yaml"
        doc = _load_yaml(p)
        ch = _patch_vram(body["vram"], doc)
        if ch:
            _atomic_write_yaml(p, doc)
            all_changes.extend(ch)

    # router.yaml
    if "router" in body and isinstance(body["router"], dict):
        p = config_dir / "router.yaml"
        doc = _load_yaml(p)
        ch = _patch_router(body["router"], doc)
        if ch:
            _atomic_write_yaml(p, doc)
            all_changes.extend(ch)

    # auth.yaml
    if "auth" in body and isinstance(body["auth"], dict):
        p = config_dir / "auth.yaml"
        doc = _load_yaml(p)
        ch = _patch_auth(body["auth"], doc)
        if ch:
            _atomic_write_yaml(p, doc)
            all_changes.extend(ch)

    # models.yaml (tier params)
    if "tiers" in body and isinstance(body["tiers"], dict):
        p = config_dir / "models.yaml"
        doc = _load_yaml(p)
        ch, dirty = _patch_tiers(body["tiers"], doc)
        if ch:
            _atomic_write_yaml(p, doc)
            all_changes.extend(ch)
            dirty_tiers |= dirty

    # runtime.yaml (workers_target, redis_url) — requires restart
    if "concurrency" in body and isinstance(body["concurrency"], dict):
        p = config_dir / "runtime.yaml"
        doc = _load_yaml(p)
        ch, rr = _patch_concurrency(body["concurrency"], doc)
        if ch:
            _atomic_write_yaml(p, doc)
            all_changes.extend(ch)
            if rr:
                requires_restart = True

    if not all_changes:
        return {"ok": True, "changes": [], "message": "No changes applied."}

    # Hot-reload in-memory config + re-compile router signals.
    try:
        new_cfg = AppConfig.load()
        backend_main.state.config = new_cfg
        backend_main.state.signals = new_cfg.compile_signals()
        backend_main.app.state.app_config = new_cfg
    except Exception as e:
        logger.exception("Config reload after PATCH failed")
        raise HTTPException(500, f"Saved files, but reload failed: {e}")

    # Sanity check: multi-agent min_workers must not exceed the worker
    # tier's parallel_slots, or all workers would queue forever.
    ma = new_cfg.router.multi_agent
    worker_tier = new_cfg.models.tiers.get(ma.worker_tier)
    if worker_tier:
        worker_slots = max(1, int(getattr(worker_tier, "parallel_slots", 1)))
        if ma.min_workers > worker_slots:
            raise HTTPException(
                400,
                f"multi_agent.min_workers={ma.min_workers} exceeds the "
                f"worker tier's parallel_slots={worker_slots}. Raise "
                f"tiers.{ma.worker_tier}.parallel_slots first or lower "
                f"min_workers.",
            )

    # Hot-apply runtime settings that don't need a restart.
    try:
        from .middleware.rate_limit import rate_limiter
        rate_limiter.configure(
            per_minute=new_cfg.auth.rate_limits.requests_per_minute_per_user,
            per_day=new_cfg.auth.rate_limits.requests_per_day_per_user,
            redis_client=getattr(backend_main.state, "redis", None),
        )
    except Exception:
        logger.exception("Rate limiter reconfigure after PATCH failed")

    # Mark any tiers whose slot_capacity changed as dirty so the scheduler
    # evicts and reloads them on the next reserve.
    if dirty_tiers:
        scheduler = getattr(backend_main.state, "scheduler", None)
        if scheduler is not None:
            for tier_id in dirty_tiers:
                try:
                    await scheduler.mark_tier_dirty(tier_id)
                except Exception:
                    logger.exception("mark_tier_dirty failed for %s", tier_id)

    logger.info("admin %s updated config: %s", actor["email"], ", ".join(all_changes))
    return {
        "ok": True,
        "changes": all_changes,
        "requires_restart": requires_restart,
        "dirty_tiers": sorted(dirty_tiers),
        "ts": time.time(),
    }


@router.post("/reload")
async def reload_config(_: dict = Depends(require_admin)):
    from . import main as backend_main
    new_cfg = AppConfig.load()
    backend_main.state.config = new_cfg
    backend_main.state.signals = new_cfg.compile_signals()
    backend_main.app.state.app_config = new_cfg
    return {"ok": True}


# ── Airgap mode ─────────────────────────────────────────────────────────

@router.get("/airgap")
async def get_airgap(_: dict = Depends(require_admin)):
    """Return the current airgap state plus a quick summary of what the
    toggle affects so the UI can render warnings without hard-coding
    them."""
    from . import main as backend_main
    snap = backend_main.state.airgap.snapshot()
    return {
        **snap,
        "description": (
            "When ON, the backend blocks outbound web search and any tool "
            "that requires an external service. New chats and distilled "
            "memories are stored in a separate encrypted store so airgap "
            "and normal conversations never mix on disk."
        ),
    }


@router.patch("/airgap")
async def set_airgap(body: dict, actor: dict = Depends(require_admin)):
    """Toggle airgap mode. Body: `{"enabled": true|false}`."""
    from . import main as backend_main
    if "enabled" not in body:
        raise HTTPException(400, "Missing `enabled` field")
    want = bool(body["enabled"])
    current_state = backend_main.state.airgap
    if current_state.enabled == want:
        return {"ok": True, "unchanged": True, **current_state.snapshot()}
    snap = await current_state.set(want, actor.get("email"))
    logger.warning(
        "admin %s %s airgap mode",
        actor.get("email"),
        "ENABLED" if want else "DISABLED",
    )
    return {"ok": True, "unchanged": False, **snap}


# ── free_games marketplace workflow ──────────────────────────────────────
#
# The free_games tool exposes a configurable marketplace layer (search /
# extract / download arbitrary game-source sites the user configures via
# its MARKETPLACES valve). These endpoints surface that workflow to the
# desktop admin GUI so users can compose and test marketplace configs
# from a UI instead of editing a JSON valve by hand.

def _free_games_instance():
    """Return the shared Tools() instance for the free_games tool, so we
    can call its methods directly and read/write its valves."""
    from . import main as backend_main
    reg = backend_main.state.tools
    # Any method on free_games shares the same Tools() — pick a stable one.
    for name in (
        "free_games.list_marketplaces",
        "free_games.find_free",
    ):
        entry = reg.tools.get(name)
        if entry is not None:
            return entry.handler.__self__
    raise HTTPException(503, "free_games tool not loaded.")


def _read_marketplaces(instance) -> list[dict]:
    import json as _json
    try:
        data = _json.loads(instance.valves.MARKETPLACES or "[]")
    except _json.JSONDecodeError as e:
        raise HTTPException(500, f"MARKETPLACES valve is not valid JSON: {e}")
    if not isinstance(data, list):
        raise HTTPException(500, "MARKETPLACES valve must be a JSON array.")
    return data


def _write_marketplaces(instance, entries: list[dict]) -> None:
    import json as _json
    instance.valves.MARKETPLACES = _json.dumps(entries, ensure_ascii=False)


@router.get("/marketplaces")
async def list_marketplaces(_: dict = Depends(require_admin)):
    """List configured free_games marketplaces plus the relevant valve state."""
    inst = _free_games_instance()
    entries = _read_marketplaces(inst)
    return {
        "marketplaces": entries,
        "download_dir": inst.valves.DOWNLOAD_DIR,
        "write_enabled": bool(inst.valves.WRITE_ENABLED),
        "request_headers": inst.valves.REQUEST_HEADERS,
        "user_agent": inst.valves.USER_AGENT,
    }


@router.get("/marketplaces/recipes")
async def get_recipe_templates(_: dict = Depends(require_admin)):
    """Return the recipe templates the tool publishes for common site
    layouts. The GUI uses these to populate a 'Browse Recipes' picker."""
    inst = _free_games_instance()
    return {"markdown": inst.recipe_templates()}


@router.post("/marketplaces/test")
async def test_marketplace_config(body: dict, _: dict = Depends(require_admin)):
    """Run a marketplace config without saving it. Body: `{config: {...},
    query: "..."}`. Returns the diagnostic Markdown."""
    import json as _json
    cfg = body.get("config")
    if not isinstance(cfg, dict):
        raise HTTPException(400, "Missing or non-object 'config'.")
    query = body.get("query") or "test"
    inst = _free_games_instance()
    report = await inst.test_marketplace_config(_json.dumps(cfg), query=query)
    return {"markdown": report}


@router.post("/marketplaces/probe")
async def probe_marketplace(body: dict, _: dict = Depends(require_admin)):
    """Probe an *already-saved* marketplace by name. Body: `{name, query}`."""
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Missing 'name'.")
    query = body.get("query") or "test"
    inst = _free_games_instance()
    report = await inst.probe_marketplace(name, query)
    return {"markdown": report}


@router.post("/marketplaces/probe-download")
async def probe_download(body: dict, _: dict = Depends(require_admin)):
    """HEAD a candidate download URL and report what would be downloaded."""
    url = (body.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "Missing 'url'.")
    inst = _free_games_instance()
    report = await inst.probe_download(url)
    return {"markdown": report}


@router.post("/marketplaces")
async def save_marketplace(body: dict, _: dict = Depends(require_admin)):
    """Append or overwrite a marketplace config. Body must include name,
    search_url, result_pattern; download_pattern is optional. If a
    marketplace with the same name already exists, it is replaced."""
    for k in ("name", "search_url", "result_pattern"):
        if not body.get(k):
            raise HTTPException(400, f"Missing required field '{k}'.")
    cfg = {
        "name": str(body["name"]).strip(),
        "search_url": str(body["search_url"]),
        "result_pattern": str(body["result_pattern"]),
    }
    if body.get("download_pattern"):
        cfg["download_pattern"] = str(body["download_pattern"])
    inst = _free_games_instance()
    entries = _read_marketplaces(inst)
    replaced = False
    for i, e in enumerate(entries):
        if e.get("name", "").lower() == cfg["name"].lower():
            entries[i] = cfg
            replaced = True
            break
    if not replaced:
        entries.append(cfg)
    _write_marketplaces(inst, entries)
    return {"ok": True, "replaced": replaced, "count": len(entries)}


@router.delete("/marketplaces/{name}")
async def delete_marketplace(name: str, _: dict = Depends(require_admin)):
    inst = _free_games_instance()
    entries = _read_marketplaces(inst)
    new_entries = [e for e in entries if e.get("name", "").lower() != name.lower()]
    if len(new_entries) == len(entries):
        raise HTTPException(404, f"No marketplace named '{name}'.")
    _write_marketplaces(inst, new_entries)
    return {"ok": True, "name": name, "count": len(new_entries)}


@router.patch("/marketplaces/valves")
async def patch_marketplace_valves(body: dict, _: dict = Depends(require_admin)):
    """Update DOWNLOAD_DIR / WRITE_ENABLED / REQUEST_HEADERS / USER_AGENT.
    Any subset of fields can be supplied."""
    inst = _free_games_instance()
    if "download_dir" in body:
        inst.valves.DOWNLOAD_DIR = str(body["download_dir"])
    if "write_enabled" in body:
        inst.valves.WRITE_ENABLED = bool(body["write_enabled"])
    if "request_headers" in body:
        # Accept a JSON object or pre-stringified JSON.
        rh = body["request_headers"]
        if isinstance(rh, str):
            inst.valves.REQUEST_HEADERS = rh
        else:
            import json as _json
            inst.valves.REQUEST_HEADERS = _json.dumps(rh)
    if "user_agent" in body:
        inst.valves.USER_AGENT = str(body["user_agent"])
    return {
        "ok": True,
        "download_dir": inst.valves.DOWNLOAD_DIR,
        "write_enabled": bool(inst.valves.WRITE_ENABLED),
        "request_headers": inst.valves.REQUEST_HEADERS,
        "user_agent": inst.valves.USER_AGENT,
    }
