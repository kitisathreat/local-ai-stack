"""
title: Yahoo Finance Extended — Financials, History & Options
author: local-ai-stack
description: Deep financial data via Yahoo Finance. Get historical OHLCV price data, income statements, balance sheets, cash flow statements, key ratios (P/E, P/B, beta), options chains, earnings calendar, and analyst recommendations. No API key required.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
import json
from datetime import datetime, timedelta
from typing import Callable, Any, Optional
from pydantic import BaseModel, Field

BASE = "https://query1.finance.yahoo.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}


class Tools:
    class Valves(BaseModel):
        DEFAULT_PERIOD: str = Field(
            default="1y",
            description="Default history period: 1d, 5d, 1mo, 3mo, 6mo, 1y, 2y, 5y, 10y, ytd, max",
        )

    def __init__(self):
        self.valves = self.Valves()

    async def get_price_history(
        self,
        ticker: str,
        period: str = "",
        interval: str = "1d",
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get historical OHLCV (Open/High/Low/Close/Volume) price data for a stock, ETF, or index.
        :param ticker: Stock ticker symbol (e.g. 'AAPL', 'SPY', '^GSPC' for S&P 500)
        :param period: Time period (1d/5d/1mo/3mo/6mo/1y/2y/5y/10y/ytd/max) — default 1y
        :param interval: Data interval (1m/2m/5m/15m/30m/60m/90m/1h/1d/5d/1wk/1mo/3mo)
        :return: OHLCV table with price change summary
        """
        period = period or self.valves.DEFAULT_PERIOD
        ticker = ticker.upper()

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Fetching {ticker} price history...", "done": False}})

        try:
            async with httpx.AsyncClient(timeout=15, headers=HEADERS) as client:
                resp = await client.get(
                    f"{BASE}/v8/finance/chart/{ticker}",
                    params={"range": period, "interval": interval, "includePrePost": "false"},
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            return f"Price history error for {ticker}: {str(e)}"

        result = data.get("chart", {}).get("result", [])
        if not result:
            err = data.get("chart", {}).get("error", {})
            return f"No data for {ticker}: {err.get('description', 'Unknown error')}"

        r = result[0]
        meta = r.get("meta", {})
        timestamps = r.get("timestamp", [])
        quote = r.get("indicators", {}).get("quote", [{}])[0]

        opens = quote.get("open", [])
        highs = quote.get("high", [])
        lows = quote.get("low", [])
        closes = quote.get("close", [])
        volumes = quote.get("volume", [])

        currency = meta.get("currency", "USD")
        exchange = meta.get("exchangeName", "")
        name = meta.get("longName") or meta.get("shortName") or ticker

        # Show last 30 rows max
        show = list(zip(timestamps, opens, highs, lows, closes, volumes))[-30:]

        lines = [f"## {name} ({ticker}) — {period} History\n"]
        lines.append(f"Exchange: {exchange} | Currency: {currency}\n")
        lines.append("| Date | Open | High | Low | Close | Volume |")
        lines.append("|------|------|------|-----|-------|--------|")

        for ts, o, h, l, c, v in show:
            date = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
            o_s = f"{o:.2f}" if o else "-"
            h_s = f"{h:.2f}" if h else "-"
            l_s = f"{l:.2f}" if l else "-"
            c_s = f"{c:.2f}" if c else "-"
            v_s = f"{int(v):,}" if v else "-"
            lines.append(f"| {date} | {o_s} | {h_s} | {l_s} | {c_s} | {v_s} |")

        # Summary stats
        valid_closes = [c for c in closes if c]
        if len(valid_closes) >= 2:
            first = valid_closes[0]
            last = valid_closes[-1]
            pct = (last - first) / first * 100
            high = max(h for h in highs if h)
            low = min(l for l in lows if l)
            direction = "▲" if pct >= 0 else "▼"
            lines.append(f"\n**Period Return:** {pct:+.2f}% {direction} | **52-wk Range in period:** {low:.2f} – {high:.2f} {currency}")

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        return "\n".join(lines)

    async def get_financials(
        self,
        ticker: str,
        statement: str = "income",
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get financial statements for a company — income statement, balance sheet, or cash flow.
        :param ticker: Stock ticker (e.g. 'MSFT', 'GOOGL', 'AMZN')
        :param statement: Which statement: 'income', 'balance', or 'cashflow'
        :return: Annual financial data with key line items
        """
        ticker = ticker.upper()
        module_map = {
            "income": "incomeStatementHistory",
            "balance": "balanceSheetHistory",
            "cashflow": "cashflowStatementHistory",
        }
        module = module_map.get(statement, "incomeStatementHistory")

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Fetching {ticker} {statement} statement...", "done": False}})

        try:
            async with httpx.AsyncClient(timeout=15, headers=HEADERS) as client:
                resp = await client.get(
                    f"{BASE}/v10/finance/quoteSummary/{ticker}",
                    params={"modules": module},
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            return f"Financials error: {str(e)}"

        result = data.get("quoteSummary", {}).get("result", [])
        if not result:
            return f"No financial data found for {ticker}"

        statements = result[0].get(module, {}).get(
            "incomeStatementHistory" if statement == "income" else
            "balanceSheetStatements" if statement == "balance" else
            "cashflowStatements", []
        )

        if not statements:
            return f"No {statement} statement data for {ticker}"

        INCOME_FIELDS = [
            ("totalRevenue", "Total Revenue"),
            ("grossProfit", "Gross Profit"),
            ("ebit", "EBIT"),
            ("ebitda", "EBITDA"),
            ("netIncome", "Net Income"),
            ("researchDevelopment", "R&D Expense"),
            ("totalOperatingExpenses", "Total OpEx"),
            ("basicEPS", "Basic EPS"),
        ]
        BALANCE_FIELDS = [
            ("cash", "Cash & Equivalents"),
            ("totalCurrentAssets", "Total Current Assets"),
            ("totalAssets", "Total Assets"),
            ("totalCurrentLiabilities", "Total Current Liabilities"),
            ("totalLiab", "Total Liabilities"),
            ("totalStockholderEquity", "Stockholder Equity"),
            ("longTermDebt", "Long-Term Debt"),
            ("retainedEarnings", "Retained Earnings"),
        ]
        CASHFLOW_FIELDS = [
            ("totalCashFromOperatingActivities", "Operating Cash Flow"),
            ("capitalExpenditures", "Capital Expenditures"),
            ("freeCashFlow", "Free Cash Flow"),
            ("totalCashFromInvestingActivities", "Investing Cash Flow"),
            ("totalCashFromFinancingActivities", "Financing Cash Flow"),
            ("dividendsPaid", "Dividends Paid"),
            ("repurchaseOfStock", "Stock Buybacks"),
        ]

        field_map = {"income": INCOME_FIELDS, "balance": BALANCE_FIELDS, "cashflow": CASHFLOW_FIELDS}
        fields = field_map.get(statement, INCOME_FIELDS)

        periods = []
        for stmt in statements[:4]:
            date = stmt.get("endDate", {}).get("fmt", "")
            periods.append((date, stmt))

        lines = [f"## {ticker} — {statement.title()} Statement (Annual)\n"]
        header = "| Item | " + " | ".join(p[0] for p in periods) + " |"
        lines.append(header)
        lines.append("|------|" + "------|" * len(periods))

        def fmt_val(v):
            if v is None:
                return "—"
            try:
                n = float(v)
                if abs(n) >= 1e9:
                    return f"${n/1e9:.2f}B"
                elif abs(n) >= 1e6:
                    return f"${n/1e6:.1f}M"
                else:
                    return f"${n:,.0f}"
            except Exception:
                return str(v)

        for field_key, label in fields:
            row = f"| **{label}** |"
            for _, stmt in periods:
                field_data = stmt.get(field_key, {})
                val = field_data.get("raw") if isinstance(field_data, dict) else field_data
                row += f" {fmt_val(val)} |"
            lines.append(row)

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        return "\n".join(lines)

    async def get_key_stats(
        self,
        ticker: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get key financial ratios and statistics for a stock: P/E, P/B, EV/EBITDA, beta, margins, and more.
        :param ticker: Stock ticker symbol (e.g. 'NVDA', 'TSLA', 'JPM')
        :return: Valuation ratios, profitability metrics, and analyst targets
        """
        ticker = ticker.upper()

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Fetching {ticker} key stats...", "done": False}})

        try:
            async with httpx.AsyncClient(timeout=15, headers=HEADERS) as client:
                resp = await client.get(
                    f"{BASE}/v10/finance/quoteSummary/{ticker}",
                    params={"modules": "defaultKeyStatistics,financialData,summaryDetail,price"},
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            return f"Key stats error: {str(e)}"

        result = data.get("quoteSummary", {}).get("result", [{}])
        if not result:
            return f"No stats found for {ticker}"

        r = result[0]
        stats = r.get("defaultKeyStatistics", {})
        fin = r.get("financialData", {})
        summary = r.get("summaryDetail", {})
        price_data = r.get("price", {})

        def g(d, k):
            v = d.get(k, {})
            if isinstance(v, dict):
                return v.get("fmt") or v.get("raw")
            return v

        name = g(price_data, "longName") or ticker
        mktcap = g(price_data, "marketCap")
        currency = g(price_data, "currency") or "USD"

        lines = [f"## {name} ({ticker}) — Key Statistics\n"]

        sections = [
            ("Valuation", [
                ("Market Cap", g(price_data, "marketCap")),
                ("Enterprise Value", g(stats, "enterpriseValue")),
                ("Trailing P/E", g(summary, "trailingPE")),
                ("Forward P/E", g(summary, "forwardPE")),
                ("P/B Ratio", g(stats, "priceToBook")),
                ("EV/Revenue", g(stats, "enterpriseToRevenue")),
                ("EV/EBITDA", g(stats, "enterpriseToEbitda")),
                ("PEG Ratio", g(stats, "pegRatio")),
            ]),
            ("Profitability", [
                ("Revenue (TTM)", g(fin, "totalRevenue")),
                ("Gross Margin", g(fin, "grossMargins")),
                ("Operating Margin", g(fin, "operatingMargins")),
                ("Net Profit Margin", g(fin, "profitMargins")),
                ("Return on Assets", g(fin, "returnOnAssets")),
                ("Return on Equity", g(fin, "returnOnEquity")),
                ("Free Cash Flow", g(fin, "freeCashflow")),
            ]),
            ("Risk & Dividends", [
                ("Beta", g(summary, "beta")),
                ("52-Week High", g(summary, "fiftyTwoWeekHigh")),
                ("52-Week Low", g(summary, "fiftyTwoWeekLow")),
                ("Dividend Yield", g(summary, "dividendYield")),
                ("Payout Ratio", g(summary, "payoutRatio")),
                ("Short % of Float", g(stats, "shortPercentOfFloat")),
            ]),
            ("Analyst Targets", [
                ("Target Price", g(fin, "targetMeanPrice")),
                ("Target High", g(fin, "targetHighPrice")),
                ("Target Low", g(fin, "targetLowPrice")),
                ("Recommendation", g(fin, "recommendationKey")),
                ("# Analysts", g(fin, "numberOfAnalystOpinions")),
            ]),
        ]

        def fmt(v):
            if v is None:
                return "—"
            try:
                f = float(v)
                if abs(f) >= 1e12:
                    return f"${f/1e12:.2f}T"
                elif abs(f) >= 1e9:
                    return f"${f/1e9:.2f}B"
                elif abs(f) >= 1e6:
                    return f"${f/1e6:.1f}M"
                elif abs(f) < 10:
                    return f"{f:.2f}"
                else:
                    return f"{f:,.2f}"
            except Exception:
                return str(v)

        for section_name, items in sections:
            lines.append(f"\n**{section_name}**")
            for label, value in items:
                if value is not None:
                    lines.append(f"- {label}: {fmt(value)}")

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        return "\n".join(lines)

    async def get_options_chain(
        self,
        ticker: str,
        option_type: str = "calls",
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get the options chain (near-term expiration) for a stock showing strikes, premiums, and Greeks.
        :param ticker: Stock ticker symbol (e.g. 'AAPL', 'SPY', 'TSLA')
        :param option_type: 'calls', 'puts', or 'both'
        :return: Options chain with strike, bid, ask, IV, open interest, and volume
        """
        ticker = ticker.upper()

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Fetching {ticker} options chain...", "done": False}})

        try:
            async with httpx.AsyncClient(timeout=15, headers=HEADERS) as client:
                resp = await client.get(
                    f"{BASE}/v7/finance/options/{ticker}",
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            return f"Options error: {str(e)}"

        result = data.get("optionChain", {}).get("result", [])
        if not result:
            return f"No options data for {ticker}"

        r = result[0]
        expiry_ts = r.get("expirationDate", 0)
        expiry = datetime.utcfromtimestamp(expiry_ts).strftime("%Y-%m-%d") if expiry_ts else "?"
        spot = r.get("quote", {}).get("regularMarketPrice", 0)
        options = r.get("options", [{}])[0]

        lines = [f"## {ticker} Options Chain — Expiry: {expiry} | Spot: ${spot:.2f}\n"]

        def render_contracts(contracts, label):
            if not contracts:
                return
            lines.append(f"### {label}")
            lines.append("| Strike | Bid | Ask | Last | IV | Volume | OI | ITM |")
            lines.append("|--------|-----|-----|------|----|--------|-----|-----|")
            # Show 15 nearest-the-money strikes
            atm = sorted(contracts, key=lambda c: abs(c.get("strike", 0) - spot))[:15]
            atm_sorted = sorted(atm, key=lambda c: c.get("strike", 0))
            for c in atm_sorted:
                strike = c.get("strike", 0)
                bid = c.get("bid", 0) or 0
                ask = c.get("ask", 0) or 0
                last = c.get("lastPrice", 0) or 0
                iv = c.get("impliedVolatility", 0) or 0
                vol = c.get("volume", 0) or 0
                oi = c.get("openInterest", 0) or 0
                itm = "✓" if c.get("inTheMoney") else ""
                lines.append(f"| ${strike:.2f} | ${bid:.2f} | ${ask:.2f} | ${last:.2f} | {iv*100:.1f}% | {int(vol):,} | {int(oi):,} | {itm} |")

        if option_type in ("calls", "both"):
            render_contracts(options.get("calls", []), "Calls")
        if option_type in ("puts", "both"):
            render_contracts(options.get("puts", []), "Puts")

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        return "\n".join(lines)

    async def get_earnings_calendar(
        self,
        ticker: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get upcoming and historical earnings dates and EPS estimates vs actuals for a stock.
        :param ticker: Stock ticker symbol
        :return: Earnings history with surprise %, EPS estimates, and next earnings date
        """
        ticker = ticker.upper()

        try:
            async with httpx.AsyncClient(timeout=15, headers=HEADERS) as client:
                resp = await client.get(
                    f"{BASE}/v10/finance/quoteSummary/{ticker}",
                    params={"modules": "earningsHistory,calendarEvents"},
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            return f"Earnings error: {str(e)}"

        result = data.get("quoteSummary", {}).get("result", [{}])
        if not result:
            return f"No earnings data for {ticker}"

        r = result[0]
        history = r.get("earningsHistory", {}).get("history", [])
        calendar = r.get("calendarEvents", {})
        earnings_cal = calendar.get("earnings", {})
        next_dates = earnings_cal.get("earningsDate", [])

        lines = [f"## {ticker} Earnings\n"]

        if next_dates:
            next_dt = next_dates[0].get("fmt", "")
            lines.append(f"**Next Earnings Date:** {next_dt}\n")
            est = earnings_cal.get("earningsAverage", {}).get("fmt", "")
            if est:
                lines.append(f"**EPS Estimate:** {est}\n")

        if history:
            lines.append("### Earnings History\n")
            lines.append("| Quarter | EPS Estimate | EPS Actual | Surprise | Surprise % |")
            lines.append("|---------|-------------|-----------|----------|------------|")
            for h in reversed(history[-8:]):
                quarter = h.get("period", "")
                date = h.get("quarter", {}).get("fmt", "")
                est_val = h.get("epsEstimate", {}).get("fmt", "—")
                act_val = h.get("epsActual", {}).get("fmt", "—")
                surp = h.get("epsDifference", {}).get("fmt", "—")
                surp_pct = h.get("surprisePercent", {}).get("fmt", "—")
                lines.append(f"| {date} {quarter} | {est_val} | {act_val} | {surp} | {surp_pct} |")

        return "\n".join(lines)
