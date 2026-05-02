"""
title: KiCad Author — Schematic + PCB Authoring with 3rd-party Libraries
author: local-ai-stack
description: Author KiCad designs end-to-end. Create a project (`.kicad_pro` + empty `.kicad_sch` + empty `.kicad_pcb`), register 3rd-party symbol/footprint libraries (point at a SnapEDA / Ultra Librarian / GitHub URL or a local path), search SnapEDA for parts by name/MPN, place components on a schematic with hierarchical sheet support, sync the netlist into the PCB, lay out tracks and vias, and run ERC + DRC. Symbol-and-footprint editing uses S-expression I/O so no live KiCad instance is required. Pair with the existing `kicad` tool to render PDFs, gerbers, STEP, BOM, and netlists once the design is ready. If layer count, board size, manufacturing constraints (ENIG vs HASL, hole ranges, copper weight) or part substitutions are unclear, call `ask_clarification` first.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import urllib.request
import zipfile
from pathlib import Path
from textwrap import dedent
from typing import Any, Optional

from pydantic import BaseModel, Field

import httpx


def _kicad_tool():
    spec = importlib.util.spec_from_file_location(
        "_lai_kicad_runner", Path(__file__).parent / "kicad.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod.Tools()


# ─────────────────────────────────────────────────────────────────────────────
# Bare-minimum KiCad 7+ schematic skeleton (S-expression). New components and
# wires are appended to this template by the author tool.
# ─────────────────────────────────────────────────────────────────────────────
_EMPTY_SCH = dedent('''
    (kicad_sch (version 20231120) (generator local_ai_stack)
      (uuid "{sch_uuid}")
      (paper "A4")
      (lib_symbols)
      (sheet_instances
        (path "/" (page "1"))
      )
    )
''').strip()

_EMPTY_PCB = dedent('''
    (kicad_pcb (version 20240108) (generator local_ai_stack)
      (general (thickness 1.6))
      (paper "A4")
      (layers
        (0 "F.Cu" signal)
        (31 "B.Cu" signal)
        (32 "B.Adhes" user)
        (33 "F.Adhes" user)
        (34 "B.Paste" user)
        (35 "F.Paste" user)
        (36 "B.SilkS" user)
        (37 "F.SilkS" user)
        (38 "B.Mask" user)
        (39 "F.Mask" user)
        (40 "Dwgs.User" user)
        (41 "Cmts.User" user)
        (44 "Edge.Cuts" user)
        (45 "Margin" user)
        (49 "F.CrtYd" user)
        (50 "B.CrtYd" user)
        (51 "F.Fab" user)
        (52 "B.Fab" user)
      )
      (setup
        (pad_to_mask_clearance 0)
      )
      (net 0 "")
    )
''').strip()


def _new_uuid() -> str:
    import uuid
    return str(uuid.uuid4())


class Tools:
    class Valves(BaseModel):
        SNAPEDA_API_KEY: str = Field(
            default="",
            description="SnapEDA API key for symbol+footprint search (free, https://www.snapeda.com/account/api/).",
        )
        LIBRARIES_DIR: str = Field(
            default=str(Path.home() / "Documents" / "KiCad" / "third_party_libs"),
            description="Where to fetch/cache 3rd-party libraries (SnapEDA, Ultra Librarian, GitHub).",
        )
        DEFAULT_LAYERS: int = Field(default=2, description="Default copper-layer count for new boards.")

    def __init__(self):
        self.valves = self.Valves()

    # ── Project scaffolding ───────────────────────────────────────────────

    def create_project(
        self,
        project_dir: str,
        name: str = "design",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Create a fresh KiCad project: writes `<name>.kicad_pro`,
        `<name>.kicad_sch` (empty schematic skeleton), and
        `<name>.kicad_pcb` (empty board with the standard layer stack).
        :param project_dir: Directory to create. Must not already contain a project of the same name.
        :param name: Project base name.
        :return: Confirmation listing the three created files.
        """
        d = Path(project_dir).expanduser().resolve()
        d.mkdir(parents=True, exist_ok=True)
        sch_path = d / f"{name}.kicad_sch"
        pcb_path = d / f"{name}.kicad_pcb"
        pro_path = d / f"{name}.kicad_pro"
        if sch_path.exists() or pcb_path.exists():
            return f"Refusing to overwrite existing project at {d}"
        sch_path.write_text(_EMPTY_SCH.format(sch_uuid=_new_uuid()), encoding="utf-8")
        pcb_path.write_text(_EMPTY_PCB, encoding="utf-8")
        # Minimal kicad_pro JSON.
        pro = {
            "board": {"layer_pairs": [], "design_settings": {"defaults": {}}},
            "meta": {"filename": f"{name}.kicad_pro", "version": 1},
            "schematic": {"meta": {"version": 1}},
            "libraries": {"pinned_symbol_libs": [], "pinned_footprint_libs": []},
            "pcbnew": {"page_layout_descr_file": ""},
        }
        pro_path.write_text(json.dumps(pro, indent=2), encoding="utf-8")
        return (
            f"created KiCad project '{name}' under {d}:\n"
            f"  - {pro_path.name}\n  - {sch_path.name}\n  - {pcb_path.name}"
        )

    # ── 3rd-party libraries ──────────────────────────────────────────────

    def fetch_library(
        self,
        url: str,
        name: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Download a symbol/footprint library archive (.zip / .tar.gz / direct
        .kicad_sym / .kicad_mod) into the LIBRARIES_DIR. ZIPs are unpacked.
        Use this for SnapEDA / Ultra Librarian / GitHub libraries.
        :param url: Direct URL to the archive or library file.
        :param name: Optional friendly name (subfolder under LIBRARIES_DIR).
        :return: Path of the downloaded files.
        """
        out_root = Path(self.valves.LIBRARIES_DIR).expanduser()
        out_root.mkdir(parents=True, exist_ok=True)
        target_dir = out_root / (name or Path(url).stem)
        target_dir.mkdir(parents=True, exist_ok=True)
        try:
            with httpx.Client(timeout=60, follow_redirects=True) as client:
                r = client.get(url)
                if r.status_code != 200:
                    return f"download failed: HTTP {r.status_code} from {url}"
                # Detect archive type.
                tail = url.lower().rsplit("?", 1)[0].rsplit("/", 1)[-1]
                if tail.endswith(".zip"):
                    z_path = target_dir / tail
                    z_path.write_bytes(r.content)
                    with zipfile.ZipFile(z_path) as z:
                        z.extractall(target_dir)
                    z_path.unlink()
                    return f"unpacked {len(list(target_dir.rglob('*')))} files -> {target_dir}"
                else:
                    out = target_dir / tail
                    out.write_bytes(r.content)
                    return f"saved {len(r.content)} bytes -> {out}"
        except Exception as e:
            return f"failed: {e}"

    def list_local_libraries(self, __user__: Optional[dict] = None) -> str:
        """
        Enumerate `.kicad_sym` and `.pretty/` paths under LIBRARIES_DIR so
        the model can register them in the project's symbol-library /
        footprint-library tables.
        :return: Two-section listing (symbols vs footprints).
        """
        d = Path(self.valves.LIBRARIES_DIR).expanduser()
        if not d.exists():
            return f"(no libraries dir yet) {d}"
        syms = sorted(p for p in d.rglob("*.kicad_sym"))
        feet = sorted(p for p in d.rglob("*.pretty") if p.is_dir())
        out = ["── symbol libraries (.kicad_sym) ──"]
        out += [str(p) for p in syms] or ["(none)"]
        out.append("\n── footprint libraries (.pretty/) ──")
        out += [str(p) for p in feet] or ["(none)"]
        return "\n".join(out)

    def register_library(
        self,
        project_dir: str,
        symbol_lib: str = "",
        footprint_lib: str = "",
        nickname: str = "third_party",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Register a symbol library (`.kicad_sym`) and/or a footprint library
        (`.pretty/`) in the project's `sym-lib-table` and `fp-lib-table`
        files so KiCad sees them on next open.
        :param project_dir: KiCad project directory.
        :param symbol_lib: Absolute path to a .kicad_sym file. Empty to skip.
        :param footprint_lib: Absolute path to a .pretty directory. Empty to skip.
        :param nickname: Short id used to reference symbols (e.g. `nickname:STM32F4`).
        :return: Per-table confirmation.
        """
        d = Path(project_dir).expanduser().resolve()
        out: list[str] = []
        if symbol_lib:
            sym_table = d / "sym-lib-table"
            entry = f'  (lib (name "{nickname}")(type "KiCad")(uri "{symbol_lib}")(options "")(descr ""))\n'
            if sym_table.exists():
                txt = sym_table.read_text()
                if nickname not in txt:
                    txt = txt.replace(")", entry + ")", 1)
                    sym_table.write_text(txt, encoding="utf-8")
                    out.append(f"+ symbol lib '{nickname}' -> {sym_table}")
                else:
                    out.append(f"already present in sym-lib-table: {nickname}")
            else:
                sym_table.write_text(f"(sym_lib_table\n{entry})\n", encoding="utf-8")
                out.append(f"created sym-lib-table with '{nickname}' -> {sym_table}")
        if footprint_lib:
            fp_table = d / "fp-lib-table"
            entry = f'  (lib (name "{nickname}")(type "KiCad")(uri "{footprint_lib}")(options "")(descr ""))\n'
            if fp_table.exists():
                txt = fp_table.read_text()
                if nickname not in txt:
                    txt = txt.replace(")", entry + ")", 1)
                    fp_table.write_text(txt, encoding="utf-8")
                    out.append(f"+ footprint lib '{nickname}' -> {fp_table}")
                else:
                    out.append(f"already present in fp-lib-table: {nickname}")
            else:
                fp_table.write_text(f"(fp_lib_table\n{entry})\n", encoding="utf-8")
                out.append(f"created fp-lib-table with '{nickname}' -> {fp_table}")
        return "\n".join(out) or "Nothing to register."

    # ── SnapEDA search ────────────────────────────────────────────────────

    def search_snapeda(
        self,
        query: str,
        limit: int = 10,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search the SnapEDA catalogue for parts (ICs, connectors, sensors).
        Returns name, manufacturer, MPN, and a download URL for each match.
        Hand the URL to `fetch_library` to install the symbol+footprint.
        :param query: Search string, typically a manufacturer part number.
        :param limit: Max results.
        :return: One row per match.
        """
        if not self.valves.SNAPEDA_API_KEY:
            return ("SNAPEDA_API_KEY not set on the kicad_author tool's Valves. "
                    "Get a free key at https://www.snapeda.com/account/api/.")
        try:
            with httpx.Client(timeout=15) as c:
                r = c.get(
                    "https://www.snapeda.com/api/v1/parts/search/",
                    params={"q": query, "limit": limit, "kicad": 1},
                    headers={"Authorization": f"Token {self.valves.SNAPEDA_API_KEY}"},
                )
        except Exception as e:
            return f"SnapEDA error: {e}"
        if r.status_code != 200:
            return f"SnapEDA HTTP {r.status_code}: {r.text[:200]}"
        results = (r.json() or {}).get("results", []) or []
        rows = []
        for p in results[:limit]:
            rows.append(
                f"{p.get('part_number','?'):<22}  by {p.get('manufacturer',{}).get('name','?'):<20}  "
                f"{p.get('short_description','')[:40]}\n"
                f"  download: {p.get('download_url') or p.get('url')}"
            )
        return "\n".join(rows) if rows else "(no matches)"

    # ── Schematic primitives (S-expression append) ───────────────────────

    def add_symbol(
        self,
        project_dir: str,
        sch_name: str,
        lib_id: str,
        reference: str,
        value: str,
        x_mm: float, y_mm: float,
        rotation_deg: int = 0,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Append a symbol instance to a schematic. Coordinates are in
        millimetres relative to the page origin (top-left).
        :param project_dir: KiCad project directory.
        :param sch_name: Schematic file name (e.g. "design.kicad_sch" or sub-sheet name).
        :param lib_id: "<nickname>:<symbol_name>" — refer to a registered library.
        :param reference: Reference designator (e.g. "U1", "R1", "C1").
        :param value: Display value (e.g. "STM32F411", "10kΩ", "100nF").
        :param x_mm: X position.
        :param y_mm: Y position.
        :param rotation_deg: 0, 90, 180, or 270.
        :return: Confirmation.
        """
        sch = Path(project_dir).expanduser().resolve() / sch_name
        if not sch.exists():
            return f"Not found: {sch}"
        text = sch.read_text(encoding="utf-8")
        u = _new_uuid()
        block = (
            f'\n  (symbol (lib_id "{lib_id}") (at {x_mm} {y_mm} {rotation_deg})'
            f' (unit 1) (uuid "{u}")\n'
            f'    (property "Reference" "{reference}" (at {x_mm + 2} {y_mm - 2} 0))\n'
            f'    (property "Value" "{value}" (at {x_mm + 2} {y_mm + 2} 0))\n'
            f'  )\n'
        )
        # Insert before the final `)` (close of kicad_sch).
        idx = text.rfind(")")
        new = text[:idx] + block + text[idx:]
        sch.write_text(new, encoding="utf-8")
        return f"+ {reference} ({value}) {lib_id} @ ({x_mm}, {y_mm}) on {sch.name}"

    def add_wire(
        self,
        project_dir: str,
        sch_name: str,
        x1_mm: float, y1_mm: float,
        x2_mm: float, y2_mm: float,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Add a single wire segment connecting two points on the schematic.
        :param project_dir: KiCad project directory.
        :param sch_name: Schematic file name.
        :param x1_mm: Start X (mm).
        :param y1_mm: Start Y (mm).
        :param x2_mm: End X (mm).
        :param y2_mm: End Y (mm).
        :return: Confirmation.
        """
        sch = Path(project_dir).expanduser().resolve() / sch_name
        if not sch.exists():
            return f"Not found: {sch}"
        text = sch.read_text(encoding="utf-8")
        u = _new_uuid()
        block = (
            f'\n  (wire (pts (xy {x1_mm} {y1_mm}) (xy {x2_mm} {y2_mm}))'
            f' (stroke (width 0)(type default)) (uuid "{u}"))\n'
        )
        idx = text.rfind(")")
        sch.write_text(text[:idx] + block + text[idx:], encoding="utf-8")
        return f"+ wire ({x1_mm},{y1_mm}) → ({x2_mm},{y2_mm})"

    def add_label(
        self,
        project_dir: str,
        sch_name: str,
        net_name: str,
        x_mm: float, y_mm: float,
        rotation_deg: int = 0,
        kind: str = "local",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Add a net label at a wire endpoint to assign it a name. `kind`
        selects local / global / hierarchical.
        :param project_dir: KiCad project directory.
        :param sch_name: Schematic file.
        :param net_name: Net name string (e.g. "VCC", "GND", "SDA").
        :param x_mm: X.
        :param y_mm: Y.
        :param rotation_deg: 0/90/180/270.
        :param kind: local, global, hierarchical.
        :return: Confirmation.
        """
        sch = Path(project_dir).expanduser().resolve() / sch_name
        if not sch.exists():
            return f"Not found: {sch}"
        text = sch.read_text(encoding="utf-8")
        u = _new_uuid()
        kw = {"local": "label", "global": "global_label",
              "hierarchical": "hierarchical_label"}.get(kind, "label")
        block = (
            f'\n  ({kw} "{net_name}" (shape input) (at {x_mm} {y_mm} {rotation_deg})'
            f' (effects (font (size 1.27 1.27))) (uuid "{u}"))\n'
        )
        idx = text.rfind(")")
        sch.write_text(text[:idx] + block + text[idx:], encoding="utf-8")
        return f"+ {kind} label '{net_name}' @ ({x_mm}, {y_mm})"

    def add_hierarchical_sheet(
        self,
        project_dir: str,
        parent_sch: str,
        child_sch: str,
        sheet_name: str,
        x_mm: float, y_mm: float,
        w_mm: float = 30.0,
        h_mm: float = 20.0,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Add a hierarchical sheet block on `parent_sch` that references a
        child schematic file. Creates an empty `child_sch` if it doesn't
        exist already.
        :param project_dir: KiCad project directory.
        :param parent_sch: Parent schematic filename.
        :param child_sch: Child schematic filename to link.
        :param sheet_name: Display name for the sheet block.
        :param x_mm: Top-left X.
        :param y_mm: Top-left Y.
        :param w_mm: Sheet block width.
        :param h_mm: Sheet block height.
        :return: Confirmation.
        """
        d = Path(project_dir).expanduser().resolve()
        parent = d / parent_sch
        child = d / child_sch
        if not parent.exists():
            return f"Not found: {parent}"
        if not child.exists():
            child.write_text(_EMPTY_SCH.format(sch_uuid=_new_uuid()), encoding="utf-8")
        text = parent.read_text(encoding="utf-8")
        u = _new_uuid()
        block = (
            f'\n  (sheet (at {x_mm} {y_mm}) (size {w_mm} {h_mm}) (fields_autoplaced)'
            f' (stroke (width 0.1524) (type solid)) (fill (color 0 0 0 0)) (uuid "{u}")\n'
            f'    (property "Sheetname" "{sheet_name}" (at {x_mm} {y_mm - 1} 0))\n'
            f'    (property "Sheetfile" "{child_sch}" (at {x_mm} {y_mm + h_mm + 1} 0))\n'
            f'  )\n'
        )
        idx = text.rfind(")")
        parent.write_text(text[:idx] + block + text[idx:], encoding="utf-8")
        return f"+ hierarchical sheet '{sheet_name}' -> {child_sch} @ ({x_mm}, {y_mm})"

    # ── PCB primitives ────────────────────────────────────────────────────

    def add_track(
        self,
        project_dir: str,
        pcb_name: str,
        x1_mm: float, y1_mm: float,
        x2_mm: float, y2_mm: float,
        layer: str = "F.Cu",
        width_mm: float = 0.25,
        net: int = 0,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Add a single PCB track (segment) on the named layer.
        :param project_dir: KiCad project directory.
        :param pcb_name: PCB filename.
        :param x1_mm: Start X.
        :param y1_mm: Start Y.
        :param x2_mm: End X.
        :param y2_mm: End Y.
        :param layer: Layer name (F.Cu / B.Cu / In1.Cu / In2.Cu / ...).
        :param width_mm: Track width.
        :param net: Net id (0 = no-net; sync_pcb_from_schematic generates real ids).
        :return: Confirmation.
        """
        pcb = Path(project_dir).expanduser().resolve() / pcb_name
        if not pcb.exists():
            return f"Not found: {pcb}"
        text = pcb.read_text(encoding="utf-8")
        u = _new_uuid()
        block = (
            f'\n  (segment (start {x1_mm} {y1_mm}) (end {x2_mm} {y2_mm})'
            f' (width {width_mm}) (layer "{layer}") (net {net}) (tstamp "{u}"))\n'
        )
        idx = text.rfind(")")
        pcb.write_text(text[:idx] + block + text[idx:], encoding="utf-8")
        return f"+ track {layer} {width_mm}mm ({x1_mm},{y1_mm}) → ({x2_mm},{y2_mm})"

    def add_via(
        self,
        project_dir: str,
        pcb_name: str,
        x_mm: float, y_mm: float,
        size_mm: float = 0.6,
        drill_mm: float = 0.3,
        net: int = 0,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Add a through-hole via at (x, y).
        :param project_dir: KiCad project directory.
        :param pcb_name: PCB filename.
        :param x_mm: X.
        :param y_mm: Y.
        :param size_mm: Annular ring outer diameter.
        :param drill_mm: Drilled hole diameter.
        :param net: Net id (0 = no-net).
        :return: Confirmation.
        """
        pcb = Path(project_dir).expanduser().resolve() / pcb_name
        if not pcb.exists():
            return f"Not found: {pcb}"
        text = pcb.read_text(encoding="utf-8")
        u = _new_uuid()
        block = (
            f'\n  (via (at {x_mm} {y_mm}) (size {size_mm}) (drill {drill_mm})'
            f' (layers "F.Cu" "B.Cu") (net {net}) (tstamp "{u}"))\n'
        )
        idx = text.rfind(")")
        pcb.write_text(text[:idx] + block + text[idx:], encoding="utf-8")
        return f"+ via @ ({x_mm}, {y_mm}) size={size_mm} drill={drill_mm}"

    def add_board_outline(
        self,
        project_dir: str,
        pcb_name: str,
        width_mm: float,
        height_mm: float,
        x_offset_mm: float = 50.0,
        y_offset_mm: float = 50.0,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Add a rectangular board outline on Edge.Cuts.
        :param project_dir: KiCad project directory.
        :param pcb_name: PCB filename.
        :param width_mm: Board width.
        :param height_mm: Board height.
        :param x_offset_mm: X position of the bottom-left corner.
        :param y_offset_mm: Y position of the bottom-left corner.
        :return: Confirmation.
        """
        pcb = Path(project_dir).expanduser().resolve() / pcb_name
        if not pcb.exists():
            return f"Not found: {pcb}"
        text = pcb.read_text(encoding="utf-8")
        x1, y1 = x_offset_mm, y_offset_mm
        x2, y2 = x_offset_mm + width_mm, y_offset_mm + height_mm
        seg = lambda a, b: (
            f'  (gr_line (start {a[0]} {a[1]}) (end {b[0]} {b[1]})'
            f' (stroke (width 0.05)(type default)) (layer "Edge.Cuts") (tstamp "{_new_uuid()}"))\n'
        )
        block = (
            "\n" + seg((x1, y1), (x2, y1)) + seg((x2, y1), (x2, y2))
            + seg((x2, y2), (x1, y2)) + seg((x1, y2), (x1, y1))
        )
        idx = text.rfind(")")
        pcb.write_text(text[:idx] + block + text[idx:], encoding="utf-8")
        return f"+ board outline {width_mm}×{height_mm} mm at ({x_offset_mm}, {y_offset_mm})"

    # ── Sync + verify ────────────────────────────────────────────────────

    def sync_pcb_from_schematic(
        self,
        project_dir: str,
        sch_name: str,
        pcb_name: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Pull the netlist out of the schematic and into the PCB. Uses
        `kicad-cli sch export netlist` + manual placement of footprints
        based on the schematic's symbol references.
        :param project_dir: KiCad project directory.
        :param sch_name: Schematic filename.
        :param pcb_name: PCB filename to update.
        :return: kicad-cli output.
        """
        d = Path(project_dir).expanduser().resolve()
        sch = d / sch_name
        nl_path = d / f"{sch.stem}.net"
        runner = _kicad_tool()
        result = runner.export_netlist(str(sch), str(nl_path), format="kicadsexpr")
        return (
            f"{result}\n\n"
            "Note: kicad_author writes a netlist beside the schematic. To pull it into the PCB,\n"
            "open the .kicad_pcb in pcbnew and run Tools → Update PCB from Schematic, or run\n"
            "`kicad-cli pcb update <pcb>`. The author tool stops short of mutating footprint\n"
            "placement automatically — that step often needs human review of fanout / placement."
        )

    def run_erc(
        self,
        project_dir: str,
        sch_name: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Run ERC on the schematic via kicad-cli.
        :param project_dir: KiCad project directory.
        :param sch_name: Schematic filename.
        :return: kicad-cli output.
        """
        sch = Path(project_dir).expanduser().resolve() / sch_name
        return _kicad_tool().run_erc(str(sch))

    def run_drc(
        self,
        project_dir: str,
        pcb_name: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Run DRC on the PCB via kicad-cli.
        :param project_dir: KiCad project directory.
        :param pcb_name: PCB filename.
        :return: kicad-cli output.
        """
        pcb = Path(project_dir).expanduser().resolve() / pcb_name
        return _kicad_tool().run_drc(str(pcb))
