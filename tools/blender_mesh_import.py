"""
title: Blender Mesh Import — Read GLB / STL / OBJ into a blender_author Spec
author: local-ai-stack
description: Parse an existing 3D model file (GLB, GLTF, STL, OBJ, PLY, FBX) into the in-memory scene spec used by `blender_author`, so the model can extend a pre-built mesh with materials / lighting / extra primitives before committing to a .blend file. Uses the optional `trimesh` package; falls back to a minimal pure-Python STL/OBJ parser when trimesh isn't installed.
required_open_webui_version: 0.4.0
requirements: trimesh
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

import importlib.util
import struct
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field


def _blender_author():
    spec = importlib.util.spec_from_file_location(
        "_lai_blender_author", Path(__file__).parent / "blender_author.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _parse_stl_binary(path: Path) -> tuple[list[list[float]], list[list[int]]]:
    with path.open("rb") as f:
        f.read(80)   # header
        n = struct.unpack("<I", f.read(4))[0]
        verts: list[list[float]] = []
        faces: list[list[int]] = []
        for _ in range(n):
            f.read(12)  # normal
            tri = []
            for _ in range(3):
                xyz = struct.unpack("<fff", f.read(12))
                tri.append(len(verts))
                verts.append(list(xyz))
            faces.append(tri)
            f.read(2)
    return verts, faces


def _parse_obj(path: Path) -> tuple[list[list[float]], list[list[int]]]:
    verts: list[list[float]] = []
    faces: list[list[int]] = []
    for line in path.read_text(errors="ignore").splitlines():
        if line.startswith("v "):
            parts = line.split()
            verts.append([float(parts[1]), float(parts[2]), float(parts[3])])
        elif line.startswith("f "):
            ids = []
            for tok in line.split()[1:]:
                idx = int(tok.split("/")[0])
                ids.append(idx - 1 if idx > 0 else len(verts) + idx)
            faces.append(ids)
    return verts, faces


class Tools:
    class Valves(BaseModel):
        MAX_VERTICES: int = Field(
            default=200_000,
            description="Cap on imported mesh size (very high-poly meshes blow up the spec).",
        )

    def __init__(self):
        self.valves = self.Valves()

    def import_mesh(
        self,
        scene_path: str,
        mesh_path: str,
        name: str = "",
        location: list[float] = None,
        material: str = "",
        collection: str = "",
        decimate_to: int = 0,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Read a 3D model file and append it as a `mesh` entry on the
        blender_author scene spec keyed by `scene_path`.
        :param scene_path: Same value used in blender_author.new_scene().
        :param mesh_path: Path to .glb / .gltf / .stl / .obj / .ply / .fbx.
        :param name: Object name (defaults to the file stem).
        :param location: [x, y, z] world-space placement.
        :param material: Material name from blender_author.add_material.
        :param collection: Collection name from blender_author.add_collection.
        :param decimate_to: Optional target face count for decimation. 0 = no decimation.
        :return: Confirmation with vertex / face counts.
        """
        ba = _blender_author()
        ba_tools = ba.Tools()
        path = Path(mesh_path).expanduser().resolve()
        if not path.exists():
            return f"Not found: {path}"

        verts: list[list[float]] = []
        faces: list[list[int]] = []
        try:
            import trimesh  # type: ignore
            mesh = trimesh.load(path, force="mesh")
            if hasattr(mesh, "vertices") and hasattr(mesh, "faces"):
                verts = mesh.vertices.tolist()
                faces = mesh.faces.tolist()
                if decimate_to and decimate_to < len(faces):
                    try:
                        simplified = mesh.simplify_quadric_decimation(decimate_to)
                        verts = simplified.vertices.tolist()
                        faces = simplified.faces.tolist()
                    except Exception:
                        pass
        except ImportError:
            ext = path.suffix.lower()
            if ext == ".stl":
                verts, faces = _parse_stl_binary(path)
            elif ext == ".obj":
                verts, faces = _parse_obj(path)
            else:
                return (
                    f"trimesh not installed; pure-Python parser only supports .stl and .obj. "
                    f"Run: pip install trimesh  (got {ext})"
                )

        if not verts:
            return "no vertices parsed — file may be empty or unsupported"
        if len(verts) > self.valves.MAX_VERTICES:
            return (
                f"mesh has {len(verts)} verts > MAX_VERTICES ({self.valves.MAX_VERTICES}). "
                "Pre-decimate via trimesh or raise the valve."
            )
        # Append to the scene spec.
        scene = ba._get_scene(str(Path(scene_path).expanduser().resolve()))
        scene["objects"].append({
            "type": "mesh",
            "name": name or path.stem,
            "vertices": verts,
            "faces": faces,
            "location": location or [0, 0, 0],
            "material": material,
            "collection": collection,
        })
        return f"+ mesh '{name or path.stem}' from {path.name} ({len(verts)} verts, {len(faces)} faces)"
