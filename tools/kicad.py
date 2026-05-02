"""
title: KiCad — Schematic / PCB Open & CLI Automation
author: local-ai-stack
description: Open KiCad projects, schematics (.kicad_sch), and PCBs (.kicad_pcb) in the GUI; run KiCad's headless CLI (`kicad-cli`, KiCad 7+) to export Gerbers, drill files, STEP 3D models, schematic PDFs, BOMs, and netlists; run ERC and DRC programmatically. Default executable paths target Windows installs and can be overridden in the Valves.
required_open_webui_version: 0.4.0
requirements:
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


class Tools:
    class Valves(BaseModel):
        KICAD_EXE: str = Field(
            default=r"C:\Program Files\KiCad\9.0\bin\kicad.exe",
            description="Path to the KiCad GUI binary (kicad.exe). Used to open projects.",
        )
        KICAD_CLI: str = Field(
            default=r"C:\Program Files\KiCad\9.0\bin\kicad-cli.exe",
            description="Path to kicad-cli.exe (KiCad 7+). Used for headless ERC/DRC/exports.",
        )
        DEFAULT_TIMEOUT_SECS: int = Field(
            default=120,
            description="Default timeout for kicad-cli operations.",
        )

    def __init__(self):
        self.valves = self.Valves()

    # ── Helpers ───────────────────────────────────────────────────────────

    def _resolve(self, path: str) -> Path:
        if not path:
            raise ValueError("path is required")
        return Path(path).expanduser().resolve()

    def _which(self, exe: str) -> str:
        if Path(exe).exists():
            return exe
        located = shutil.which(exe)
        if located:
            return located
        raise FileNotFoundError(f"KiCad binary not found: {exe}")

    def _run_cli(self, args: list[str], timeout: int = 0) -> str:
        cli = self._which(self.valves.KICAD_CLI)
        timeout = timeout or self.valves.DEFAULT_TIMEOUT_SECS
        try:
            res = subprocess.run(
                [cli, *args], capture_output=True, text=True,
                timeout=timeout, check=False,
            )
        except subprocess.TimeoutExpired as e:
            return f"timeout after {timeout}s\nstdout:\n{e.stdout or ''}\nstderr:\n{e.stderr or ''}"
        return (
            f"exit={res.returncode}  argv: kicad-cli {' '.join(args)}\n"
            f"---- stdout ----\n{res.stdout}\n---- stderr ----\n{res.stderr}"
        )

    def _open_in_gui(self, target: Path) -> str:
        gui = self._which(self.valves.KICAD_EXE)
        kwargs: dict = {"close_fds": True}
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x00000008 | 0x00000200
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen([gui, str(target)], **kwargs)
        return f"opened in KiCad: {target} (pid={proc.pid})"

    # ── GUI launch ────────────────────────────────────────────────────────

    def open_project(self, path: str, __user__: Optional[dict] = None) -> str:
        """
        Open a KiCad project (.kicad_pro) in the KiCad project manager GUI.
        :param path: Absolute path to the .kicad_pro file.
        :return: Confirmation with PID.
        """
        p = self._resolve(path)
        if p.suffix.lower() != ".kicad_pro":
            return f"Expected a .kicad_pro file, got: {p.suffix}"
        return self._open_in_gui(p)

    def open_schematic(self, path: str, __user__: Optional[dict] = None) -> str:
        """
        Open a schematic (.kicad_sch) in the eeschema editor.
        :param path: Path to the .kicad_sch file.
        :return: Confirmation.
        """
        p = self._resolve(path)
        return self._open_in_gui(p)

    def open_pcb(self, path: str, __user__: Optional[dict] = None) -> str:
        """
        Open a PCB layout (.kicad_pcb) in the pcbnew editor.
        :param path: Path to the .kicad_pcb file.
        :return: Confirmation.
        """
        p = self._resolve(path)
        return self._open_in_gui(p)

    # ── Headless operations (kicad-cli) ──────────────────────────────────

    def run_erc(
        self,
        schematic: str,
        report_path: str = "",
        format: str = "report",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Run Electrical Rules Check on a schematic. Returns the report (or
        writes it to disk).
        :param schematic: Path to .kicad_sch (or .kicad_pro).
        :param report_path: Optional file to save the ERC report (.rpt or .json).
        :param format: "report", "json".
        :return: kicad-cli output / exit code.
        """
        s = self._resolve(schematic)
        args = ["sch", "erc", "--format", format]
        if report_path:
            args += ["--output", str(self._resolve(report_path))]
        args.append(str(s))
        return self._run_cli(args)

    def run_drc(
        self,
        pcb: str,
        report_path: str = "",
        format: str = "report",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Run Design Rules Check on a PCB layout.
        :param pcb: Path to .kicad_pcb.
        :param report_path: Optional file to save the DRC report (.rpt or .json).
        :param format: "report", "json".
        :return: kicad-cli output.
        """
        p = self._resolve(pcb)
        args = ["pcb", "drc", "--format", format]
        if report_path:
            args += ["--output", str(self._resolve(report_path))]
        args.append(str(p))
        return self._run_cli(args)

    def export_gerbers(
        self,
        pcb: str,
        output_dir: str,
        layers: str = "F.Cu,B.Cu,F.SilkS,B.SilkS,F.Mask,B.Mask,Edge.Cuts",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Export Gerber files for a PCB. The default layer set covers a 2-layer
        manufacturable board; pass a comma-separated list to customise.
        :param pcb: Path to .kicad_pcb.
        :param output_dir: Directory to write Gerber files into (created if missing).
        :param layers: Comma-separated KiCad layer names.
        :return: kicad-cli output.
        """
        p = self._resolve(pcb)
        out = self._resolve(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        args = ["pcb", "export", "gerbers", "--output", str(out), "--layers", layers, str(p)]
        return self._run_cli(args)

    def export_drill(
        self,
        pcb: str,
        output_dir: str,
        format: str = "excellon",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Export drill files (Excellon by default).
        :param pcb: Path to .kicad_pcb.
        :param output_dir: Directory for output files.
        :param format: "excellon" or "gerber".
        :return: kicad-cli output.
        """
        p = self._resolve(pcb)
        out = self._resolve(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        args = ["pcb", "export", "drill", "--format", format, "--output", str(out), str(p)]
        return self._run_cli(args)

    def export_step(
        self,
        pcb: str,
        output: str,
        subst_models: bool = True,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Export the PCB as a STEP 3D model — useful for mechanical CAD review
        (e.g. dropping the board into Fusion 360 or FreeCAD).
        :param pcb: Path to .kicad_pcb.
        :param output: Path to the output .step file.
        :param subst_models: When True, substitutes 3D models referenced by footprints.
        :return: kicad-cli output.
        """
        p = self._resolve(pcb)
        out = self._resolve(output)
        out.parent.mkdir(parents=True, exist_ok=True)
        args = ["pcb", "export", "step", "--output", str(out)]
        if subst_models:
            args.append("--subst-models")
        args.append(str(p))
        return self._run_cli(args, timeout=300)

    def export_schematic_pdf(
        self,
        schematic: str,
        output: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Render the schematic to a PDF.
        :param schematic: Path to .kicad_sch.
        :param output: Output .pdf path.
        :return: kicad-cli output.
        """
        s = self._resolve(schematic)
        out = self._resolve(output)
        out.parent.mkdir(parents=True, exist_ok=True)
        return self._run_cli(["sch", "export", "pdf", "--output", str(out), str(s)])

    def export_bom(
        self,
        schematic: str,
        output: str,
        format: str = "csv",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Export a Bill of Materials from a schematic.
        :param schematic: Path to .kicad_sch.
        :param output: Output file (.csv or .xml).
        :param format: "csv" or "xml".
        :return: kicad-cli output.
        """
        s = self._resolve(schematic)
        out = self._resolve(output)
        out.parent.mkdir(parents=True, exist_ok=True)
        args = ["sch", "export", "bom" if format == "csv" else "python-bom",
                "--output", str(out), str(s)]
        return self._run_cli(args)

    def export_netlist(
        self,
        schematic: str,
        output: str,
        format: str = "kicadsexpr",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Export a netlist for downstream simulation or PCB import.
        :param schematic: Path to .kicad_sch.
        :param output: Output netlist path.
        :param format: "kicadsexpr", "orcadpcb2", "spice", "cadstar", "allegro".
        :return: kicad-cli output.
        """
        s = self._resolve(schematic)
        out = self._resolve(output)
        out.parent.mkdir(parents=True, exist_ok=True)
        return self._run_cli(["sch", "export", "netlist", "--format", format,
                              "--output", str(out), str(s)])

    def cli_version(self, __user__: Optional[dict] = None) -> str:
        """
        Return the kicad-cli version string. Use as a smoke test.
        :return: stdout from `kicad-cli --version`.
        """
        return self._run_cli(["--version"], timeout=10)
