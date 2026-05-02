"""
title: yt-dlp — Universal Audio/Video Downloader (1000+ sites)
author: local-ai-stack
description: Wrap the yt-dlp CLI to inspect, extract audio, or download video from 1000+ sites — YouTube, SoundCloud, Bandcamp pages, Vimeo, Internet Archive, Twitch, Mixcloud, NicoNico, BBC, NPR, podcasts, etc. Probes a URL with `-J` to list available formats; extracts best lossless / hi-bitrate audio (`bestaudio` → flac/wav/m4a/opus/mp3 via ffmpeg); downloads video at chosen height; supports playlist range and metadata embedding. Writes only inside DOWNLOAD_DIR; download primitive is gated behind WRITE_ENABLED.
required_open_webui_version: 0.4.0
requirements: yt-dlp, ffmpeg
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

import asyncio
import json
import shlex
import shutil
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field


_LOSSLESS_PRIORITY = [
    # Codec preference, best to worst.
    ("flac",   ["flac"]),
    ("wav",    ["wav"]),
    ("alac",   ["alac"]),
    ("opus",   ["opus"]),
    ("vorbis", ["vorbis"]),
    ("aac",    ["aac", "m4a", "mp4a"]),
    ("mp3",    ["mp3"]),
]


class Tools:
    class Valves(BaseModel):
        DOWNLOAD_DIR: str = Field(
            default=str(Path.home() / "Downloads" / "yt-dlp"),
            description="Where downloads land. Created on first run.",
        )
        WRITE_ENABLED: bool = Field(
            default=False,
            description="Master switch — writes only happen when on.",
        )
        YT_DLP_BIN: str = Field(
            default="yt-dlp",
            description="yt-dlp binary or absolute path. Override if it isn't on PATH.",
        )
        FFMPEG_BIN: str = Field(
            default="ffmpeg",
            description="ffmpeg binary or absolute path (needed for audio extraction).",
        )
        MAX_TIMEOUT: int = Field(
            default=900,
            description="Hard ceiling on a single yt-dlp invocation, seconds.",
        )
        PREFER_AUDIO_FORMAT: str = Field(
            default="best",
            description="Audio target when extracting: best (keep source codec, best bitrate), flac, wav, alac, m4a, opus, vorbis, mp3.",
        )
        EMBED_METADATA: bool = Field(
            default=True,
            description="Embed title/artist/thumbnail tags in extracted audio.",
        )

    def __init__(self):
        self.valves = self.Valves()

    # ── helpers ───────────────────────────────────────────────────────────

    def _which_binaries_ok(self) -> Optional[str]:
        if not shutil.which(self.valves.YT_DLP_BIN) and not Path(self.valves.YT_DLP_BIN).exists():
            return f"yt-dlp not found ({self.valves.YT_DLP_BIN}). Install: pip install yt-dlp"
        return None

    async def _run(self, args: list[str], cwd: Optional[str] = None) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.valves.MAX_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            return 124, "", f"timeout after {self.valves.MAX_TIMEOUT}s"
        return proc.returncode or 0, stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")

    # ── inspect formats ──────────────────────────────────────────────────

    async def list_formats(
        self,
        url: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Probe a URL with `yt-dlp -J` and list available formats.
        Useful before deciding whether to download as audio (and which codec)
        or as video (and which resolution).
        :param url: Source URL (any yt-dlp-supported site).
        :return: Markdown list of formats with codec, bitrate, container, size.
        """
        err = self._which_binaries_ok()
        if err:
            return err
        rc, out, errtxt = await self._run([self.valves.YT_DLP_BIN, "-J", "--no-warnings", url])
        if rc != 0:
            return f"yt-dlp failed ({rc}): {errtxt[:600] or out[:600]}"
        try:
            data = json.loads(out)
        except Exception as e:
            return f"yt-dlp returned non-JSON: {e}"

        formats = data.get("formats") or []
        if not formats and "entries" in data and data["entries"]:
            formats = (data["entries"][0] or {}).get("formats") or []

        title = data.get("title", "—")
        uploader = data.get("uploader", "—")
        duration = data.get("duration") or 0

        lines = [
            f"## {title}",
            f"uploader: {uploader}  ·  duration: {int(duration)//60}:{int(duration)%60:02d}",
            f"page: {data.get('webpage_url', url)}",
            "",
            "| id | ext | acodec | abr | vcodec | res | filesize |",
            "|---|---|---|---|---|---|---|",
        ]
        for f in formats:
            fid = f.get("format_id", "")
            ext = f.get("ext", "")
            acodec = f.get("acodec", "—") or "—"
            vcodec = f.get("vcodec", "—") or "—"
            abr = f"{f.get('abr', '—')}k" if f.get("abr") else "—"
            res = f"{f.get('width', '?')}x{f.get('height', '?')}" if f.get("height") else "audio-only"
            size = f.get("filesize") or f.get("filesize_approx")
            size_s = f"{int(size):,}" if size else "—"
            lines.append(f"| {fid} | {ext} | {acodec} | {abr} | {vcodec} | {res} | {size_s} |")
        return "\n".join(lines)

    # ── audio extraction (writes) ────────────────────────────────────────

    async def extract_audio(
        self,
        url: str,
        format: str = "",
        playlist: bool = False,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Download a URL and extract audio. Defaults to keeping the source
        codec ('best') so no lossy reencoding happens; pick a specific
        format (flac, wav, m4a, opus, mp3) to force a target.
        :param url: Source URL.
        :param format: Override valves.PREFER_AUDIO_FORMAT. Use 'best' to keep source.
        :param playlist: When True, download the full playlist. Default False.
        :return: Path(s) on disk + yt-dlp summary.
        """
        if not self.valves.WRITE_ENABLED:
            return "Audio extraction blocked: flip WRITE_ENABLED in this tool's Valves first."
        err = self._which_binaries_ok()
        if err:
            return err

        out_dir = Path(self.valves.DOWNLOAD_DIR).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)
        target = (format or self.valves.PREFER_AUDIO_FORMAT).lower()

        args = [
            self.valves.YT_DLP_BIN, url,
            "-f", "bestaudio/best",
            "--extract-audio",
            "--audio-quality", "0",
            "-o", str(out_dir / "%(uploader)s - %(title)s.%(ext)s"),
            "--no-warnings",
            "--ffmpeg-location", self.valves.FFMPEG_BIN,
        ]
        if target != "best":
            args += ["--audio-format", target]
        if self.valves.EMBED_METADATA:
            args += ["--embed-metadata", "--embed-thumbnail"]
        if not playlist:
            args += ["--no-playlist"]

        rc, out, errtxt = await self._run(args)
        if rc != 0:
            return f"yt-dlp failed ({rc}):\n{errtxt[:1500] or out[:1500]}"
        return f"Audio extracted to {out_dir}\n\n{out[-1500:]}"

    # ── video download (writes) ──────────────────────────────────────────

    async def download_video(
        self,
        url: str,
        max_height: int = 1080,
        playlist: bool = False,
        subtitles: bool = True,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Download video at up to `max_height` pixels (e.g. 720, 1080, 2160).
        :param url: Source URL.
        :param max_height: Cap on video height (px).
        :param playlist: Whole playlist when True. Default False.
        :param subtitles: Embed subtitles when available.
        :return: Path on disk + summary.
        """
        if not self.valves.WRITE_ENABLED:
            return "Video download blocked: flip WRITE_ENABLED in this tool's Valves first."
        err = self._which_binaries_ok()
        if err:
            return err

        out_dir = Path(self.valves.DOWNLOAD_DIR).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)
        fmt = f"bv*[height<={int(max_height)}]+ba/b[height<={int(max_height)}]"
        args = [
            self.valves.YT_DLP_BIN, url,
            "-f", fmt,
            "--merge-output-format", "mp4",
            "-o", str(out_dir / "%(uploader)s - %(title)s.%(ext)s"),
            "--no-warnings",
            "--ffmpeg-location", self.valves.FFMPEG_BIN,
        ]
        if subtitles:
            args += ["--write-subs", "--embed-subs", "--sub-langs", "en.*"]
        if self.valves.EMBED_METADATA:
            args += ["--embed-metadata", "--embed-thumbnail"]
        if not playlist:
            args += ["--no-playlist"]

        rc, out, errtxt = await self._run(args)
        if rc != 0:
            return f"yt-dlp failed ({rc}):\n{errtxt[:1500] or out[:1500]}"
        return f"Video downloaded to {out_dir}\n\n{out[-1500:]}"

    # ── lossless-aware audio extraction ──────────────────────────────────

    async def extract_lossless(
        self,
        url: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Pick the best lossless or near-lossless audio format the source
        actually offers, in priority order: flac > wav > alac > opus >
        vorbis > aac > mp3. Inspects the URL with -J first, then re-runs
        the download targeting the best-available codec.
        :param url: Source URL.
        :return: Chosen codec + final file paths.
        """
        if not self.valves.WRITE_ENABLED:
            return "Lossless extraction blocked: flip WRITE_ENABLED in this tool's Valves first."
        err = self._which_binaries_ok()
        if err:
            return err

        rc, out, errtxt = await self._run([self.valves.YT_DLP_BIN, "-J", "--no-warnings", url])
        if rc != 0:
            return f"yt-dlp probe failed ({rc}): {errtxt[:600] or out[:600]}"
        try:
            data = json.loads(out)
        except Exception as e:
            return f"yt-dlp returned non-JSON: {e}"

        formats = data.get("formats") or []
        # Pick the best codec the source actually has.
        chosen_codec: Optional[str] = None
        for label, codecs in _LOSSLESS_PRIORITY:
            if any((f.get("acodec") or "").lower() in codecs for f in formats):
                chosen_codec = label
                break
        if chosen_codec is None:
            return "No audio formats found by yt-dlp."

        out_dir = Path(self.valves.DOWNLOAD_DIR).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)
        args = [
            self.valves.YT_DLP_BIN, url,
            "-f", "bestaudio/best",
            "--extract-audio",
            "--audio-quality", "0",
            "-o", str(out_dir / "%(uploader)s - %(title)s.%(ext)s"),
            "--no-warnings",
            "--no-playlist",
            "--ffmpeg-location", self.valves.FFMPEG_BIN,
        ]
        if chosen_codec in ("flac", "wav", "alac", "opus", "vorbis", "aac", "mp3"):
            args += ["--audio-format", chosen_codec]
        if self.valves.EMBED_METADATA:
            args += ["--embed-metadata", "--embed-thumbnail"]

        rc, out, errtxt = await self._run(args)
        if rc != 0:
            return f"yt-dlp failed ({rc}):\n{errtxt[:1500] or out[:1500]}"
        return f"Best available codec: **{chosen_codec}** → saved to {out_dir}\n\n{out[-1200:]}"

    # ── version probe ────────────────────────────────────────────────────

    async def version(self, __user__: Optional[dict] = None) -> str:
        """Return yt-dlp + ffmpeg versions."""
        err = self._which_binaries_ok()
        if err:
            return err
        rc1, out1, _ = await self._run([self.valves.YT_DLP_BIN, "--version"])
        rc2, out2, _ = await self._run([self.valves.FFMPEG_BIN, "-version"])
        return (
            f"yt-dlp: {out1.strip() or '?'}\n"
            f"ffmpeg: {out2.splitlines()[0] if out2 else '(not found — audio extraction will fail)'}"
        )
