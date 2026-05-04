"""Centralized logging + process-identification + runtime-state helpers.

Three concerns lived in three different places before this module:
  1. logging.basicConfig() in backend/main.py — only set up the *backend*'s
     root logger. The model_resolver, the bench script, and the launcher's
     supplemental scripts all configured logging ad-hoc (or not at all),
     which made tail-the-right-file impossible during incident triage.
  2. Process names: every Python entrypoint (backend, resolver, bench,
     scripts/*.py) showed up as plain `python.exe` in tasklist. The only
     way to tell them apart was grep'ing argv via wmic / Get-CimInstance,
     which is fragile and breaks for non-admin shells.
  3. Runtime discovery: which port is the backend on? Which PID owns it?
     What git SHA was it built from? The answer lived in pids.json (only
     populated when launched via LocalAIStack.ps1 -Start) and was missing
     for any backend started via `python -m uvicorn` directly. Several
     incidents this week ended in "wait, which port is the real backend"
     debugging.

This module is intentionally dependency-light — only stdlib + setproctitle.
Importing it has zero side effects; the install_* functions are explicit
opt-in. Every entrypoint should call:

    from backend import observability as obs
    obs.install("backend")          # or "resolver", "bench", "watchdog"...

at the very top of `if __name__ == "__main__"` (or for backend/main.py,
just after the standard library imports). That single call:

    - Sets the OS process title to `lai-<component>` (or
      `lai-<component>-<suffix>` if a suffix is provided).
    - Configures root logging to write to BOTH stderr (line-buffered,
      colorized in TTY) AND data/logs/<component>-<YYYYMMDD>.log via a
      RotatingFileHandler that caps individual files at 10 MB and keeps
      five backups (so a runaway loop doesn't fill the disk).
    - Writes data/runtime/<component>.json with PID, port (when
      applicable), start time, git SHA, branch, Python version, argv —
      so any other tool can locate the live process without grep'ing
      tasklist.
    - Registers an atexit hook that removes the runtime/<component>.json
      so stale entries don't outlive the process.

The companion `state_snapshot()` helper reads every runtime/*.json file
and returns a list — used by `LocalAIStack.ps1 -Status` and by the
benchmarks' "find the live backend" lookup.
"""

from __future__ import annotations

import atexit
import json
import logging
import logging.handlers
import os
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

# setproctitle is the only third-party dep here; tolerate its absence so
# importing observability never breaks an entrypoint that hasn't installed
# it yet (e.g. during the requirements-update window after a fresh pull).
try:
    import setproctitle as _setproctitle  # type: ignore
except ImportError:  # pragma: no cover - graceful degradation only
    _setproctitle = None


_INSTALLED: dict[str, Any] | None = None


# ── Path resolution ───────────────────────────────────────────────────────

def _repo_root() -> Path:
    """Walk up from this file to the repo root. Stable across CWD changes
    (some entrypoints chdir to the repo root, others don't)."""
    return Path(__file__).resolve().parent.parent


def _data_dir() -> Path:
    """Mirror backend.config._data_dir() so logs land where the rest of the
    stack expects, but don't import config.py (heavy + circular)."""
    env = os.getenv("LAI_DATA_DIR")
    if env:
        return Path(env)
    return _repo_root() / "data"


def logs_dir() -> Path:
    p = _data_dir() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def runtime_dir() -> Path:
    p = _data_dir() / "runtime"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ── Git introspection (best-effort) ───────────────────────────────────────

def _git(args: list[str]) -> str | None:
    """Run a git command in the repo root. Returns stdout stripped, or
    None on any failure (no git in PATH, not a checkout, detached HEAD on
    some commands, etc.). Logged at DEBUG so it doesn't pollute the log."""
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=_repo_root(),
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if out.returncode != 0:
            return None
        return out.stdout.strip() or None
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def git_info() -> dict[str, str | None]:
    return {
        "sha": _git(["rev-parse", "HEAD"]),
        "branch": _git(["rev-parse", "--abbrev-ref", "HEAD"]),
        "dirty": (_git(["status", "--porcelain"]) or "") != "",
    }


