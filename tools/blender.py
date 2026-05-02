"""
title: Blender — GUI launch + Headless Python Scripting
author: local-ai-stack
description: Open .blend files in Blender's GUI, or run arbitrary Python scripts headlessly via `blender -b -P script.py` to create geometry, render frames, and export models in any format Blender supports (glTF, FBX, OBJ, STL, USD, Alembic). The model writes a Python script — this tool feeds it to Blender's bundled interpreter, which has full access to the bpy API.
required_open_webui_version: 0.4.0
requirements:
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


# A small library of script templates the model can invoke by name. Keeping
# them server-side means the LLM doesn't have to re-derive boilerplate every
# turn — it just supplies the parameters.
_TEMPLATES: dict[str, str] = {
    "render_frame": """
import bpy, sys
out = r"{output}"
frame = {frame}
bpy.context.scene.render.filepath = out
bpy.context.scene.render.image_settings.file_format = "{img_format}"
bpy.context.scene.frame_set(frame)
bpy.ops.render.render(write_still=True)
print("RENDERED:", out)
""",
    "export_model": """
import bpy
fmt = "{fmt}".lower()
out = r"{output}"
if fmt in ("glb", "gltf"):
    bpy.ops.export_scene.gltf(filepath=out, export_format="GLB" if fmt == "glb" else "GLTF_SEPARATE")
elif fmt == "fbx":
    bpy.ops.export_scene.fbx(filepath=out)
elif fmt == "obj":
    bpy.ops.wm.obj_export(filepath=out)
elif fmt == "stl":
    bpy.ops.wm.stl_export(filepath=out)
elif fmt == "usd":
    bpy.ops.wm.usd_export(filepath=out)
elif fmt == "abc":
    bpy.ops.wm.alembic_export(filepath=out)
else:
    raise SystemExit(f"unsupported export format: {{fmt}}")
print("EXPORTED:", out)
""",
    "scene_info": """
