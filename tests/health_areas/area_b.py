"""Area B — Backend startup probes."""

from __future__ import annotations
import asyncio
import importlib
import os
import pathlib
import subprocess
import sys
import time


def _env_file_path() -> pathlib.Path:
    """Resolve .env location (installed vs dev checkout)."""
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        installed = pathlib.Path(local_appdata) / "LocalAIStack" / ".env"
        if installed.exists():
            return installed
    repo = pathlib.Path(__file__).resolve().parents[3]
    return repo / ".env"


def _read_env() -> dict[str, str]:
    env_path = _env_file_path()
    if not env_path.exists():
        return {}
    result: dict[str, str] = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        result[k.strip()] = v.strip().strip('"').strip("'")
    return result


def _http_get(url: str, timeout: float = 10.0):
    try:
        import httpx
        r = httpx.get(url, timeout=timeout)
        return r.status_code, r.text
    except Exception as e:
        return None, str(e)


def run() -> list[dict]:
    results = []

    def probe(name: str, fn) -> None:
        try:
            status, detail, fix_hint = fn()
        except Exception as e:
            status, detail, fix_hint = "FAIL", str(e), "Check logs\\backend.log"
        results.append({"area": "B", "test": name, "status": status,
                        "detail": detail, "fix_hint": fix_hint})

    env = _read_env()

    # .env present
    def _env_present():
        p = _env_file_path()
        if p.exists():
            return "PASS", str(p), ""
        return "FAIL", f"Not found: {p}", "Run LocalAIStack.ps1 -Setup"

    probe("env_present", _env_present)

    # AUTH_SECRET_KEY set
    def _auth_key():
        val = env.get("AUTH_SECRET_KEY", "")
        if val and len(val) >= 32:
            return "PASS", f"key length {len(val)}", ""
        return "FAIL", "AUTH_SECRET_KEY is empty or too short", \
               "Re-run setup wizard"

    probe("auth_secret_key", _auth_key)

    # HISTORY_SECRET_KEY set
    def _history_key():
        val = env.get("HISTORY_SECRET_KEY", "")
        if val and len(val) >= 32:
            return "PASS", f"key length {len(val)}", ""
        return "WARN", "HISTORY_SECRET_KEY unset — history encrypted with AUTH_SECRET_KEY", \
               "Re-run setup wizard to set a separate key"

    probe("history_secret_key", _history_key)

    # Admin user exists
    def _admin_exists():
        repo = pathlib.Path(__file__).resolve().parents[3]
        venv_py = repo / "vendor" / "venv-backend" / "Scripts" / "python.exe"
        if not venv_py.exists():
            return "WARN", "Backend venv not found — skipping admin check", \
                   "Run LocalAIStack.ps1 -Setup"
        r = subprocess.run(
            [str(venv_py), "-m", "backend.seed_admin", "--check-only"],
            capture_output=True, text=True, timeout=15, cwd=str(repo)
        )
        if r.returncode == 0:
            return "PASS", r.stdout.strip() or "Admin user found", ""
        return "FAIL", r.stdout.strip() or r.stderr.strip() or "No admin user", \
               "Re-run setup wizard"

    probe("admin_user_exists", _admin_exists)

    # Backend venv intact
    def _venv():
        repo = pathlib.Path(__file__).resolve().parents[3]
        py = repo / "vendor" / "venv-backend" / "Scripts" / "python.exe"
        if py.exists():
            return "PASS", str(py), ""
        return "FAIL", str(py), "Run LocalAIStack.ps1 -Setup"

    probe("backend_venv", _venv)

    # Backend imports cleanly
    def _imports():
        repo = pathlib.Path(__file__).resolve().parents[3]
        venv_py = repo / "vendor" / "venv-backend" / "Scripts" / "python.exe"
        if not venv_py.exists():
            return "WARN", "Backend venv not found", "Run LocalAIStack.ps1 -Setup"
        r = subprocess.run(
            [str(venv_py), "-c", "from backend import main; print('ok')"],
            capture_output=True, text=True, timeout=30, cwd=str(repo),
            env={**os.environ, "AUTH_SECRET_KEY": env.get("AUTH_SECRET_KEY", "x" * 48),
                 "OFFLINE": "1"}
        )
        if r.returncode == 0:
            return "PASS", "backend.main imports cleanly", ""
        return "FAIL", r.stderr.strip()[-500:], "Check logs\\backend.log"

    probe("backend_imports", _imports)

    # /healthz responds 200
    def _healthz():
        status_code, body = _http_get("http://127.0.0.1:18000/healthz", timeout=30)
        if status_code == 200:
            return "PASS", f"HTTP 200: {body[:100]}", ""
        if status_code is None:
            return "FAIL", body, "Run LocalAIStack.ps1 -Start"
        return "WARN", f"HTTP {status_code}: {body[:100]}", "Start missing services"

    probe("backend_healthz", _healthz)

    # /healthz status = "ok"
    def _healthz_ok():
        import json
        status_code, body = _http_get("http://127.0.0.1:18000/healthz", timeout=30)
        if status_code is None:
            return "FAIL", body, "Run LocalAIStack.ps1 -Start"
        try:
            data = json.loads(body)
            s = data.get("status", "")
            if s == "ok":
                return "PASS", "status=ok", ""
            return "WARN", f"status={s!r} (degraded — some services unavailable)", \
                   "Start Ollama and Qdrant: LocalAIStack.ps1 -Start"
        except Exception:
            return "WARN", f"Could not parse JSON: {body[:100]}", ""

    probe("backend_status_ok", _healthz_ok)

    return results
