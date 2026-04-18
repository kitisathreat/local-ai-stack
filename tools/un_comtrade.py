"""
title: UN Comtrade — Global Trade Statistics
author: local-ai-stack
description: Free international trade data from the United Nations Comtrade database. Query bilateral trade flows by country, commodity (HS), and year. Uses the free public tier (rate-limited, 100 queries per IP per hour).
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from pydantic import BaseModel, Field
from typing import Optional


BASE = "https://comtradeapi.un.org/public/v1/preview"


class Tools:
    class Valves(BaseModel):
        SUBKEY: str = Field(default="", description="Optional UN Comtrade subscription key (higher limits)")
        MAX_ROWS: int = Field(default=50, description="Max rows displayed")

    def __init__(self):
        self.valves = self.Valves()

    def _headers(self) -> dict:
        h = {"Accept": "application/json"}
        if self.valves.SUBKEY:
            h["Ocp-Apim-Subscription-Key"] = self.valves.SUBKEY
        return h

    async def trade(
        self,
        reporter: str = "842",
        partner: str = "0",
        period: str = "2023",
        flow: str = "M",
        commodity: str = "TOTAL",
        freq: str = "A",
        type_: str = "C",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Query UN Comtrade trade flows.
        :param reporter: Reporter country (M49 code, e.g. "842" USA, "156" China, "all")
        :param partner: Partner country (0 = World)
        :param period: Year(s), e.g. "2023" or "2020,2021,2022"
        :param flow: "M" imports, "X" exports, "RX" re-exports
        :param commodity: HS code (e.g. "TOTAL", "27" petroleum, "8703" passenger cars)
        :param freq: "A" annual, "M" monthly
        :param type_: "C" commodity, "S" service
        :return: Trade flows with reporter, partner, commodity, and value in USD
        """
        url = f"{BASE}/{type_}/{freq}/HS"
        params = {
            "reporterCode": reporter,
            "partnerCode": partner,
            "period": period,
            "flowCode": flow,
            "cmdCode": commodity,
        }
        try:
            async with httpx.AsyncClient(timeout=45) as client:
                r = await client.get(url, params=params, headers=self._headers())
                if r.status_code != 200:
                    return f"UN Comtrade HTTP {r.status_code}: {r.text[:300]}"
                data = r.json()
            rows = data.get("data", [])
            if not rows:
                return "No Comtrade rows — check reporter/partner/period combination."
            lines = [f"## UN Comtrade {flow} (reporter={reporter}, commodity={commodity})\n"]
            lines.append("| Year | Reporter | Partner | HS | USD |\n|---|---|---|---|---|")
            for r_ in rows[: self.valves.MAX_ROWS]:
                lines.append(
                    f"| {r_.get('period','')} | {r_.get('reporterDesc','')} | {r_.get('partnerDesc','')} | {r_.get('cmdCode','')} | {r_.get('primaryValue', 0):,.0f} |"
                )
            return "\n".join(lines)
        except Exception as e:
            return f"UN Comtrade error: {e}"
