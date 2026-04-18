"""
title: FRED — Federal Reserve Economic Data
author: local-ai-stack
description: Access Federal Reserve Economic Data (FRED) from the St. Louis Fed. Get GDP, CPI, unemployment, interest rates, money supply, treasury yields, and 800,000+ economic series. Free API key required (30-second signup at fred.stlouisfed.org/docs/api/api_key.html).
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import os
import httpx
import json
from datetime import datetime, timedelta
from typing import Callable, Any, Optional
from pydantic import BaseModel, Field

COMMON_SERIES = {
    # GDP & Growth
    "gdp": ("GDPC1", "Real GDP (Chained 2017 Dollars, Quarterly)"),
    "nominal_gdp": ("GDP", "Nominal GDP (Current Dollars, Quarterly)"),
    "gdp_growth": ("A191RL1Q225SBEA", "Real GDP Growth Rate (Annual Rate, Quarterly)"),
    # Inflation
    "cpi": ("CPIAUCSL", "CPI All Urban Consumers (Monthly)"),
    "core_cpi": ("CPILFESL", "Core CPI ex Food & Energy (Monthly)"),
    "pce": ("PCEPI", "PCE Price Index (Monthly)"),
    "core_pce": ("PCEPILFE", "Core PCE Price Index — Fed's Preferred Gauge (Monthly)"),
    "ppi": ("PPIACO", "Producer Price Index All Commodities (Monthly)"),
    # Employment
    "unemployment": ("UNRATE", "Unemployment Rate (Monthly)"),
    "nonfarm_payrolls": ("PAYEMS", "Total Nonfarm Payrolls (Monthly, Thousands)"),
    "labor_participation": ("CIVPART", "Labor Force Participation Rate (Monthly)"),
    "jolts": ("JTSJOL", "Job Openings (Monthly, Thousands)"),
    # Interest Rates
    "fed_funds": ("FEDFUNDS", "Federal Funds Effective Rate (Monthly)"),
    "fed_funds_target": ("DFEDTARU", "Fed Funds Target Rate Upper Bound (Daily)"),
    "prime_rate": ("DPRIME", "Bank Prime Loan Rate (Daily)"),
    "sofr": ("SOFR", "Secured Overnight Financing Rate (Daily)"),
    # Treasury Yields
    "t10y": ("GS10", "10-Year Treasury Constant Maturity Rate (Monthly)"),
    "t2y": ("GS2", "2-Year Treasury Constant Maturity Rate (Monthly)"),
    "t1y": ("GS1", "1-Year Treasury Constant Maturity Rate (Monthly)"),
    "t30y": ("GS30", "30-Year Treasury Constant Maturity Rate (Monthly)"),
    "yield_curve": ("T10Y2Y", "10-Year minus 2-Year Treasury Spread (Daily)"),
    "tips10y": ("DFII10", "10-Year TIPS Real Yield (Daily)"),
    # Money Supply
    "m2": ("M2SL", "M2 Money Stock (Monthly, Billions)"),
    "m1": ("M1SL", "M1 Money Stock (Monthly, Billions)"),
    # Financial Conditions
    "vix": ("VIXCLS", "CBOE Volatility Index — VIX (Daily)"),
    "credit_spread": ("BAMLC0A0CM", "Investment Grade Corporate Bond Spread (Daily)"),
    "hy_spread": ("BAMLH0A0HYM2", "High Yield Bond Spread (Daily)"),
    "mortgage_30y": ("MORTGAGE30US", "30-Year Fixed Mortgage Rate (Weekly)"),
    # Housing
    "housing_starts": ("HOUST", "Housing Starts (Monthly, Thousands)"),
    "case_shiller": ("CSUSHPINSA", "Case-Shiller US Home Price Index (Monthly)"),
    # Trade & Production
    "trade_balance": ("BOPGSTB", "US Trade Balance (Monthly, Millions)"),
    "industrial_production": ("INDPRO", "Industrial Production Index (Monthly)"),
    "retail_sales": ("RSAFS", "Advance Retail Sales (Monthly, Millions)"),
    # Consumer
    "consumer_confidence": ("UMCSENT", "University of Michigan Consumer Sentiment (Monthly)"),
    "personal_income": ("PI", "Personal Income (Monthly, Billions)"),
    "personal_savings": ("PSAVERT", "Personal Savings Rate (Monthly)"),
}


class Tools:
    class Valves(BaseModel):
        FRED_API_KEY: str = Field(
            default_factory=lambda: os.environ.get("FRED_API_KEY", ""),
            description="FRED API key — free at https://fred.stlouisfed.org/docs/api/api_key.html",
        )

    def __init__(self):
        self.valves = self.Valves()

    def _check_key(self) -> Optional[str]:
        if not self.valves.FRED_API_KEY:
            return (
                "FRED API key required.\n"
                "1. Sign up free at https://fred.stlouisfed.org/docs/api/api_key.html\n"
                "2. Add key in Open WebUI > Tools > FRED > FRED_API_KEY"
            )
        return None

    async def get_economic_indicator(
        self,
        indicator: str,
        periods: int = 12,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get the latest values for a key economic indicator from FRED.
        :param indicator: Indicator name (e.g. 'gdp', 'cpi', 'unemployment', 'fed_funds', 't10y', 'vix', 'm2', 'mortgage_30y') or a FRED series ID (e.g. 'GDPC1')
        :param periods: Number of recent observations to return (default 12)
        :return: Recent values with dates and brief context
        """
        err = self._check_key()
        if err:
            return err

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Fetching {indicator} from FRED...", "done": False}})

        # Resolve alias
        series_id = indicator.upper()
        series_desc = ""
        if indicator.lower() in COMMON_SERIES:
            series_id, series_desc = COMMON_SERIES[indicator.lower()]

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                # Get series info
                info_resp = await client.get(
                    "https://api.stlouisfed.org/fred/series",
                    params={"series_id": series_id, "api_key": self.valves.FRED_API_KEY, "file_type": "json"},
                )
                info_resp.raise_for_status()
                info = info_resp.json().get("seriess", [{}])[0]

                # Get observations
                obs_resp = await client.get(
                    "https://api.stlouisfed.org/fred/series/observations",
                    params={
                        "series_id": series_id,
                        "api_key": self.valves.FRED_API_KEY,
                        "file_type": "json",
                        "sort_order": "desc",
                        "limit": periods,
                    },
                )
                obs_resp.raise_for_status()
                observations = obs_resp.json().get("observations", [])

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 400:
                return f"Series '{series_id}' not found. Try `list_indicators` for common series IDs."
            return f"FRED API error: HTTP {e.response.status_code}"
        except Exception as e:
            return f"FRED fetch error: {str(e)}"

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        title = info.get("title", series_id)
        units = info.get("units_short", info.get("units", ""))
        freq = info.get("frequency_short", "")
        last_updated = info.get("last_updated", "")[:10]

        lines = [f"## FRED: {title}\n"]
        lines.append(f"**Series:** {series_id} | **Frequency:** {freq} | **Units:** {units} | **Updated:** {last_updated}\n")

        if series_desc:
            lines.append(f"_{series_desc}_\n")

        lines.append("| Date | Value |")
        lines.append("|------|-------|")
        for obs in observations:
            date = obs.get("date", "")
            val = obs.get("value", ".")
            if val == ".":
                val = "N/A"
            else:
                try:
                    val = f"{float(val):,.3f}"
                except ValueError:
                    pass
            lines.append(f"| {date} | {val} {units} |")

        # Compute change
        valid = [obs for obs in observations if obs.get("value", ".") != "."]
        if len(valid) >= 2:
            latest = float(valid[0]["value"])
            prior = float(valid[1]["value"])
            change = latest - prior
            pct = (change / abs(prior) * 100) if prior != 0 else 0
            direction = "▲" if change > 0 else "▼"
            lines.append(f"\n**Latest:** {latest:,.3f} {units} ({direction} {abs(change):.3f}, {pct:+.2f}% vs prior period)")

        return "\n".join(lines)

    async def search_fred_series(
        self,
        query: str,
        limit: int = 10,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search for any economic data series available in the FRED database (800,000+ series).
        :param query: Search terms (e.g. "housing prices", "bank lending", "consumer debt", "S&P 500")
        :param limit: Number of results to show (max 20)
        :return: Matching series IDs, titles, frequency, and units
        """
        err = self._check_key()
        if err:
            return err

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Searching FRED for '{query}'...", "done": False}})

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    "https://api.stlouisfed.org/fred/series/search",
                    params={
                        "search_text": query,
                        "api_key": self.valves.FRED_API_KEY,
                        "file_type": "json",
                        "limit": min(limit, 20),
                        "order_by": "popularity",
                        "sort_order": "desc",
                    },
                )
                resp.raise_for_status()
                data = resp.json()

        except Exception as e:
            return f"FRED search error: {str(e)}"

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        series_list = data.get("seriess", [])
        if not series_list:
            return f"No FRED series found for: {query}"

        lines = [f"## FRED Search: '{query}' — {len(series_list)} results\n"]
        lines.append("| Series ID | Title | Freq | Units | Last Updated |")
        lines.append("|-----------|-------|------|-------|--------------|")
        for s in series_list:
            sid = s.get("id", "")
            title = s.get("title", "")[:60]
            freq = s.get("frequency_short", "")
            units = s.get("units_short", "")[:20]
            updated = s.get("last_updated", "")[:10]
            lines.append(f"| `{sid}` | {title} | {freq} | {units} | {updated} |")

        lines.append(f"\nUse `get_economic_indicator('{series_list[0].get('id', '')}')` to fetch data for any series.")
        return "\n".join(lines)

    async def get_dashboard(
        self,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get a macro-economic dashboard with the latest values for key US economic indicators.
        :return: Current snapshot of GDP growth, inflation, unemployment, rates, and yields
        """
        err = self._check_key()
        if err:
            return err

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Fetching macro dashboard from FRED...", "done": False}})

        dashboard_series = [
            ("gdp_growth", "GDP Growth"),
            ("core_pce", "Core PCE Inflation"),
            ("cpi", "CPI"),
            ("unemployment", "Unemployment"),
            ("fed_funds", "Fed Funds Rate"),
            ("t10y", "10Y Treasury"),
            ("t2y", "2Y Treasury"),
            ("yield_curve", "Yield Curve (10Y-2Y)"),
            ("mortgage_30y", "30Y Mortgage"),
            ("m2", "M2 Money Supply"),
            ("vix", "VIX"),
        ]

        lines = [f"## US Macro Dashboard — {datetime.now().strftime('%Y-%m-%d')}\n"]
        lines.append("| Indicator | Latest Value | Series |")
        lines.append("|-----------|-------------|--------|")

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                for alias, label in dashboard_series:
                    series_id, _ = COMMON_SERIES[alias]
                    try:
                        resp = await client.get(
                            "https://api.stlouisfed.org/fred/series/observations",
                            params={
                                "series_id": series_id,
                                "api_key": self.valves.FRED_API_KEY,
                                "file_type": "json",
                                "sort_order": "desc",
                                "limit": 2,
                            },
                        )
                        obs = resp.json().get("observations", [])
                        valid = [o for o in obs if o.get("value", ".") != "."]
                        if valid:
                            val = float(valid[0]["value"])
                            date = valid[0]["date"]
                            if len(valid) >= 2:
                                prior = float(valid[1]["value"])
                                arrow = "▲" if val > prior else "▼"
                            else:
                                arrow = ""
                            lines.append(f"| **{label}** | {val:,.2f} {arrow} ({date}) | {series_id} |")
                        else:
                            lines.append(f"| **{label}** | N/A | {series_id} |")
                    except Exception:
                        lines.append(f"| **{label}** | Error | {series_id} |")

        except Exception as e:
            return f"Dashboard error: {str(e)}"

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        return "\n".join(lines)

    def list_indicators(self, __user__: Optional[dict] = None) -> str:
        """
        List all available shortcut names for common economic indicators.
        :return: Table of indicator names, FRED series IDs, and descriptions
        """
        lines = ["## FRED Common Indicators\n"]
        lines.append("| Shortcut | Series ID | Description |")
        lines.append("|----------|-----------|-------------|")
        for alias, (sid, desc) in COMMON_SERIES.items():
            lines.append(f"| `{alias}` | `{sid}` | {desc} |")
        lines.append("\nUse any shortcut or raw series ID with `get_economic_indicator()`.")
        return "\n".join(lines)
