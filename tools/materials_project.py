"""
title: Materials Project — Computational Materials Science Database
author: local-ai-stack
description: Search the Materials Project database of 150,000+ inorganic compounds. Get crystal structures, electronic band gaps, formation energies, magnetic properties, elasticity, and thermodynamic stability data computed via high-throughput DFT. Essential for materials engineering, solid-state physics, and battery/catalyst research. Free API key at materialsproject.org.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import os
import httpx
from typing import Callable, Any, Optional
from pydantic import BaseModel, Field

BASE = "https://api.materialsproject.org"


class Tools:
    class Valves(BaseModel):
        MP_API_KEY: str = Field(
            default_factory=lambda: os.environ.get("MP_API_KEY", ""),
            description="Materials Project API key — free at https://materialsproject.org (register → API key in dashboard)",
        )

    def __init__(self):
        self.valves = self.Valves()

    def _check_key(self) -> Optional[str]:
        if not self.valves.MP_API_KEY:
            return (
                "Materials Project API key required.\n"
                "1. Register free at: https://materialsproject.org\n"
                "2. Go to Dashboard → API key\n"
                "3. Add it in Open WebUI > Tools > Materials Project > MP_API_KEY"
            )
        return None

    def _headers(self) -> dict:
        return {"X-API-KEY": self.valves.MP_API_KEY, "Accept": "application/json"}

    async def search_materials(
        self,
        formula: str = "",
        elements: str = "",
        band_gap_min: float = -1,
        band_gap_max: float = -1,
        is_stable: bool = False,
        limit: int = 10,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search the Materials Project database for inorganic compounds by formula, elements, or band gap.
        :param formula: Chemical formula to search (e.g. 'Fe2O3', 'LiCoO2', 'GaN', 'SiC', 'TiO2') — supports wildcards like 'Li*O'
        :param elements: Comma-separated elements that must be present (e.g. 'Li,Co,O' for lithium cobalt oxides)
        :param band_gap_min: Minimum electronic band gap in eV (-1 = no filter). Use 0 for metals, 0.5 for semiconductors.
        :param band_gap_max: Maximum electronic band gap in eV (-1 = no filter). Use 0 for metals, 4 for visible light absorption.
        :param is_stable: If True, only return thermodynamically stable phases (energy above hull = 0)
        :param limit: Number of results (max 20)
        :return: Material ID, formula, energy above hull, band gap, space group, and key properties
        """
        err = self._check_key()
        if err:
            return err

        if not formula and not elements:
            return "Provide at least a formula (e.g. 'TiO2') or elements (e.g. 'Ti,O')."

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Searching Materials Project...", "done": False}})

        params = {
            "_limit": min(limit, 20),
            "fields": "material_id,formula_pretty,energy_above_hull,band_gap,is_stable,symmetry,volume,density,nsites",
        }
        if formula:
            params["formula"] = formula
        if elements:
            params["elements"] = elements
        if band_gap_min >= 0:
            params["band_gap_min"] = band_gap_min
        if band_gap_max >= 0:
            params["band_gap_max"] = band_gap_max
        if is_stable:
            params["is_stable"] = True

        try:
            async with httpx.AsyncClient(timeout=20, headers=self._headers()) as client:
                resp = await client.get(f"{BASE}/materials/summary/", params=params)
                if resp.status_code == 403:
                    return "Invalid API key. Check your Materials Project API key."
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            return f"Materials Project error: {str(e)}"

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        materials = data.get("data", [])
        total = data.get("meta", {}).get("total_doc", len(materials))

        if not materials:
            return f"No materials found. Try a broader search (e.g. different formula or fewer element constraints)."

        lines = [f"## Materials Project Search\n"]
        search_desc = []
        if formula:
            search_desc.append(f"Formula: {formula}")
        if elements:
            search_desc.append(f"Elements: {elements}")
        if band_gap_min >= 0 or band_gap_max >= 0:
            bg_range = f"{band_gap_min if band_gap_min >= 0 else '?'} – {band_gap_max if band_gap_max >= 0 else '?'} eV"
            search_desc.append(f"Band gap: {bg_range}")
        if is_stable:
            search_desc.append("Stable phases only")
        lines.append(f"**Search:** {' | '.join(search_desc)}")
        lines.append(f"**Results:** {len(materials)} of {total} total\n")

        lines.append("| Material ID | Formula | Band Gap (eV) | E above hull (eV/atom) | Stable | Space Group | Density (g/cm³) |")
        lines.append("|-------------|---------|-------------|----------------------|--------|------------|----------------|")

        for m in materials:
            mid = m.get("material_id", "")
            formula_pretty = m.get("formula_pretty", "")
            bg = m.get("band_gap")
            e_hull = m.get("energy_above_hull")
            stable = "✓" if m.get("is_stable") else ""
            sym = m.get("symmetry", {})
            spg = sym.get("symbol", "") if isinstance(sym, dict) else ""
            density = m.get("density")

            bg_str = f"{bg:.3f}" if bg is not None else "—"
            e_hull_str = f"{e_hull:.4f}" if e_hull is not None else "—"
            density_str = f"{density:.3f}" if density is not None else "—"

            # Classify band gap
            if bg is not None:
                if bg < 0.1:
                    bg_str += " (Metal)"
                elif bg < 1.5:
                    bg_str += " (Narrow gap)"
                elif bg < 3.0:
                    bg_str += " (Semiconductor)"
                else:
                    bg_str += " (Wide gap/Insulator)"

            mp_url = f"https://materialsproject.org/materials/{mid}"
            lines.append(f"| [{mid}]({mp_url}) | **{formula_pretty}** | {bg_str} | {e_hull_str} | {stable} | {spg} | {density_str} |")

        lines.append("\n**Notes:**")
        lines.append("- Energy above hull = 0 means thermodynamically stable; > 0.1 eV/atom indicates metastable")
        lines.append("- Band gap = 0 → metal; 0–1.5 eV → narrow gap; 1.5–3 eV → semiconductor; > 3 eV → insulator")
        return "\n".join(lines)

    async def get_material_details(
        self,
        material_id: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get detailed properties for a specific material by its Materials Project ID.
        :param material_id: Materials Project ID (e.g. 'mp-19017' for Fe2O3, 'mp-22526' for LiCoO2, 'mp-661' for Si)
        :return: Full property set: crystal structure, electronic, magnetic, mechanical, and thermodynamic data
        """
        err = self._check_key()
        if err:
            return err

        if not material_id.startswith("mp-"):
            material_id = f"mp-{material_id}"

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Fetching {material_id} properties...", "done": False}})

        fields = (
            "material_id,formula_pretty,energy_above_hull,band_gap,is_stable,"
            "symmetry,volume,density,nsites,elements,nelements,"
            "total_magnetization,is_magnetic,ordering,"
            "theoretical,deprecated"
        )

        try:
            async with httpx.AsyncClient(timeout=20, headers=self._headers()) as client:
                resp = await client.get(
                    f"{BASE}/materials/summary/",
                    params={"material_ids": material_id, "fields": fields},
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            return f"Materials Project error: {str(e)}"

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        results = data.get("data", [])
        if not results:
            return f"Material '{material_id}' not found. Check the ID format (e.g. 'mp-661')."

        m = results[0]
        formula = m.get("formula_pretty", "")
        bg = m.get("band_gap")
        e_hull = m.get("energy_above_hull")
        stable = m.get("is_stable")
        sym = m.get("symmetry", {})
        spg = sym.get("symbol", "") if isinstance(sym, dict) else ""
        crystal_sys = sym.get("crystal_system", "") if isinstance(sym, dict) else ""
        volume = m.get("volume")
        density = m.get("density")
        nsites = m.get("nsites")
        elements = m.get("elements", [])
        total_mag = m.get("total_magnetization")
        is_magnetic = m.get("is_magnetic")
        ordering = m.get("ordering", "")
        theoretical = m.get("theoretical", False)

        mp_url = f"https://materialsproject.org/materials/{material_id}"

        lines = [f"## {formula} — Materials Project: {material_id}\n"]
        lines.append(f"[View on Materials Project]({mp_url})\n")

        lines.append("### Crystal Structure")
        lines.append(f"- **Space Group:** {spg}")
        lines.append(f"- **Crystal System:** {crystal_sys}")
        lines.append(f"- **Volume:** {volume:.3f} Å³" if volume else "")
        lines.append(f"- **Density:** {density:.3f} g/cm³" if density else "")
        lines.append(f"- **Sites (atoms/cell):** {nsites}")
        lines.append(f"- **Constituent Elements:** {', '.join(elements)}")
        lines.append(f"- **Source:** {'Theoretical prediction' if theoretical else 'Experimental / DFT verified'}")

        lines.append("\n### Electronic Properties")
        if bg is not None:
            bg_type = "Metal" if bg < 0.1 else ("Semiconductor" if bg < 3.0 else "Insulator/Wide-gap")
            lines.append(f"- **Band Gap:** {bg:.3f} eV ({bg_type})")
        lines.append(f"- **Thermodynamic Stability:** {'✅ Stable (hull = 0)' if stable else f'⚠️ Metastable (Ehull = {e_hull:.4f} eV/atom)' if e_hull is not None else '—'}")

        if is_magnetic is not None:
            lines.append("\n### Magnetic Properties")
            lines.append(f"- **Magnetic:** {'Yes' if is_magnetic else 'No'}")
            if ordering:
                lines.append(f"- **Magnetic Ordering:** {ordering}")
            if total_mag is not None:
                lines.append(f"- **Total Magnetization:** {total_mag:.3f} μB/f.u.")

        return "\n".join(l for l in lines if l != "")

    async def search_by_property(
        self,
        property_type: str,
        min_value: float = 0,
        max_value: float = 0,
        elements_required: str = "",
        limit: int = 10,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Find materials by specific property ranges — useful for materials discovery and screening.
        :param property_type: Property to filter on: 'band_gap' (eV), 'energy_above_hull' (eV/atom)
        :param min_value: Minimum property value
        :param max_value: Maximum property value (0 = no upper limit)
        :param elements_required: Comma-separated elements that must be present (e.g. 'Li,P' for solid electrolytes)
        :param limit: Number of results
        :return: Materials satisfying the property constraints, ranked by stability
        """
        err = self._check_key()
        if err:
            return err

        property_map = {
            "band_gap": ("band_gap_min", "band_gap_max", "Band Gap (eV)"),
            "energy_above_hull": ("energy_above_hull_min", "energy_above_hull_max", "Energy Above Hull (eV/atom)"),
        }

        if property_type not in property_map:
            return f"Property must be one of: {', '.join(property_map.keys())}"

        param_min, param_max, display_name = property_map[property_type]

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Searching for materials with {property_type} {min_value}–{max_value}...", "done": False}})

        params = {
            "_limit": min(limit, 20),
            "fields": "material_id,formula_pretty,energy_above_hull,band_gap,is_stable,symmetry,density",
            param_min: min_value,
        }
        if max_value > 0:
            params[param_max] = max_value
        if elements_required:
            params["elements"] = elements_required

        try:
            async with httpx.AsyncClient(timeout=20, headers=self._headers()) as client:
                resp = await client.get(f"{BASE}/materials/summary/", params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            return f"Materials Project error: {str(e)}"

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        materials = data.get("data", [])
        if not materials:
            return f"No materials found with {property_type} in range [{min_value}, {max_value if max_value else '∞'}]."

        range_str = f"{min_value} – {max_value if max_value else '∞'}"
        lines = [f"## Materials by {display_name}: {range_str}\n"]
        if elements_required:
            lines.append(f"**Required elements:** {elements_required}\n")
        lines.append(f"Found {len(materials)} materials\n")

        lines.append("| Material ID | Formula | Band Gap (eV) | E Hull (eV/atom) | Stable | Space Group |")
        lines.append("|-------------|---------|-------------|-----------------|--------|------------|")

        for m in materials:
            mid = m.get("material_id", "")
            formula = m.get("formula_pretty", "")
            bg = m.get("band_gap")
            e_hull = m.get("energy_above_hull")
            stable = "✓" if m.get("is_stable") else ""
            sym = m.get("symmetry", {})
            spg = sym.get("symbol", "") if isinstance(sym, dict) else ""

            bg_str = f"{bg:.3f}" if bg is not None else "—"
            e_hull_str = f"{e_hull:.4f}" if e_hull is not None else "—"

            lines.append(f"| {mid} | **{formula}** | {bg_str} | {e_hull_str} | {stable} | {spg} |")

        return "\n".join(lines)