# ── Logging setup ─────────────────────────────────────────────────────────

# Format chosen to be greppable: %asctime is ISO-ish, %name is the dotted
# logger path (e.g. backend.vram_scheduler), %levelname is fixed-width when
# padded. "%(filename)s:%(lineno)d" added so an INFO line points at the
# exact source location — invaluable when the same message gets emitted
# from multiple call sites.
_LOG_FMT = (
    "%(asctime)s.%(msecs)03d %(levelname)-7s "
    "[%(name)s] %(filename)s:%(lineno)d  %(message)s"
)
_LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"


class _ConsoleColorFormatter(logging.Formatter):
    """ANSI color on level name when stderr is a TTY. Plain text otherwise.
    File handlers always get the plain formatter (color codes in a log
    file are noise)."""

    _COLORS = {
        "DEBUG": "\x1b[90m",     # bright black
        "INFO": "\x1b[36m",      # cyan
        "WARNING": "\x1b[33m",   # yellow
        "ERROR": "\x1b[31m",     # red
        "CRITICAL": "\x1b[1;31m",  # bold red
    }
    _RESET = "\x1b[0m"

    def format(self, record: logging.LogRecord) -> str:
        if sys.stderr.isatty() and record.levelname in self._COLORS:
            saved = record.levelname
            record.levelname = (
                f"{self._COLORS[saved]}{saved}{self._RESET}"
            )
            try:
                return super().format(record)
            finally:
                record.levelname = saved
        return super().format(record)


def _build_handlers(component: str, log_path: Path) -> list[logging.Handler]:
    """Create the stderr + rotating-file handler pair. Both share the same
    format module so a line is byte-identical across the two destinations
    (modulo the color codes on stderr)."""
    handlers: list[logging.Handler] = []

    console = logging.StreamHandler(stream=sys.stderr)
    console.setFormatter(_ConsoleColorFormatter(_LOG_FMT, datefmt=_LOG_DATEFMT))
    handlers.append(console)

    # 10 MB per file × 5 backups = ~50 MB ceiling per component per day.
    # delay=True so the file isn't created on import (only on first write),
    # which keeps `data/logs/` clean for components that import this module
    # but never emit a log line.
    rotating = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
        delay=True,
    )
    rotating.setFormatter(logging.Formatter(_LOG_FMT, datefmt=_LOG_DATEFMT))
    handlers.append(rotating)

    return handlers


