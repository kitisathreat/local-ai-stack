"""
title: SIMBAD — Astronomical Database (Stars, Galaxies, Nebulae)
author: local-ai-stack
description: Query the SIMBAD Astronomical Database at the Centre de Données astronomiques de Strasbourg (CDS). Look up 15+ million astronomical objects: stars, galaxies, nebulae, pulsars, black holes, and exoplanet host stars. Get coordinates, spectral types, parallax, proper motion, radial velocity, photometry, and bibliography. No API key required.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
import re
from typing import Callable, Any, Optional
from pydantic import BaseModel, Field

SIMBAD_SCRIPT = "https://simbad.u-strasbg.fr/simbad/sim-script"
SIMBAD_TAP = "https://simbad.u-strasbg.fr/simbad/sim-tap/sync"
SIMBAD_ID = "https://simbad.u-strasbg.fr/simbad/sim-id"


class Tools:
    class Valves(BaseModel):
        pass

    def __init__(self):
        self.valves = self.Valves()

    async def lookup_object(
        self,
        name: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Look up an astronomical object by name in SIMBAD. Works with common names, catalog IDs, and designations.
        :param name: Object name (e.g. 'Sirius', 'Andromeda Galaxy', 'Betelgeuse', 'Crab Nebula', 'M87', 'NGC 224', 'Alpha Centauri', 'Proxima Centauri', '51 Pegasi')
        :return: Object type, coordinates, distance, spectral type, magnitude, and physical properties
        """
        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Looking up {name} in SIMBAD...", "done": False}})

        # Use the SIMBAD script interface with formatted output
        script = f"""format object "%IDLIST(1)\\t%OTYPELIST\\t%OTYPE(S)\\t%COO(d;A D)\\t%PLX(V)\\t%RV(V)\\t%SP(S)\\t%FLUXLIST(V,R,B,J,K;N=V,R,B,J,K;F=5.3)"
query id {name}"""

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(
                    SIMBAD_SCRIPT,
                    data={"submit": "submit script", "script": script},
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                resp.raise_for_status()
                raw = resp.text
        except Exception as e:
            return f"SIMBAD lookup error: {str(e)}"

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        if "Object not found" in raw or "No object found" in raw:
            return f"'{name}' not found in SIMBAD. Try alternate names or catalog IDs (e.g. 'NGC 224' for Andromeda, 'M 31')."

        # Fall back to the simpler ID query for structured data
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(
                    SIMBAD_ID,
                    params={
                        "Ident": name,
                        "output.format": "ASCII",
                        "list.idsel": "on",
                        "obj.bibsel": "off",
                    },
                )
                text = resp.text
        except Exception as e:
            text = ""

        lines = [f"## SIMBAD: {name}\n"]

        # Parse key fields from ASCII output
        def extract(label, text):
            m = re.search(rf"{label}[:\s]+(.+)", text)
            return m.group(1).strip() if m else ""

        if text:
            obj_type = extract("Object type", text) or extract("OTYPE", text)
            ra_dec = extract("Coordinates", text) or extract("RA", text)
            parallax = extract("Parallax", text) or extract("Plx", text)
            radial_vel = extract("Radial velocity", text) or extract("RV", text)
            spec_type = extract("Spectral type", text) or extract("SpType", text)
            proper_motion = extract("Proper motion", text) or extract("pm", text)

            if obj_type:
                lines.append(f"**Object Type:** {obj_type}")
            if ra_dec:
                lines.append(f"**Coordinates:** {ra_dec}")
            if parallax:
                try:
                    plx_val = float(re.findall(r"[-\d.]+", parallax)[0])
                    if plx_val > 0:
                        dist_pc = 1000 / plx_val
                        dist_ly = dist_pc * 3.26156
                        lines.append(f"**Parallax:** {plx_val:.4f} mas → {dist_pc:.1f} pc ({dist_ly:.1f} light-years)")
                except Exception:
                    lines.append(f"**Parallax:** {parallax}")
            if radial_vel:
                lines.append(f"**Radial Velocity:** {radial_vel}")
            if spec_type:
                lines.append(f"**Spectral Type:** {spec_type}")
            if proper_motion:
                lines.append(f"**Proper Motion:** {proper_motion}")

        # Raw excerpt
        clean_lines = [l for l in raw.split("\n") if l.strip() and not l.startswith("::") and not l.startswith("%")][:30]
        if clean_lines:
            lines.append("\n### Raw SIMBAD Data\n```")
            lines.extend(clean_lines[:20])
            lines.append("```")

        lines.append(f"\n[View on SIMBAD](https://simbad.u-strasbg.fr/simbad/sim-id?Ident={name.replace(' ', '+')}&output.format=HTML)")
        return "\n".join(lines)

    async def search_region(
        self,
        ra: float,
        dec: float,
        radius_arcmin: float = 5,
        object_types: str = "",
        limit: int = 20,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search SIMBAD for all astronomical objects within a circular sky region.
        :param ra: Right Ascension in decimal degrees (0-360, e.g. 187.7059 for Virgo cluster)
        :param dec: Declination in decimal degrees (-90 to +90, e.g. 12.3911 for Virgo cluster)
        :param radius_arcmin: Search radius in arcminutes (default 5, max 60)
        :param object_types: Filter by SIMBAD object type (e.g. 'Star', 'Galaxy', 'GlobCluster', 'Pulsar', 'BlackHole')
        :param limit: Maximum objects to return
        :return: Objects in the region with type, designation, and distance from center
        """
        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Searching SIMBAD at RA={ra:.3f}, Dec={dec:.3f}...", "done": False}})

        radius_deg = min(radius_arcmin, 60) / 60

        otype_filter = f"AND otype = '{object_types}'" if object_types else ""
        adql = f"""SELECT TOP {min(limit,50)} main_id, otype, ra, dec, plx_value, rvz_radvel, sp_type
FROM basic
JOIN allfluxes ON oid = allfluxes.oidref
WHERE CONTAINS(POINT('ICRS', ra, dec), CIRCLE('ICRS', {ra}, {dec}, {radius_deg})) = 1
{otype_filter}
ORDER BY DISTANCE(POINT('ICRS', ra, dec), POINT('ICRS', {ra}, {dec}))"""

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(
                    SIMBAD_TAP,
                    params={"query": adql, "format": "json", "REQUEST": "doQuery", "LANG": "ADQL", "VERSION": "1.0"},
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            # Fallback to simpler query without join
            try:
                simple_adql = f"""SELECT TOP {min(limit,50)} main_id, otype, ra, dec, plx_value, rvz_radvel, sp_type
FROM basic
WHERE CONTAINS(POINT('ICRS', ra, dec), CIRCLE('ICRS', {ra}, {dec}, {radius_deg})) = 1
ORDER BY DISTANCE(POINT('ICRS', ra, dec), POINT('ICRS', {ra}, {dec}))"""
                async with httpx.AsyncClient(timeout=20) as client:
                    resp = await client.get(
                        SIMBAD_TAP,
                        params={"query": simple_adql, "format": "json", "REQUEST": "doQuery", "LANG": "ADQL", "VERSION": "1.0"},
                    )
                    resp.raise_for_status()
                    data = resp.json()
            except Exception as e2:
                return f"SIMBAD region search error: {str(e2)}"

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        metadata = data.get("metadata", [])
        rows = data.get("data", [])

        if not rows:
            return f"No objects found within {radius_arcmin}' of RA={ra}, Dec={dec}."

        col_names = [m.get("name", "") for m in metadata]

        def get_col(row, name):
            try:
                idx = col_names.index(name)
                return row[idx]
            except (ValueError, IndexError):
                return None

        lines = [f"## SIMBAD Region Search\n"]
        lines.append(f"**Center:** RA={ra}°, Dec={dec}° | **Radius:** {radius_arcmin}' | **Objects found:** {len(rows)}\n")
        lines.append("| Name | Type | RA (°) | Dec (°) | Spectral Type | Parallax (mas) |")
        lines.append("|------|------|--------|---------|--------------|---------------|")

        for row in rows:
            name = get_col(row, "main_id") or ""
            otype = get_col(row, "otype") or ""
            ra_obj = get_col(row, "ra")
            dec_obj = get_col(row, "dec")
            plx = get_col(row, "plx_value")
            sp = get_col(row, "sp_type") or ""

            ra_s = f"{ra_obj:.4f}" if ra_obj is not None else "—"
            dec_s = f"{dec_obj:.4f}" if dec_obj is not None else "—"
            plx_s = f"{plx:.4f}" if plx is not None else "—"

            lines.append(f"| {name} | {otype} | {ra_s} | {dec_s} | {sp} | {plx_s} |")

        return "\n".join(lines)

    async def search_by_type(
        self,
        object_type: str,
        limit: int = 20,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search SIMBAD for the brightest/most prominent objects of a given astronomical type.
        :param object_type: Object type code (e.g. 'Pulsar', 'GlobCluster', 'GalaxyGroup', 'HII', 'Wolf-Rayet', 'Mira', 'BlackHole', 'SNRemnant', 'QSO')
        :param limit: Number of results (max 50)
        :return: List of objects of that type with coordinates and key properties
        """
        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Searching SIMBAD for {object_type} objects...", "done": False}})

        adql = f"""SELECT TOP {min(limit, 50)} main_id, otype, ra, dec, plx_value, sp_type
FROM basic
WHERE otype LIKE '%{object_type}%'
ORDER BY main_id"""

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(
                    SIMBAD_TAP,
                    params={"query": adql, "format": "json", "REQUEST": "doQuery", "LANG": "ADQL", "VERSION": "1.0"},
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            return f"SIMBAD type search error: {str(e)}"

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        rows = data.get("data", [])
        metadata = data.get("metadata", [])

        if not rows:
            return f"No objects found of type '{object_type}'. Try: Pulsar, GlobCluster, QSO, HII, Mira, SNRemnant."

        col_names = [m.get("name", "") for m in metadata]

        def get_col(row, name):
            try:
                idx = col_names.index(name)
                return row[idx]
            except (ValueError, IndexError):
                return None

        lines = [f"## SIMBAD: {object_type} Objects ({len(rows)} results)\n"]
        lines.append("| Name | Type | RA (°) | Dec (°) | Spectral Type |")
        lines.append("|------|------|--------|---------|--------------|")
        for row in rows:
            name = get_col(row, "main_id") or ""
            otype = get_col(row, "otype") or ""
            ra_obj = get_col(row, "ra")
            dec_obj = get_col(row, "dec")
            sp = get_col(row, "sp_type") or ""
            lines.append(f"| {name} | {otype} | {ra_obj:.3f if ra_obj else '—'} | {dec_obj:.3f if dec_obj else '—'} | {sp} |")

        return "\n".join(lines)
