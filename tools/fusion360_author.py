"""
title: Fusion 360 Author — Generate Parametric CAD Scripts
author: local-ai-stack
description: Author Fusion 360 designs programmatically. Build a "design spec" (sketches, extrudes, fillets, chamfers, holes, materials, multi-component assemblies, render setup), then commit it: this tool emits a single Fusion 360 Python script that constructs the model end-to-end and installs it via the existing `fusion360.install_script` tool. Fusion has no headless mode, so the workflow is: this tool writes the .py + .manifest under `%APPDATA%\\Autodesk\\Autodesk Fusion 360\\API\\Scripts\\<name>\\` → user opens Fusion → Tools → Scripts and Add-ins → Run. If the prompt is ambiguous (no units, no material, missing dimensions), call `ask_clarification` first.
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


def _fusion_tool():
    spec = importlib.util.spec_from_file_location(
        "_lai_fusion_runner", Path(__file__).parent / "fusion360.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod.Tools()


_DESIGNS: dict[str, dict[str, Any]] = {}


def _get(name: str) -> dict[str, Any]:
    if name not in _DESIGNS:
        _DESIGNS[name] = {
            "units": "mm",
            "components": [],   # [{name, parent, sketches: [...], features: [...], material, appearance}]
            "exports": [],      # [{format, path}]
        }
    return _DESIGNS[name]


_BUILD_SCRIPT = dedent('''
    import adsk.core, adsk.fusion, traceback, json, math
    SPEC = json.loads({spec_json!r})

    def run(context):
        app = adsk.core.Application.get()
        ui = app.userInterface
        try:
            doc = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
            design = adsk.fusion.Design.cast(app.activeProduct)
            design.designType = adsk.fusion.DesignTypes.ParametricDesignType

            unit_mgr = design.unitsManager
            unit_mgr.distanceDisplayUnits = {{
                "mm": adsk.fusion.DistanceUnits.MillimeterDistanceUnits,
                "cm": adsk.fusion.DistanceUnits.CentimeterDistanceUnits,
                "in": adsk.fusion.DistanceUnits.InchDistanceUnits,
                "ft": adsk.fusion.DistanceUnits.FootDistanceUnits,
                "m":  adsk.fusion.DistanceUnits.MeterDistanceUnits,
            }}.get(SPEC.get("units", "mm"), adsk.fusion.DistanceUnits.MillimeterDistanceUnits)

            root = design.rootComponent

            # Build components in order (parent must precede children).
            comp_lookup = {{"root": root}}
            for c in SPEC.get("components", []):
                parent = comp_lookup.get(c.get("parent", "root"), root)
                if c["name"] == "root":
                    comp = root
                else:
                    occ = parent.occurrences.addNewComponent(adsk.core.Matrix3D.create())
                    comp = occ.component
                    comp.name = c["name"]
                comp_lookup[c["name"]] = comp

                # Sketches.
                sketch_lookup = {{}}
                for s in c.get("sketches", []):
                    plane_name = s.get("plane", "xy")
                    plane = {{
                        "xy": comp.xYConstructionPlane,
                        "yz": comp.yZConstructionPlane,
                        "xz": comp.xZConstructionPlane,
                    }}.get(plane_name, comp.xYConstructionPlane)
                    sk = comp.sketches.add(plane)
                    sk.name = s.get("name", "Sketch")
                    sketch_lookup[sk.name] = sk
                    for shape in s.get("shapes", []):
                        kind = shape["type"]
                        if kind == "rectangle":
                            p1 = adsk.core.Point3D.create(*shape["p1"], 0)
                            p2 = adsk.core.Point3D.create(*shape["p2"], 0)
                            sk.sketchCurves.sketchLines.addTwoPointRectangle(p1, p2)
                        elif kind == "circle":
                            ctr = adsk.core.Point3D.create(*shape["center"], 0)
                            sk.sketchCurves.sketchCircles.addByCenterRadius(ctr, shape["radius"])
                        elif kind == "polygon":
                            ctr = adsk.core.Point3D.create(*shape["center"], 0)
                            sk.sketchCurves.sketchLines.addCenterPointCircumscribedPolygon(
                                ctr, shape.get("sides", 6), shape.get("radius", 1.0))
                        elif kind == "line":
                            p1 = adsk.core.Point3D.create(*shape["p1"], 0)
                            p2 = adsk.core.Point3D.create(*shape["p2"], 0)
                            sk.sketchCurves.sketchLines.addByTwoPoints(p1, p2)
                        elif kind == "spline":
                            pts = adsk.core.ObjectCollection.create()
                            for p in shape["points"]:
                                pts.add(adsk.core.Point3D.create(*p, 0))
                            sk.sketchCurves.sketchFittedSplines.add(pts)

                # Features.
                for f in c.get("features", []):
                    kind = f["type"]
                    if kind == "extrude":
                        sk = sketch_lookup.get(f["sketch"])
                        if not sk or not sk.profiles.count:
                            continue
                        prof = sk.profiles.item(f.get("profile_index", 0))
                        ext_in = comp.features.extrudeFeatures.createInput(
                            prof, {{
                                "new":       adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
                                "join":      adsk.fusion.FeatureOperations.JoinFeatureOperation,
                                "cut":       adsk.fusion.FeatureOperations.CutFeatureOperation,
                                "intersect": adsk.fusion.FeatureOperations.IntersectFeatureOperation,
                            }}.get(f.get("operation", "new"), adsk.fusion.FeatureOperations.NewBodyFeatureOperation))
                        dist = adsk.core.ValueInput.createByReal(f["distance"])
                        ext_in.setDistanceExtent(False, dist)
                        comp.features.extrudeFeatures.add(ext_in)
                    elif kind == "fillet":
                        # Fillet every edge of the most-recently-added body.
                        bodies = comp.bRepBodies
                        if bodies.count == 0: continue
                        body = bodies.item(bodies.count - 1)
                        edges = adsk.core.ObjectCollection.create()
                        for e in body.edges:
                            edges.add(e)
                        fil_in = comp.features.filletFeatures.createInput()
                        fil_in.addConstantRadiusEdgeSet(
                            edges, adsk.core.ValueInput.createByReal(f["radius"]), True)
                        comp.features.filletFeatures.add(fil_in)
                    elif kind == "chamfer":
                        bodies = comp.bRepBodies
                        if bodies.count == 0: continue
                        body = bodies.item(bodies.count - 1)
                        edges = adsk.core.ObjectCollection.create()
                        for e in body.edges:
                            edges.add(e)
                        ch_in = comp.features.chamferFeatures.createInput(
                            edges,
                            adsk.core.ValueInput.createByReal(f["distance"]),
                            False,
                        )
                        comp.features.chamferFeatures.add(ch_in)
                    elif kind == "shell":
                        bodies = comp.bRepBodies
                        if bodies.count == 0: continue
                        body = bodies.item(bodies.count - 1)
                        faces_to_remove = adsk.core.ObjectCollection.create()
                        # Best-effort: remove the highest-Z face.
                        if body.faces.count:
                            top = max(body.faces, key=lambda fa: fa.boundingBox.maxPoint.z)
                            faces_to_remove.add(top)
                        sh_in = comp.features.shellFeatures.createInput(faces_to_remove, False)
                        sh_in.insideThickness = adsk.core.ValueInput.createByReal(f["thickness"])
                        comp.features.shellFeatures.add(sh_in)

                # Material / appearance assignment to bodies in this component.
                if c.get("material"):
                    try:
                        lib = app.materialLibraries.itemByName("Fusion Material Library")
                        mat = lib.materials.itemByName(c["material"]) if lib else None
                        if mat:
                            for b in comp.bRepBodies:
                                b.material = mat
                    except Exception:
                        pass
                if c.get("appearance"):
                    try:
                        appr_lib = app.materialLibraries.itemByName("Fusion Appearance Library")
                        appr = appr_lib.appearances.itemByName(c["appearance"]) if appr_lib else None
                        if appr:
                            for b in comp.bRepBodies:
                                b.appearance = appr
                    except Exception:
                        pass

            # Exports.
            for e in SPEC.get("exports", []):
                fmt = e["format"].lower()
                path = e["path"]
                em = design.exportManager
                if fmt == "step":
                    em.execute(em.createSTEPExportOptions(path, root))
                elif fmt == "iges":
                    em.execute(em.createIGESExportOptions(path, root))
                elif fmt == "stl":
                    opts = em.createSTLExportOptions(root, path)
                    opts.meshRefinement = adsk.fusion.MeshRefinementSettings.MeshRefinementMedium
                    em.execute(opts)
                elif fmt == "f3d":
                    em.execute(em.createFusionArchiveExportOptions(path, root))
                elif fmt == "smt":
                    em.execute(em.createSMTExportOptions(path, root))

            ui.messageBox(f"Built '{{SPEC.get('name', '<unnamed>')}}' OK")
        except Exception:
            if ui:
                ui.messageBox("Author script failed:\\n" + traceback.format_exc())
    ''')


class Tools:
    class Valves(BaseModel):
        DEFAULT_UNITS: str = Field(default="mm", description="mm, cm, in, ft, m.")

    def __init__(self):
        self.valves = self.Valves()

    # ── Spec accumulators ────────────────────────────────────────────────

    def new_design(
        self,
        name: str,
        units: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Start a fresh design spec keyed by `name`. Subsequent calls
        (add_component, add_sketch, add_extrude, …) layer in features.
        Commit with `build()` — that emits a Fusion-installed script.
        :param name: Design name, also the Fusion script folder name.
        :param units: mm / cm / in / ft / m.
        :return: Confirmation.
        """
        _DESIGNS.pop(name, None)
        spec = _get(name)
        spec["name"] = name
        spec["units"] = units or self.valves.DEFAULT_UNITS
        # Always start with the root component.
        spec["components"].append({
            "name": "root", "parent": "root",
            "sketches": [], "features": [],
            "material": "", "appearance": "",
        })
        return f"new design '{name}'  units={spec['units']}"

    def add_component(
        self,
        design: str,
        name: str,
        parent: str = "root",
        material: str = "",
        appearance: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Add a sub-component to the design (used for assemblies). Components
        are independent build contexts — sketches and features added to
        component X live inside X, not the parent.
        :param design: Design name from new_design.
        :param name: Component name.
        :param parent: Parent component name. Default "root".
        :param material: Optional Fusion material name (e.g. "Aluminum 6061", "ABS Plastic", "Steel").
        :param appearance: Optional appearance name (e.g. "Metal - Aluminum - Brushed").
        :return: Confirmation.
        """
        spec = _get(design)
        spec["components"].append({
            "name": name, "parent": parent,
            "sketches": [], "features": [],
            "material": material, "appearance": appearance,
        })
        return f"+ component '{name}' (parent={parent}, material={material or '-'})"

    def _component(self, design: str, comp: str) -> dict:
        spec = _get(design)
        for c in spec["components"]:
            if c["name"] == comp:
                return c
        raise KeyError(f"unknown component {comp!r} on design {design!r}")

    def add_sketch(
        self,
        design: str,
        component: str,
        name: str,
        plane: str = "xy",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Add a 2D sketch on a construction plane.
        :param design: Design name.
        :param component: Component to add the sketch to (use "root" for top-level).
        :param name: Sketch name (used as a reference in extrudes).
        :param plane: xy / yz / xz.
        :return: Confirmation.
        """
        c = self._component(design, component)
        c["sketches"].append({"name": name, "plane": plane.lower(), "shapes": []})
        return f"+ sketch '{name}' on {plane.upper()} in '{component}'"

    def _sketch(self, design: str, component: str, sketch: str) -> dict:
        c = self._component(design, component)
        for s in c["sketches"]:
            if s["name"] == sketch:
                return s
        raise KeyError(f"unknown sketch {sketch!r}")

    def sketch_rectangle(
        self,
        design: str, component: str, sketch: str,
        p1: list[float], p2: list[float],
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Add a 2-point rectangle to a sketch.
        :param design: Design name.
        :param component: Component name.
        :param sketch: Sketch name.
        :param p1: [x, y] of one corner (units match design units).
        :param p2: [x, y] of the opposite corner.
        :return: Confirmation.
        """
        s = self._sketch(design, component, sketch)
        s["shapes"].append({"type": "rectangle", "p1": p1, "p2": p2})
        return f"+ rect {p1} → {p2} on sketch '{sketch}'"

    def sketch_circle(
        self,
        design: str, component: str, sketch: str,
        center: list[float], radius: float,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Add a circle to a sketch.
        :param design: Design name.
        :param component: Component name.
        :param sketch: Sketch name.
        :param center: [x, y] center.
        :param radius: Circle radius.
        :return: Confirmation.
        """
        s = self._sketch(design, component, sketch)
        s["shapes"].append({"type": "circle", "center": center, "radius": radius})
        return f"+ circle r={radius} @ {center} on '{sketch}'"

    def sketch_polygon(
        self,
        design: str, component: str, sketch: str,
        center: list[float], sides: int = 6, radius: float = 1.0,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Add a regular polygon (3+ sides) inscribed in a circle of `radius`.
        :param design: Design name.
        :param component: Component name.
        :param sketch: Sketch name.
        :param center: [x, y] center.
        :param sides: Number of sides (3+).
        :param radius: Circumscribed radius.
        :return: Confirmation.
        """
        s = self._sketch(design, component, sketch)
        s["shapes"].append({"type": "polygon", "center": center, "sides": sides,
                            "radius": radius})
        return f"+ {sides}-gon r={radius} @ {center} on '{sketch}'"

    def sketch_line(
        self,
        design: str, component: str, sketch: str,
        p1: list[float], p2: list[float],
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Add a single line segment to a sketch.
        :param design: Design name.
        :param component: Component name.
        :param sketch: Sketch name.
        :param p1: [x, y] start.
        :param p2: [x, y] end.
        :return: Confirmation.
        """
        s = self._sketch(design, component, sketch)
        s["shapes"].append({"type": "line", "p1": p1, "p2": p2})
        return f"+ line {p1} → {p2}"

    def sketch_spline(
        self,
        design: str, component: str, sketch: str,
        points: list[list[float]],
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Add a fitted spline through the given 2D points.
        :param design: Design name.
        :param component: Component name.
        :param sketch: Sketch name.
        :param points: List of [x, y] control points (3+).
        :return: Confirmation.
        """
        s = self._sketch(design, component, sketch)
        s["shapes"].append({"type": "spline", "points": points})
        return f"+ spline {len(points)} pts on '{sketch}'"

    # ── Features ─────────────────────────────────────────────────────────

    def add_extrude(
        self,
        design: str, component: str, sketch: str,
        distance: float,
        operation: str = "new",
        profile_index: int = 0,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Extrude a sketch profile to create / modify a body.
        :param design: Design name.
        :param component: Component name.
        :param sketch: Sketch name to extrude.
        :param distance: Signed extrude distance (units of design).
        :param operation: new (new body), join (add), cut (subtract), intersect.
        :param profile_index: Which profile to extrude when multiple closed loops exist (default 0).
        :return: Confirmation.
        """
        c = self._component(design, component)
        c["features"].append({
            "type": "extrude", "sketch": sketch,
            "distance": distance, "operation": operation,
            "profile_index": profile_index,
        })
        return f"+ extrude '{sketch}' {distance} ({operation}) in '{component}'"

    def add_fillet(
        self,
        design: str, component: str, radius: float,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Apply a constant-radius fillet to every edge of the most-recently
        added body in the component. (For per-edge selection, write a
        custom Fusion script via `fusion360.install_script`.)
        :param design: Design name.
        :param component: Component name.
        :param radius: Fillet radius.
        :return: Confirmation.
        """
        c = self._component(design, component)
        c["features"].append({"type": "fillet", "radius": radius})
        return f"+ fillet r={radius} on '{component}'"

    def add_chamfer(
        self,
        design: str, component: str, distance: float,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Apply a uniform chamfer to every edge of the most-recent body.
        :param design: Design name.
        :param component: Component name.
        :param distance: Chamfer distance.
        :return: Confirmation.
        """
        c = self._component(design, component)
        c["features"].append({"type": "chamfer", "distance": distance})
        return f"+ chamfer d={distance} on '{component}'"

    def add_shell(
        self,
        design: str, component: str, thickness: float,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Hollow the most-recent body by removing its top face and shelling
        to the given wall thickness.
        :param design: Design name.
        :param component: Component name.
        :param thickness: Wall thickness.
        :return: Confirmation.
        """
        c = self._component(design, component)
        c["features"].append({"type": "shell", "thickness": thickness})
        return f"+ shell t={thickness} on '{component}'"

    # ── Export ────────────────────────────────────────────────────────────

    def add_export(
        self,
        design: str,
        format: str,
        path: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Queue an export at the end of the build (writes a STEP / IGES /
        STL / F3D / SMT file once the geometry is finished).
        :param design: Design name.
        :param format: step, iges, stl, f3d, smt.
        :param path: Absolute output path.
        :return: Confirmation.
        """
        spec = _get(design)
        spec["exports"].append({"format": format.lower(), "path": str(Path(path).expanduser().resolve())})
        return f"+ export {format.upper()} -> {path}"

    # ── Build ─────────────────────────────────────────────────────────────

    def build(
        self,
        design: str,
        autostart_in_fusion: bool = False,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Generate a Fusion 360 Python script from the spec and install it
        as a user script under `%APPDATA%\\Autodesk\\Autodesk Fusion 360\\
        API\\Scripts\\<design>\\`. Open Fusion 360 and run it from
        Tools → Scripts and Add-ins → My Scripts → <design>.
        :param design: Design name.
        :param autostart_in_fusion: When True, also launches Fusion 360.
        :return: Path the script was installed to + Fusion launch hint.
        """
        spec = _get(design).copy()
        if not spec.get("components"):
            return f"design '{design}' has no components — call new_design first"
        spec["name"] = design
        script_src = _BUILD_SCRIPT.format(spec_json=json.dumps(spec))
        runner = _fusion_tool()
        result = runner.install_script(
            name=design,
            full_source=script_src,
            description=f"Auto-generated by local-ai-stack ({len(spec['components'])} components)",
        )
        if autostart_in_fusion:
            launch = runner.launch_fusion()
            result += f"\n{launch}"
        _DESIGNS.pop(design, None)
        return result

    # ── Inspection ────────────────────────────────────────────────────────

    def show_spec(self, design: str, __user__: Optional[dict] = None) -> str:
        """
        Pretty-print the current accumulated design spec without committing.
        :param design: Design name.
        :return: JSON dump of the spec.
        """
        if design not in _DESIGNS:
            return f"(no spec for {design})"
        return json.dumps(_DESIGNS[design], indent=2)

    def list_materials(self, __user__: Optional[dict] = None) -> str:
        """
        List a curated subset of common Fusion 360 materials by name. (The
        full library is enumerated only inside Fusion.) Use these names
        verbatim with add_component(material=...).
        :return: Newline-delimited materials.
        """
        return "\n".join([
            "Aluminum 6061", "Aluminum 7075", "Steel", "Stainless Steel 304",
            "Brass", "Copper", "Titanium", "ABS Plastic", "Nylon 6/6",
            "Polycarbonate", "PLA Plastic", "Acrylic - Transparent",
            "Glass", "Ceramic - Generic", "Wood - Oak", "Wood - Pine",
            "Wood - Walnut", "Concrete", "Rubber - Soft",
        ])
