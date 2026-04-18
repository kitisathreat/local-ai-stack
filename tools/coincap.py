"""
title: CoinCap — Free Real-Time Crypto Market Data
author: local-ai-stack
description: Free, no-key real-time crypto asset prices, market caps, and historical candles via CoinCap 2.0. Complements the existing finance tool with broader coverage of altcoins and OHLC history.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from pydantic import BaseModel, Field
from typing import Optional


BASE = "https://api.coincap.io/v2"


class Tools:
    class Valves(BaseModel):
        LIMIT: int = Field(default=20, description="Max rows")

    def __init__(self):
        self.valves = self.Valves()

    async def top(self, limit: int = 20, __user__: Optional[dict] = None) -> str:
        """
        Top crypto assets by market cap.
        :param limit: Number of assets (default 20)
        :return: Ranked table with price, 24h %, market cap
        """
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(f"{BASE}/assets", params={"limit": min(limit, 100)})
                r.raise_for_status()
                rows = r.json().get("data", [])
            lines = ["## CoinCap Top Assets\n", "| # | Symbol | Name | Price | 24h % | Market cap |", "|---|---|---|---|---|---|"]
            for a in rows:
                rk = a.get("rank", "")
                sym = a.get("symbol", "")
                name = a.get("name", "")
                price = float(a.get("priceUsd", 0) or 0)
                chg = float(a.get("changePercent24Hr", 0) or 0)
                mcap = float(a.get("marketCapUsd", 0) or 0)
                lines.append(f"| {rk} | {sym} | {name} | ${price:,.4f} | {chg:+.2f}% | ${mcap:,.0f} |")
            return "\n".join(lines)
        except Exception as e:
            return f"CoinCap error: {e}"

    async def asset(self, asset_id: str, __user__: Optional[dict] = None) -> str:
        """
        Live price and stats for a single crypto asset.
        :param asset_id: CoinCap asset id (e.g. "bitcoin", "ethereum", "solana")
        :return: Price, supply, vol, market cap, dominance
        """
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(f"{BASE}/assets/{asset_id.lower()}")
                if r.status_code != 200:
                    return f"Asset not found: {asset_id}"
                a = r.json().get("data", {})
            return (
                f"## {a.get('name','')} ({a.get('symbol','')})\n"
                f"**Rank:** {a.get('rank','')}   **Price:** ${float(a.get('priceUsd', 0) or 0):,.4f}\n"
                f"**24h Δ:** {float(a.get('changePercent24Hr', 0) or 0):+.2f}%\n"
                f"**Market cap:** ${float(a.get('marketCapUsd', 0) or 0):,.0f}\n"
                f"**24h volume:** ${float(a.get('volumeUsd24Hr', 0) or 0):,.0f}\n"
                f"**Supply:** {float(a.get('supply', 0) or 0):,.0f} / max {float(a.get('maxSupply', 0) or 0):,.0f}"
            )
        except Exception as e:
            return f"CoinCap error: {e}"

    async def history(
        self,
        asset_id: str,
        interval: str = "d1",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Historical price points for a crypto asset.
        :param asset_id: CoinCap id (e.g. "bitcoin")
        :param interval: "m1", "m5", "m15", "m30", "h1", "h2", "h6", "h12", "d1"
        :return: Last ~30 points of (time, price)
        """
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(
                    f"{BASE}/assets/{asset_id.lower()}/history",
                    params={"interval": interval},
                )
                r.raise_for_status()
                data = r.json().get("data", [])
            if not data:
                return "No history returned."
            import datetime as dt
            lines = [f"## {asset_id} history ({interval}) — last 30 samples\n", "| Time | Price USD |", "|---|---|"]
            for p in data[-30:]:
                t = dt.datetime.utcfromtimestamp(p.get("time", 0) / 1000).strftime("%Y-%m-%d %H:%M")
                price = float(p.get("priceUsd", 0) or 0)
                lines.append(f"| {t} | ${price:,.4f} |")
            return "\n".join(lines)
        except Exception as e:
            return f"CoinCap error: {e}"