import bpy, json
info = {{
    "blend_file": bpy.data.filepath,
    "scenes": [s.name for s in bpy.data.scenes],
    "objects": [{{"name": o.name, "type": o.type}} for o in bpy.context.scene.objects],
    "materials": [m.name for m in bpy.data.materials],
    "frame_start": bpy.context.scene.frame_start,
    "frame_end": bpy.context.scene.frame_end,
    "render_engine": bpy.context.scene.render.engine,
    "resolution": [bpy.context.scene.render.resolution_x, bpy.context.scene.render.resolution_y],
}}
print("SCENE_INFO:", json.dumps(info))
""",
}


class Tools:
    class Valves(BaseModel):
        BLENDER_EXE: str = Field(
            default=r"C:\Program Files\Blender Foundation\Blender 4.2\blender.exe",
            description="Path to blender.exe. Override per Blender release on the system.",
        )
        DEFAULT_TIMEOUT_SECS: int = Field(
            default=300,
            description="Cap on each headless run (renders can be heavy — increase as needed).",
        )

    def __init__(self):
        self.valves = self.Valves()

    # ── Helpers ───────────────────────────────────────────────────────────

    def _which(self) -> str:
        exe = self.valves.BLENDER_EXE
        if Path(exe).exists():
            return exe
        located = shutil.which(exe) or shutil.which("blender")
        if located:
            return located
        raise FileNotFoundError(f"Blender binary not found: {exe}")

    def _run_headless(
        self,
        script_text: str,
        blend_file: str = "",
        extra_args: list[str] | None = None,
        timeout: int = 0,
    ) -> str:
        timeout = timeout or self.valves.DEFAULT_TIMEOUT_SECS
        with tempfile.NamedTemporaryFile(
            "w", suffix=".py", delete=False, encoding="utf-8",
        ) as f:
            f.write(script_text)
            script_path = f.name

        argv = [self._which(), "-b"]
        if blend_file:
            argv.append(str(Path(blend_file).expanduser().resolve()))
        argv += ["-P", script_path]
        if extra_args:
            argv += list(extra_args)

        try:
            res = subprocess.run(argv, capture_output=True, text=True,
                                 timeout=timeout, check=False)
        except subprocess.TimeoutExpired as e:
            return f"timeout after {timeout}s\n{e.stdout or ''}\n{e.stderr or ''}"
        finally:
            Path(script_path).unlink(missing_ok=True)
        return (
            f"exit={res.returncode}\n"
            f"---- stdout ----\n{res.stdout}\n---- stderr ----\n{res.stderr}"
        )

    # ── GUI launch ────────────────────────────────────────────────────────

    def launch_gui(
        self,
        blend_file: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Open Blender's interactive GUI, optionally loading a .blend file.
        Detached — returns immediately with the PID.
        :param blend_file: Optional .blend to open.
        :return: Confirmation with PID.
        """
        argv = [self._which()]
        if blend_file:
            argv.append(str(Path(blend_file).expanduser().resolve()))
        kwargs: dict = {"close_fds": True}
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x00000008 | 0x00000200
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(argv, **kwargs)
        return f"opened Blender (pid={proc.pid}){' with ' + blend_file if blend_file else ''}"

    # ── Headless scripting ───────────────────────────────────────────────

    def run_python(
        self,
        code: str,
        blend_file: str = "",
        timeout_secs: int = 0,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Run arbitrary Python in Blender's bundled interpreter (full bpy API
        available). Use this to procedurally build geometry, modify scenes,
        bake animations, or anything else.
        :param code: Python source. Will be written to a temp .py and passed via -P.
        :param blend_file: Optional .blend to load before the script runs.
        :param timeout_secs: Max wait time (0 → DEFAULT_TIMEOUT_SECS).
        :return: Combined stdout/stderr from Blender.
        """
        return self._run_headless(code, blend_file=blend_file, timeout=timeout_secs)

    def render_frame(
        self,
        blend_file: str,
        output: str,
        frame: int = 1,
        img_format: str = "PNG",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Render a single frame of a .blend file via Blender's CLI.
        :param blend_file: Path to .blend.
        :param output: Output image path (without extension is fine; Blender adds it).
        :param frame: Frame number to render.
        :param img_format: PNG, JPEG, OPEN_EXR, TIFF, BMP, WEBP.
        :return: Headless run output (look for "RENDERED: <path>").
        """
        script = _TEMPLATES["render_frame"].format(
            output=str(Path(output).expanduser().resolve()),
            frame=int(frame),
            img_format=img_format,
        )
        return self._run_headless(script, blend_file=blend_file, timeout=600)

    def render_animation(
        self,
        blend_file: str,
        output_dir: str,
        start: int = 0,
        end: int = 0,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Render a frame range to PNG files using Blender's built-in -a flag.
        :param blend_file: Path to .blend.
        :param output_dir: Directory to write frames into (// prefix supported).
        :param start: First frame (0 = use the .blend's frame_start).
        :param end: Last frame (0 = use the .blend's frame_end).
        :return: Blender CLI output.
        """
        argv: list[str] = ["-o", str(Path(output_dir).expanduser().resolve()) + "/frame_####"]
        if start:
            argv += ["-s", str(start)]
        if end:
            argv += ["-e", str(end)]
        argv.append("-a")  # Render the active range
        # We use a no-op script just to satisfy _run_headless plumbing.
        return self._run_headless("print('starting render')",
                                  blend_file=blend_file, extra_args=argv, timeout=3600)

    def export_model(
        self,
        blend_file: str,
        output: str,
        fmt: str = "glb",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Open a .blend and export the active scene to glb/gltf/fbx/obj/stl/usd/abc.
        :param blend_file: Path to .blend.
        :param output: Path to write the exported model.
        :param fmt: One of: glb, gltf, fbx, obj, stl, usd, abc.
        :return: Headless run output.
        """
        script = _TEMPLATES["export_model"].format(
            fmt=fmt, output=str(Path(output).expanduser().resolve()),
        )
        return self._run_headless(script, blend_file=blend_file)

    def scene_info(
        self,
        blend_file: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Inspect a .blend headlessly and print a JSON summary (objects,
        materials, frame range, render engine, resolution).
        :param blend_file: Path to .blend.
        :return: Headless run output (look for "SCENE_INFO: {...}").
        """
        return self._run_headless(_TEMPLATES["scene_info"], blend_file=blend_file, timeout=60)

    def version(self, __user__: Optional[dict] = None) -> str:
        """
        Return Blender's version string.
        :return: stdout from `blender --version`.
        """
        try:
            res = subprocess.run([self._which(), "--version"],
                                 capture_output=True, text=True, timeout=15, check=False)
        except Exception as e:
            return f"failed: {e}"
        return res.stdout.strip() or res.stderr.strip()
