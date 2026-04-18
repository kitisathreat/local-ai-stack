"""
title: Finance — Stocks & Crypto
author: local-ai-stack
description: Get real-time stock quotes, historical prices, and company info via Yahoo Finance. Get crypto prices and market data via CoinGecko. Both free, no API key required.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from pydantic import BaseModel, Field
from typing import Callable, Any, Optional


YF_QUOTE = "https://query1.finance.yahoo.com/v8/finance/quote"
YF_CHART = "https://query1.finance.yahoo.com/v8/finance/chart"
CG_API   = "https://api.coingecko.com/api/v3"


class Tools:
    class Valves(BaseModel):
        CURRENCY: str = Field(default="usd", description="Fiat currency for crypto prices (usd, eur, gbp, jpy...)")

    def __init__(self):
        self.valves = self.Valves()

    def _yf_headers(self) -> dict:
        return {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        }

    async def get_stock_quote(
        self,
        symbols: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get real-time stock or ETF quotes from Yahoo Finance.
        :param symbols: Comma-separated ticker symbols (e.g. "AAPL,MSFT,NVDA" or "SPY,QQQ")
        :return: Current price, change, market cap, P/E ratio, and 52-week range
        """
        symbol_list = [s.strip().upper() for s in symbols.split(",") if s.strip()][:8]
        if not symbol_list:
            return "No symbols provided."

        if __event_emitter__:
            await __event_emitter__(
                {"type": "status", "data": {"description": f"Fetching quotes: {', '.join(symbol_list)}", "done": False}}
            )

        try:
            params = {"symbols": ",".join(symbol_list), "fields": "regularMarketPrice,regularMarketChange,regularMarketChangePercent,regularMarketVolume,marketCap,trailingPE,fiftyTwoWeekHigh,fiftyTwoWeekLow,shortName,regularMarketPreviousClose,currency"}
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(YF_QUOTE, params=params, headers=self._yf_headers())
                resp.raise_for_status()
                data = resp.json()

            results = data.get("quoteResponse", {}).get("result", [])
            if not results:
                return f"No data found for: {', '.join(symbol_list)}"

            if __event_emitter__:
                await __event_emitter__(
                    {"type": "status", "data": {"description": "Quotes retrieved", "done": True}}
                )

            lines = ["## Stock Quotes\n"]
            for q in results:
                symbol  = q.get("symbol", "?")
                name    = q.get("shortName", symbol)
                price   = q.get("regularMarketPrice", 0)
                change  = q.get("regularMarketChange", 0)
                pct     = q.get("regularMarketChangePercent", 0)
                prev    = q.get("regularMarketPreviousClose", 0)
                vol     = q.get("regularMarketVolume", 0)
                mcap    = q.get("marketCap", 0)
                pe      = q.get("trailingPE")
                wk52h   = q.get("fiftyTwoWeekHigh", 0)
                wk52l   = q.get("fiftyTwoWeekLow", 0)
                cur     = q.get("currency", "USD")
                arrow   = "▲" if change >= 0 else "▼"
                mcap_str = f"${mcap/1e12:.2f}T" if mcap >= 1e12 else (f"${mcap/1e9:.1f}B" if mcap >= 1e9 else f"${mcap/1e6:.0f}M") if mcap else "N/A"

                lines.append(f"**{symbol}** — {name}")
                lines.append(f"  Price:   {cur} {price:.2f}  {arrow} {change:+.2f} ({pct:+.2f}%)  prev: {prev:.2f}")
                lines.append(f"  Mkt Cap: {mcap_str}  |  P/E: {pe:.1f}" if pe else f"  Mkt Cap: {mcap_str}")
                lines.append(f"  Volume:  {vol:,}  |  52-wk: {wk52l:.2f} – {wk52h:.2f}")
                lines.append("")

            return "\n".join(lines)

        except httpx.HTTPStatusError as e:
            return f"Yahoo Finance error: HTTP {e.response.status_code}"
        except Exception as e:
            return f"Stock quote error: {str(e)}"

    async def get_crypto_price(
        self,
        coins: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get cryptocurrency prices, market caps, and 24h changes from CoinGecko.
        :param coins: Comma-separated coin names or IDs (e.g. "bitcoin,ethereum,solana" or "BTC,ETH,SOL")
        :return: Current price, 24h change, market cap, volume, and all-time high
        """
        raw = [c.strip().lower() for c in coins.split(",") if c.strip()][:8]

        # Map common ticker symbols → CoinGecko IDs
        ticker_map = {
            "btc": "bitcoin", "eth": "ethereum", "sol": "solana", "bnb": "binancecoin",
            "ada": "cardano", "xrp": "ripple", "doge": "dogecoin", "dot": "polkadot",
            "matic": "matic-network", "avax": "avalanche-2", "link": "chainlink",
            "ltc": "litecoin", "uni": "uniswap", "shib": "shiba-inu", "atom": "cosmos",
        }
        ids = [ticker_map.get(c, c) for c in raw]

        if __event_emitter__:
            await __event_emitter__(
                {"type": "status", "data": {"description": f"Fetching crypto prices...", "done": False}}
            )

        try:
            cur = self.valves.CURRENCY
            params = {
                "ids": ",".join(ids),
                "vs_currencies": cur,
                "include_market_cap": "true",
                "include_24hr_vol": "true",
                "include_24hr_change": "true",
                "include_ath": "true",
            }
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{CG_API}/simple/price", params=params)
                resp.raise_for_status()
                data = resp.json()

            if not data:
                return f"No crypto data found. Check coin names (use 'bitcoin', 'ethereum', etc.)"

            if __event_emitter__:
                await __event_emitter__(
                    {"type": "status", "data": {"description": "Crypto prices retrieved", "done": True}}
                )

            lines = [f"## Crypto Prices ({cur.upper()})\n"]
            for coin_id, vals in data.items():
                price   = vals.get(cur, 0)
                change  = vals.get(f"{cur}_24h_change", 0)
                mcap    = vals.get(f"{cur}_market_cap", 0)
                vol     = vals.get(f"{cur}_24h_vol", 0)
                arrow   = "▲" if change >= 0 else "▼"
                mcap_str = f"${mcap/1e12:.2f}T" if mcap >= 1e12 else (f"${mcap/1e9:.1f}B" if mcap >= 1e9 else f"${mcap/1e6:.0f}M")
                price_fmt = f"${price:,.8f}" if price < 0.01 else f"${price:,.4f}" if price < 1 else f"${price:,.2f}"

                lines.append(f"**{coin_id.title()}**")
                lines.append(f"  Price:   {price_fmt}  {arrow} {change:+.2f}% (24h)")
                lines.append(f"  Mkt Cap: {mcap_str}  |  24h Vol: ${vol/1e9:.2f}B")
                lines.append("")

            return "\n".join(lines)

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                return "CoinGecko rate limit hit. Wait 60 seconds and try again."
            return f"CoinGecko error: HTTP {e.response.status_code}"
        except Exception as e:
            return f"Crypto price error: {str(e)}"

    async def search_ticker(
        self,
        query: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search Yahoo Finance for a stock ticker symbol by company name.
        :param query: Company name to search (e.g. "Apple", "NVIDIA", "Tesla")
        :return: Matching tickers with exchange and type
        """
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    "https://query1.finance.yahoo.com/v1/finance/search",
                    params={"q": query, "quotesCount": 6, "newsCount": 0, "listsCount": 0},
                    headers=self._yf_headers(),
                )
                resp.raise_for_status()
                quotes = resp.json().get("quotes", [])

            if not quotes:
                return f"No tickers found for: {query}"

            lines = [f"## Ticker Search: {query}\n"]
            for q in quotes[:6]:
                sym = q.get("symbol", "")
                name = q.get("shortname") or q.get("longname", "")
                exch = q.get("exchDisp", "")
                qtype = q.get("typeDisp", "")
                lines.append(f"**{sym}** — {name} ({exch}, {qtype})")

            return "\n".join(lines)

        except Exception as e:
            return f"Ticker search error: {str(e)}"
