"""
title: KiCad Autoroute — Run Freerouting Against a .kicad_pcb
author: local-ai-stack
description: Automatically route the unrouted nets on a KiCad PCB using the open-source `freerouting` JAR. Workflow: kicad-cli exports the board to Specctra DSN → freerouting computes routes → result imported back as Specctra SES → kicad-cli applies the SES to the .kicad_pcb. Pair with `kicad_author` for the schematic + initial PCB scaffolding, then this tool to finish the routes.
required_open_webui_version: 0.4.0
requirements:
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field


def _kicad():
    spec = importlib.util.spec_from_file_location(
        "_lai_kicad_runner", Path(__file__).parent / "kicad.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod.Tools()


class Tools:
    class Valves(BaseModel):
        FREEROUTING_JAR: str = Field(
            default=str(Path.home() / "Documents" / "freerouting" / "freerouting.jar"),
            description="Path to freerouting.jar — download from https://github.com/freerouting/freerouting/releases.",
        )
        JAVA_EXE: str = Field(default="java", description="Java executable. Java 21+ required by recent freerouting builds.")
        PASSES: int = Field(default=20, description="Optimisation passes (more = better, slower).")
        TIMEOUT_SECS: int = Field(default=1800, description="30 min cap on freerouting.")

    def __init__(self):
        self.valves = self.Valves()

    def autoroute(
        self,
        pcb_path: str,
        output_pcb: str = "",
        passes: int = 0,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Export the .kicad_pcb to DSN, run freerouting, and re-import the
        SES into a new .kicad_pcb (defaults to writing alongside the input
        as `<name>.routed.kicad_pcb`).
        :param pcb_path: Path to .kicad_pcb.
        :param output_pcb: Optional output path. Empty = beside input.
        :param passes: Optimisation passes. 0 = PASSES default (20).
        :return: Combined log: DSN export, freerouting run, SES import.
        """
        pcb = Path(pcb_path).expanduser().resolve()
        if not pcb.exists():
            return f"Not found: {pcb}"
        if not Path(self.valves.FREEROUTING_JAR).exists():
            return f"freerouting JAR not found: {self.valves.FREEROUTING_JAR}"
        java = shutil.which(self.valves.JAVA_EXE) or self.valves.JAVA_EXE
        out_pcb = Path(output_pcb).expanduser().resolve() if output_pcb else pcb.with_suffix(".routed.kicad_pcb")
        dsn = pcb.with_suffix(".dsn")
        ses = pcb.with_suffix(".ses")
        kicad = _kicad()
        log: list[str] = []

        # 1. DSN export.
        export = kicad._run_cli(["pcb", "export", "specctra", "--output", str(dsn), str(pcb)])
        log.append("── DSN export ──\n" + export)
        if not dsn.exists():
            return "\n".join(log) + "\n(DSN file was not produced — aborting)"

        # 2. freerouting.
        try:
            res = subprocess.run(
                [java, "-jar", self.valves.FREEROUTING_JAR,
                 "-de", str(dsn), "-do", str(ses),
                 "-mp", str(passes or self.valves.PASSES)],
                capture_output=True, text=True, timeout=self.valves.TIMEOUT_SECS,
            )
            log.append(
                f"── freerouting ──\nexit={res.returncode}\n"
                f"---- stdout ----\n{res.stdout[:2000]}\n"
                f"---- stderr ----\n{res.stderr[:2000]}"
            )
        except subprocess.TimeoutExpired:
            return "\n".join(log) + f"\n(freerouting timed out after {self.valves.TIMEOUT_SECS}s)"
        except Exception as e:
            return "\n".join(log) + f"\n(freerouting failed: {e})"

        if not ses.exists():
            return "\n".join(log) + "\n(SES file was not produced — aborting)"

        # 3. SES import.
        # kicad-cli has an `import` subcommand for .ses → .kicad_pcb. Some
        # versions expect a different incantation; try the common form.
        imp = kicad._run_cli(["pcb", "import", "specctra-ses",
                              "--output", str(out_pcb), str(ses), str(pcb)])
        log.append("── SES import ──\n" + imp)
        return "\n".join(log)
