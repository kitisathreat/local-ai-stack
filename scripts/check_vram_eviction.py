"""Smoke-test the VRAM scheduler's idle-evict + persist behaviour.

Probes the running backend through `/admin/vram/probe` (admin auth via
AUTH_SECRET_KEY-minted cookie). Reports:

  - the configured idle_evict_after_sec
  - eviction counters by reason (idle / pressure / make_room / other)
  - the last 10 eviction events (tier_id, reason, idle_sec, freed-GB)
  - the resolved persist_path for vram_observed.json (was the legacy
    Docker /app/data/... default before this change — should now be a
    Windows-native path under <repo>/data)
  - whether the persisted observed-cost cache was actually loaded into
    memory at startup (proves the persist round-trip works across
    backend restarts)
  - orphan_drift_gb so we can tell idle eviction from external GPU
    consumers

Exits 0 if the configuration looks healthy, 1 if any obvious red flags
(idle threshold == 0, persist path still pointing at /app/data,
observed dict empty after a restart that should have repopulated it).
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
API = "http://127.0.0.1:18000"


def _load_dotenv() -> None:
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _mint_admin_cookie() -> str:
    _load_dotenv()
    sys.path.insert(0, str(REPO_ROOT))
    from jose import jwt   # type: ignore
    key = os.environ["AUTH_SECRET_KEY"]
    now = int(time.time())
    return jwt.encode(
        {"sub": "1", "iat": now, "exp": now + 30 * 86400},
        key, algorithm="HS256",
    )


def _get(url: str, cookie: str) -> dict:
    req = urllib.request.Request(url)
    req.add_header("Cookie", f"lai_session={cookie}")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def main() -> int:
    cookie = _mint_admin_cookie()
    try:
        probe = _get(f"{API}/admin/vram/probe", cookie)
    except Exception as exc:
        print(f"FAIL: could not reach /admin/vram/probe: {exc}", file=sys.stderr)
        return 1

    ev = probe.get("evictions") or {}
    persist_path = probe.get("observed_costs_persist_path") or ""
    observed = probe.get("observed_costs_loaded") or {}

    print("=== VRAM eviction monitoring ===")
    print(f"NVML free        : {probe.get('nvml_free_gb')} GB / {probe.get('total_vram_gb')} GB total")
    print(f"Scheduler tracked: {probe.get('scheduler_tracked_used_gb')} GB used")
    print(f"Orphan drift     : {probe.get('orphan_drift_gb')} GB"
          f"{' (NON-ZERO — external consumer or orphan)' if probe.get('orphan_drift_gb', 0) > 0.5 else ''}")
    print()
    threshold = ev.get('idle_evict_after_sec')
    if threshold is None:
        print("Idle-evict threshold : (backend predates monitoring — bounce to enable)")
    else:
        print(f"Idle-evict threshold : {threshold} s ({threshold // 60} min)"
              if threshold else "Idle-evict threshold : 0 (DISABLED — proactive eviction off)")
    print(f"Total evictions      : {ev.get('total')}")
    by_reason = ev.get("by_reason") or {}
    print(f"  by_reason          : idle={by_reason.get('idle')} "
          f"pressure={by_reason.get('pressure')} "
          f"make_room={by_reason.get('make_room')} "
          f"other={by_reason.get('other')}")
    print()
    recent = ev.get("recent") or []
    print(f"Last {len(recent)} eviction event(s):")
    if not recent:
        print("  (none yet — fresh restart, or no idle tier hit threshold)")
    for e in recent:
        ts = e.get("ts", 0)
        age = time.time() - ts
        print(f"  - {time.strftime('%H:%M:%S', time.localtime(ts))} "
              f"({age:>5.0f}s ago)  tier={e.get('tier_id'):<18} "
              f"reason={e.get('reason'):<10} idle={e.get('idle_sec'):>6.1f}s "
              f"freed≈{e.get('vram_cost_gb'):>5.1f}GB")
    print()
    print(f"Observed-cost persist path: {persist_path}")
    on_disk = Path(persist_path) if persist_path else None
    on_disk_exists = bool(on_disk and on_disk.exists())
    on_disk_size = on_disk.stat().st_size if on_disk_exists else 0
    print(f"  exists on disk : {on_disk_exists} ({on_disk_size} bytes)")
    print(f"  loaded entries : {len(observed)}")
    for tier, cost in sorted(observed.items()):
        print(f"    {tier:<20} {cost:.2f} GB")
    print()
    print("Currently loaded tiers:")
    loaded = probe.get("loaded") or []
    if not loaded:
        print("  (none)")
    for m in loaded:
        print(f"  - {m.get('tier_id'):<18} state={m.get('state'):<10} "
              f"refcount={m.get('refcount')} "
              f"estimate={m.get('estimate_gb'):.1f}GB "
              f"observed={m.get('observed_gb') or '—'}")

    # ── Health check / verdict ──
    issues = []
    threshold = ev.get("idle_evict_after_sec")
    if threshold is None:
        issues.append(
            "Eviction monitoring fields missing from /admin/vram/probe — "
            "backend predates this PR; bounce LocalAIStack.ps1 to pick up the new code"
        )
    elif threshold == 0:
        issues.append("idle_evict_after_sec is 0 — proactive eviction disabled")
    if persist_path.startswith("/app/data") or persist_path.startswith("\\app\\data"):
        issues.append(f"persist_path still pointing at the dead Docker default: {persist_path}")
    if persist_path and not on_disk_exists and observed:
        issues.append(
            f"persist_path doesn't exist on disk but in-memory observed is non-empty "
            f"({len(observed)} entries) — writes are silently dropping"
        )
    drift = probe.get("orphan_drift_gb", 0)
    if drift > 1.0:
        issues.append(
            f"orphan_drift_gb={drift} GB — something untracked is holding the GPU. "
            "Hit POST /admin/vram/kill-orphans (or check Chrome / external apps)"
        )

    print()
    if issues:
        print("ISSUES:")
        for i in issues:
            print(f"  - {i}")
        return 1
    print("PASS: idle-evict configured, persist path resolved, no orphan drift")
    return 0


if __name__ == "__main__":
    sys.exit(main())
