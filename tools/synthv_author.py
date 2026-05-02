"""
title: SynthV Author — Build .svp Projects (notes, lyrics, parameters)
author: local-ai-stack
description: Author Synthesizer V Studio Pro `.svp` project files programmatically. Create a project with tempo/time-signature/key, add tracks (each bound to an installed singer voice database), add note groups, place notes with pitch + onset + duration + lyric + phonemes, and write parameter curves (pitch deviation, vibrato, gender, tension, dynamics). Output is plain JSON in SynthV's published format — open it in Synthesizer V Studio Pro and the project loads with everything wired up. Pair with `synthv_studio.open_project` to load it, or `synthv_studio.render_project` to batch-render.
required_open_webui_version: 0.4.0
requirements:
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field


# SynthV uses "blicks" — 1 quarter note = 705_600_000 blicks. This odd number
# divides cleanly by 8/16/32/64 note tuplets without floating-point rounding.
BLICKS_PER_QUARTER = 705_600_000


def _name_to_pitch(name: str) -> int:
    s = name.strip()
    letter = s[0].upper()
    rest = s[1:]
    accidental = 0
    while rest and rest[0] in "#b":
        accidental += 1 if rest[0] == "#" else -1
        rest = rest[1:]
    octave = int(rest)
    base = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}[letter]
    return (octave + 1) * 12 + base + accidental


def _empty_project(tempo_bpm: float, ts: str, key: str) -> dict:
    """Minimal valid SynthV project structure."""
    try:
        num, den = (int(x) for x in ts.split("/"))
    except Exception:
        num, den = 4, 4
    return {
        "version": 153,
        "time": {
            "meter": [{"index": 0, "numerator": num, "denominator": den}],
            "tempo": [{"position": 0, "bpm": tempo_bpm}],
        },
        "library": [],
        "tracks": [],
        "renderConfig": {
            "destination": "./",
            "filename": "untitled",
            "numChannels": 1,
            "aspirationFormat": "noAspiration",
            "bitDepth": 16,
            "sampleRate": 44100,
            "exportMixDown": True,
            "exportPitch": False,
        },
        "instantModeEnabled": False,
        "key": key,
    }


def _empty_track(name: str, voice_id: str) -> dict:
    return {
        "name": name,
        "dispColor": "ff7db235",
        "dispOrder": 0,
        "renderEnabled": True,
        "mixer": {
            "gainDecibel": 0.0,
            "pan": 0.0,
            "mute": False,
            "solo": False,
            "display": True,
        },
        "mainGroup": {
            "name": "main",
            "uuid": _uuid(),
            "parameters": _empty_parameters(),
            "notes": [],
            "vocalModes": {},
        },
        "mainRef": {
            "groupID": "",
            "blickAbsoluteBegin": 0,
            "blickAbsoluteEnd": 0,
            "isInstrumental": False,
            "database": _voice_db(voice_id),
            "dictionary": "",
            "voice": _voice_init(),
            "pitchTakes": {"activeTakeId": 0, "takes": []},
            "timbreTakes": {"activeTakeId": 0, "takes": []},
        },
        "groups": [],
    }


def _voice_db(voice_id: str) -> dict:
    """SynthV identifies a singer voice DB by `name`/`language`/`phoneset`/`version`.
    The model usually doesn't know exact metadata; we leave optional fields
    blank and let SynthV fall back to whatever the user has installed when
    the file opens."""
    return {
        "name": voice_id,
        "language": "",
        "phoneset": "",
        "languageOverride": "",
        "phonesetOverride": "",
        "backendType": "",
        "version": "",
    }


def _voice_init() -> dict:
    return {
        "vocalModeInherited": True,
        "vocalModePreset": "",
        "vocalModeParams": {},
        "paramLoudness": 0.0,
        "paramTension": 0.0,
        "paramBreathiness": 0.0,
        "paramVoicing": 1.0,
        "paramGender": 0.0,
        "paramToneShift": 0.0,
    }


def _empty_parameters() -> dict:
    """All eight automatable parameters, each with an empty point list."""
    return {
        "pitchDelta":   {"mode": "linear", "points": []},
        "vibratoEnv":   {"mode": "linear", "points": []},
        "loudness":     {"mode": "linear", "points": []},
        "tension":      {"mode": "linear", "points": []},
        "breathiness":  {"mode": "linear", "points": []},
        "voicing":      {"mode": "linear", "points": []},
        "gender":       {"mode": "linear", "points": []},
        "toneShift":    {"mode": "linear", "points": []},
    }


def _uuid() -> str:
    """Lowercase 32-hex string SynthV uses for note-group ids."""
    import uuid
    return uuid.uuid4().hex


def _beats_to_blicks(beats: float) -> int:
    return int(round(beats * BLICKS_PER_QUARTER))


_LYRIC_TOKEN = re.compile(r"\S+")


class Tools:
    class Valves(BaseModel):
        DEFAULT_TEMPO_BPM: float = Field(default=120.0)
        DEFAULT_TIME_SIGNATURE: str = Field(default="4/4")
        DEFAULT_KEY: str = Field(default="C major")
        DEFAULT_VOICE: str = Field(
            default="",
            description="Voice DB id to populate by default (leave blank — operator picks at open time).",
        )

    def __init__(self):
        self.valves = self.Valves()

    # ── Helpers ───────────────────────────────────────────────────────────

    def _load(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def _save(self, path: Path, data: dict) -> None:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    # ── Project ───────────────────────────────────────────────────────────

    def create_project(
        self,
        output_path: str,
        tempo_bpm: float = 0.0,
        time_signature: str = "",
        key: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Create a fresh .svp project with tempo, time signature, and key.
        :param output_path: Path to the .svp file to write.
        :param tempo_bpm: BPM. 0 = use DEFAULT_TEMPO_BPM.
        :param time_signature: "4/4", "3/4", "6/8", etc.
        :param key: Key name, e.g. "C major", "Am", "F# minor".
        :return: Confirmation with path written.
        """
        path = Path(output_path).expanduser().resolve()
        if path.suffix.lower() != ".svp":
            return f"output must end in .svp: {path}"
        path.parent.mkdir(parents=True, exist_ok=True)
        bpm = tempo_bpm or self.valves.DEFAULT_TEMPO_BPM
        ts = time_signature or self.valves.DEFAULT_TIME_SIGNATURE
        k = key or self.valves.DEFAULT_KEY
        data = _empty_project(bpm, ts, k)
        self._save(path, data)
        return f"created SynthV project -> {path} ({bpm} BPM, {ts}, {k})"

    def add_track(
        self,
        svp_path: str,
        name: str,
        voice_id: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Append a new vocal track. The voice_id is the name of a singer
        database the user has installed; SynthV substitutes a default if
        the requested voice is missing.
        :param svp_path: Path to existing .svp file.
        :param name: Track name shown in the UI.
        :param voice_id: Singer DB name (e.g. "ANRI", "Eleanor Forte AI", "Saki AI"). Empty = DEFAULT_VOICE.
        :return: New track index.
        """
        path = Path(svp_path).expanduser().resolve()
        data = self._load(path)
        track = _empty_track(name, voice_id or self.valves.DEFAULT_VOICE)
        track["dispOrder"] = len(data["tracks"])
        data["tracks"].append(track)
        self._save(path, data)
        return f"added track[{len(data['tracks']) - 1}] '{name}' (voice={voice_id or '(default)'})"

    def add_note(
        self,
        svp_path: str,
        track_idx: int,
        pitch: str,
        onset_beat: float,
        duration_beat: float,
        lyric: str = "la",
        phonemes: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Add a single note to a track's main vocal group.
        :param svp_path: Path to .svp.
        :param track_idx: 0-based track index.
        :param pitch: Note name like "C4", "F#3", or integer MIDI number 0-127.
        :param onset_beat: When the note starts (in beats from the start of the song).
        :param duration_beat: How long the note lasts (in beats).
        :param lyric: Syllable / word the singer pronounces.
        :param phonemes: Optional explicit phoneme override (X-SAMPA, e.g. "k a").
        :return: Confirmation.
        """
        path = Path(svp_path).expanduser().resolve()
        data = self._load(path)
        if track_idx < 0 or track_idx >= len(data["tracks"]):
            return f"track[{track_idx}] does not exist"
        try:
            pn = int(pitch) if str(pitch).lstrip("-").isdigit() else _name_to_pitch(str(pitch))
        except Exception as e:
            return f"bad pitch {pitch!r}: {e}"

        note = {
            "onset": _beats_to_blicks(onset_beat),
            "duration": _beats_to_blicks(duration_beat),
            "lyrics": lyric,
            "phonemes": phonemes,
            "accent": "",
            "pitch": pn,
            "detune": 0,
            "instantMode": False,
            "attributes": {},
            "musicalType": "singing",
            "pitchTakeId": 0,
            "timbreTakeId": 0,
        }
        notes = data["tracks"][track_idx]["mainGroup"]["notes"]
        notes.append(note)
        notes.sort(key=lambda n: n["onset"])
        self._save(path, data)
        return f"+ note pitch={pn} ({pitch}) @ {onset_beat}b dur={duration_beat}b lyric={lyric!r} -> track[{track_idx}]"

    def add_lyric_line(
        self,
        svp_path: str,
        track_idx: int,
        lyrics: str,
        start_beat: float,
        beats_per_syllable: float = 1.0,
        pitch: str = "C4",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Drop an entire lyric line as a row of notes at a constant pitch.
        Useful for sketching melody-less placeholders that the user can
        later re-pitch in the GUI.
        :param svp_path: Path to .svp.
        :param track_idx: Target track.
        :param lyrics: Free-text lyric. Whitespace-split into syllables.
        :param start_beat: Onset of the first syllable.
        :param beats_per_syllable: Duration of each syllable in beats.
        :param pitch: Constant pitch for all syllables.
        :return: Number of notes added.
        """
        tokens = _LYRIC_TOKEN.findall(lyrics)
        added = 0
        for i, tok in enumerate(tokens):
            self.add_note(svp_path, track_idx, pitch,
                          start_beat + i * beats_per_syllable,
                          beats_per_syllable, tok)
            added += 1
        return f"+ {added} lyric notes -> track[{track_idx}]"

    def set_parameter_curve(
        self,
        svp_path: str,
        track_idx: int,
        parameter: str,
        points: list[list[float]],
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Set an automation curve for a vocal parameter on the track's
        main group.
        :param svp_path: Path to .svp.
        :param track_idx: Target track.
        :param parameter: pitchDelta, vibratoEnv, loudness, tension, breathiness, voicing, gender, toneShift.
        :param points: List of [beat, value] pairs. value is in [-1, 1] for most params (cents for pitchDelta).
        :return: Number of points written.
        """
        path = Path(svp_path).expanduser().resolve()
        data = self._load(path)
        if track_idx < 0 or track_idx >= len(data["tracks"]):
            return f"track[{track_idx}] does not exist"
        params = data["tracks"][track_idx]["mainGroup"]["parameters"]
        if parameter not in params:
            return f"unknown parameter: {parameter}. Try: {sorted(params)}"
        params[parameter]["points"] = [
            [_beats_to_blicks(b), float(v)] for b, v in points
        ]
        self._save(path, data)
        return f"set {parameter}: {len(points)} points -> track[{track_idx}]"

    # ── Composition helpers ──────────────────────────────────────────────

    def import_midi(
        self,
        svp_path: str,
        midi_path: str,
        track_idx: int = 0,
        midi_track_idx: int = 1,
        default_lyric: str = "la",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Pull notes from a MIDI file (built with composition.create_project /
        add_note) into a SynthV vocal track. Use this to compose a melody
        with the composition tool first, then layer lyrics here.
        :param svp_path: Destination .svp.
        :param midi_path: Source .mid.
        :param track_idx: Destination SynthV track index.
        :param midi_track_idx: Source MIDI track to read from (0 is the conductor track).
        :param default_lyric: Lyric placed on every imported note ("la" by default).
        :return: Number of notes imported.
        """
        try:
            import mido
        except ImportError:
            return "mido not installed. Run: pip install mido"
        mp = Path(midi_path).expanduser().resolve()
        if not mp.exists():
            return f"Not found: {mp}"
        mid = mido.MidiFile(mp)
        if midi_track_idx < 0 or midi_track_idx >= len(mid.tracks):
            return f"midi track index out of range (0-{len(mid.tracks)-1})"
        track = mid.tracks[midi_track_idx]
        tpb = mid.ticks_per_beat
        pending: dict[int, int] = {}   # pitch -> on_tick
        added = 0
        abs_t = 0
        for msg in track:
            abs_t += msg.time
            if msg.type == "note_on" and msg.velocity > 0:
                pending[msg.note] = abs_t
            elif msg.type in ("note_off",) or (msg.type == "note_on" and msg.velocity == 0):
                start = pending.pop(msg.note, None)
                if start is None:
                    continue
                onset_beat = start / tpb
                dur_beat = (abs_t - start) / tpb
                self.add_note(svp_path, track_idx, str(msg.note),
                              onset_beat, dur_beat, default_lyric)
                added += 1
        return f"imported {added} notes from {mp.name} -> track[{track_idx}]"

    def summarise(self, svp_path: str, __user__: Optional[dict] = None) -> str:
        """
        Print a sanity-check summary of a .svp file.
        :param svp_path: Path to .svp.
        :return: Multi-line summary.
        """
        path = Path(svp_path).expanduser().resolve()
        data = self._load(path)
        rows = [
            f"path:    {path}",
            f"version: {data.get('version')}",
            f"key:     {data.get('key','?')}",
            f"tempo:   {data['time']['tempo'][0]['bpm']} BPM",
            f"meter:   {data['time']['meter'][0]['numerator']}/{data['time']['meter'][0]['denominator']}",
            f"tracks:  {len(data['tracks'])}",
        ]
        for i, t in enumerate(data["tracks"]):
            n = len(t["mainGroup"]["notes"])
            voice = t["mainRef"]["database"]["name"]
            rows.append(f"  [{i}] {t['name']:<24}  notes={n}  voice={voice or '(default)'}")
        return "\n".join(rows)
