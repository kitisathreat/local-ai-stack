"""
title: BEA — U.S. Bureau of Economic Analysis
author: local-ai-stack
description: Fetch US GDP, personal income, trade, regional accounts, and industry data from the Bureau of Economic Analysis. Free API key required (instant signup).
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import os
import httpx
from pydantic import BaseModel, Field
from typing import Optional


BASE = "https://apps.bea.gov/api/data"


class Tools:
    class Valves(BaseModel):
        API_KEY: str = Field(default_factory=lambda: os.environ.get("BEA_API_KEY", ""), description="BEA API key (free at https://apps.bea.gov/api/signup/)")

    def __init__(self):
        self.valves = self.Valves()

    async def list_datasets(self, __user__: Optional[dict] = None) -> str:
        """
        List BEA datasets (NIPA, Regional, ITA, IIP, etc.).
        :return: Dataset names and descriptions
        """
        if not self.valves.API_KEY:
            return "Set BEA API_KEY valve."
        params = {"UserID": self.valves.API_KEY, "method": "GetDatasetList", "ResultFormat": "JSON"}
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(BASE, params=params)
                r.raise_for_status()
                data = r.json()
            ds = data.get("BEAAPI", {}).get("Results", {}).get("Dataset", [])
            lines = ["## BEA Datasets\n", "| Name | Description |", "|---|---|"]
            for d in ds:
                lines.append(f"| `{d.get('DatasetName','')}` | {d.get('DatasetDescription','')} |")
            return "\n".join(lines)
        except Exception as e:
            return f"BEA error: {e}"

    async def nipa(
        self,
        table_name: str = "T10101",
        frequency: str = "Q",
        year: str = "LAST5",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Fetch a National Income & Product Accounts (NIPA) table.
        :param table_name: NIPA table ID (e.g. "T10101" = Percent Change From Preceding Period in Real GDP)
        :param frequency: "A" (annual), "Q" (quarterly), "M" (monthly)
        :param year: Year specification: "ALL", "LAST5", "LAST10", or comma list "2020,2021,2022"
        :return: Table of values per line description
        """
        if not self.valves.API_KEY:
            return "Set BEA API_KEY valve."
        params = {
            "UserID": self.valves.API_KEY, "method": "GetData", "DatasetName": "NIPA",
            "TableName": table_name, "Frequency": frequency, "Year": year,
            "ResultFormat": "JSON",
        }
        try:
            async with httpx.AsyncClient(timeout=45) as client:
                r = await client.get(BASE, params=params)
                r.raise_for_status()
                data = r.json()
            res = data.get("BEAAPI", {}).get("Results", {})
            rows = res.get("Data", [])
            if not rows:
                err = res.get("Error", {})
                return f"No BEA data: {err or 'empty response'}"
            lines = [f"## BEA NIPA {table_name} ({frequency}, {year})\n", "| Line | Description | Period | Value |", "|---|---|---|---|"]
            for row in rows[:80]:
                lines.append(
                    f"| {row.get('LineNumber','')} | {row.get('LineDescription','')[:60]} | {row.get('TimePeriod','')} | {row.get('DataValue','')} |"
                )
            return "\n".join(lines)
        except Exception as e:
            return f"BEA error: {e}"
