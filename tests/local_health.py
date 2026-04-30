"""
Local install health-check suite.

Invoked by:
    python -m tests.local_health              # full suite
    python -m tests.local_health --area B     # single area
    python -m tests.local_health --fix        # run + attempt safe auto-fixes
    python -m tests.local_health --json       # JSON-lines to stdout only (no GUI)

Called from:
    LocalAIStack.ps1 -Test [-Fix] [-Area <letter>]

Results are written to:
    %LOCALAPPDATA%\\LocalAIStack\\logs\\health-<YYYYMMDD-HHmmss>.log  (JSON-lines)

After the suite finishes, the PySide6 diagnostics window is spawned unless
--json is passed or the GUI venv is unavailable.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import subprocess
import sys


# ---------------------------------------------------------------------------
# Log path
# ---------------------------------------------------------------------------

def _log_path() -> pathlib.Path:
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        log_dir = pathlib.Path(local_appdata) / "LocalAIStack" / "logs"
    else:
        # Fallback for non-Windows / dev mode
        repo = pathlib.Path(__file__).resolve().parents[1]
        log_dir = repo / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return log_dir / f"health-{ts}.log"


# ---------------------------------------------------------------------------
# Auto-fix actions (conservative — never deletes data)
# ---------------------------------------------------------------------------

_FIX_ACTIONS: dict[str, list[str]] = {
    # area:test → shell command to run
    "A:qdrant_binary": [],          # no safe auto-fix
    "B:backend_healthz": [],        # handled by service restart below
    "C:cloudflared_service_running": ["sc", "start", "cloudflared"],
    "D:ollama_running": ["ollama", "serve"],
}


def _attempt_fix(result: dict) -> str | None:
    key = f"{result['area']}:{result['test']}"
    cmd = _FIX_ACTIONS.get(key)
    if not cmd:
        return None
    try:
        subprocess.Popen(cmd, shell=(os.name == "nt"))
        return f"Started: {' '.join(cmd)}"
    except Exception as e:
        return f"Fix failed: {e}"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_suite(areas: list[str] | None = None, fix: bool = False) -> list[dict]:
    from tests.health_areas import area_a, area_b, area_c, area_d, area_e

    area_map = {
        "A": area_a.run,
        "B": area_b.run,
        "C": area_c.run,
        "D": area_d.run,
        "E": area_e.run,
    }

    selected = {k: v for k, v in area_map.items()
                if areas is None or k in (areas or [])}

    all_results: list[dict] = []
    for letter, fn in selected.items():
        print(f"\n── Area {letter} ──────────────────────────────", flush=True)
        try:
            results = fn()
        except Exception as e:
            results = [{"area": letter, "test": "runner_error",
                        "status": "FAIL", "detail": str(e), "fix_hint": ""}]

        for r in results:
            icon = {"PASS": "✓", "WARN": "!", "FAIL": "✗", "SKIP": "-"}.get(
                r["status"], "?"
            )
            print(f"  [{icon}] {r['test']:<35} {r['status']:<5}  {r.get('detail','')[:80]}")
            if fix and r["status"] == "FAIL":
                fix_result = _attempt_fix(r)
                if fix_result:
                    r["auto_fix"] = fix_result
                    print(f"       → auto-fix: {fix_result}")

        all_results.extend(results)

    return all_results


# ---------------------------------------------------------------------------
# Log writer
# ---------------------------------------------------------------------------

def write_log(results: list[dict], log_path: pathlib.Path) -> None:
    with log_path.open("w", encoding="utf-8") as f:
        for r in results:
            row = {
                "ts": datetime.datetime.now().isoformat(timespec="seconds"),
                **r,
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"\nLog: {log_path}", flush=True)


# ---------------------------------------------------------------------------
# Spawn GUI results window
# ---------------------------------------------------------------------------

def _launch_gui(log_path: pathlib.Path) -> None:
    repo = pathlib.Path(__file__).resolve().parents[1]
    pythonw = repo / "vendor" / "venv-gui" / "Scripts" / "pythonw.exe"
    if not pythonw.exists():
        print("GUI venv not found — skipping diagnostics window.")
        return
    diag = repo / "gui" / "windows" / "diagnostics.py"
    if not diag.exists():
        print("diagnostics.py not yet implemented — skipping GUI.")
        return
    subprocess.Popen(
        [str(pythonw), str(diag), "--log", str(log_path)],
        cwd=str(repo),
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Local AI Stack health-check suite")
    parser.add_argument("--area", metavar="LETTER", help="Run only this area (A-E)")
    parser.add_argument("--fix", action="store_true", help="Attempt auto-fix on failures")
    parser.add_argument("--json", action="store_true",
                        help="Output JSON-lines to stdout only; no GUI window")
    args = parser.parse_args()

    areas = [args.area.upper()] if args.area else None

    print("Local AI Stack — health-check suite", flush=True)
    print("=" * 50, flush=True)

    results = run_suite(areas=areas, fix=args.fix)
    log_path = _log_path()
    write_log(results, log_path)

    if args.json:
        for r in results:
            print(json.dumps(r))
        return 0

    # Summary
    counts = {"PASS": 0, "WARN": 0, "FAIL": 0, "SKIP": 0}
    for r in results:
        counts[r.get("status", "FAIL")] = counts.get(r.get("status", "FAIL"), 0) + 1

    print(f"\nResults: {counts['PASS']} PASS  {counts['WARN']} WARN  "
          f"{counts['FAIL']} FAIL  {counts['SKIP']} SKIP")

    _launch_gui(log_path)

    return 1 if counts["FAIL"] > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
