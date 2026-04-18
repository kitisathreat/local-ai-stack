"""
title: Finnhub — Real-Time Stocks, Fundamentals & Market News
author: local-ai-stack
description: Real-time stock quotes, company fundamentals, earnings history, analyst ratings, IPO calendar, insider transactions, and financial news via Finnhub. Covers US/international stocks, forex, and crypto. Free API key at finnhub.io (60 calls/minute on free tier).
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from datetime import datetime, timedelta
from typing import Callable, Any, Optional
from pydantic import BaseModel, Field

BASE = "https://finnhub.io/api/v1"


class Tools:
    class Valves(BaseModel):
        FINNHUB_API_KEY: str = Field(
            default="",
            description="Finnhub API key — free at https://finnhub.io (60 calls/minute free)",
        )

    def __init__(self):
        self.valves = self.Valves()

    def _check_key(self) -> Optional[str]:
        if not self.valves.FINNHUB_API_KEY:
            return (
                "Finnhub API key required.\n"
                "Get a free key at: https://finnhub.io\n"
                "Add it in Open WebUI > Tools > Finnhub > FINNHUB_API_KEY"
            )
        return None

    def _params(self, **kwargs) -> dict:
        return {"token": self.valves.FINNHUB_API_KEY, **kwargs}

    async def get_company_profile(
        self,
        symbol: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get detailed company profile: description, sector, industry, market cap, employee count, CEO, website.
        :param symbol: Stock ticker symbol (e.g. 'AAPL', 'MSFT', 'JPM', 'XOM')
        :return: Company overview, fundamentals, and key financial metrics
        """
        err = self._check_key()
        if err:
            return err

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Fetching {symbol} company profile...", "done": False}})

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                profile_resp = await client.get(f"{BASE}/stock/profile2", params=self._params(symbol=symbol))
                metrics_resp = await client.get(f"{BASE}/stock/metric", params=self._params(symbol=symbol, metric="all"))
                profile_resp.raise_for_status()
                profile = profile_resp.json()
                metrics = metrics_resp.json().get("metric", {})
        except Exception as e:
            return f"Finnhub error: {str(e)}"

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        if not profile:
            return f"No data found for '{symbol}'."

        name = profile.get("name", symbol)
        country = profile.get("country", "")
        exchange = profile.get("exchange", "")
        industry = profile.get("finnhubIndustry", "")
        mktcap = profile.get("marketCapitalization", 0)
        shares = profile.get("shareOutstanding", 0)
        ipo = profile.get("ipo", "")
        weburl = profile.get("weburl", "")
        logo = profile.get("logo", "")
        currency = profile.get("currency", "USD")

        def fmt_cap(v):
            if v >= 1e6:
                return f"${v/1e6:.2f}T"
            elif v >= 1e3:
                return f"${v/1e3:.2f}B"
            return f"${v:.0f}M"

        lines = [f"## {name} ({symbol.upper()})\n"]
        lines.append(f"**Exchange:** {exchange} | **Country:** {country} | **Industry:** {industry}")
        lines.append(f"**Market Cap:** {fmt_cap(mktcap)} | **IPO:** {ipo} | **Currency:** {currency}")
        if weburl:
            lines.append(f"**Website:** {weburl}\n")

        # Key financial metrics
        metric_display = [
            ("52WeekHigh", "52W High", "${:.2f}"),
            ("52WeekLow", "52W Low", "${:.2f}"),
            ("peBasicExclExtraTTM", "P/E (TTM)", "{:.2f}"),
            ("pbAnnual", "P/B (Annual)", "{:.2f}"),
            ("epsBasicExclExtraAnnual", "EPS (Annual)", "${:.2f}"),
            ("dividendYieldIndicatedAnnual", "Dividend Yield", "{:.2f}%"),
            ("roeTTM", "ROE (TTM)", "{:.2f}%"),
            ("roiTTM", "ROI (TTM)", "{:.2f}%"),
            ("netProfitMarginTTM", "Net Margin (TTM)", "{:.2f}%"),
            ("revenueGrowthTTMYoy", "Revenue Growth YoY", "{:.2f}%"),
            ("beta", "Beta", "{:.3f}"),
            ("10DayAverageTradingVolume", "10D Avg Volume", "{:,.0f}M shares"),
        ]

        lines.append("\n### Key Metrics\n")
        lines.append("| Metric | Value |")
        lines.append("|--------|-------|")
        for key, label, fmt in metric_display:
            val = metrics.get(key)
            if val is not None:
                try:
                    lines.append(f"| {label} | {fmt.format(val)} |")
                except Exception:
                    lines.append(f"| {label} | {val} |")

        return "\n".join(lines)

    async def get_earnings(
        self,
        symbol: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get quarterly earnings history with EPS estimates vs actuals, surprises, and next earnings date.
        :param symbol: Stock ticker symbol (e.g. 'AAPL', 'NVDA', 'AMZN')
        :return: EPS history with beat/miss analysis and upcoming earnings date
        """
        err = self._check_key()
        if err:
            return err

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Fetching {symbol} earnings...", "done": False}})

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                hist_resp = await client.get(f"{BASE}/stock/earnings", params=self._params(symbol=symbol, limit=8))
                cal_resp = await client.get(
                    f"{BASE}/calendar/earnings",
                    params=self._params(
                        symbol=symbol,
                        from_=(datetime.now()).strftime("%Y-%m-%d"),
                        to=(datetime.now() + timedelta(days=90)).strftime("%Y-%m-%d"),
                    ),
                )
                hist = hist_resp.json()
                cal = cal_resp.json().get("earningsCalendar", [])
        except Exception as e:
            return f"Finnhub earnings error: {str(e)}"

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        lines = [f"## {symbol.upper()} — Earnings History\n"]

        if cal:
            next_e = cal[0]
            lines.append(f"**Next Earnings:** {next_e.get('date', '')} | EPS Estimate: {next_e.get('epsEstimate', 'N/A')}\n")

        if hist:
            lines.append("| Quarter | EPS Estimate | EPS Actual | Surprise | Surprise % |")
            lines.append("|---------|-------------|-----------|----------|------------|")
            for e in reversed(hist):
                period = e.get("period", "")
                estimate = e.get("estimate")
                actual = e.get("actual")
                surprise = e.get("surprise")
                surprise_pct = e.get("surprisePercent")

                est_s = f"${estimate:.2f}" if estimate is not None else "—"
                act_s = f"${actual:.2f}" if actual is not None else "—"
                surp_s = f"${surprise:+.2f}" if surprise is not None else "—"
                pct_s = f"{surprise_pct:+.1f}%" if surprise_pct is not None else "—"
                beat = "✅" if surprise and surprise > 0 else ("❌" if surprise and surprise < 0 else "—")

                lines.append(f"| {period} | {est_s} | {act_s} {beat} | {surp_s} | {pct_s} |")
        else:
            lines.append("No earnings history available.")

        return "\n".join(lines)

    async def get_news_sentiment(
        self,
        symbol: str = "",
        category: str = "general",
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get recent financial news and AI sentiment scores for a stock or market category.
        :param symbol: Stock ticker for company-specific news (leave blank for market news)
        :param category: News category for general news: 'general', 'forex', 'crypto', 'merger'
        :return: Recent headlines with sentiment scores (bearish/bullish) and source URLs
        """
        err = self._check_key()
        if err:
            return err

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Fetching news...", "done": False}})

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                if symbol:
                    end = datetime.now().strftime("%Y-%m-%d")
                    start = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
                    resp = await client.get(
                        f"{BASE}/company-news",
                        params=self._params(symbol=symbol, from_=start, to=end),
                    )
                    sentiment_resp = await client.get(
                        f"{BASE}/news-sentiment",
                        params=self._params(symbol=symbol),
                    )
                    articles = resp.json()
                    sentiment_data = sentiment_resp.json()
                else:
                    resp = await client.get(f"{BASE}/news", params=self._params(category=category))
                    articles = resp.json()
                    sentiment_data = {}
        except Exception as e:
            return f"Finnhub news error: {str(e)}"

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        title_str = f"{symbol.upper()} News" if symbol else f"{category.title()} Market News"
        lines = [f"## {title_str}\n"]

        if sentiment_data:
            bull = sentiment_data.get("buzz", {}).get("bullishPercent", 0)
            bear = sentiment_data.get("buzz", {}).get("bearishPercent", 0)
            score = sentiment_data.get("companyNewsScore", 0)
            lines.append(f"**Sentiment:** 🟢 Bullish {bull*100:.0f}% / 🔴 Bearish {bear*100:.0f}% | Score: {score:.3f}\n")

        if not articles:
            return "\n".join(lines) + "\nNo recent news found."

        for article in articles[:10]:
            headline = article.get("headline", "")
            source = article.get("source", "")
            url = article.get("url", "")
            ts = article.get("datetime", 0)
            date = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d") if ts else ""
            summary = article.get("summary", "")[:120]

            lines.append(f"**[{headline}]({url})**")
            lines.append(f"*{source} — {date}*")
            if summary:
                lines.append(f"{summary}...")
            lines.append("")

        return "\n".join(lines)

    async def get_analyst_recommendations(
        self,
        symbol: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get analyst buy/sell/hold recommendations and price target consensus for a stock.
        :param symbol: Stock ticker symbol (e.g. 'TSLA', 'META', 'AMD')
        :return: Monthly breakdown of analyst ratings (strong buy, buy, hold, sell, strong sell)
        """
        err = self._check_key()
        if err:
            return err

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                recs_resp = await client.get(f"{BASE}/stock/recommendation", params=self._params(symbol=symbol))
                target_resp = await client.get(f"{BASE}/stock/price-target", params=self._params(symbol=symbol))
                recs = recs_resp.json()
                target = target_resp.json()
        except Exception as e:
            return f"Finnhub recommendation error: {str(e)}"

        lines = [f"## {symbol.upper()} — Analyst Recommendations\n"]

        if target:
            high = target.get("targetHigh", 0)
            low = target.get("targetLow", 0)
            mean = target.get("targetMean", 0)
            median = target.get("targetMedian", 0)
            n = target.get("lastUpdated", "")
            lines.append(f"**Price Target:** ${mean:.2f} mean / ${median:.2f} median (Range: ${low:.2f} – ${high:.2f})")
            lines.append(f"**Updated:** {n}\n")

        if recs:
            lines.append("| Month | Strong Buy | Buy | Hold | Sell | Strong Sell |")
            lines.append("|-------|------------|-----|------|------|-------------|")
            for r in recs[:6]:
                period = r.get("period", "")
                sb = r.get("strongBuy", 0)
                b = r.get("buy", 0)
                h = r.get("hold", 0)
                s = r.get("sell", 0)
                ss = r.get("strongSell", 0)
                lines.append(f"| {period} | {sb} | {b} | {h} | {s} | {ss} |")
        else:
            lines.append("No analyst recommendations available.")

        return "\n".join(lines)

    async def get_insider_transactions(
        self,
        symbol: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get recent insider buying and selling transactions for a company.
        :param symbol: Stock ticker symbol (e.g. 'AAPL', 'TSLA', 'AMZN')
        :return: Insider name, title, transaction type, shares, and dollar value
        """
        err = self._check_key()
        if err:
            return err

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Fetching {symbol} insider transactions...", "done": False}})

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(f"{BASE}/stock/insider-transactions", params=self._params(symbol=symbol))
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            return f"Finnhub insider error: {str(e)}"

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        transactions = data.get("data", [])
        if not transactions:
            return f"No insider transaction data for {symbol}."

        lines = [f"## {symbol.upper()} — Insider Transactions\n"]
        lines.append("| Date | Name | Title | Type | Shares | Price | Value |")
        lines.append("|------|------|-------|------|--------|-------|-------|")

        for t in transactions[:20]:
            date = t.get("filingDate", t.get("transactionDate", ""))
            name = t.get("name", "")
            title = t.get("officerTitle", "")[:20]
            t_type = t.get("transactionCode", "")
            # P = Purchase, S = Sale
            type_str = "🟢 Buy" if t_type == "P" else ("🔴 Sell" if t_type == "S" else t_type)
            shares = t.get("share", 0) or 0
            price = t.get("price", 0) or 0
            value = shares * price

            lines.append(
                f"| {date} | {name} | {title} | {type_str} | "
                f"{int(shares):,} | ${price:.2f} | ${value:,.0f} |"
            )

        return "\n".join(lines)
