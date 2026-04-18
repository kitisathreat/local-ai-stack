"""
title: World Bank — Global Economic & Development Data
author: local-ai-stack
description: Access World Bank open data for 200+ countries. Get GDP, inflation, population, poverty, education, health, trade, and development indicators. Compare countries and track progress on Sustainable Development Goals. No API key required.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from typing import Callable, Any, Optional
from pydantic import BaseModel, Field

BASE = "https://api.worldbank.org/v2"

COMMON_INDICATORS = {
    # Economic
    "gdp": ("NY.GDP.MKTP.CD", "GDP (current US$)"),
    "gdp_per_capita": ("NY.GDP.PCAP.CD", "GDP per capita (current US$)"),
    "gdp_growth": ("NY.GDP.MKTP.KD.ZG", "GDP growth (annual %)"),
    "gni_per_capita": ("NY.GNP.PCAP.CD", "GNI per capita, Atlas method"),
    "inflation": ("FP.CPI.TOTL.ZG", "Inflation, consumer prices (annual %)"),
    "unemployment": ("SL.UEM.TOTL.ZS", "Unemployment, total (% of labor force)"),
    "trade_pct_gdp": ("NE.TRD.GNFS.ZS", "Trade (% of GDP)"),
    "exports": ("NE.EXP.GNFS.CD", "Exports of goods and services (current US$)"),
    "imports": ("NE.IMP.GNFS.CD", "Imports of goods and services (current US$)"),
    "fdi": ("BX.KLT.DINV.CD.WD", "Foreign direct investment, net inflows (BoP, current US$)"),
    "current_account": ("BN.CAB.XOKA.CD", "Current account balance (BoP, current US$)"),
    "debt_pct_gdp": ("GC.DOD.TOTL.GD.ZS", "Central government debt, total (% of GDP)"),
    "tax_revenue": ("GC.TAX.TOTL.GD.ZS", "Tax revenue (% of GDP)"),
    # Demographic
    "population": ("SP.POP.TOTL", "Population, total"),
    "population_growth": ("SP.POP.GROW", "Population growth (annual %)"),
    "urban_population_pct": ("SP.URB.TOTL.IN.ZS", "Urban population (% of total)"),
    "life_expectancy": ("SP.DYN.LE00.IN", "Life expectancy at birth, total (years)"),
    "fertility_rate": ("SP.DYN.TFRT.IN", "Fertility rate, total (births per woman)"),
    "infant_mortality": ("SP.DYN.IMRT.IN", "Mortality rate, infant (per 1,000 live births)"),
    # Education
    "literacy_rate": ("SE.ADT.LITR.ZS", "Literacy rate, adult total (% of people 15+)"),
    "school_enrollment": ("SE.SEC.ENRR", "School enrollment, secondary (% gross)"),
    "education_pct_gdp": ("SE.XPD.TOTL.GD.ZS", "Government expenditure on education, total (% of GDP)"),
    # Health
    "health_pct_gdp": ("SH.XPD.CHEX.GD.ZS", "Current health expenditure (% of GDP)"),
    "physicians": ("SH.MED.PHYS.ZS", "Physicians (per 1,000 people)"),
    "hiv_prevalence": ("SH.DYN.AIDS.ZS", "Prevalence of HIV, total (% of population 15-49)"),
    # Poverty & Inequality
    "poverty_rate": ("SI.POV.DDAY", "Poverty headcount ratio at $2.15/day (2017 PPP)"),
    "gini": ("SI.POV.GINI", "Gini index (World Bank estimate)"),
    "income_share_top10": ("SI.DST.10TH.10", "Income share held by highest 10%"),
    # Infrastructure & Environment
    "electricity_access": ("EG.ELC.ACCS.ZS", "Access to electricity (% of population)"),
    "internet_users": ("IT.NET.USER.ZS", "Individuals using the Internet (% of population)"),
    "co2_emissions": ("EN.ATM.CO2E.PC", "CO2 emissions (metric tons per capita)"),
    "forest_pct": ("AG.LND.FRST.ZS", "Forest area (% of land area)"),
    "renewable_energy": ("EG.FEC.RNEW.ZS", "Renewable energy consumption (% of total final energy)"),
}

REGION_CODES = {
    "world": "WLD",
    "east_asia": "EAS",
    "europe": "ECS",
    "latin_america": "LCN",
    "middle_east": "MEA",
    "north_america": "NAC",
    "south_asia": "SAS",
    "sub_saharan_africa": "SSF",
    "high_income": "HIC",
    "middle_income": "MIC",
    "low_income": "LIC",
    "oecd": "OED",
}


class Tools:
    class Valves(BaseModel):
        DEFAULT_YEARS: int = Field(default=10, description="Default number of years of historical data to retrieve")

    def __init__(self):
        self.valves = self.Valves()

    async def get_indicator(
        self,
        country: str,
        indicator: str,
        years: int = 0,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get a World Bank economic or development indicator for a country or region.
        :param country: Country name or ISO2 code (e.g. 'United States', 'US', 'Germany', 'CN', 'India', 'world')
        :param indicator: Indicator name (e.g. 'gdp', 'inflation', 'population', 'gini', 'life_expectancy') or World Bank indicator code (e.g. 'NY.GDP.MKTP.CD')
        :param years: Number of years of history (default 10)
        :return: Historical data table with trend
        """
        years = years or self.valves.DEFAULT_YEARS

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Fetching World Bank data for {country}...", "done": False}})

        # Resolve indicator
        ind_code = indicator
        ind_name = indicator
        if indicator.lower() in COMMON_INDICATORS:
            ind_code, ind_name = COMMON_INDICATORS[indicator.lower()]

        # Resolve country
        country_code = await self._resolve_country(country)
        if not country_code:
            return f"Country '{country}' not found. Use ISO2 codes (US, DE, CN) or country names."

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{BASE}/country/{country_code}/indicator/{ind_code}",
                    params={"format": "json", "per_page": years, "mrv": years},
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            return f"World Bank API error: {str(e)}"

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        if not data or len(data) < 2:
            return f"No data returned for indicator '{ind_code}' in country '{country}'."

        meta = data[0]
        records = data[1]

        if not records:
            return f"No data available for '{ind_name}' in {country}."

        country_name = records[0].get("country", {}).get("value", country)

        lines = [f"## {country_name} — {ind_name}\n"]
        lines.append(f"**Indicator:** `{ind_code}` | **Source:** World Bank\n")

        lines.append("| Year | Value |")
        lines.append("|------|-------|")

        valid = [(r["date"], r["value"]) for r in records if r.get("value") is not None]
        valid.sort(key=lambda x: x[0])

        for date, value in valid:
            try:
                fval = float(value)
                if abs(fval) >= 1e12:
                    formatted = f"${fval/1e12:.2f}T"
                elif abs(fval) >= 1e9:
                    formatted = f"${fval/1e9:.2f}B"
                elif abs(fval) >= 1e6:
                    formatted = f"${fval/1e6:.2f}M"
                elif abs(fval) >= 1000:
                    formatted = f"{fval:,.1f}"
                else:
                    formatted = f"{fval:.2f}"
            except (ValueError, TypeError):
                formatted = str(value)
            lines.append(f"| {date} | {formatted} |")

        if len(valid) >= 2:
            first_val = float(valid[0][1])
            last_val = float(valid[-1][1])
            if first_val != 0:
                change_pct = (last_val - first_val) / abs(first_val) * 100
                direction = "▲" if change_pct >= 0 else "▼"
                lines.append(f"\n**{valid[0][0]}→{valid[-1][0]} Change:** {change_pct:+.1f}% {direction}")

        return "\n".join(lines)

    async def compare_countries(
        self,
        countries: str,
        indicator: str,
        year: str = "2022",
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Compare an economic or development indicator across multiple countries side-by-side.
        :param countries: Comma-separated country names or codes (e.g. 'US,China,Germany,Japan,India')
        :param indicator: Indicator to compare (e.g. 'gdp_per_capita', 'gini', 'life_expectancy', 'co2_emissions')
        :param year: Year to compare (e.g. '2022', '2021', '2019')
        :return: Ranked table of all countries for the indicator
        """
        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Comparing countries on {indicator}...", "done": False}})

        country_list = [c.strip() for c in countries.split(",")]

        # Resolve indicator
        ind_code = indicator
        ind_name = indicator
        if indicator.lower() in COMMON_INDICATORS:
            ind_code, ind_name = COMMON_INDICATORS[indicator.lower()]

        results = []
        async with httpx.AsyncClient(timeout=20) as client:
            for country in country_list:
                code = await self._resolve_country(country)
                if not code:
                    continue
                try:
                    resp = await client.get(
                        f"{BASE}/country/{code}/indicator/{ind_code}",
                        params={"format": "json", "mrv": 5},
                    )
                    data = resp.json()
                    if data and len(data) >= 2 and data[1]:
                        records = [r for r in data[1] if r.get("value") is not None]
                        if records:
                            records.sort(key=lambda x: x["date"], reverse=True)
                            country_name = records[0].get("country", {}).get("value", country)
                            val = records[0]["value"]
                            val_year = records[0]["date"]
                            results.append((country_name, val, val_year))
                except Exception:
                    pass

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        if not results:
            return f"No data found for indicator '{indicator}' for the specified countries."

        results.sort(key=lambda x: (x[1] if x[1] is not None else float('-inf')), reverse=True)

        lines = [f"## Country Comparison: {ind_name}\n"]
        lines.append(f"**Indicator:** `{ind_code}` | Most recent available data\n")
        lines.append("| Rank | Country | Value | Year |")
        lines.append("|------|---------|-------|------|")
        for rank, (name, val, yr) in enumerate(results, 1):
            try:
                fval = float(val)
                if abs(fval) >= 1e12:
                    formatted = f"{fval/1e12:.2f}T"
                elif abs(fval) >= 1e9:
                    formatted = f"{fval/1e9:.2f}B"
                elif abs(fval) >= 1e6:
                    formatted = f"{fval/1e6:.1f}M"
                else:
                    formatted = f"{fval:,.2f}"
            except Exception:
                formatted = str(val)
            lines.append(f"| {rank} | **{name}** | {formatted} | {yr} |")

        return "\n".join(lines)

    async def get_country_profile(
        self,
        country: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get a comprehensive economic profile for a country covering all major indicators.
        :param country: Country name or ISO2 code (e.g. 'Brazil', 'JP', 'South Africa')
        :return: Multi-indicator snapshot covering economy, demographics, health, and education
        """
        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Building profile for {country}...", "done": False}})

        country_code = await self._resolve_country(country)
        if not country_code:
            return f"Country '{country}' not found."

        profile_indicators = [
            ("gdp", "GDP"),
            ("gdp_per_capita", "GDP Per Capita"),
            ("gdp_growth", "GDP Growth"),
            ("inflation", "Inflation"),
            ("unemployment", "Unemployment"),
            ("population", "Population"),
            ("life_expectancy", "Life Expectancy"),
            ("internet_users", "Internet Users"),
            ("co2_emissions", "CO2 Emissions (per capita)"),
            ("gini", "Gini Coefficient"),
        ]

        country_name = country
        rows = []

        async with httpx.AsyncClient(timeout=30) as client:
            for alias, label in profile_indicators:
                ind_code, _ = COMMON_INDICATORS[alias]
                try:
                    resp = await client.get(
                        f"{BASE}/country/{country_code}/indicator/{ind_code}",
                        params={"format": "json", "mrv": 5},
                    )
                    data = resp.json()
                    if data and len(data) >= 2 and data[1]:
                        records = [r for r in data[1] if r.get("value") is not None]
                        if records:
                            records.sort(key=lambda x: x["date"], reverse=True)
                            country_name = records[0].get("country", {}).get("value", country)
                            val = records[0]["value"]
                            yr = records[0]["date"]
                            try:
                                fval = float(val)
                                if abs(fval) >= 1e12:
                                    formatted = f"${fval/1e12:.2f}T"
                                elif abs(fval) >= 1e9:
                                    formatted = f"${fval/1e9:.2f}B"
                                elif abs(fval) >= 1e6:
                                    formatted = f"${fval/1e6:.1f}M"
                                elif label in ("Inflation", "GDP Growth", "Unemployment", "Internet Users", "Gini Coefficient"):
                                    formatted = f"{fval:.1f}%"
                                elif label == "Life Expectancy":
                                    formatted = f"{fval:.1f} years"
                                else:
                                    formatted = f"{fval:,.2f}"
                            except Exception:
                                formatted = str(val)
                            rows.append((label, formatted, yr))
                except Exception:
                    pass

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        lines = [f"## {country_name} — Economic Profile\n"]
        lines.append("| Indicator | Value | Year |")
        lines.append("|-----------|-------|------|")
        for label, val, yr in rows:
            lines.append(f"| **{label}** | {val} | {yr} |")
        lines.append("\nSource: World Bank Open Data (data.worldbank.org)")

        return "\n".join(lines)

    async def _resolve_country(self, country: str) -> str:
        if country.lower() in REGION_CODES:
            return REGION_CODES[country.lower()]
        if len(country) == 2:
            return country.upper()
        # Common aliases
        aliases = {
            "united states": "US", "usa": "US", "america": "US",
            "united kingdom": "GB", "uk": "GB", "britain": "GB",
            "china": "CN", "germany": "DE", "france": "FR",
            "japan": "JP", "india": "IN", "brazil": "BR",
            "canada": "CA", "australia": "AU", "south korea": "KR",
            "russia": "RU", "mexico": "MX", "italy": "IT",
            "spain": "ES", "netherlands": "NL", "switzerland": "CH",
            "saudi arabia": "SA", "turkey": "TR", "south africa": "ZA",
            "indonesia": "ID", "argentina": "AR", "nigeria": "NG",
            "world": "WLD",
        }
        return aliases.get(country.lower().strip(), "")

    def list_indicators(self, __user__: Optional[dict] = None) -> str:
        """
        List all available World Bank indicator shortcuts by category.
        :return: Table of indicator names, codes, and descriptions
        """
        categories = {
            "Economic": ["gdp", "gdp_per_capita", "gdp_growth", "gni_per_capita", "inflation", "unemployment", "trade_pct_gdp", "exports", "imports", "fdi", "debt_pct_gdp", "tax_revenue"],
            "Demographic": ["population", "population_growth", "urban_population_pct", "life_expectancy", "fertility_rate", "infant_mortality"],
            "Education": ["literacy_rate", "school_enrollment", "education_pct_gdp"],
            "Health": ["health_pct_gdp", "physicians", "hiv_prevalence"],
            "Poverty & Inequality": ["poverty_rate", "gini", "income_share_top10"],
            "Infrastructure & Environment": ["electricity_access", "internet_users", "co2_emissions", "forest_pct", "renewable_energy"],
        }

        lines = ["## World Bank Indicators\n"]
        for cat, keys in categories.items():
            lines.append(f"### {cat}")
            lines.append("| Shortcut | World Bank Code | Description |")
            lines.append("|----------|----------------|-------------|")
            for key in keys:
                code, desc = COMMON_INDICATORS[key]
                lines.append(f"| `{key}` | `{code}` | {desc} |")
            lines.append("")

        return "\n".join(lines)
