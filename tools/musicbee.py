"""
title: MusicBee — Local Music Player Control
author: local-ai-stack
description: Drive a local MusicBee install via its command-line switches: launch the player, play / pause / next / previous, set volume, mute, queue and play specific files, and open library or playlist files. Also reads MusicBee's "MusicBee Library.mbl" path so the model can confirm where the library lives.
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


def _default_musicbee_exe() -> str:
    candidates = [
        Path(r"C:\Program Files (x86)\MusicBee\MusicBee.exe"),
        Path(r"C:\Program Files\MusicBee\MusicBee.exe"),
        Path.home() / "AppData/Local/MusicBee/MusicBee.exe",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return str(candidates[0])


def _default_library_root() -> str:
    return str(Path.home() / "AppData/Roaming/MusicBee")


class Tools:
    class Valves(BaseModel):
        MUSICBEE_EXE: str = Field(
            default_factory=_default_musicbee_exe,
            description="Path to MusicBee.exe.",
        )
        LIBRARY_ROOT: str = Field(
            default_factory=_default_library_root,
            description="MusicBee user data folder (contains MusicBeeLibrary.mbl, Playlists/).",
        )

    def __init__(self):
        self.valves = self.Valves()

    # ── Helpers ───────────────────────────────────────────────────────────

    def _which(self) -> str:
        exe = self.valves.MUSICBEE_EXE
        if Path(exe).exists():
            return exe
        located = shutil.which(exe) or shutil.which("MusicBee")
        if located:
            return located
        raise FileNotFoundError(f"MusicBee binary not found: {exe}")

    def _spawn(self, args: list[str]) -> int:
        kwargs: dict = {"close_fds": True}
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x00000008 | 0x00000200
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen([self._which(), *args], **kwargs)
        return proc.pid

    # ── Launch / playback control ─────────────────────────────────────────

    def launch(self, __user__: Optional[dict] = None) -> str:
        """
        Open MusicBee.
        :return: Confirmation with PID.
        """
        return f"opened MusicBee (pid={self._spawn([])})"

    def play(self, __user__: Optional[dict] = None) -> str:
        """
        Resume playback (or start playing the current selection).
        :return: Confirmation.
        """
        return f"play (pid={self._spawn(['/Play'])})"

    def pause(self, __user__: Optional[dict] = None) -> str:
        """
        Pause playback.
        :return: Confirmation.
        """
        return f"pause (pid={self._spawn(['/Pause'])})"

    def play_pause(self, __user__: Optional[dict] = None) -> str:
        """
        Toggle play/pause.
        :return: Confirmation.
        """
        return f"play/pause (pid={self._spawn(['/PlayPause'])})"

    def stop(self, __user__: Optional[dict] = None) -> str:
        """
        Stop playback.
        :return: Confirmation.
        """
        return f"stop (pid={self._spawn(['/Stop'])})"

    def next_track(self, __user__: Optional[dict] = None) -> str:
        """
        Skip to the next track.
        :return: Confirmation.
        """
        return f"next (pid={self._spawn(['/Next'])})"

    def previous_track(self, __user__: Optional[dict] = None) -> str:
        """
        Skip to the previous track.
        :return: Confirmation.
        """
        return f"previous (pid={self._spawn(['/Previous'])})"

    def mute(self, __user__: Optional[dict] = None) -> str:
        """
        Toggle mute.
        :return: Confirmation.
        """
        return f"mute toggle (pid={self._spawn(['/ToggleMute'])})"

    def set_volume(self, percent: int, __user__: Optional[dict] = None) -> str:
        """
        Set MusicBee's volume (0-100).
        :param percent: Target volume 0-100.
        :return: Confirmation.
        """
        if not 0 <= percent <= 100:
            return f"volume out of range: {percent}"
        return f"volume={percent} (pid={self._spawn([f'/Volume={percent}'])})"

    # ── Library / files ───────────────────────────────────────────────────

    def open_file(self, path: str, __user__: Optional[dict] = None) -> str:
        """
        Open and start playing an audio file or playlist (.m3u/.m3u8/.pls)
        in MusicBee.
        :param path: Path to the audio or playlist file.
        :return: Confirmation.
        """
        target = Path(path).expanduser().resolve()
        if not target.exists():
            return f"Not found: {target}"
        return f"opened {target} (pid={self._spawn([str(target)])})"

    def queue_file(self, path: str, __user__: Optional[dict] = None) -> str:
        """
        Queue an audio file to play after the current track without
        interrupting it.
        :param path: Path to the audio file.
        :return: Confirmation.
        """
        target = Path(path).expanduser().resolve()
        if not target.exists():
            return f"Not found: {target}"
        return f"queued {target} (pid={self._spawn(['/QueueLast', str(target)])})"

    def list_playlists(self, __user__: Optional[dict] = None) -> str:
        """
        List MusicBee playlist files (.mbp / .m3u / .m3u8) in the user
        Playlists folder.
        :return: Newline-delimited playlist paths.
        """
        d = Path(self.valves.LIBRARY_ROOT).expanduser() / "Playlists"
        if not d.exists():
            return f"(no playlists dir) {d}"
        rows = [str(p) for p in sorted(d.glob("*.*"))
                if p.suffix.lower() in {".mbp", ".m3u", ".m3u8", ".pls", ".xspf"}]
        return "\n".join(rows) if rows else "(no playlists)"

    def library_path(self, __user__: Optional[dict] = None) -> str:
        """
        Show the location of MusicBee's library database (MusicBeeLibrary.mbl).
        :return: Path or "not found".
        """
        d = Path(self.valves.LIBRARY_ROOT).expanduser()
        for cand in ("MusicBeeLibrary.mbl", "Library/MusicBeeLibrary.mbl"):
            p = d / cand
            if p.exists():
                return str(p)
        return f"(not found under {d})"
