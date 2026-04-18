"""
title: Alpha Vantage — Stocks, Forex, Crypto & Economic Data
author: local-ai-stack
description: Comprehensive financial market data via Alpha Vantage. Real-time and historical stock prices, forex rates, cryptocurrency data, technical indicators (50+), and US macroeconomic data (GDP, CPI, inflation, treasury yields). Free API key required at alphavantage.co (5 requests/min on free tier).
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import os
import httpx
from typing import Callable, Any, Optional
from pydantic import BaseModel, Field

BASE = "https://www.alphavantage.co/query"


class Tools:
    class Valves(BaseModel):
        ALPHA_VANTAGE_API_KEY: str = Field(
            default_factory=lambda: os.environ.get("ALPHA_VANTAGE_API_KEY", ""),
            description="Alpha Vantage API key — free at https://www.alphavantage.co/support/#api-key",
        )

    def __init__(self):
        self.valves = self.Valves()

    def _check_key(self) -> Optional[str]:
        if not self.valves.ALPHA_VANTAGE_API_KEY:
            return (
                "Alpha Vantage API key required.\n"
                "Get a free key at: https://www.alphavantage.co/support/#api-key\n"
                "Add it in Open WebUI > Tools > Alpha Vantage > ALPHA_VANTAGE_API_KEY"
            )
        return None

    async def get_stock_quote(
        self,
        symbol: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get a real-time stock quote with price, change, volume, and 52-week range.
        :param symbol: Stock ticker symbol (e.g. 'IBM', 'AAPL', 'TSLA', 'MSFT')
        :return: Current price, change, % change, volume, open/high/low, 52-week range
        """
        err = self._check_key()
        if err:
            return err

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Fetching {symbol} quote...", "done": False}})

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(BASE, params={
                    "function": "GLOBAL_QUOTE",
                    "symbol": symbol.upper(),
                    "apikey": self.valves.ALPHA_VANTAGE_API_KEY,
                })
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            return f"Alpha Vantage error: {str(e)}"

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        q = data.get("Global Quote", {})
        if not q:
            if "Note" in data:
                return f"Rate limit reached. Alpha Vantage free tier allows 5 requests/minute. Try again shortly."
            return f"No data found for '{symbol}'. Check the ticker symbol."

        price = q.get("05. price", "0")
        change = q.get("09. change", "0")
        pct = q.get("10. change percent", "0%").strip("%")
        volume = q.get("06. volume", "0")
        prev_close = q.get("08. previous close", "0")
        open_ = q.get("02. open", "0")
        high = q.get("03. high", "0")
        low = q.get("04. low", "0")
        latest_day = q.get("07. latest trading day", "")

        try:
            pct_f = float(pct)
            direction = "▲" if pct_f >= 0 else "▼"
        except ValueError:
            direction = ""

        lines = [f"## {symbol.upper()} — Real-Time Quote\n"]
        lines.append(f"**Price:** ${float(price):.2f} {direction} {change} ({pct}%)")
        lines.append(f"**As of:** {latest_day}\n")
        lines.append(f"| Metric | Value |")
        lines.append(f"|--------|-------|")
        lines.append(f"| Open | ${float(open_):.2f} |")
        lines.append(f"| High | ${float(high):.2f} |")
        lines.append(f"| Low | ${float(low):.2f} |")
        lines.append(f"| Prev. Close | ${float(prev_close):.2f} |")
        lines.append(f"| Volume | {int(volume):,} |")

        return "\n".join(lines)

    async def get_daily_prices(
        self,
        symbol: str,
        output_size: str = "compact",
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get daily OHLCV historical price data for a stock (adjusted for splits and dividends).
        :param symbol: Stock ticker symbol (e.g. 'AAPL', 'SPY', 'GOOGL')
        :param output_size: 'compact' for last 100 days, 'full' for 20+ years of daily data
        :return: Daily OHLCV table with the most recent 30 days shown
        """
        err = self._check_key()
        if err:
            return err

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Fetching {symbol} price history...", "done": False}})

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(BASE, params={
                    "function": "TIME_SERIES_DAILY_ADJUSTED",
                    "symbol": symbol.upper(),
                    "outputsize": output_size,
                    "apikey": self.valves.ALPHA_VANTAGE_API_KEY,
                })
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            return f"Alpha Vantage error: {str(e)}"

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        if "Note" in data:
            return "Rate limit reached. Free tier: 5 requests/minute. Please wait and try again."

        ts = data.get("Time Series (Daily)", {})
        meta = data.get("Meta Data", {})
        if not ts:
            return f"No price data found for '{symbol}'."

        dates = sorted(ts.keys(), reverse=True)[:30]

        lines = [f"## {symbol.upper()} — Daily Price History\n"]
        lines.append(f"**Last Refreshed:** {meta.get('3. Last Refreshed', '')} | Showing last {len(dates)} days\n")
        lines.append("| Date | Open | High | Low | Close (Adj.) | Volume |")
        lines.append("|------|------|------|-----|-------------|--------|")
        for d in dates:
            row = ts[d]
            lines.append(
                f"| {d} | ${float(row['1. open']):.2f} | ${float(row['2. high']):.2f} | "
                f"${float(row['3. low']):.2f} | ${float(row['5. adjusted close']):.2f} | "
                f"{int(row['6. volume']):,} |"
            )

        # Compute return
        if len(dates) >= 2:
            first = float(ts[dates[-1]]["5. adjusted close"])
            last = float(ts[dates[0]]["5. adjusted close"])
            ret = (last - first) / first * 100
            lines.append(f"\n**30-Day Return:** {ret:+.2f}%")

        return "\n".join(lines)

    async def get_technical_indicator(
        self,
        symbol: str,
        indicator: str = "RSI",
        interval: str = "daily",
        period: int = 14,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Calculate technical indicators from Alpha Vantage's 50+ indicator library.
        :param symbol: Stock ticker symbol
        :param indicator: Indicator name: RSI, MACD, BBANDS, SMA, EMA, STOCH, ADX, CCI, OBV, AROON, WILLR, MFI
        :param interval: Time interval: daily, weekly, monthly (or 1min, 5min, 15min, 30min, 60min for intraday)
        :param period: Lookback period (e.g. 14 for RSI-14, 20 for BBANDS-20)
        :return: Recent indicator values with dates
        """
        err = self._check_key()
        if err:
            return err

        indicator = indicator.upper()
        supported = ["RSI", "MACD", "BBANDS", "SMA", "EMA", "STOCH", "ADX", "CCI", "OBV", "AROON", "WILLR", "MFI", "ATR", "VWAP", "DEMA", "TEMA"]

        if indicator not in supported:
            return f"Indicator '{indicator}' not in supported list: {', '.join(supported)}"

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Calculating {indicator} for {symbol}...", "done": False}})

        params = {
            "function": indicator,
            "symbol": symbol.upper(),
            "interval": interval,
            "time_period": period,
            "series_type": "close",
            "apikey": self.valves.ALPHA_VANTAGE_API_KEY,
        }
        # Some indicators don't use time_period
        if indicator in ("OBV", "VWAP"):
            params.pop("time_period", None)
            params.pop("series_type", None)

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(BASE, params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            return f"Alpha Vantage error: {str(e)}"

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        if "Note" in data:
            return "Rate limit reached. Free tier: 5 requests/minute."

        # Find the data key
        ts_key = next((k for k in data.keys() if "Technical Analysis" in k), None)
        if not ts_key:
            return f"No {indicator} data for {symbol}. Check the symbol or try a different interval."

        ts = data[ts_key]
        dates = sorted(ts.keys(), reverse=True)[:20]

        lines = [f"## {indicator}({period}) — {symbol.upper()} ({interval})\n"]

        # Get field names from first record
        if dates:
            fields = list(ts[dates[0]].keys())
            lines.append("| Date | " + " | ".join(fields) + " |")
            lines.append("|------|" + "------|" * len(fields))
            for d in dates:
                row = ts[d]
                vals = " | ".join(f"{float(v):.4f}" for v in row.values())
                lines.append(f"| {d} | {vals} |")

        # Interpret RSI
        if indicator == "RSI" and dates:
            rsi = float(ts[dates[0]].get("RSI", 50))
            if rsi > 70:
                lines.append(f"\n**Signal:** 🔴 Overbought (RSI={rsi:.1f} > 70)")
            elif rsi < 30:
                lines.append(f"\n**Signal:** 🟢 Oversold (RSI={rsi:.1f} < 30)")
            else:
                lines.append(f"\n**Signal:** ⚪ Neutral (RSI={rsi:.1f})")

        return "\n".join(lines)

    async def get_economic_data(
        self,
        indicator: str = "REAL_GDP",
        interval: str = "quarterly",
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get US macroeconomic data from Alpha Vantage: GDP, CPI, inflation, unemployment, treasury yields, federal funds rate.
        :param indicator: Economic indicator: REAL_GDP, REAL_GDP_PER_CAPITA, TREASURY_YIELD, FEDERAL_FUNDS_RATE, CPI, INFLATION, RETAIL_SALES, DURABLES, UNEMPLOYMENT, NONFARM_PAYROLL
        :param interval: Frequency — quarterly or annual for GDP; monthly for others
        :return: Recent economic data values with dates
        """
        err = self._check_key()
        if err:
            return err

        indicator = indicator.upper()
        valid = {
            "REAL_GDP": "Real GDP (Billions USD)",
            "REAL_GDP_PER_CAPITA": "Real GDP Per Capita (USD)",
            "TREASURY_YIELD": "Treasury Yield",
            "FEDERAL_FUNDS_RATE": "Federal Funds Rate (%)",
            "CPI": "Consumer Price Index",
            "INFLATION": "Inflation Rate (%)",
            "RETAIL_SALES": "Retail Sales (Millions USD)",
            "DURABLES": "Durable Goods Orders (Millions USD)",
            "UNEMPLOYMENT": "Unemployment Rate (%)",
            "NONFARM_PAYROLL": "Nonfarm Payroll (Thousands)",
        }

        if indicator not in valid:
            return f"Indicator must be one of: {', '.join(valid.keys())}"

        params = {"function": indicator, "apikey": self.valves.ALPHA_VANTAGE_API_KEY}
        if indicator in ("REAL_GDP", "REAL_GDP_PER_CAPITA"):
            params["interval"] = interval
        elif indicator == "TREASURY_YIELD":
            params["interval"] = interval
            params["maturity"] = "10year"

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Fetching {indicator}...", "done": False}})

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(BASE, params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            return f"Alpha Vantage error: {str(e)}"

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        if "Note" in data:
            return "Rate limit reached. Free tier: 5 requests/minute."

        records = data.get("data", [])
        if not records:
            return f"No data returned for {indicator}."

        title = valid[indicator]
        unit = data.get("unit", "")

        lines = [f"## Alpha Vantage: {title}\n"]
        if unit:
            lines.append(f"**Unit:** {unit}\n")
        lines.append("| Date | Value |")
        lines.append("|------|-------|")
        for r in records[:20]:
            val = r.get("value", ".")
            date = r.get("date", "")
            try:
                formatted = f"{float(val):,.3f}"
            except (ValueError, TypeError):
                formatted = val
            lines.append(f"| {date} | {formatted} |")

        if len(records) >= 2:
            try:
                latest = float(records[0]["value"])
                prior = float(records[1]["value"])
                change = latest - prior
                pct = change / abs(prior) * 100 if prior != 0 else 0
                direction = "▲" if change > 0 else "▼"
                lines.append(f"\n**Latest:** {latest:,.3f} {direction} {abs(change):.3f} ({pct:+.2f}% vs prior)")
            except Exception:
                pass

        return "\n".join(lines)

    async def get_crypto_data(
        self,
        symbol: str = "BTC",
        market: str = "USD",
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get daily cryptocurrency OHLCV data and current exchange rate.
        :param symbol: Crypto symbol (e.g. 'BTC', 'ETH', 'SOL', 'ADA', 'DOGE', 'XRP')
        :param market: Market currency (e.g. 'USD', 'EUR', 'CNY')
        :return: Current price, daily data, and 30-day return
        """
        err = self._check_key()
        if err:
            return err

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Fetching {symbol}/{market} data...", "done": False}})

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                # Get exchange rate
                rate_resp = await client.get(BASE, params={
                    "function": "CURRENCY_EXCHANGE_RATE",
                    "from_currency": symbol.upper(),
                    "to_currency": market.upper(),
                    "apikey": self.valves.ALPHA_VANTAGE_API_KEY,
                })
                rate_data = rate_resp.json()
        except Exception as e:
            return f"Alpha Vantage crypto error: {str(e)}"

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        if "Note" in rate_data:
            return "Rate limit reached. Free tier: 5 requests/minute."

        rate_info = rate_data.get("Realtime Currency Exchange Rate", {})
        if not rate_info:
            return f"No data for {symbol}/{market}. Check the crypto symbol."

        from_name = rate_info.get("2. From_Currency Name", symbol)
        to_name = rate_info.get("4. To_Currency Name", market)
        rate = rate_info.get("5. Exchange Rate", "0")
        bid = rate_info.get("8. Bid Price", "0")
        ask = rate_info.get("9. Ask Price", "0")
        last_refresh = rate_info.get("6. Last Refreshed", "")

        lines = [f"## {from_name} ({symbol.upper()}) → {to_name}\n"]
        lines.append(f"**Price:** {float(rate):,.4f} {market.upper()}")
        lines.append(f"**Bid:** {float(bid):,.4f} | **Ask:** {float(ask):,.4f}")
        lines.append(f"**Updated:** {last_refresh}")

        return "\n".join(lines)