def install(
    component: str,
    *,
    suffix: str | None = None,
    port: int | None = None,
    extra_state: dict[str, Any] | None = None,
    log_level: str | None = None,
) -> dict[str, Any]:
    """Install logging + proctitle + runtime-state for `component`.

    Idempotent: calling install() twice in the same process is a no-op
    after the first call (the first call wins; second-call args are
    silently ignored to avoid surprise reconfiguration). Returns the
    state dict so callers can inspect what was registered.

    Args:
        component: Short stable name. Use one of: "backend", "resolver",
            "bench", "watchdog", "eval", "gui". Custom names allowed for
            scripts/* — keep them lowercase + hyphenated.
        suffix: Optional disambiguator appended to the proctitle and the
            runtime-state filename. E.g. install("resolver", suffix="coding")
            sets proctitle to `lai-resolver-coding` and writes runtime
            state to runtime/resolver-coding.json — useful when several
            instances of the same component run concurrently.
        port: HTTP port the component listens on, when applicable. Surfaces
            in the runtime state so other tools can locate the live process.
        extra_state: Additional fields to merge into the runtime state JSON.
        log_level: Override LOG_LEVEL env var (defaults to INFO).
    """
    global _INSTALLED
    if _INSTALLED is not None:
        return _INSTALLED

    full_name = f"{component}-{suffix}" if suffix else component

    # Set process title first — even if logging setup fails, the OS-level
    # naming is what helps incident responders find the right PID.
    if _setproctitle is not None:
        try:
            _setproctitle.setproctitle(f"lai-{full_name}")
        except Exception:  # noqa: BLE001 - best-effort, don't crash
            pass

    # Configure root logger. Tear down any pre-existing handlers (uvicorn
    # installs its own handlers on import; basicConfig() in main.py also
    # does — we want a single source of truth so log lines aren't doubled
    # or lost).
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
        try:
            h.close()
        except Exception:  # noqa: BLE001
            pass

    log_path = logs_dir() / f"{full_name}-{datetime.now():%Y%m%d}.log"
    for h in _build_handlers(full_name, log_path):
        root.addHandler(h)

    level_str = (log_level or os.getenv("LOG_LEVEL") or "INFO").upper()
    root.setLevel(getattr(logging, level_str, logging.INFO))

    # Tame the very-noisy third-party loggers. INFO -> WARNING moves
    # things like httpx's "HTTP/1.1 200 OK" preamble out of the way
    # without hiding genuine warnings/errors.
    for noisy in ("httpx", "httpcore", "urllib3", "huggingface_hub.file_download"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # Write runtime/<full_name>.json so other tools can find this process
    # without grep'ing tasklist.
    state: dict[str, Any] = {
        "component": component,
        "suffix": suffix,
        "full_name": full_name,
        "pid": os.getpid(),
        "port": port,
        "started_at": time.time(),
        "started_at_iso": datetime.now().isoformat(timespec="seconds"),
        "host": socket.gethostname(),
        "python_version": sys.version.split()[0],
        "argv": sys.argv,
        "log_path": str(log_path),
        "git": git_info(),
    }
    if extra_state:
        state.update(extra_state)

    state_path = runtime_dir() / f"{full_name}.json"
    try:
        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError as exc:
        # Log the failure but don't crash — the runtime state is helpful
        # but not required for the component to work.
        logging.getLogger("backend.observability").warning(
            "failed to write runtime state %s: %s", state_path, exc,
        )
    else:
        # Best-effort cleanup on graceful exit so stale state files don't
        # accumulate. Crashes leave the file behind, which is intentional
        # — the next state_snapshot() call surfaces it for triage.
        atexit.register(_remove_state_file, state_path)

    _INSTALLED = state
    logging.getLogger("backend.observability").info(
        "observability installed: component=%s pid=%d port=%s log=%s",
        full_name, os.getpid(), port, log_path,
    )
    return state


def _remove_state_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def state_snapshot() -> list[dict[str, Any]]:
    """Read every runtime/*.json and return them as a list. Used by
    `LocalAIStack.ps1 -Status` and by `scripts/eval_tiers.py` to discover
    the live backend port. Stale entries (where the recorded PID is no
    longer alive) are kept in the result with `alive: False` so callers
    can decide whether to reap them."""
    snapshots: list[dict[str, Any]] = []
    for p in sorted(runtime_dir().glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        data["alive"] = _pid_alive(data.get("pid"))
        data["state_file"] = str(p)
        snapshots.append(data)
    return snapshots


def _pid_alive(pid: int | None) -> bool:
    if not pid or not isinstance(pid, int):
        return False
    if sys.platform == "win32":
        # On Windows, signal-0 isn't a thing; use OpenProcess via ctypes.
        # Simpler path: try psutil if available, else fall back to a
        # tasklist scan (slow but reliable).
        try:
            import psutil  # type: ignore
            return psutil.pid_exists(pid)
        except ImportError:
            try:
                out = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                    capture_output=True, text=True, timeout=3, check=False,
                )
                return f" {pid} " in out.stdout
            except (FileNotFoundError, subprocess.TimeoutExpired):
                return False
    # POSIX: signal-0 raises ProcessLookupError if the PID is gone.
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
