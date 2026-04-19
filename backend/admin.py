"""Admin dashboard API.

Access model:
    - ADMIN_EMAILS env var (comma-separated) lists users allowed to call
      /admin/* endpoints. A signed-in user whose email matches gets through;
      anyone else receives 403.
    - If ADMIN_EMAILS is unset, admin endpoints are disabled (503).

Endpoints mounted under /admin:
    GET   /admin/me                  - {email, is_admin}
    GET   /admin/overview            - counters + totals
    GET   /admin/usage               - bucketed time series
    GET   /admin/usage/by_tier
    GET   /admin/usage/by_user
    GET   /admin/errors              - recent error events
    GET   /admin/users               - full user list
    DELETE /admin/users/{id}         - hard-delete a user (cascades)
    GET   /admin/config              - current config snapshot (tweakable fields)
    PATCH /admin/config              - apply patches, write YAML, hot-reload
    GET   /admin/tools               - tool registry with enabled flags
    PATCH /admin/tools/{name}        - enable/disable a tool (memory-only)
    POST  /admin/reload              - force reload config from disk

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

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request

from . import auth, db, metrics
from .config import AppConfig, CONFIG_DIR


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


# ── Role gate ────────────────────────────────────────────────────────────

def _admin_emails() -> set[str]:
    raw = os.getenv("ADMIN_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def is_admin_email(email: str | None) -> bool:
    if not email:
        return False
    return email.lower() in _admin_emails()


async def require_admin(user: dict = Depends(auth.current_user)) -> dict:
    admins = _admin_emails()
    if not admins:
        raise HTTPException(
            503,
            "Admin dashboard is disabled. Set ADMIN_EMAILS env var (comma-"
            "separated list of admin email addresses) to enable it.",
        )
    if (user.get("email") or "").lower() not in admins:
        raise HTTPException(403, "Not an admin account")
    return user


# ── Me ──────────────────────────────────────────────────────────────────

@router.get("/me")
async def admin_me(user: dict = Depends(auth.current_user)):
    return {
        "email": user["email"],
        "is_admin": is_admin_email(user.get("email")),
        "admin_configured": bool(_admin_emails()),
    }


# ── Metrics ─────────────────────────────────────────────────────────────

@router.get("/overview")
async def overview(
    window: int = 86400,
    _: dict = Depends(require_admin),
):
    data = await metrics.overview(window_seconds=window)
    return data


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
            "SELECT u.id, u.email, u.created_at, u.last_login_at, "
            "       (SELECT COUNT(*) FROM conversations c WHERE c.user_id = u.id) AS conversations, "
            "       (SELECT COUNT(*) FROM memories m WHERE m.user_id = u.id) AS memories, "
            "       (SELECT COUNT(*) FROM rag_docs r WHERE r.user_id = u.id) AS rag_docs "
            "FROM users u ORDER BY u.last_login_at DESC NULLS LAST",
        )).fetchall()
    admins = _admin_emails()
    return {"data": [
        {**dict(r), "is_admin": (r["email"] or "").lower() in admins}
        for r in rows
    ]}


@router.delete("/users/{user_id}")
async def delete_user(user_id: int, actor: dict = Depends(require_admin)):
    if user_id == actor["id"]:
        raise HTTPException(400, "Refusing to delete your own admin account")
    async with db.get_conn() as c:
        cur = await c.execute("DELETE FROM users WHERE id = ?", (user_id,))
        await c.commit()
        if cur.rowcount == 0:
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


@router.get("/tools")
async def list_tools(_: dict = Depends(require_admin)):
    from . import main as backend_main
    return {
        "data": [
            {
                "name": t.name,
                "description": t.schema.get("function", {}).get("description", ""),
                "default_enabled": t.default_enabled,
                "enabled": t.default_enabled,
                "requires_service": t.requires_service,
            }
            for t in backend_main.state.tools.tools.values()
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


# ── Config GET ──────────────────────────────────────────────────────────

@router.get("/config")
async def get_config(_: dict = Depends(require_admin)):
    from . import main as backend_main
    cfg: AppConfig = backend_main.state.config
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
            "ollama": {
                "keep_alive_default": cfg.vram.ollama.keep_alive_default,
                "keep_alive_pinned": cfg.vram.ollama.keep_alive_pinned,
            },
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
                "worker_tier": cfg.router.multi_agent.worker_tier,
                "orchestrator_tier": cfg.router.multi_agent.orchestrator_tier,
            },
        },
        "auth": {
            "magic_link_expiry_minutes": cfg.auth.magic_link.expiry_minutes,
            "allowed_email_domains": list(cfg.auth.allowed_email_domains),
            "rate_limits": {
                "requests_per_hour_per_email": cfg.auth.rate_limits.requests_per_hour_per_email,
                "requests_per_hour_per_ip": cfg.auth.rate_limits.requests_per_hour_per_ip,
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
    oll = patch.get("ollama") or {}
    if "keep_alive_default" in oll:
        _set_deep(doc, ["ollama", "keep_alive_default"], str(oll["keep_alive_default"])); changes.append("vram.ollama.keep_alive_default")
    if "keep_alive_pinned" in oll:
        _set_deep(doc, ["ollama", "keep_alive_pinned"], int(oll["keep_alive_pinned"])); changes.append("vram.ollama.keep_alive_pinned")
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
        doc.setdefault("multi_agent", {})["max_workers"] = int(ma["max_workers"])
        changes.append("router.multi_agent.max_workers")
    if "worker_tier" in ma:
        doc.setdefault("multi_agent", {})["worker_tier"] = str(ma["worker_tier"])
        changes.append("router.multi_agent.worker_tier")
    if "orchestrator_tier" in ma:
        doc.setdefault("multi_agent", {})["orchestrator_tier"] = str(ma["orchestrator_tier"])
        changes.append("router.multi_agent.orchestrator_tier")
    return changes


def _patch_auth(patch: dict, doc: dict) -> list[str]:
    changes: list[str] = []
    if "magic_link_expiry_minutes" in patch:
        doc.setdefault("magic_link", {})["expiry_minutes"] = int(patch["magic_link_expiry_minutes"])
        changes.append("auth.magic_link.expiry_minutes")
    if "allowed_email_domains" in patch:
        val = patch["allowed_email_domains"]
        if isinstance(val, str):
            val = [d.strip() for d in val.split(",") if d.strip()]
        doc["allowed_email_domains"] = [str(d).lower() for d in val]
        changes.append("auth.allowed_email_domains")
    rl = patch.get("rate_limits") or {}
    if "requests_per_hour_per_email" in rl:
        doc.setdefault("rate_limits", {})["requests_per_hour_per_email"] = int(rl["requests_per_hour_per_email"])
        changes.append("auth.rate_limits.requests_per_hour_per_email")
    if "requests_per_hour_per_ip" in rl:
        doc.setdefault("rate_limits", {})["requests_per_hour_per_ip"] = int(rl["requests_per_hour_per_ip"])
        changes.append("auth.rate_limits.requests_per_hour_per_ip")
    ses = patch.get("session") or {}
    if "cookie_ttl_days" in ses:
        doc.setdefault("session", {})["cookie_ttl_days"] = int(ses["cookie_ttl_days"])
        changes.append("auth.session.cookie_ttl_days")
    return changes


def _patch_tiers(patch: dict, doc: dict) -> list[str]:
    """Patch a subset of fields on existing tiers in models.yaml.

    Only allows edits to fields the dashboard shows: context_window,
    think_default, vram_estimate_gb, description, and a flat `params`
    dict (temperature/top_p/top_k/num_ctx). New tiers cannot be created
    this way.
    """
    changes: list[str] = []
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
        if "params" in body and isinstance(body["params"], dict):
            t.setdefault("params", {})
            for pk, pv in body["params"].items():
                if pv is None:
                    t["params"].pop(pk, None)
                    changes.append(f"models.tiers.{name}.params.{pk}=null")
                else:
                    t["params"][pk] = pv
                    changes.append(f"models.tiers.{name}.params.{pk}")
    return changes


@router.patch("/config")
async def patch_config(body: dict, actor: dict = Depends(require_admin)):
    from . import main as backend_main

    all_changes: list[str] = []
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
        ch = _patch_tiers(body["tiers"], doc)
        if ch:
            _atomic_write_yaml(p, doc)
            all_changes.extend(ch)

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

    logger.info("admin %s updated config: %s", actor["email"], ", ".join(all_changes))
    return {"ok": True, "changes": all_changes, "ts": time.time()}


@router.post("/reload")
async def reload_config(_: dict = Depends(require_admin)):
    from . import main as backend_main
    new_cfg = AppConfig.load()
    backend_main.state.config = new_cfg
    backend_main.state.signals = new_cfg.compile_signals()
    backend_main.app.state.app_config = new_cfg
    return {"ok": True}
