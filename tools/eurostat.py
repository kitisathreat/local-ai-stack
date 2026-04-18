"""
title: Eurostat — European Statistics
author: local-ai-stack
description: Access Eurostat's European statistical data through the JSON-stat API. GDP, population, trade, labor, energy, transport, demographics for EU27 + EFTA + candidate countries. No API key required.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from pydantic import BaseModel, Field
from typing import Optional


BASE = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data"


class Tools:
    class Valves(BaseModel):
        DEFAULT_LIMIT: int = Field(default=50, description="Max rows in a flattened response")

    def __init__(self):
        self.valves = self.Valves()

    async def dataset(
        self,
        dataset_code: str,
        filters: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Fetch a Eurostat dataset (JSON-stat) and flatten a subset of values.
        :param dataset_code: Eurostat dataset code (e.g. "nama_10_gdp", "une_rt_m", "prc_hicp_manr")
        :param filters: Optional filters as key=value comma separated (e.g. "geo=DE,unit=CP_MEUR"); use browser to find codes
        :return: Table of (dimensions → value, time)
        """
        params = {"format": "JSON", "lang": "EN"}
        for f in filters.split(",") if filters else []:
            if "=" in f:
                k, v = f.split("=", 1)
                params[k.strip()] = v.strip()
        try:
            async with httpx.AsyncClient(timeout=45) as client:
                r = await client.get(f"{BASE}/{dataset_code}", params=params)
                if r.status_code != 200:
                    return f"Eurostat returned HTTP {r.status_code}: {r.text[:300]}"
                data = r.json()
            dims = data.get("dimension", {})
            dim_ids = data.get("id", [])
            sizes = data.get("size", [])
            values = data.get("value", {})
            # Build index decoder
            strides = []
            acc = 1
            for s in reversed(sizes):
                strides.insert(0, acc)
                acc *= s
            dim_labels = {}
            for dim_id in dim_ids:
                cat = dims.get(dim_id, {}).get("category", {})
                idx = cat.get("index", {})
                lbl = cat.get("label", {})
                if isinstance(idx, list):
                    idx = {v: i for i, v in enumerate(idx)}
                dim_labels[dim_id] = {v: (k, lbl.get(k, k)) for k, v in idx.items()}
            label = (data.get("label") or dataset_code)
            lines = [f"## Eurostat {dataset_code}", f"_{label}_\n"]
            header = "| " + " | ".join(dim_ids) + " | value |"
            sep = "|" + "|".join("---" for _ in dim_ids) + "|---|"
            lines += [header, sep]
            count = 0
            for k, v in values.items():
                try:
                    idx = int(k)
                except Exception:
                    continue
                coords = []
                rem = idx
                for st in strides:
                    coords.append(rem // st)
                    rem = rem % st
                row = []
                for dim_id, coord in zip(dim_ids, coords):
                    code, lbl = dim_labels[dim_id].get(coord, (str(coord), str(coord)))
                    row.append(f"{code} ({lbl[:30]})" if lbl != code else code)
                lines.append("| " + " | ".join(row) + f" | {v} |")
                count += 1
                if count >= self.valves.DEFAULT_LIMIT:
                    break
            return "\n".join(lines)
        except Exception as e:
            return f"Eurostat error: {e}"
