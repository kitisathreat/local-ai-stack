"""
title: Blender Author — Build 3D Scenes from Prompt Specs
author: local-ai-stack
description: Author Blender scenes programmatically. Build a "scene spec" (objects, materials, lights, cameras, render settings, multi-collection assemblies), then commit it: this tool generates a single bpy script that constructs the whole scene and runs it via the existing `blender` headless tool — producing a real .blend file plus an optional render. Supports primitives (cube/sphere/cylinder/cone/torus/plane/icosphere), arbitrary mesh-from-vertices, PBR materials (base color, metallic, roughness, emission, normal), point/sun/spot/area lights, perspective/orthographic cameras, and Cycles or Eevee render setup. If the prompt is ambiguous (no scale, no materials), the model should call `ask_clarification` first.
required_open_webui_version: 0.4.0
requirements:
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from textwrap import dedent
from typing import Any, Optional

from pydantic import BaseModel, Field


def _blender_tool():
    """Lazy import of the existing blender (headless) tool."""
    spec = importlib.util.spec_from_file_location(
        "_lai_blender_runner", Path(__file__).parent / "blender.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod.Tools()


# Spec storage (in-memory, keyed by output_path) so the model can build a
# scene incrementally across multiple tool calls before committing.
_SCENES: dict[str, dict[str, Any]] = {}


def _get_scene(output_path: str) -> dict[str, Any]:
    p = str(Path(output_path).expanduser().resolve())
    if p not in _SCENES:
        _SCENES[p] = {
            "engine": "CYCLES",
            "samples": 64,
            "resolution": [1920, 1080],
            "background_color": [0.05, 0.05, 0.05, 1.0],
            "world_strength": 1.0,
            "objects": [],
            "materials": [],
            "lights": [],
            "cameras": [],
            "active_camera": None,
            "frame_start": 1,
            "frame_end": 1,
            "collections": {},
        }
    return _SCENES[p]


# Generated bpy script template — a single mega-script that consumes the
# spec dict and builds the scene end-to-end.
_BUILD_SCRIPT = dedent('''
    import bpy, bmesh, json, math, sys

    SPEC = json.loads({spec_json!r})

    # Wipe the default scene.
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.render.engine = SPEC.get("engine", "CYCLES")
    if scene.render.engine == "CYCLES":
        scene.cycles.samples = SPEC.get("samples", 64)
    elif scene.render.engine == "BLENDER_EEVEE":
        try:
            scene.eevee.taa_render_samples = SPEC.get("samples", 64)
        except AttributeError:
            pass
    rx, ry = SPEC.get("resolution", [1920, 1080])
    scene.render.resolution_x = rx
    scene.render.resolution_y = ry
    scene.frame_start = SPEC.get("frame_start", 1)
    scene.frame_end = SPEC.get("frame_end", 1)

    # World background.
    world = bpy.data.worlds.get("World") or bpy.data.worlds.new("World")
    world.use_nodes = True
    bg_node = world.node_tree.nodes.get("Background")
    if bg_node:
        bg_node.inputs[0].default_value = SPEC.get("background_color", [0.05, 0.05, 0.05, 1])
        bg_node.inputs[1].default_value = SPEC.get("world_strength", 1.0)
    scene.world = world

    # Collections — built first so objects can be linked correctly.
    for cname in SPEC.get("collections", {{}}):
        if cname not in bpy.data.collections:
            col = bpy.data.collections.new(cname)
            scene.collection.children.link(col)

    # Materials.
    mat_lookup = {{}}
    for m in SPEC.get("materials", []):
        mat = bpy.data.materials.new(m["name"])
        mat.use_nodes = True
        bsdf = mat.node_tree.nodes.get("Principled BSDF")
        if bsdf:
            bsdf.inputs["Base Color"].default_value = m.get("base_color", [0.8, 0.8, 0.8, 1])
            bsdf.inputs["Metallic"].default_value = m.get("metallic", 0.0)
            bsdf.inputs["Roughness"].default_value = m.get("roughness", 0.5)
            try:
                bsdf.inputs["Emission Color"].default_value = m.get("emission", [0, 0, 0, 1])
                bsdf.inputs["Emission Strength"].default_value = m.get("emission_strength", 0.0)
            except KeyError:
                # Older Blender versions
                if "Emission" in bsdf.inputs:
                    bsdf.inputs["Emission"].default_value = m.get("emission", [0, 0, 0, 1])
            try:
                bsdf.inputs["IOR"].default_value = m.get("ior", 1.45)
            except KeyError:
                pass
            try:
                bsdf.inputs["Transmission Weight"].default_value = m.get("transmission", 0.0)
            except KeyError:
                pass
        mat_lookup[m["name"]] = mat

    # Helper to assign material + collection to the just-created active object.
    def _post(spec_obj):
        ob = bpy.context.active_object
        if not ob:
            return
        ob.name = spec_obj.get("name", ob.name)
        ob.location = spec_obj.get("location", [0, 0, 0])
        ob.rotation_euler = [math.radians(a) for a in spec_obj.get("rotation_deg", [0, 0, 0])]
        sc = spec_obj.get("scale", [1, 1, 1])
        if isinstance(sc, (int, float)):
            sc = [sc, sc, sc]
        ob.scale = sc
        mat_name = spec_obj.get("material")
        if mat_name and mat_name in mat_lookup:
            ob.data.materials.clear()
            ob.data.materials.append(mat_lookup[mat_name])
        coll = spec_obj.get("collection")
        if coll and coll in bpy.data.collections:
            for c in list(ob.users_collection):
                c.objects.unlink(ob)
            bpy.data.collections[coll].objects.link(ob)
        return ob

    # Objects.
    for o in SPEC.get("objects", []):
        kind = o["type"]
        if kind == "cube":
            bpy.ops.mesh.primitive_cube_add(size=o.get("size", 2.0))
        elif kind == "sphere":
            bpy.ops.mesh.primitive_uv_sphere_add(radius=o.get("radius", 1.0),
                                                  segments=o.get("segments", 32),
                                                  ring_count=o.get("rings", 16))
        elif kind == "icosphere":
            bpy.ops.mesh.primitive_ico_sphere_add(radius=o.get("radius", 1.0),
                                                   subdivisions=o.get("subdivisions", 2))
        elif kind == "cylinder":
            bpy.ops.mesh.primitive_cylinder_add(radius=o.get("radius", 1.0),
                                                 depth=o.get("depth", 2.0),
                                                 vertices=o.get("vertices", 32))
        elif kind == "cone":
            bpy.ops.mesh.primitive_cone_add(radius1=o.get("radius1", 1.0),
                                             radius2=o.get("radius2", 0.0),
                                             depth=o.get("depth", 2.0),
                                             vertices=o.get("vertices", 32))
        elif kind == "torus":
            bpy.ops.mesh.primitive_torus_add(major_radius=o.get("major", 1.0),
                                              minor_radius=o.get("minor", 0.25),
                                              major_segments=o.get("major_segments", 48),
                                              minor_segments=o.get("minor_segments", 12))
        elif kind == "plane":
            bpy.ops.mesh.primitive_plane_add(size=o.get("size", 2.0))
        elif kind == "mesh":
            mesh = bpy.data.meshes.new(o.get("name", "Mesh"))
            mesh.from_pydata(o["vertices"], o.get("edges", []), o.get("faces", []))
            mesh.update()
            ob = bpy.data.objects.new(o.get("name", "Mesh"), mesh)
            scene.collection.objects.link(ob)
            bpy.context.view_layer.objects.active = ob
        elif kind == "text":
            bpy.ops.object.text_add()
            bpy.context.active_object.data.body = o.get("body", "Hello")
            bpy.context.active_object.data.extrude = o.get("extrude", 0.05)
        else:
            print("WARN: unknown object type", kind)
            continue
        _post(o)

    # Lights.
    for L in SPEC.get("lights", []):
        kind = L.get("type", "point").upper()
        bpy.ops.object.light_add(type=kind, location=L.get("location", [0, 0, 5]))
        light = bpy.context.active_object
        light.data.energy = L.get("energy", 1000.0)
        light.data.color = L.get("color", [1, 1, 1])
        if kind == "SPOT":
            light.data.spot_size = math.radians(L.get("spot_angle_deg", 45))
        if "size" in L and kind == "AREA":
            light.data.size = L["size"]

    # Cameras.
    cam_lookup = {{}}
    for cam in SPEC.get("cameras", []):
        bpy.ops.object.camera_add(location=cam.get("location", [7, -7, 5]))
        c = bpy.context.active_object
        c.name = cam.get("name", "Camera")
        c.data.lens = cam.get("focal_length_mm", 50)
        c.data.type = cam.get("projection", "PERSP").upper()
        if "look_at" in cam:
            import mathutils
            tgt = mathutils.Vector(cam["look_at"])
            direction = tgt - c.location
            rot = direction.to_track_quat("-Z", "Y")
            c.rotation_euler = rot.to_euler()
        cam_lookup[c.name] = c

    if SPEC.get("active_camera") in cam_lookup:
        scene.camera = cam_lookup[SPEC["active_camera"]]
    elif cam_lookup:
        scene.camera = list(cam_lookup.values())[0]

    # Save the .blend.
    out = SPEC["output_path"]
    bpy.ops.wm.save_as_mainfile(filepath=out)
    print("SAVED:", out)

    render_to = SPEC.get("render_output")
    if render_to:
        scene.render.filepath = render_to
        scene.render.image_settings.file_format = SPEC.get("render_format", "PNG")
        bpy.ops.render.render(write_still=True)
        print("RENDERED:", render_to)
    ''')


class Tools:
    class Valves(BaseModel):
        DEFAULT_OUTPUT_DIR: str = Field(
            default=str(Path.home() / "Documents" / "Blender" / "lai-output"),
            description="Where generated .blend files land when no path is supplied.",
        )

    def __init__(self):
        self.valves = self.Valves()

    # ── Scene-spec accumulators ──────────────────────────────────────────

    def new_scene(
        self,
        output_path: str,
        engine: str = "CYCLES",
        samples: int = 64,
        resolution_x: int = 1920,
        resolution_y: int = 1080,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Start a fresh scene spec keyed by `output_path`. Subsequent calls
        (add_primitive, add_material, add_light, add_camera, set_render)
        layer in details. Commit with `build()`.
        :param output_path: Path to the .blend file the build step will write.
        :param engine: CYCLES or BLENDER_EEVEE.
        :param samples: Render samples (Cycles only).
        :param resolution_x: Pixels.
        :param resolution_y: Pixels.
        :return: Confirmation.
        """
        path = str(Path(output_path).expanduser().resolve())
        _SCENES.pop(path, None)
        scene = _get_scene(path)
        scene["engine"] = engine.upper()
        scene["samples"] = samples
        scene["resolution"] = [int(resolution_x), int(resolution_y)]
        return f"new scene -> {path}  engine={engine.upper()}  res={resolution_x}x{resolution_y}"

    def add_collection(
        self,
        output_path: str,
        name: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Add a Blender collection (named group) so subsequent objects can
        be linked into it via the `collection` field.
        :param output_path: The scene spec key.
        :param name: Collection name.
        :return: Confirmation.
        """
        scene = _get_scene(str(Path(output_path).expanduser().resolve()))
        scene["collections"][name] = True
        return f"+ collection '{name}'"

    def add_material(
        self,
        output_path: str,
        name: str,
        base_color: list[float] = None,
        metallic: float = 0.0,
        roughness: float = 0.5,
        emission_color: list[float] = None,
        emission_strength: float = 0.0,
        ior: float = 1.45,
        transmission: float = 0.0,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Define a PBR material (Principled BSDF). Reference it by name when
        adding objects via the `material` field.
        :param output_path: Scene spec key.
        :param name: Material name (used as the `material` ref on objects).
        :param base_color: [r, g, b, a] in [0, 1].
        :param metallic: 0-1.
        :param roughness: 0-1.
        :param emission_color: [r, g, b, a].
        :param emission_strength: >= 0.
        :param ior: Index of refraction (1.45 ≈ glass; 1.33 ≈ water).
        :param transmission: 0-1 (1 = fully transparent like glass).
        :return: Confirmation.
        """
        scene = _get_scene(str(Path(output_path).expanduser().resolve()))
        scene["materials"].append({
            "name": name,
            "base_color": base_color or [0.8, 0.8, 0.8, 1.0],
            "metallic": metallic,
            "roughness": roughness,
            "emission": emission_color or [0, 0, 0, 1],
            "emission_strength": emission_strength,
            "ior": ior,
            "transmission": transmission,
        })
        return f"+ material '{name}'"

    def add_primitive(
        self,
        output_path: str,
        type: str,
        name: str = "",
        location: list[float] = None,
        rotation_deg: list[float] = None,
        scale: Any = 1.0,
        material: str = "",
        collection: str = "",
        size: float = 2.0,
        radius: float = 1.0,
        depth: float = 2.0,
        major: float = 1.0,
        minor: float = 0.25,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Add a primitive object. `type`: cube, sphere, icosphere, cylinder,
        cone, torus, plane, text.
        :param output_path: Scene spec key.
        :param type: Primitive type.
        :param name: Object name.
        :param location: [x, y, z].
        :param rotation_deg: [rx, ry, rz] in degrees.
        :param scale: Uniform scale or [sx, sy, sz].
        :param material: Optional material name (added via add_material).
        :param collection: Optional collection name (added via add_collection).
        :param size: For cube / plane.
        :param radius: For sphere / icosphere / cylinder / cone.
        :param depth: For cylinder / cone.
        :param major: For torus, major radius.
        :param minor: For torus, minor radius.
        :return: Confirmation.
        """
        scene = _get_scene(str(Path(output_path).expanduser().resolve()))
        spec = {
            "type": type.lower(),
            "name": name or type.title(),
            "location": location or [0, 0, 0],
            "rotation_deg": rotation_deg or [0, 0, 0],
            "scale": scale,
            "material": material,
            "collection": collection,
            "size": size, "radius": radius, "depth": depth,
            "major": major, "minor": minor,
        }
        scene["objects"].append(spec)
        return f"+ {type} '{spec['name']}' @ {spec['location']}  material={material or '-'}"

    def add_mesh(
        self,
        output_path: str,
        name: str,
        vertices: list[list[float]],
        faces: list[list[int]],
        location: list[float] = None,
        material: str = "",
        collection: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Add a custom mesh from explicit vertices and face index lists.
        Useful when no primitive fits the requested shape.
        :param output_path: Scene spec key.
        :param name: Object name.
        :param vertices: List of [x, y, z] vertex coordinates.
        :param faces: List of vertex-index lists, e.g. [[0,1,2,3]] for a quad.
        :param location: [x, y, z] world-space position.
        :param material: Material name.
        :param collection: Collection name.
        :return: Confirmation.
        """
        scene = _get_scene(str(Path(output_path).expanduser().resolve()))
        scene["objects"].append({
            "type": "mesh", "name": name,
            "vertices": vertices, "faces": faces,
            "location": location or [0, 0, 0],
            "material": material, "collection": collection,
        })
        return f"+ mesh '{name}' ({len(vertices)} verts, {len(faces)} faces)"

    def add_light(
        self,
        output_path: str,
        type: str = "point",
        location: list[float] = None,
        energy: float = 1000.0,
        color: list[float] = None,
        spot_angle_deg: float = 45.0,
        size: float = 1.0,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Add a light. `type`: point, sun, spot, area.
        :param output_path: Scene spec key.
        :param type: point / sun / spot / area.
        :param location: [x, y, z].
        :param energy: Watts (point/spot/area) or strength multiplier (sun).
        :param color: RGB.
        :param spot_angle_deg: For spot lights.
        :param size: For area lights.
        :return: Confirmation.
        """
        scene = _get_scene(str(Path(output_path).expanduser().resolve()))
        scene["lights"].append({
            "type": type.lower(),
            "location": location or [0, 0, 5],
            "energy": energy,
            "color": color or [1, 1, 1],
            "spot_angle_deg": spot_angle_deg,
            "size": size,
        })
        return f"+ {type} light @ {scene['lights'][-1]['location']}"

    def add_camera(
        self,
        output_path: str,
        name: str = "Camera",
        location: list[float] = None,
        look_at: list[float] = None,
        focal_length_mm: float = 50.0,
        projection: str = "PERSP",
        active: bool = True,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Add a camera. By default the new camera becomes the active render
        camera (set active=False to keep an earlier one).
        :param output_path: Scene spec key.
        :param name: Camera name.
        :param location: [x, y, z].
        :param look_at: Optional [x, y, z] look-at target — if set, camera is rotated to face it.
        :param focal_length_mm: Lens focal length.
        :param projection: PERSP or ORTHO.
        :param active: When True, set as active camera.
        :return: Confirmation.
        """
        scene = _get_scene(str(Path(output_path).expanduser().resolve()))
        cam = {
            "name": name,
            "location": location or [7, -7, 5],
            "focal_length_mm": focal_length_mm,
            "projection": projection.upper(),
        }
        if look_at:
            cam["look_at"] = look_at
        scene["cameras"].append(cam)
        if active:
            scene["active_camera"] = name
        return f"+ camera '{name}' @ {cam['location']}  active={active}"

    def set_render(
        self,
        output_path: str,
        render_output: str = "",
        format: str = "PNG",
        background_color: list[float] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Configure the render — output file path, image format, background colour.
        :param output_path: Scene spec key.
        :param render_output: Where to write the rendered image. Empty = no render.
        :param format: PNG / JPEG / OPEN_EXR / TIFF / WEBP.
        :param background_color: [r, g, b, a] world background.
        :return: Confirmation.
        """
        scene = _get_scene(str(Path(output_path).expanduser().resolve()))
        if render_output:
            scene["render_output"] = str(Path(render_output).expanduser().resolve())
        scene["render_format"] = format.upper()
        if background_color:
            scene["background_color"] = background_color
        return f"render config: format={format}  output={scene.get('render_output','(none)')}"

    # ── Build / commit ───────────────────────────────────────────────────

    def build(
        self,
        output_path: str,
        timeout_secs: int = 600,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Generate a Blender Python script from the accumulated spec and run
        it headlessly. Writes the .blend (and optionally a render). Clears
        the in-memory spec on success.
        :param output_path: The .blend path used as the spec key.
        :param timeout_secs: Cap on the headless run.
        :return: Combined Blender stdout/stderr.
        """
        path = str(Path(output_path).expanduser().resolve())
        scene = _get_scene(path).copy()
        scene["output_path"] = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        script = _BUILD_SCRIPT.format(spec_json=json.dumps(scene))
        runner = _blender_tool()
        result = runner.run_python(script, blend_file="", timeout_secs=timeout_secs)
        if "exit=0" in result and "SAVED" in result:
            _SCENES.pop(path, None)
        return result

    # ── Inspection ────────────────────────────────────────────────────────

    def show_spec(self, output_path: str, __user__: Optional[dict] = None) -> str:
        """
        Pretty-print the current accumulated scene spec without committing.
        :param output_path: Scene spec key.
        :return: JSON dump of the spec.
        """
        path = str(Path(output_path).expanduser().resolve())
        if path not in _SCENES:
            return f"(no spec for {path})"
        return json.dumps(_SCENES[path], indent=2)
