"""
title: News Correlator — Match News Events to Price Moves
author: local-ai-stack
description: For a given ticker and time window, pull intraday/daily price history (yahoo_finance_extended) and headlines from gdelt / guardian / nytimes, align them on a date axis, and flag the days with the largest moves. Return a markdown table mapping each notable move to the news that ran that day so the model can hypothesise causes.
required_open_webui_version: 0.4.0
requirements: httpx, pandas
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

import asyncio
import importlib.util
import re
from datetime import datetime, timedelta
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


class Tools:
    class Valves(BaseModel):
        MOVE_THRESHOLD_PCT: float = Field(
            default=3.0,
            description="Daily move (absolute %) at or above which the day is flagged.",
        )
        DEFAULT_DAYS: int = Field(default=30)

    def __init__(self):
        self.valves = self.Valves()

    async def correlate(
        self,
        ticker: str,
        days: int = 0,
        threshold_pct: float = 0.0,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        For each day in the trailing window where |return| ≥ threshold,
        pull headlines mentioning the ticker and align them.
        :param ticker: Stock ticker.
        :param days: Window length. 0 = DEFAULT_DAYS.
        :param threshold_pct: Move threshold in percent. 0 = MOVE_THRESHOLD_PCT.
        :return: Markdown table: date, return %, top headlines.
        """
        try:
            import yfinance as yf  # type: ignore
            import pandas as pd
        except ImportError:
            return "yfinance + pandas required. Run: pip install yfinance pandas"

        n = days or self.valves.DEFAULT_DAYS
        thr = threshold_pct or self.valves.MOVE_THRESHOLD_PCT
        end = datetime.utcnow()
        start = end - timedelta(days=int(n * 1.5) + 5)

        df = yf.download(ticker, start=start.strftime("%Y-%m-%d"),
                         end=end.strftime("%Y-%m-%d"),
                         auto_adjust=True, progress=False)
        if df is None or df.empty:
            return f"no price data for {ticker}"
        df["ret"] = df["Close"].pct_change() * 100
        big = df[df["ret"].abs() >= thr].tail(20)
        if big.empty:
            return f"no days with |return| >= {thr}% in the last {n} trading days"

        try:
            gdelt = _load_tool("gdelt")
        except Exception:
            gdelt = None

        rows = ["| date | return % | top headlines |", "|---|---|---|"]
        for d, row in big.iterrows():
            day_str = d.strftime("%Y-%m-%d")
            ret_str = f"{row['ret']:+.2f}"
            headlines = "(no news tool available)"
            if gdelt is not None:
                try:
                    text = await gdelt.search_news(
                        f"{ticker} {day_str}", max_results=3,
                    )
                    titles = re.findall(r"^\d+\.\s+(.+)$", text, re.MULTILINE)[:3]
                    headlines = "<br>".join(titles) or "(no headlines)"
                except Exception:
                    headlines = "(gdelt error)"
            rows.append(f"| {day_str} | {ret_str} | {headlines} |")

        return f"# News ↔ Price moves: {ticker} (last {n}d, |move| ≥ {thr}%)\n\n" + "\n".join(rows)
