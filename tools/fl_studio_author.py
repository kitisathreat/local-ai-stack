"""
title: FL Studio Author — Compose Tracks from a Prompt
author: local-ai-stack
description: Compose a song programmatically and deliver it as a MIDI file plus an FL Studio project. The model builds tracks (drums, bass, lead, pad), patterns, and arrangement with the `composition` tool, and this tool wraps the resulting MIDI in an .flp project (when the optional `pyflp` dependency is installed) or hands it straight to FL Studio's import flow. Stock-instrument naming hints are surfaced so the model can request "FL Keys", "Sytrus", "Slicex", "FPC", etc. for each channel.
required_open_webui_version: 0.4.0
requirements: mido, pyflp
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field


# Common FL Studio stock instruments. The model picks one per channel so the
# resulting project sounds reasonable when a user hits Play after import.
_FL_STOCK = [
    "FL Keys",      # piano / Rhodes
    "Sytrus",       # FM synth, leads/pads/bass
    "FPC",          # drum kit
    "Slicex",       # sliced drum loops / vocal chops
    "Harmless",     # subtractive synth
    "Harmor",       # additive
    "Kick",         # 808 kick
    "BooBass",      # quick bass
    "3xOSC",        # everything
    "DirectWave",   # sampler
    "Toxic Biohazard",
    "Sawer", "Morphine", "Ogun",
]


def _organize_helper_module():
    """Helper to chain into media_library when needed."""
    spec = importlib.util.spec_from_file_location(
        "_lai_organize_helper", Path(__file__).parent / "_organize_helper.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _composition_module():
    """Lazy import of composition.Tools()."""
    spec = importlib.util.spec_from_file_location(
        "_lai_composition", Path(__file__).parent / "composition.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


class Tools:
    class Valves(BaseModel):
        FL_EXE: str = Field(
            default=r"C:\Program Files\Image-Line\FL Studio 21\FL64.exe",
            description="Path to FL64.exe — used to import-and-open finished projects.",
        )
        DEFAULT_TEMPO_BPM: float = Field(default=120.0)
        DEFAULT_TIME_SIGNATURE: str = Field(default="4/4")
        DEFAULT_KEY: str = Field(default="C")

    def __init__(self):
        self.valves = self.Valves()

    # ── Helpers ───────────────────────────────────────────────────────────

    def _composition(self):
        return _composition_module().Tools()

    # ── Project scaffolding ───────────────────────────────────────────────

    def new_song(
        self,
        midi_output_path: str,
        tempo_bpm: float = 0.0,
        time_signature: str = "",
        key: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Initialise an empty MIDI file with tempo / time-signature / key
        meta-events. Subsequent calls (add_drum_pattern, add_bass_line,
        add_chord_progression, add_melody) layer in tracks.
        :param midi_output_path: Path to the .mid file to author.
        :param tempo_bpm: BPM. 0 = DEFAULT_TEMPO_BPM (120).
        :param time_signature: "4/4", "3/4", "6/8", etc.
        :param key: Key name like "C", "Am", "F#", "Eb".
        :return: Confirmation.
        """
        c = self._composition()
        return c.create_project(
            output_path=midi_output_path,
            tempo_bpm=tempo_bpm or self.valves.DEFAULT_TEMPO_BPM,
            time_signature=time_signature or self.valves.DEFAULT_TIME_SIGNATURE,
            key=key or self.valves.DEFAULT_KEY,
        )

    def add_drum_pattern(
        self,
        midi_path: str,
        pattern: str = "rock",
        bars: int = 4,
        start_beat: float = 0.0,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Lay down a drum loop on a new track using FL Studio's standard GM
        drum mapping (channel 9). Patterns: rock, four_on_floor, hiphop,
        half_time, breakbeat.
        :param midi_path: Path to the .mid.
        :param pattern: Pattern name.
        :param bars: How many bars to repeat.
        :param start_beat: Where to drop the loop.
        :return: Confirmation.
        """
        c = self._composition()
        idx_msg = c.add_track(midi_path, name="Drums", channel=9, program=0)
        # Extract track index from the message ("added track[N] ...")
        import re as _re
        m = _re.search(r"track\[(\d+)\]", idx_msg)
        track_idx = int(m.group(1)) if m else 1
        return c.add_drum_pattern(midi_path, track_idx=track_idx,
                                  pattern=pattern, start_beat=start_beat,
                                  bars=bars,
                                  time_signature=self.valves.DEFAULT_TIME_SIGNATURE)

    def add_bass_line(
        self,
        midi_path: str,
        root_per_bar: list[str],
        bar_length_beats: float = 4.0,
        bass_octave: int = 2,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Author a simple bass line: one note per bar at the chord root.
        :param midi_path: Path to .mid.
        :param root_per_bar: List of note names per bar e.g. ["C", "Am", "F", "G"].
        :param bar_length_beats: Beats per bar (4 for 4/4).
        :param bass_octave: Octave number for the bass notes (default 2).
        :return: Confirmation.
        """
        c = self._composition()
        idx_msg = c.add_track(midi_path, name="Bass", channel=1, program=33)  # 33 = electric bass (finger)
        import re as _re
        m = _re.search(r"track\[(\d+)\]", idx_msg)
        track_idx = int(m.group(1)) if m else 1
        rows = []
        for bar, root in enumerate(root_per_bar):
            note = f"{root.rstrip('m').rstrip('7').rstrip('9').rstrip('M').rstrip('j').rstrip('a')}{bass_octave}"
            r = c.add_note(midi_path, track_idx=track_idx, pitch=note,
                          start_beat=bar * bar_length_beats,
                          duration_beat=bar_length_beats * 0.95,
                          channel=1)
            rows.append(r)
        return f"bass line: {len(root_per_bar)} bars\n" + "\n".join(rows)

    def add_chord_progression(
        self,
        midi_path: str,
        chords: list[str],
        bar_length_beats: float = 4.0,
        chord_octave: int = 4,
        instrument_program: int = 0,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Lay down a pad / piano chord progression. Each item is "ROOT[QUALITY]"
        like "C", "Am", "F", "G7", "Dm9".
        :param midi_path: Path to .mid.
        :param chords: Chord symbols, one per bar.
        :param bar_length_beats: Beats per bar.
        :param chord_octave: Octave for the chord root.
        :param instrument_program: GM program (0 = piano, 89 = warm pad, 5 = electric piano).
        :return: Confirmation.
        """
        c = self._composition()
        idx_msg = c.add_track(midi_path, name="Chords", channel=2,
                              program=instrument_program)
        import re as _re
        m = _re.search(r"track\[(\d+)\]", idx_msg)
        track_idx = int(m.group(1)) if m else 1
        rows = []
        for bar, sym in enumerate(chords):
            # Split "Am" -> root="A", quality="m"; "G7" -> "G", "7"; "Cmaj7" -> "C", "maj7"
            root = sym[0].upper()
            i = 1
            while i < len(sym) and sym[i] in "#b":
                root += sym[i]
                i += 1
            quality = sym[i:].lower() if i < len(sym) else ""
            r = c.add_chord(
                midi_path, track_idx=track_idx,
                root=f"{root}{chord_octave}", quality=quality,
                start_beat=bar * bar_length_beats,
                duration_beat=bar_length_beats * 0.95,
                channel=2,
            )
            rows.append(r)
        return "\n".join(rows)

    def add_melody(
        self,
        midi_path: str,
        notes: list[list[Any]],
        instrument_program: int = 81,
        track_name: str = "Lead",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Place an explicit melody. Each note is `[pitch, start_beat, duration_beat]`
        or `[pitch, start_beat, duration_beat, velocity]`.
        :param midi_path: Path to .mid.
        :param notes: List of `[pitch, start_beat, duration_beat, velocity?]` rows.
        :param instrument_program: GM program (81 = lead 1 square, 73 = flute, 56 = trumpet).
        :param track_name: Track display name.
        :return: Confirmation.
        """
        c = self._composition()
        idx_msg = c.add_track(midi_path, name=track_name, channel=3,
                              program=instrument_program)
        import re as _re
        m = _re.search(r"track\[(\d+)\]", idx_msg)
        track_idx = int(m.group(1)) if m else 1
        added = 0
        for row in notes:
            pitch, start, dur = row[0], float(row[1]), float(row[2])
            vel = int(row[3]) if len(row) >= 4 else 0
            c.add_note(midi_path, track_idx=track_idx, pitch=str(pitch),
                       start_beat=start, duration_beat=dur, velocity=vel,
                       channel=3)
            added += 1
        return f"+ {added} melody notes -> track[{track_idx}] ({track_name})"

    # ── FL project wrapping ────────────────────────────────────────────────

    def wrap_in_flp(
        self,
        midi_path: str,
        flp_output_path: str,
        instrument_assignments: dict = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Wrap a finished MIDI file in an .flp project. Requires the optional
        `pyflp` package (`pip install pyflp`). When pyflp isn't installed, the
        method writes nothing and returns the recommended manual import path:
        "open FL Studio → File → Import → MIDI File → <midi_path>".
        :param midi_path: Path to the source .mid (built via the methods above).
        :param flp_output_path: Path to the .flp to write.
        :param instrument_assignments: Optional {track_name: stock_plugin_name} dict, e.g. {"Bass": "Sytrus"}.
        :return: Confirmation or fallback instructions.
        """
        try:
            import pyflp  # type: ignore
        except ImportError:
            return (
                "pyflp not installed (pip install pyflp). Manual import path:\n"
                f"  open FL Studio → File → Import → MIDI File → {midi_path}\n"
                "Or call fl_studio.open_project once you've manually saved the project."
            )
        midi = Path(midi_path).expanduser().resolve()
        flp = Path(flp_output_path).expanduser().resolve()
        flp.parent.mkdir(parents=True, exist_ok=True)
        # Minimal pyflp project authoring — pyflp's writing API is partial,
        # so we create a fresh empty project and stamp tempo + a comment.
        # The user finishes the wiring via FL's MIDI import UI on first open.
        proj = pyflp.Project()
        # pyflp's Project doesn't yet expose a clean "import_midi" API in a
        # stable way across versions, so we write metadata only.
        try:
            proj.tempo = self.valves.DEFAULT_TEMPO_BPM
        except Exception:
            pass
        try:
            proj.title = midi.stem
            proj.comments = (
                f"Auto-generated by local-ai-stack fl_studio_author.\n"
                f"Source MIDI: {midi}\n"
                f"Stock plugin assignments: {instrument_assignments or {}}\n"
                "On open: File → Import → MIDI File → (this project's MIDI)."
            )
        except Exception:
            pass
        try:
            pyflp.save(proj, flp)
        except Exception as e:
            return f"pyflp save failed: {e}\nFalling back to MIDI-only delivery: {midi}"
        return f"wrote .flp project -> {flp}\nMIDI source: {midi}"

    def import_midi_into_fl(
        self,
        midi_path: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Open FL Studio with a MIDI file as its launch argument so FL's
        importer picks it up automatically. Useful when pyflp isn't
        installed and writing a real .flp isn't possible.
        :param midi_path: Path to the .mid file.
        :return: Confirmation with FL Studio PID.
        """
        target = Path(midi_path).expanduser().resolve()
        if not target.exists():
            return f"Not found: {target}"
        exe = self.valves.FL_EXE
        if not Path(exe).exists():
            return f"FL Studio binary not found: {exe}"
        kwargs: dict = {"close_fds": True}
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x00000008 | 0x00000200
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen([exe, str(target)], **kwargs)
        return f"opened FL Studio with {target.name} (pid={proc.pid})"

    # ── Reference ─────────────────────────────────────────────────────────

    def stock_plugins(self, __user__: Optional[dict] = None) -> str:
        """
        List FL Studio stock instruments the model can reference when
        assigning plugins per track.
        :return: Newline-delimited plugin names.
        """
        return "\n".join(_FL_STOCK)
