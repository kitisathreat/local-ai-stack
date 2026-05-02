"""
title: Application Launcher — Open Programs and Files
author: local-ai-stack
description: Launch desktop programs (KiCad, Blender, Fusion 360, FL Studio, Synthesizer V Studio, browsers, anything else on PATH) and open files in their default handlers. Discovers known apps via the APPS valve, falls back to `os.startfile` on Windows / `open` on macOS / `xdg-open` on Linux. Process spawning is non-blocking — the model gets a PID back and can monitor or terminate it via the same tool.
required_open_webui_version: 0.4.0
requirements:
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


# Default executable hints — Windows-first since that's the native target,
# with sensible fallbacks for cross-platform development. The user can
# override any of these in the Valves UI.
_DEFAULT_APPS: dict[str, str] = {
    "kicad":        r"C:\Program Files\KiCad\9.0\bin\kicad.exe",
    "kicad_cli":    r"C:\Program Files\KiCad\9.0\bin\kicad-cli.exe",
    "blender":      r"C:\Program Files\Blender Foundation\Blender 4.2\blender.exe",
    "fusion360":    str(Path.home() / "AppData/Local/Autodesk/webdeploy/production/Fusion360.exe"),
    "fl_studio":    r"C:\Program Files\Image-Line\FL Studio 21\FL64.exe",
    "synthv":       r"C:\Program Files\Synthesizer V Studio Pro\synthv-studio.exe",
    "synthv_cli":   r"C:\Program Files\Synthesizer V Studio Pro\synthv-cli.exe",
    "explorer":     "explorer.exe",
    "notepad":      "notepad.exe",
    "vscode":       "code",
    "powershell":   "powershell.exe",
    "cmd":          "cmd.exe",
}


class Tools:
    class Valves(BaseModel):
        APPS: dict[str, str] = Field(
            default_factory=lambda: dict(_DEFAULT_APPS),
            description=(
                "Friendly-name -> absolute path or PATH-resolvable executable. "
                "Override paths here when your install lives in a non-default location "
                "(e.g. KiCad 8 vs 9, Blender LTS, FL Studio 24)."
            ),
        )
        ALLOW_ARBITRARY_EXEC: bool = Field(
            default=False,
            description=(
                "When False (default), launch_program() can only call entries listed in APPS. "
                "Flip to True to also allow arbitrary executables found on PATH."
            ),
        )
        DEFAULT_TIMEOUT_SECS: int = Field(
            default=0,
            description=(
                "If > 0, run_command() waits up to this long for the process and "
                "returns stdout/stderr. 0 = fire-and-forget (returns PID immediately)."
            ),
        )

    def __init__(self):
        self.valves = self.Valves()

    # ── Resolution helpers ────────────────────────────────────────────────

    def _resolve_app(self, name_or_path: str) -> str:
        """Return an absolute path or PATH-resolvable name for the executable.

        Order: APPS lookup → PATH lookup (if allowed) → raw path (if allowed).
        Raises PermissionError when ALLOW_ARBITRARY_EXEC is off and the
        executable is not in APPS.
        """
        apps = self.valves.APPS
        if name_or_path in apps:
            return apps[name_or_path]

        if not self.valves.ALLOW_ARBITRARY_EXEC:
            raise PermissionError(
                f"'{name_or_path}' is not registered in APPS and ALLOW_ARBITRARY_EXEC is False. "
                f"Add it via the admin Tools panel or pick one of: {sorted(apps)}"
            )

        located = shutil.which(name_or_path)
        if located:
            return located
        if Path(name_or_path).exists():
            return name_or_path
        raise FileNotFoundError(f"Executable not found: {name_or_path}")

    # ── Discovery ─────────────────────────────────────────────────────────

    def list_known_apps(self, __user__: Optional[dict] = None) -> str:
        """
        List the friendly app names registered in APPS, with installed/missing status.
        :return: One row per app with its configured path and existence flag.
        """
        rows = []
        for name, path in sorted(self.valves.APPS.items()):
            exists = Path(path).exists() or shutil.which(path) is not None
            rows.append(f"{'OK ' if exists else '-- '} {name:<14} {path}")
        return "\n".join(rows) if rows else "(no apps configured)"

    # ── Spawning ──────────────────────────────────────────────────────────

    def launch_program(
        self,
        app: str,
        args: list[str] = None,
        working_dir: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Spawn a known program (by friendly name) with optional arguments. Returns
        the PID immediately — the program runs detached so the chat keeps moving.
        :param app: Friendly name from APPS (e.g. "blender", "kicad", "fl_studio").
        :param args: Extra command-line arguments to pass to the program.
        :param working_dir: Optional working directory.
        :return: Confirmation including the spawned PID.
        """
        exe = self._resolve_app(app)
        cmd = [exe] + list(args or [])
        cwd = working_dir or None

        kwargs: dict = {"cwd": cwd, "close_fds": True}
        if sys.platform == "win32":
            # DETACHED_PROCESS = 0x00000008 — survive parent exit.
            kwargs["creationflags"] = 0x00000008 | 0x00000200  # + CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True

        proc = subprocess.Popen(cmd, **kwargs)
        return f"launched {app} (pid={proc.pid}) — argv: {' '.join(shlex.quote(c) for c in cmd)}"

    def run_command(
        self,
        app: str,
        args: list[str] = None,
        working_dir: str = "",
        timeout_secs: int = 0,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Run a program and capture stdout/stderr. Use for CLI tools (kicad-cli,
        blender -b, synthv-cli) where you want the output back in chat. Set
        timeout_secs > 0 to wait; 0 falls back to DEFAULT_TIMEOUT_SECS.
        :param app: Friendly name from APPS or, if ALLOW_ARBITRARY_EXEC, a raw path.
        :param args: Command-line arguments.
        :param working_dir: Optional working directory.
        :param timeout_secs: Max seconds to wait. 0 = use DEFAULT_TIMEOUT_SECS.
        :return: Combined output, exit code, and elapsed time.
        """
        import time
        exe = self._resolve_app(app)
        cmd = [exe] + list(args or [])
        cwd = working_dir or None
        timeout = timeout_secs if timeout_secs > 0 else self.valves.DEFAULT_TIMEOUT_SECS
        if timeout <= 0:
            # Mirror launch_program's fire-and-forget behaviour for scripts that
            # genuinely should detach.
            return self.launch_program(app, args=args, working_dir=working_dir)

        start = time.monotonic()
        try:
            res = subprocess.run(
                cmd, cwd=cwd, capture_output=True, text=True,
                timeout=timeout, check=False,
            )
        except subprocess.TimeoutExpired as e:
            return f"timeout after {timeout}s — partial stdout:\n{e.stdout or ''}\n\nstderr:\n{e.stderr or ''}"

        elapsed = time.monotonic() - start
        return (
            f"exit={res.returncode}  elapsed={elapsed:.2f}s  argv={' '.join(shlex.quote(c) for c in cmd)}\n"
            f"---- stdout ----\n{res.stdout}\n"
            f"---- stderr ----\n{res.stderr}"
        )

    def open_file(self, path: str, __user__: Optional[dict] = None) -> str:
        """
        Open a file with the OS-registered default application (e.g. .docx in
        Word, .blend in Blender, .flp in FL Studio, .svp in Synthesizer V Studio).
        :param path: Absolute path to the file.
        :return: Confirmation.
        """
        target = Path(path).expanduser()
        if not target.exists():
            return f"Not found: {target}"

        if sys.platform == "win32":
            os.startfile(str(target))  # type: ignore[attr-defined]
            return f"opened (via Windows shell): {target}"
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
            return f"opened (via macOS open): {target}"
        subprocess.Popen(["xdg-open", str(target)])
        return f"opened (via xdg-open): {target}"

    # ── Process management ───────────────────────────────────────────────

    def list_processes(
        self,
        name_filter: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        List running processes (best-effort). On Windows uses tasklist; on
        Unix uses `ps`. Optional case-insensitive substring filter on name.
        :param name_filter: Substring to narrow the list (e.g. "blender").
        :return: Formatted process table.
        """
        if sys.platform == "win32":
            cmd = ["tasklist", "/FO", "CSV", "/NH"]
        else:
            cmd = ["ps", "-eo", "pid,comm,etime"]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        except Exception as e:
            return f"failed to query processes: {e}"
        text = out.stdout
        if name_filter:
            needle = name_filter.lower()
            text = "\n".join(line for line in text.splitlines() if needle in line.lower())
        return text or f"(no processes matching {name_filter!r})"

    def terminate_process(
        self,
        pid: int,
        force: bool = False,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Kill a process by PID. Use SIGTERM by default; pass force=True for SIGKILL
        (or /F on Windows).
        :param pid: PID to terminate.
        :param force: Hard-kill if the process refuses to exit.
        :return: Confirmation or error message.
        """
        if pid <= 0:
            return f"refusing to kill pid={pid}"
        try:
            if sys.platform == "win32":
                cmd = ["taskkill", "/PID", str(pid)]
                if force:
                    cmd.append("/F")
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
                return f"taskkill exit={res.returncode}\n{res.stdout}{res.stderr}"
            import signal
            os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
            return f"sent {'SIGKILL' if force else 'SIGTERM'} to pid={pid}"
        except ProcessLookupError:
            return f"no such process: pid={pid}"
        except PermissionError as e:
            return f"permission denied: {e}"
        except Exception as e:
            return f"error: {e}"
