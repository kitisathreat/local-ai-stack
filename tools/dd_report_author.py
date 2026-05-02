"""
title: DD Report Author — Equity Due-Diligence Markdown
author: local-ai-stack
description: Compile a structured due-diligence report on a public-equity ticker by parallel-fetching from the existing finance + news tools (yahoo_finance_extended, sec_edgar, finnhub, gdelt, guardian, nytimes) and the technical_analysis tool. Output is a markdown report with Company Overview, Financial Snapshot, Recent Filings, Analyst Recommendations, Technical Picture, Recent News, and Risk Factors. The model can pass it back through itself to write the synthesis.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field


def _load_tool(name: str):
    spec = importlib.util.spec_from_file_location(
        f"_lai_{name}", Path(__file__).parent / f"{name}.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod.Tools()


async def _safe(coro, label: str) -> str:
    try:
        return await coro
    except Exception as e:
        return f"({label} unavailable: {e})"


class Tools:
    class Valves(BaseModel):
        INCLUDE_NEWS: bool = Field(default=True)
        INCLUDE_TECHNICAL: bool = Field(default=True)
        INCLUDE_FILINGS: bool = Field(default=True)

    def __init__(self):
        self.valves = self.Valves()

    async def build(
        self,
        ticker: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Build a markdown DD report for a single ticker. Parallel-calls the
        constituent tools.
        :param ticker: Stock ticker (e.g. "AAPL", "GOOGL").
        :return: Multi-section markdown.
        """
        ticker = ticker.upper().strip()
        yf = _load_tool("yahoo_finance_extended")
        ta = _load_tool("technical_analysis") if self.valves.INCLUDE_TECHNICAL else None
        sec = _load_tool("sec_edgar") if self.valves.INCLUDE_FILINGS else None
        fh = _load_tool("finnhub")

        # Fan out — guard each call.
        tasks = {
            "summary":     _safe(yf.financial_summary(ticker), "yf.summary"),
            "income":      _safe(yf.income_statement(ticker), "yf.income"),
            "balance":     _safe(yf.balance_sheet(ticker), "yf.balance"),
            "cashflow":    _safe(yf.cash_flow(ticker), "yf.cashflow"),
            "recs":        _safe(fh.analyst_recommendations(ticker), "finnhub.recs"),
        }
        if ta:
            tasks["technical"] = _safe(ta.full_technical_report(ticker), "ta.report")
        if sec:
            tasks["filings"] = _safe(sec.recent_filings(ticker, count=5), "sec.filings")
        if self.valves.INCLUDE_NEWS:
            try:
                gdelt = _load_tool("gdelt")
                tasks["news"] = _safe(gdelt.search_news(ticker, max_results=10), "gdelt.news")
            except Exception:
                pass

        results = await asyncio.gather(*tasks.values())
        sections = dict(zip(tasks.keys(), results))

        out = [f"# Due Diligence: {ticker}\n"]
        out.append("## Company Snapshot\n")
        out.append(sections.get("summary", ""))
        out.append("\n## Financial Statements\n")
        out.append("### Income Statement\n" + sections.get("income", ""))
        out.append("\n### Balance Sheet\n" + sections.get("balance", ""))
        out.append("\n### Cash Flow\n" + sections.get("cashflow", ""))
        if "filings" in sections:
            out.append("\n## Recent SEC Filings\n" + sections["filings"])
        out.append("\n## Analyst Recommendations\n" + sections.get("recs", ""))
        if "technical" in sections:
            out.append("\n## Technical Analysis\n" + sections["technical"])
        if "news" in sections:
            out.append("\n## Recent News\n" + sections["news"])
        out.append(
            "\n## Risk Factors\n"
            "_The model should review the latest 10-K Item 1A (call sec_edgar.fetch_filing for the full text)._\n"
        )
        return "\n".join(out)
