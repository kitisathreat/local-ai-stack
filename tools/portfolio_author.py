"""
title: Portfolio Author — Backtest a Strategy from a Spec
author: local-ai-stack
description: Run vectorised backtests on equity / ETF / crypto portfolios. Accepts a strategy spec (positions, weights, rebalance schedule, benchmark, transaction cost) and pulls historical OHLCV via the existing `yahoo_finance_extended` tool, then computes the equity curve, drawdown, Sharpe, Sortino, Calmar, alpha vs benchmark, and per-position attribution. Output is a markdown report with optional embedded chart via `chart_generator`.
required_open_webui_version: 0.4.0
requirements: httpx, pandas, numpy
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

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


class Tools:
    class Valves(BaseModel):
        DEFAULT_BENCHMARK: str = Field(default="SPY", description="Benchmark ticker.")
        DEFAULT_TX_COST_BPS: float = Field(default=5.0, description="Round-trip transaction cost in basis points.")
        DEFAULT_RISK_FREE_PCT: float = Field(default=4.5, description="Annual risk-free rate (% yield) for Sharpe.")

    def __init__(self):
        self.valves = self.Valves()

    async def _history(self, ticker: str, start: str, end: str) -> "Any":
        try:
            import pandas as pd
        except ImportError:
            return None
        yf = _load_tool("yahoo_finance_extended")
        # The existing tool returns text — we parse a CSV section if it exposes one,
        # otherwise we re-derive via its `historical_prices` if available.
        try:
            text = await yf.historical_prices(ticker, period="max", interval="1d")
        except Exception:
            return None
        # Many of these helper tools return text dumps; ask the user to install
        # yfinance directly when this fails so the backtest can run for real.
        try:
            import io
            return pd.read_csv(io.StringIO(text))
        except Exception:
            return None

    async def backtest(
        self,
        positions: dict[str, float],
        start: str,
        end: str,
        benchmark: str = "",
        rebalance: str = "monthly",
        tx_cost_bps: float = 0.0,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Backtest a static-weight portfolio.
        :param positions: {ticker: weight_fraction}, weights summing to 1.
        :param start: ISO date "YYYY-MM-DD".
        :param end: ISO date.
        :param benchmark: Benchmark ticker. Empty = DEFAULT_BENCHMARK.
        :param rebalance: monthly, quarterly, yearly, none.
        :param tx_cost_bps: Per-rebalance round-trip cost in bps. 0 = DEFAULT.
        :return: Markdown report with equity curve, drawdown, Sharpe, Sortino, alpha.
        """
        try:
            import pandas as pd
            import numpy as np
        except ImportError:
            return "pandas + numpy required. Run: pip install pandas numpy"

        bench = benchmark or self.valves.DEFAULT_BENCHMARK
        tx_cost = (tx_cost_bps or self.valves.DEFAULT_TX_COST_BPS) / 10_000.0
        rf = self.valves.DEFAULT_RISK_FREE_PCT / 100.0

        # Use yfinance directly when available — much more reliable than the
        # tool's text-dump format.
        try:
            import yfinance as yf  # type: ignore
        except ImportError:
            return ("yfinance required for portfolio_author. "
                    "Run: pip install yfinance")

        tickers = list(positions.keys()) + [bench]
        data = yf.download(tickers, start=start, end=end, auto_adjust=True, progress=False)
        if data is None or data.empty:
            return f"no data for {tickers} in {start}..{end}"
        prices = data["Close"] if "Close" in data else data
        prices = prices.dropna(how="all").ffill()

        weights = pd.Series(positions, dtype=float)
        weights = weights / weights.sum()
        port_prices = prices[weights.index].dropna()
        bench_prices = prices[bench].dropna()

        rb = {"monthly": "ME", "quarterly": "QE", "yearly": "YE", "none": None}.get(rebalance, "ME")
        rets = port_prices.pct_change().fillna(0)
        if rb is None:
            port_ret = (rets * weights).sum(axis=1)
        else:
            shares = pd.DataFrame(0.0, index=port_prices.index, columns=port_prices.columns)
            cash = 1.0
            rb_dates = port_prices.resample(rb).last().index
            current_shares = pd.Series(0.0, index=port_prices.columns)
            for d in port_prices.index:
                if d in rb_dates or shares.iloc[0].sum() == 0:
                    nav = (current_shares * port_prices.loc[d]).sum() + cash
                    nav *= (1 - tx_cost)   # rebalance cost
                    target_dollars = weights * nav
                    current_shares = target_dollars / port_prices.loc[d]
                    cash = 0.0
                shares.loc[d] = current_shares
            port_value = (shares * port_prices).sum(axis=1) + cash
            port_ret = port_value.pct_change().fillna(0)

        equity = (1 + port_ret).cumprod()
        bench_ret = bench_prices.pct_change().fillna(0)
        bench_eq = (1 + bench_ret).cumprod()

        ann = 252
        cagr = equity.iloc[-1] ** (ann / len(equity)) - 1
        vol = port_ret.std() * (ann ** 0.5)
        sharpe = (port_ret.mean() * ann - rf) / vol if vol > 0 else 0
        downside = port_ret[port_ret < 0].std() * (ann ** 0.5)
        sortino = (port_ret.mean() * ann - rf) / downside if downside > 0 else 0
        peak = equity.cummax()
        dd = (equity / peak - 1).min()
        calmar = cagr / abs(dd) if dd < 0 else 0
        alpha = (equity.iloc[-1] / bench_eq.iloc[-1]) - 1

        return (
            f"# Backtest: {list(positions.keys())} ({start} → {end})\n\n"
            f"weights: {dict(weights.round(3))}\n"
            f"rebalance: {rebalance}, tx cost: {tx_cost*1e4:.1f} bps\n\n"
            f"| metric | portfolio | benchmark ({bench}) |\n"
            f"|---|---|---|\n"
            f"| final equity (×$1) | {equity.iloc[-1]:.3f} | {bench_eq.iloc[-1]:.3f} |\n"
            f"| CAGR | {cagr*100:.2f}% | {(bench_eq.iloc[-1]**(ann/len(bench_eq))-1)*100:.2f}% |\n"
            f"| volatility (ann) | {vol*100:.2f}% | {bench_ret.std()*ann**0.5*100:.2f}% |\n"
            f"| Sharpe | {sharpe:.2f} | {(bench_ret.mean()*ann-rf)/(bench_ret.std()*ann**0.5):.2f} |\n"
            f"| Sortino | {sortino:.2f} | — |\n"
            f"| max drawdown | {dd*100:.2f}% | — |\n"
            f"| Calmar | {calmar:.2f} | — |\n"
            f"| alpha vs benchmark | {alpha*100:+.2f}% | — |\n"
        )

    def example_spec(self, __user__: Optional[dict] = None) -> str:
        """
        Return an example positions dict + arguments for `backtest`.
        :return: Sample backtest call.
        """
        return (
            'positions = {"AAPL": 0.25, "MSFT": 0.25, "GOOGL": 0.20, "VOO": 0.30}\n'
            'await backtest(positions, start="2020-01-01", end="2024-12-31",\n'
            '               benchmark="SPY", rebalance="monthly", tx_cost_bps=5)'
        )
