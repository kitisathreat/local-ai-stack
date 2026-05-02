"""
title: Composition — MIDI Authoring (notes, tracks, chords, scales)
author: local-ai-stack
description: Compose music programmatically. Build a MIDI file by adding tracks, notes (pitch + start beat + duration + velocity), chords, scales, and tempo / time-signature events. Also helpers for transposing, key fitting, and chord progressions. Output is a standard .mid file that imports cleanly into FL Studio, Synthesizer V Studio, MuseScore, Logic, Ableton, etc. Pair with `fl_studio_author` to wrap the MIDI in a .flp project, or with `synthv_author` to import as a vocal track.
required_open_webui_version: 0.4.0
requirements: mido
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

try:
    import mido
    HAS_MIDO = True
except ImportError:
    HAS_MIDO = False


# ── Music theory primitives ───────────────────────────────────────────────────

_NOTE_NAMES_SHARP = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
_NOTE_NAMES_FLAT  = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]

# Major / minor scale interval patterns (semitones from root).
_SCALES: dict[str, tuple[int, ...]] = {
    "major":            (0, 2, 4, 5, 7, 9, 11),
    "minor":            (0, 2, 3, 5, 7, 8, 10),     # natural minor
    "harmonic_minor":   (0, 2, 3, 5, 7, 8, 11),
    "melodic_minor":    (0, 2, 3, 5, 7, 9, 11),
    "dorian":           (0, 2, 3, 5, 7, 9, 10),
    "phrygian":         (0, 1, 3, 5, 7, 8, 10),
    "lydian":           (0, 2, 4, 6, 7, 9, 11),
    "mixolydian":       (0, 2, 4, 5, 7, 9, 10),
    "locrian":          (0, 1, 3, 5, 6, 8, 10),
    "pentatonic_major": (0, 2, 4, 7, 9),
    "pentatonic_minor": (0, 3, 5, 7, 10),
    "blues":            (0, 3, 5, 6, 7, 10),
    "chromatic":        tuple(range(12)),
}

# Common chord qualities → semitone intervals from root.
_CHORDS: dict[str, tuple[int, ...]] = {
    "":      (0, 4, 7),         # major triad
    "maj":   (0, 4, 7),
    "m":     (0, 3, 7),         # minor triad
    "dim":   (0, 3, 6),
    "aug":   (0, 4, 8),
    "sus2":  (0, 2, 7),
    "sus4":  (0, 5, 7),
    "7":     (0, 4, 7, 10),     # dominant 7
    "maj7":  (0, 4, 7, 11),
    "m7":    (0, 3, 7, 10),
    "dim7":  (0, 3, 6, 9),
    "m7b5":  (0, 3, 6, 10),     # half-diminished
    "9":     (0, 4, 7, 10, 14),
    "maj9":  (0, 4, 7, 11, 14),
    "m9":    (0, 3, 7, 10, 14),
    "add9":  (0, 4, 7, 14),
}


def _name_to_pitch(name: str) -> int:
    """Parse a note name like 'C4', 'F#3', 'Bb5' to a MIDI pitch (0-127)."""
    s = name.strip()
    if not s:
        raise ValueError("empty note name")
    # Letter + optional accidental + octave (signed integer)
    letter = s[0].upper()
    rest = s[1:]
    accidental = 0
    while rest and rest[0] in "#b":
        accidental += 1 if rest[0] == "#" else -1
        rest = rest[1:]
    if not rest:
        raise ValueError(f"missing octave in note name: {name!r}")
    octave = int(rest)
    base = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}[letter]
    return (octave + 1) * 12 + base + accidental


def _pitch_to_name(pitch: int) -> str:
    return f"{_NOTE_NAMES_SHARP[pitch % 12]}{pitch // 12 - 1}"


class Tools:
    class Valves(BaseModel):
        DEFAULT_TPB: int = Field(
            default=480,
            description="Ticks per beat. 480 is the de-facto standard (PPQN).",
        )
        DEFAULT_TEMPO_BPM: float = Field(
            default=120.0, description="Default tempo when not specified.",
        )
        DEFAULT_VELOCITY: int = Field(
            default=80,
            description="Default note-on velocity (0-127). 80 is medium-loud.",
        )

    def __init__(self):
        self.valves = self.Valves()

    # ── Helpers ───────────────────────────────────────────────────────────

    def _require_mido(self) -> str:
        if not HAS_MIDO:
            return "mido not installed. Run: pip install mido python-rtmidi"
        return ""

    def _load_or_new(self, path: Path) -> "mido.MidiFile":
        if path.exists():
            return mido.MidiFile(path)
        mid = mido.MidiFile(ticks_per_beat=self.valves.DEFAULT_TPB)
        return mid

    def _ensure_track(self, mid: "mido.MidiFile", track_idx: int, name: str = "") -> "mido.MidiTrack":
        while len(mid.tracks) <= track_idx:
            t = mido.MidiTrack()
            mid.tracks.append(t)
            if name:
                t.append(mido.MetaMessage("track_name", name=name, time=0))
        return mid.tracks[track_idx]

    # ── Project / file ────────────────────────────────────────────────────

    def create_project(
        self,
        output_path: str,
        tempo_bpm: float = 0.0,
        time_signature: str = "4/4",
        key: str = "C",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Create a fresh MIDI file with tempo, time signature, and key meta-events
        on track 0 (the conductor track). Subsequent tracks (1+) hold notes.
        :param output_path: Path to .mid file.
        :param tempo_bpm: Tempo in BPM. 0 = use DEFAULT_TEMPO_BPM (120).
        :param time_signature: "n/d" e.g. "4/4", "3/4", "6/8".
        :param key: Key name e.g. "C", "Am", "F#", "Eb".
        :return: Confirmation with path written.
        """
        err = self._require_mido()
        if err:
            return err
        path = Path(output_path).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        mid = mido.MidiFile(ticks_per_beat=self.valves.DEFAULT_TPB)
        conductor = mido.MidiTrack()
        mid.tracks.append(conductor)
        bpm = tempo_bpm or self.valves.DEFAULT_TEMPO_BPM
        conductor.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(bpm), time=0))
        try:
            num, den = time_signature.split("/")
            conductor.append(mido.MetaMessage("time_signature",
                                              numerator=int(num),
                                              denominator=int(den),
                                              clocks_per_click=24,
                                              notated_32nd_notes_per_beat=8,
                                              time=0))
        except Exception:
            pass
        try:
            conductor.append(mido.MetaMessage("key_signature", key=key, time=0))
        except Exception:
            pass
        mid.save(path)
        return f"created MIDI -> {path} (tempo={bpm} BPM, time={time_signature}, key={key})"

    def add_track(
        self,
        midi_path: str,
        name: str,
        channel: int = 0,
        program: int = 0,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Append a new track with name, MIDI channel (0-15), and program
        (instrument 0-127 from the General MIDI bank: 0=piano, 24=guitar,
        40=violin, 56=trumpet, 73=flute, 81=lead synth, 128=drum kit on
        channel 9).
        :param midi_path: Path to existing .mid.
        :param name: Track name shown in DAWs.
        :param channel: 0-15. Channel 9 is the standard drum kit channel.
        :param program: 0-127 GM program number.
        :return: New track index.
        """
        err = self._require_mido()
        if err:
            return err
        path = Path(midi_path).expanduser().resolve()
        mid = self._load_or_new(path)
        idx = len(mid.tracks)
        t = mido.MidiTrack()
        mid.tracks.append(t)
        t.append(mido.MetaMessage("track_name", name=name, time=0))
        t.append(mido.Message("program_change", channel=channel, program=program, time=0))
        mid.save(path)
        return f"added track[{idx}] '{name}' (ch={channel}, program={program}) -> {path}"

    def add_note(
        self,
        midi_path: str,
        track_idx: int,
        pitch: str,
        start_beat: float,
        duration_beat: float,
        velocity: int = 0,
        channel: int = 0,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Add a single note to a track. Pitches are parsed by name ("C4", "F#5",
        "Bb3") or as integer MIDI numbers. Start time and duration are in beats
        relative to the start of the song.
        :param midi_path: Path to .mid.
        :param track_idx: 1-indexed track id (0 is the conductor track).
        :param pitch: Note name like "C4" or integer 0-127.
        :param start_beat: When the note starts (beats).
        :param duration_beat: How long it lasts (beats).
        :param velocity: 0-127. 0 = use DEFAULT_VELOCITY (80).
        :param channel: MIDI channel 0-15.
        :return: Confirmation.
        """
        err = self._require_mido()
        if err:
            return err
        path = Path(midi_path).expanduser().resolve()
        mid = self._load_or_new(path)
        track = self._ensure_track(mid, track_idx)

        try:
            p = int(pitch) if str(pitch).isdigit() else _name_to_pitch(str(pitch))
        except (ValueError, KeyError) as e:
            return f"bad pitch {pitch!r}: {e}"

        vel = velocity or self.valves.DEFAULT_VELOCITY
        tpb = mid.ticks_per_beat
        on_tick = int(round(start_beat * tpb))
        off_tick = int(round((start_beat + duration_beat) * tpb))

        # mido tracks store delta times. We sort by absolute time and rebuild.
        events: list[tuple[int, "mido.Message"]] = []
        abs_t = 0
        for msg in track:
            abs_t += msg.time
            events.append((abs_t, msg.copy(time=0)))
        events.append((on_tick, mido.Message("note_on", note=p, velocity=vel, channel=channel, time=0)))
        events.append((off_tick, mido.Message("note_off", note=p, velocity=0, channel=channel, time=0)))
        events.sort(key=lambda x: x[0])
        track.clear()
        prev_t = 0
        for abs_t, msg in events:
            track.append(msg.copy(time=abs_t - prev_t))
            prev_t = abs_t
        mid.save(path)
        return f"+ note {pitch}@{start_beat}b dur={duration_beat}b -> track[{track_idx}]"

    def add_chord(
        self,
        midi_path: str,
        track_idx: int,
        root: str,
        quality: str,
        start_beat: float,
        duration_beat: float,
        velocity: int = 0,
        channel: int = 0,
        inversion: int = 0,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Add a chord (multiple simultaneous notes). Root + quality looks up the
        interval pattern (maj, m, 7, maj7, m7, dim, aug, sus2, sus4, 9, …).
        :param midi_path: Path to .mid.
        :param track_idx: Target track id.
        :param root: Root note like "C4", "F#3".
        :param quality: maj, m, 7, maj7, m7, dim, aug, sus2, sus4, 9, maj9, m9, add9.
        :param start_beat: When the chord starts.
        :param duration_beat: How long the chord lasts.
        :param velocity: 0 = default.
        :param channel: 0-15.
        :param inversion: 0 = root position; 1 = first inversion; etc.
        :return: Confirmation with the pitches written.
        """
        try:
            root_p = _name_to_pitch(root)
        except (ValueError, KeyError) as e:
            return f"bad root {root!r}: {e}"
        intervals = _CHORDS.get(quality.lower())
        if intervals is None:
            return f"unknown chord quality: {quality}. Try: {sorted(_CHORDS)}"
        pitches = [root_p + i for i in intervals]
        for _ in range(inversion):
            pitches.append(pitches.pop(0) + 12)
        results = []
        for p in pitches:
            r = self.add_note(midi_path, track_idx, str(p), start_beat,
                              duration_beat, velocity, channel)
            results.append(r)
        return f"chord {root}{quality} ({','.join(_pitch_to_name(p) for p in pitches)})"

    def add_scale_run(
        self,
        midi_path: str,
        track_idx: int,
        root: str,
        scale: str,
        start_beat: float,
        note_duration_beat: float,
        ascending: bool = True,
        octaves: int = 1,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Lay down a melodic scale run. Useful as a placeholder melody or
        warm-up exercise.
        :param midi_path: Path to .mid.
        :param track_idx: Target track id.
        :param root: First note like "C4".
        :param scale: major, minor, harmonic_minor, melodic_minor, dorian, phrygian, lydian, mixolydian, locrian, pentatonic_major, pentatonic_minor, blues, chromatic.
        :param start_beat: When the run starts.
        :param note_duration_beat: Duration of each individual note.
        :param ascending: When False, descend instead.
        :param octaves: How many octaves to span.
        :return: Confirmation.
        """
        intervals = _SCALES.get(scale.lower())
        if intervals is None:
            return f"unknown scale: {scale}. Try: {sorted(_SCALES)}"
        try:
            root_p = _name_to_pitch(root)
        except (ValueError, KeyError) as e:
            return f"bad root: {e}"
        pitches: list[int] = []
        for o in range(octaves):
            for iv in intervals:
                pitches.append(root_p + iv + 12 * o)
        pitches.append(root_p + 12 * octaves)
        if not ascending:
            pitches = list(reversed(pitches))

        t = start_beat
        for p in pitches:
            self.add_note(midi_path, track_idx, str(p), t, note_duration_beat)
            t += note_duration_beat
        return f"scale run {root} {scale} {len(pitches)} notes -> track[{track_idx}]"

    def add_drum_pattern(
        self,
        midi_path: str,
        track_idx: int,
        pattern: str,
        start_beat: float = 0.0,
        bars: int = 4,
        time_signature: str = "4/4",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Drop a stock drum pattern on a drum-channel track (channel 9).
        :param midi_path: Path to .mid.
        :param track_idx: Drum track index. Will be channel 9 regardless.
        :param pattern: rock, four_on_floor, hiphop, half_time, breakbeat.
        :param start_beat: Where to drop the pattern.
        :param bars: How many bars to repeat.
        :param time_signature: "4/4", "3/4", etc. — controls beats per bar.
        :return: Confirmation.
        """
        kick, snare, hat = 36, 38, 42       # GM drum map
        # (beat_within_bar, drum, duration, velocity)
        patterns: dict[str, list[tuple[float, int, float, int]]] = {
            "rock":          [(0, kick, 0.25, 100), (1, snare, 0.25, 100),
                              (2, kick, 0.25, 100), (3, snare, 0.25, 100)]
                            + [(b * 0.5, hat, 0.125, 70) for b in range(8)],
            "four_on_floor": [(b, kick, 0.25, 100) for b in range(4)]
                            + [(1, snare, 0.25, 90), (3, snare, 0.25, 90)]
                            + [(b * 0.25, hat, 0.125, 60) for b in range(16)],
            "hiphop":        [(0, kick, 0.25, 110), (2, kick, 0.25, 110),
                              (1, snare, 0.25, 95), (3, snare, 0.25, 95)]
                            + [(b * 0.5, hat, 0.125, 65) for b in range(8)],
            "half_time":     [(0, kick, 0.25, 100), (2, snare, 0.25, 100)]
                            + [(b * 0.5, hat, 0.125, 65) for b in range(8)],
            "breakbeat":     [(0, kick, 0.25, 110), (1, snare, 0.25, 100),
                              (1.5, snare, 0.25, 80), (2.5, kick, 0.25, 110),
                              (3, snare, 0.25, 100)]
                            + [(b * 0.25, hat, 0.125, 60) for b in range(16)],
        }
        events = patterns.get(pattern.lower())
        if events is None:
            return f"unknown drum pattern: {pattern}. Try: {sorted(patterns)}"

        try:
            num, _ = time_signature.split("/")
            beats_per_bar = int(num)
        except Exception:
            beats_per_bar = 4

        for bar in range(bars):
            base = start_beat + bar * beats_per_bar
            for beat, drum, dur, vel in events:
                self.add_note(midi_path, track_idx, str(drum), base + beat,
                              dur, vel, channel=9)
        return f"drum pattern '{pattern}' x{bars} bars on track[{track_idx}]"

    # ── Helpers / inspection ──────────────────────────────────────────────

    def list_chords(self, __user__: Optional[dict] = None) -> str:
        """
        List supported chord qualities (the second arg to add_chord).
        :return: Chord shorthand list with intervals.
        """
        return "\n".join(f"{q or '(empty=major)':<8}  intervals={iv}" for q, iv in _CHORDS.items())

    def list_scales(self, __user__: Optional[dict] = None) -> str:
        """
        List supported scale names.
        :return: Scale list with intervals.
        """
        return "\n".join(f"{name:<18}  intervals={iv}" for name, iv in _SCALES.items())

    def summarise(self, midi_path: str, __user__: Optional[dict] = None) -> str:
        """
        Return a human-readable summary of the MIDI file (tempo, time
        signature, tracks, note counts). Useful as a sanity check after
        building a composition.
        :param midi_path: Path to .mid.
        :return: Multi-line summary.
        """
        err = self._require_mido()
        if err:
            return err
        path = Path(midi_path).expanduser().resolve()
        if not path.exists():
            return f"Not found: {path}"
        mid = mido.MidiFile(path)
        rows = [f"path: {path}", f"ticks_per_beat: {mid.ticks_per_beat}",
                f"length_seconds: {mid.length:.2f}", f"tracks: {len(mid.tracks)}"]
        for i, t in enumerate(mid.tracks):
            name = next((m.name for m in t if m.type == "track_name"), "")
            notes = sum(1 for m in t if m.type == "note_on" and m.velocity > 0)
            rows.append(f"  [{i}] {name or '(unnamed)':<24}  notes={notes}  events={len(t)}")
        return "\n".join(rows)
