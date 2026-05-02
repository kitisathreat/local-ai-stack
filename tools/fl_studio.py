"""
title: FL Studio — Project Open + Headless Render + MIDI Send
author: local-ai-stack
description: Open FL Studio projects (.flp) and MIDI files in the GUI; render projects to WAV/MP3/OGG/FLAC headlessly via FL64.exe's `/R` flag; install Python MIDI Scripting controller-surface scripts into FL's user data folder; optionally send raw MIDI to a running FL Studio instance via mido/python-rtmidi when those packages are available.
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


def _default_user_data() -> str:
    docs = Path.home() / "Documents"
    return str(docs / "Image-Line" / "FL Studio" / "Settings" / "Hardware")


class Tools:
    class Valves(BaseModel):
        FL_EXE: str = Field(
            default=r"C:\Program Files\Image-Line\FL Studio 21\FL64.exe",
            description="Path to FL64.exe (or FL.exe). Update for your installed FL Studio version.",
        )
        USER_DATA: str = Field(
            default_factory=_default_user_data,
            description="FL Studio Hardware/MIDI script root (Documents\\Image-Line\\FL Studio\\Settings\\Hardware).",
        )
        DEFAULT_RENDER_FORMAT: str = Field(
            default="wav",
            description="Default audio format for headless render: wav, mp3, ogg, flac, mid.",
        )
        DEFAULT_TIMEOUT_SECS: int = Field(
            default=900,
            description="Cap on a headless render call (15 minutes).",
        )

    def __init__(self):
        self.valves = self.Valves()

    # ── Helpers ───────────────────────────────────────────────────────────

    def _which(self) -> str:
        exe = self.valves.FL_EXE
        if Path(exe).exists():
            return exe
        located = shutil.which(exe) or shutil.which("FL64") or shutil.which("FL")
        if located:
            return located
        raise FileNotFoundError(f"FL Studio binary not found: {exe}")

    # ── Launch / open ─────────────────────────────────────────────────────

    def launch_fl_studio(
        self,
        project: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Open FL Studio. If `project` is given, opens the .flp on launch.
        :param project: Optional path to a .flp project.
        :return: Confirmation with PID.
        """
        argv = [self._which()]
        if project:
            argv.append(str(Path(project).expanduser().resolve()))
        kwargs: dict = {"close_fds": True}
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x00000008 | 0x00000200
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(argv, **kwargs)
        return f"opened FL Studio (pid={proc.pid}){' with ' + project if project else ''}"

    def open_project(self, path: str, __user__: Optional[dict] = None) -> str:
        """
        Open a .flp project in FL Studio.
        :param path: Path to .flp file.
        :return: Confirmation.
        """
        return self.launch_fl_studio(project=path)

    def open_midi(self, path: str, __user__: Optional[dict] = None) -> str:
        """
        Open a .mid file in FL Studio (passes the file as the launch argument).
        :param path: Path to .mid file.
        :return: Confirmation.
        """
        return self.launch_fl_studio(project=path)

    # ── Headless render ───────────────────────────────────────────────────

    def render_project(
        self,
        project: str,
        output: str = "",
        format: str = "",
        timeout_secs: int = 0,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Render a .flp project to audio (or MIDI) headlessly via FL64.exe's
        `/R` (render) flag. FL still opens a small UI for the render but does
        not require user interaction. Output goes to <project_dir>/<project>.wav
        unless `output` is provided.
        :param project: Path to .flp.
        :param output: Optional output file path.
        :param format: wav, mp3, ogg, flac, mid (defaults to DEFAULT_RENDER_FORMAT).
        :param timeout_secs: Cap on the wait. 0 → DEFAULT_TIMEOUT_SECS.
        :return: Combined stdout/stderr and exit code.
        """
        p = Path(project).expanduser().resolve()
        if not p.exists():
            return f"Not found: {p}"
        fmt = (format or self.valves.DEFAULT_RENDER_FORMAT).lower()
        argv = [self._which(), "/R", f"/F{fmt}"]
        if output:
            argv += ["/E", str(Path(output).expanduser().resolve())]
        argv.append(str(p))
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

    # ── MIDI Scripting (controller surfaces) ──────────────────────────────

    def install_midi_script(
        self,
        name: str,
        source: str,
        device_xml: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Install an FL Studio MIDI Scripting "controller surface" Python script
        into the user data Hardware folder so FL picks it up on next start
        (Options → MIDI Settings → Controller type → Scripted devices).
        :param name: Script folder/file name (no spaces).
        :param source: Python source text (uses FL's `device.*` API). Saved as device_<name>.py.
        :param device_xml: Optional MIDI Devices XML file content. Saved as <name>.xml.
        :return: Confirmation with paths written.
        """
        if not name or any(c in name for c in r' /\:*?"<>|'):
            return f"Invalid name: {name!r}"
        root = Path(self.valves.USER_DATA).expanduser() / name
        root.mkdir(parents=True, exist_ok=True)
        py_path = root / f"device_{name}.py"
        py_path.write_text(source, encoding="utf-8")
        out = [f"wrote {py_path}"]
        if device_xml:
            xml_path = root / f"{name}.xml"
            xml_path.write_text(device_xml, encoding="utf-8")
            out.append(f"wrote {xml_path}")
        out.append("Restart FL Studio and select the script under MIDI Settings → Controller type.")
        return "\n".join(out)

    def list_midi_scripts(self, __user__: Optional[dict] = None) -> str:
        """
        List installed MIDI Scripting controller surfaces.
        :return: Newline-delimited list of script directories.
        """
        d = Path(self.valves.USER_DATA).expanduser()
        if not d.exists():
            return f"(no Hardware dir yet) {d}"
        rows = [str(p) for p in sorted(d.iterdir()) if p.is_dir()]
        return "\n".join(rows) if rows else "(no installed scripts)"

    # ── Live MIDI to FL ───────────────────────────────────────────────────

    def list_midi_ports(self, __user__: Optional[dict] = None) -> str:
        """
        List available MIDI output ports on the system. Requires the optional
        `mido` + `python-rtmidi` packages — installs as `pip install mido python-rtmidi`.
        :return: Available output port names, newline-delimited.
        """
        try:
            import mido  # type: ignore
        except ImportError:
            return "mido not installed. Run: pip install mido python-rtmidi"
        try:
            outs = mido.get_output_names()
        except Exception as e:
            return f"failed to enumerate MIDI ports: {e}"
        return "\n".join(outs) if outs else "(no MIDI output ports)"

    def send_midi_message(
        self,
        port_name: str,
        message_type: str,
        note: int = 60,
        velocity: int = 100,
        channel: int = 0,
        control: int = 1,
        value: int = 64,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Send a single MIDI message to a port (e.g. FL Studio's loopback or a
        hardware controller). Supports note_on, note_off, control_change.
        Requires `mido` + `python-rtmidi`.
        :param port_name: Exact MIDI output port name (see list_midi_ports).
        :param message_type: note_on, note_off, control_change.
        :param note: Note number (note_on/off only).
        :param velocity: Velocity 0-127 (note_on/off).
        :param channel: 0-15.
        :param control: CC number (control_change).
        :param value: CC value (control_change).
        :return: Confirmation or error.
        """
        try:
            import mido  # type: ignore
        except ImportError:
            return "mido not installed. Run: pip install mido python-rtmidi"
        try:
            with mido.open_output(port_name) as port:
                if message_type == "note_on":
                    msg = mido.Message("note_on", note=note, velocity=velocity, channel=channel)
                elif message_type == "note_off":
                    msg = mido.Message("note_off", note=note, velocity=velocity, channel=channel)
                elif message_type == "control_change":
                    msg = mido.Message("control_change", control=control, value=value, channel=channel)
                else:
                    return f"unsupported message_type: {message_type}"
                port.send(msg)
            return f"sent {msg!r} -> {port_name}"
        except Exception as e:
            return f"failed: {e}"

    def send_midi_file(
        self,
        port_name: str,
        midi_path: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Stream a .mid file to a MIDI output port in real time. Requires `mido`
        + `python-rtmidi`. Useful for piping a generated melody into FL Studio's
        loopback MIDI port.
        :param port_name: Exact MIDI output port name.
        :param midi_path: Path to .mid.
        :return: Confirmation when finished.
        """
        try:
            import mido  # type: ignore
        except ImportError:
            return "mido not installed. Run: pip install mido python-rtmidi"
        p = Path(midi_path).expanduser().resolve()
        if not p.exists():
            return f"Not found: {p}"
        try:
            mid = mido.MidiFile(str(p))
            with mido.open_output(port_name) as port:
                for msg in mid.play():
                    port.send(msg)
            return f"streamed {p} -> {port_name}"
        except Exception as e:
            return f"failed: {e}"
