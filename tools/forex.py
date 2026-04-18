"""
title: Forex — Live Exchange Rates & Currency Conversion
author: local-ai-stack
description: Real-time foreign exchange rates, historical currency data, and multi-currency conversion via the free Frankfurter API (European Central Bank data). Covers 30+ currencies including USD, EUR, GBP, JPY, CHF, CAD, AUD, CNY. No API key required.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from datetime import datetime, timedelta
from typing import Callable, Any, Optional
from pydantic import BaseModel, Field

BASE = "https://api.frankfurter.app"

CURRENCY_NAMES = {
    "AUD": "Australian Dollar",
    "BGN": "Bulgarian Lev",
    "BRL": "Brazilian Real",
    "CAD": "Canadian Dollar",
    "CHF": "Swiss Franc",
    "CNY": "Chinese Yuan Renminbi",
    "CZK": "Czech Koruna",
    "DKK": "Danish Krone",
    "EUR": "Euro",
    "GBP": "British Pound Sterling",
    "HKD": "Hong Kong Dollar",
    "HUF": "Hungarian Forint",
    "IDR": "Indonesian Rupiah",
    "ILS": "Israeli New Shekel",
    "INR": "Indian Rupee",
    "ISK": "Icelandic Króna",
    "JPY": "Japanese Yen",
    "KRW": "South Korean Won",
    "MXN": "Mexican Peso",
    "MYR": "Malaysian Ringgit",
    "NOK": "Norwegian Krone",
    "NZD": "New Zealand Dollar",
    "PHP": "Philippine Peso",
    "PLN": "Polish Zloty",
    "RON": "Romanian Leu",
    "SEK": "Swedish Krona",
    "SGD": "Singapore Dollar",
    "THB": "Thai Baht",
    "TRY": "Turkish Lira",
    "USD": "US Dollar",
    "ZAR": "South African Rand",
}


class Tools:
    class Valves(BaseModel):
        DEFAULT_BASE: str = Field(default="USD", description="Default base currency for rates")

    def __init__(self):
        self.valves = self.Valves()

    async def convert_currency(
        self,
        amount: float,
        from_currency: str,
        to_currency: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Convert an amount from one currency to another using live ECB exchange rates.
        :param amount: Amount to convert (e.g. 1000)
        :param from_currency: Source currency code (e.g. 'USD', 'EUR', 'GBP', 'JPY')
        :param to_currency: Target currency code (e.g. 'EUR', 'CHF', 'CNY') — or 'ALL' for all currencies
        :return: Converted amount with current exchange rate and timestamp
        """
        from_currency = from_currency.upper().strip()
        to_currency = to_currency.upper().strip()

        params = {"amount": amount, "from": from_currency}
        if to_currency != "ALL":
            params["to"] = to_currency

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{BASE}/latest", params=params)
                if resp.status_code == 422:
                    return f"Invalid currency code. Use 3-letter codes like USD, EUR, GBP. Run `list_currencies()` to see all supported currencies."
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            return f"Currency conversion error: {str(e)}"

        date = data.get("date", "")
        rates = data.get("rates", {})
        base = data.get("base", from_currency)

        from_name = CURRENCY_NAMES.get(from_currency, from_currency)

        if to_currency == "ALL":
            lines = [f"## Currency Conversion: {amount:,.2f} {from_currency} ({from_name})\n"]
            lines.append(f"**Rate Date:** {date} (European Central Bank)\n")
            lines.append("| Currency | Name | Rate | Converted Amount |")
            lines.append("|----------|------|------|-----------------|")
            for code, rate in sorted(rates.items()):
                name = CURRENCY_NAMES.get(code, "")
                converted = amount * rate
                lines.append(f"| **{code}** | {name} | {rate:.4f} | **{converted:,.2f}** |")
            return "\n".join(lines)

        if to_currency not in rates:
            return f"Currency '{to_currency}' not found. Run `list_currencies()` to see supported codes."

        rate = rates[to_currency]
        converted = amount * rate
        to_name = CURRENCY_NAMES.get(to_currency, to_currency)

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        return (
            f"## Currency Conversion\n\n"
            f"**{amount:,.2f} {from_currency}** ({from_name})\n"
            f"= **{converted:,.4f} {to_currency}** ({to_name})\n\n"
            f"Rate: 1 {from_currency} = {rate:.6f} {to_currency}\n"
            f"Inverse: 1 {to_currency} = {1/rate:.6f} {from_currency}\n"
            f"Data date: {date} (European Central Bank)"
        )

    async def get_live_rates(
        self,
        base: str = "",
        currencies: str = "",
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get current exchange rates for a base currency against major world currencies.
        :param base: Base currency (e.g. 'USD', 'EUR', 'GBP') — defaults to USD
        :param currencies: Comma-separated list of target currencies (e.g. 'EUR,GBP,JPY,CHF') — blank for all majors
        :return: Live exchange rates table
        """
        base = (base or self.valves.DEFAULT_BASE).upper().strip()

        MAJORS = ["EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "CNY", "HKD", "SGD", "NOK", "SEK", "DKK", "NZD", "MXN", "BRL", "INR", "KRW", "ZAR", "TRY"]

        params = {"from": base}
        if currencies:
            target_list = [c.strip().upper() for c in currencies.split(",") if c.strip()]
            target_list = [c for c in target_list if c != base]
            params["to"] = ",".join(target_list)

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{BASE}/latest", params=params)
                if resp.status_code == 422:
                    return f"Invalid currency code '{base}'. Use 3-letter codes like USD, EUR, GBP."
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            return f"Rate fetch error: {str(e)}"

        date = data.get("date", "")
        rates = data.get("rates", {})
        base_name = CURRENCY_NAMES.get(base, base)

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        lines = [f"## {base} ({base_name}) Exchange Rates\n"]
        lines.append(f"**Date:** {date} | Source: European Central Bank\n")
        lines.append("| Currency | Name | Rate | 1 Unit in {base} |".format(base=base))
        lines.append("|----------|------|------|-------------------|")

        # Show requested currencies or all
        display_rates = {}
        if currencies:
            display_rates = rates
        else:
            for code in MAJORS:
                if code in rates and code != base:
                    display_rates[code] = rates[code]

        for code, rate in sorted(display_rates.items()):
            name = CURRENCY_NAMES.get(code, "")
            inverse = 1 / rate if rate else 0
            lines.append(f"| **{code}** | {name} | {rate:.4f} | {inverse:.4f} |")

        return "\n".join(lines)

    async def get_historical_rate(
        self,
        from_currency: str,
        to_currency: str,
        start_date: str = "",
        end_date: str = "",
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get historical exchange rate data between two currencies over a date range.
        :param from_currency: Base currency (e.g. 'USD')
        :param to_currency: Target currency (e.g. 'EUR')
        :param start_date: Start date YYYY-MM-DD (default: 3 months ago)
        :param end_date: End date YYYY-MM-DD (default: today)
        :return: Historical rates table with min/max/average over the period
        """
        from_currency = from_currency.upper().strip()
        to_currency = to_currency.upper().strip()

        if not end_date:
            end_date = datetime.now().strftime("%Y-%m-%d")
        if not start_date:
            start_date = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Fetching {from_currency}/{to_currency} history...", "done": False}})

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{BASE}/{start_date}..{end_date}",
                    params={"from": from_currency, "to": to_currency},
                )
                if resp.status_code == 422:
                    return "Invalid currency code or date format. Use YYYY-MM-DD."
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            return f"Historical rate error: {str(e)}"

        rates_by_date = data.get("rates", {})
        if not rates_by_date:
            return f"No historical data found for {from_currency}/{to_currency} in that period."

        all_rates = []
        rows = []
        for date in sorted(rates_by_date.keys()):
            rate = rates_by_date[date].get(to_currency)
            if rate is not None:
                all_rates.append(rate)
                rows.append((date, rate))

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        lines = [f"## {from_currency}/{to_currency} Historical Rates\n"]
        lines.append(f"Period: {start_date} to {end_date} | Source: European Central Bank\n")

        if all_rates:
            avg = sum(all_rates) / len(all_rates)
            first = rows[0][1]
            last = rows[-1][1]
            change = (last - first) / first * 100
            direction = "▲" if change >= 0 else "▼"
            lines.append(f"**Period Avg:** {avg:.4f} | **Change:** {change:+.2f}% {direction} | **Range:** {min(all_rates):.4f} – {max(all_rates):.4f}\n")

        # Show sampled rows (max 30)
        sample = rows[::max(1, len(rows)//30)]
        lines.append("| Date | Rate |")
        lines.append("|------|------|")
        for date, rate in sample:
            lines.append(f"| {date} | {rate:.4f} |")

        return "\n".join(lines)

    def list_currencies(self, __user__: Optional[dict] = None) -> str:
        """
        List all supported currency codes and their full names.
        :return: Table of 3-letter currency codes and country/currency names
        """
        lines = ["## Supported Currencies (Frankfurter / ECB)\n"]
        lines.append("| Code | Currency Name |")
        lines.append("|------|--------------|")
        for code, name in sorted(CURRENCY_NAMES.items()):
            lines.append(f"| **{code}** | {name} |")
        lines.append("\nAll rates are from the European Central Bank (ECB) and updated on business days.")
        return "\n".join(lines)
