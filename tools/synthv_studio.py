"""
title: Synthesizer V Studio — Project Open + Batch Render + JS Scripting
author: local-ai-stack
description: Open Synthesizer V Studio Pro projects (.svp / .s5p) in the GUI; render projects to WAV via Synthesizer V's batch CLI (synthv-cli or synthv-studio --batch-render); install JavaScript automation scripts into SynthV's user `scripts/` folder so they appear under Scripts → User in the Pro UI. JavaScript scripts use SynthV's `SV` API to manipulate notes, parameters, vocal groups, etc.
required_open_webui_version: 0.4.0
requirements:
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


def _default_user_scripts() -> str:
    appdata = Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming")))
    return str(appdata / "Dreamtonics" / "Synthesizer V Studio" / "scripts")


class Tools:
    class Valves(BaseModel):
        SYNTHV_EXE: str = Field(
            default=r"C:\Program Files\Synthesizer V Studio Pro\synthv-studio.exe",
            description="Path to synthv-studio.exe (Synthesizer V Studio Pro GUI).",
        )
        SYNTHV_CLI: str = Field(
            default=r"C:\Program Files\Synthesizer V Studio Pro\synthv-cli.exe",
            description="Path to synthv-cli.exe (batch render CLI). Some installs only ship the GUI exe — leave blank to fall back on `synthv-studio --batch-render`.",
        )
        USER_SCRIPTS: str = Field(
            default_factory=_default_user_scripts,
            description="SynthV user scripts directory (%APPDATA%\\Dreamtonics\\Synthesizer V Studio\\scripts).",
        )
        DEFAULT_TIMEOUT_SECS: int = Field(
            default=900,
            description="Cap on a batch render call (15 minutes).",
        )

    def __init__(self):
        self.valves = self.Valves()

    # ── Helpers ───────────────────────────────────────────────────────────

    def _which_gui(self) -> str:
        exe = self.valves.SYNTHV_EXE
        if Path(exe).exists():
            return exe
        located = shutil.which(exe) or shutil.which("synthv-studio")
        if located:
            return located
        raise FileNotFoundError(f"Synthesizer V Studio binary not found: {exe}")

    def _which_cli(self) -> tuple[str, list[str]]:
        """Return (binary, leading_args) for batch-render dispatch."""
        cli = self.valves.SYNTHV_CLI
        if cli and Path(cli).exists():
            return cli, []
        # Fall back on the GUI exe with --batch-render flag (Pro-only).
        return self._which_gui(), ["--batch-render"]

    # ── Launch / open ─────────────────────────────────────────────────────

    def launch_synthv(
        self,
        project: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Open Synthesizer V Studio. Optionally loads a .svp or .s5p project on launch.
        :param project: Optional path to .svp or .s5p file.
        :return: Confirmation with PID.
        """
        argv = [self._which_gui()]
        if project:
            argv.append(str(Path(project).expanduser().resolve()))
        kwargs: dict = {"close_fds": True}
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x00000008 | 0x00000200
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(argv, **kwargs)
        return f"opened Synthesizer V (pid={proc.pid}){' with ' + project if project else ''}"

    def open_project(self, path: str, __user__: Optional[dict] = None) -> str:
        """
        Open a SynthV project in the GUI.
        :param path: Path to .svp or .s5p file.
        :return: Confirmation.
        """
        return self.launch_synthv(project=path)

    # ── Headless batch render ─────────────────────────────────────────────

    def render_project(
        self,
        project: str,
        output: str = "",
        format: str = "wav",
        timeout_secs: int = 0,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Render a SynthV project to audio headlessly via synthv-cli (or
        `synthv-studio --batch-render` if synthv-cli isn't installed).
        :param project: Path to .svp or .s5p.
        :param output: Output audio path. Defaults next to the project.
        :param format: wav or wav-mix-only (Pro batch supports separate stems).
        :param timeout_secs: Cap on the wait. 0 → DEFAULT_TIMEOUT_SECS.
        :return: Combined stdout/stderr and exit code.
        """
        p = Path(project).expanduser().resolve()
        if not p.exists():
            return f"Not found: {p}"
        out = Path(output).expanduser().resolve() if output else p.with_suffix(f".{format}")
        binary, lead = self._which_cli()
        argv = [binary, *lead, "-o", str(out), str(p)]
        timeout = timeout_secs or self.valves.DEFAULT_TIMEOUT_SECS
        try:
            res = subprocess.run(argv, capture_output=True, text=True,
                                 timeout=timeout, check=False)
        except subprocess.TimeoutExpired as e:
            return f"timeout after {timeout}s\n{e.stdout or ''}\n{e.stderr or ''}"
        return (
            f"exit={res.returncode}\nargv: {' '.join(argv)}\n"
            f"---- stdout ----\n{res.stdout}\n---- stderr ----\n{res.stderr}"
        )

    # ── JS automation scripts ─────────────────────────────────────────────

    def install_js_script(
        self,
        name: str,
        source: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Install a SynthV user JavaScript script into %APPDATA%\\Dreamtonics\\
        Synthesizer V Studio\\scripts\\ so it appears under Scripts → User in
        the Pro UI. Scripts use SynthV's `SV` JS API to read/edit notes,
        groups, parameters, and tempo.
        :param name: File name (no spaces; .js suffix added automatically).
        :param source: JavaScript source text.
        :return: Path written.
        """
        if not name or any(c in name for c in r' /\:*?"<>|'):
            return f"Invalid name: {name!r}"
        if not name.endswith(".js"):
            name = f"{name}.js"
        d = Path(self.valves.USER_SCRIPTS).expanduser()
        d.mkdir(parents=True, exist_ok=True)
        path = d / name
        path.write_text(source, encoding="utf-8")
        return f"installed SynthV script -> {path}\nReload via Scripts → Re-scan in Synthesizer V Studio Pro."

    def list_js_scripts(self, __user__: Optional[dict] = None) -> str:
        """
        List installed user JavaScript scripts for SynthV.
        :return: Newline-delimited list of .js files.
        """
        d = Path(self.valves.USER_SCRIPTS).expanduser()
        if not d.exists():
            return f"(no user scripts dir yet) {d}"
        rows = [str(p) for p in sorted(d.glob("*.js"))]
        return "\n".join(rows) if rows else "(no installed JS scripts)"

    def remove_js_script(self, name: str, __user__: Optional[dict] = None) -> str:
        """
        Delete a previously installed user JavaScript script.
        :param name: Script name (with or without .js suffix).
        :return: Confirmation.
        """
        if not name or any(c in name for c in r' /\:*?"<>|'):
            return f"Invalid name: {name!r}"
        if not name.endswith(".js"):
            name = f"{name}.js"
        path = Path(self.valves.USER_SCRIPTS).expanduser() / name
        if not path.exists():
            return f"Not found: {path}"
        path.unlink()
        return f"removed -> {path}"
