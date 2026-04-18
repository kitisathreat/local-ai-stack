"""
title: Financial Modelling — DCF, Monte Carlo, Efficient Frontier & Scenario Analysis
author: local-ai-stack
description: Professional financial modelling with inline charts. Build Discounted Cash Flow (DCF) valuation models with sensitivity tables, run Monte Carlo price simulations using Geometric Brownian Motion, construct Markowitz Efficient Frontier for portfolio optimization, and perform bear/base/bull scenario analysis. All models render as embedded charts. No API key required.
required_open_webui_version: 0.4.0
requirements: httpx matplotlib pandas numpy scipy
version: 1.0.0
licence: MIT
"""

import io
import math
import base64
import asyncio
import random
from typing import Callable, Any, Optional
from pydantic import BaseModel, Field

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import numpy as np

try:
    import pandas as pd
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False

import httpx

YAHOO_BASE = "https://query1.finance.yahoo.com"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def _fig_to_base64(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor=fig.get_facecolor())
    buf.seek(0)
    encoded = base64.b64encode(buf.read()).decode("utf-8")
    plt.close(fig)
    return f"![chart](data:image/png;base64,{encoded})"


async def _fetch_closes(ticker: str, period: str = "2y") -> list:
    url = f"{YAHOO_BASE}/v8/finance/chart/{ticker.upper()}"
    params = {"interval": "1d", "range": period, "includePrePost": "false"}
    async with httpx.AsyncClient(timeout=20, headers=HEADERS) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
    result = data.get("chart", {}).get("result", [])
    if not result:
        return []
    closes = result[0].get("indicators", {}).get("quote", [{}])[0].get("close", [])
    return [c for c in closes if c is not None]


class Tools:
    class Valves(BaseModel):
        RISK_FREE_RATE: float = Field(
            default=0.045,
            description="Annual risk-free rate for portfolio calculations (e.g. 0.045 = 4.5% US Treasury)",
        )

    def __init__(self):
        self.valves = self.Valves()

    async def dcf_model(
        self,
        ticker: str = "",
        free_cash_flows: str = "",
        growth_rate: float = 0.05,
        discount_rate: float = 0.10,
        terminal_growth: float = 0.025,
        years: int = 5,
        shares_outstanding: float = 0,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Build a Discounted Cash Flow (DCF) valuation model with sensitivity analysis heatmap.
        :param ticker: Optional — ticker to fetch current price for comparison (e.g. 'AAPL'). Can be blank.
        :param free_cash_flows: Comma-separated FCF values in millions (e.g. '5000,5500,6100,6700,7400'). If blank, uses growth_rate to project from a base of 1000M.
        :param growth_rate: Near-term FCF growth rate per year (e.g. 0.08 = 8%)
        :param discount_rate: Weighted Average Cost of Capital WACC (e.g. 0.10 = 10%)
        :param terminal_growth: Perpetuity growth rate for terminal value (e.g. 0.025 = 2.5%)
        :param years: Projection years (3–10, default 5)
        :param shares_outstanding: Shares outstanding in millions for per-share value (0 = skip per-share calc)
        :return: DCF valuation with NPV breakdown, sensitivity table, and chart
        """
        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Building DCF model...", "done": False}})

        years = max(3, min(10, years))

        if free_cash_flows.strip():
            try:
                fcf_list = [float(x.strip().replace(",", "")) for x in free_cash_flows.split(",")]
                base_fcf = fcf_list[0]
                projected = fcf_list[:years]
                while len(projected) < years:
                    projected.append(projected[-1] * (1 + growth_rate))
            except ValueError:
                return "Invalid free_cash_flows format. Use comma-separated numbers like '5000,5500,6000'."
        else:
            base_fcf = 1000.0
            projected = [base_fcf * (1 + growth_rate) ** y for y in range(1, years + 1)]

        current_price = None
        if ticker:
            try:
                data = await _fetch_closes(ticker, "5d")
                if data:
                    current_price = data[-1]
            except Exception:
                pass

        def _npv(fcfs, dr, tg):
            pv_fcfs = [cf / (1 + dr) ** (i + 1) for i, cf in enumerate(fcfs)]
            terminal = (fcfs[-1] * (1 + tg)) / (dr - tg) if dr > tg else 0
            pv_terminal = terminal / (1 + dr) ** len(fcfs)
            return sum(pv_fcfs), pv_terminal, sum(pv_fcfs) + pv_terminal

        pv_fcfs_sum, pv_terminal, total_npv = _npv(projected, discount_rate, terminal_growth)

        per_share = total_npv / shares_outstanding if shares_outstanding > 0 else None

        wacc_range = [discount_rate + (i - 4) * 0.01 for i in range(9)]
        tg_range = [terminal_growth + (j - 2) * 0.005 for j in range(5)]
        sensitivity = np.array([[_npv(projected, w, t)[2] for t in tg_range] for w in wacc_range])

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Rendering charts...", "done": False}})

        with plt.style.context("dark_background"):
            fig = plt.figure(figsize=(14, 8), facecolor="#1a1a2e")
            gs = gridspec.GridSpec(1, 2, figure=fig, width_ratios=[1, 1.4])
            ax_bar = fig.add_subplot(gs[0])
            ax_heat = fig.add_subplot(gs[1])

            for ax in [ax_bar, ax_heat]:
                ax.set_facecolor("#1a1a2e")

            periods = [f"Y{i+1}" for i in range(years)] + ["Terminal\nValue"]
            pv_fcfs_list = [cf / (1 + discount_rate) ** (i + 1) for i, cf in enumerate(projected)]
            values = pv_fcfs_list + [pv_terminal]
            colors = ["#42a5f5"] * years + ["#f59e0b"]

            bars = ax_bar.bar(periods, values, color=colors, alpha=0.85, edgecolor="#333333")
            ax_bar.set_title("DCF Value Breakdown", color="white", fontsize=11)
            ax_bar.set_ylabel("Present Value ($M)", color="#cccccc")
            ax_bar.tick_params(colors="#cccccc")
            ax_bar.grid(alpha=0.2, axis="y")

            for bar, val in zip(bars, values):
                ax_bar.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + max(values) * 0.01,
                            f"${val:.0f}M", ha="center", va="bottom", color="white", fontsize=7)

            cmap = plt.get_cmap("RdYlGn")
            im = ax_heat.imshow(sensitivity, cmap=cmap, aspect="auto")
            ax_heat.set_xticks(range(len(tg_range)))
            ax_heat.set_yticks(range(len(wacc_range)))
            ax_heat.set_xticklabels([f"{t*100:.1f}%" for t in tg_range], color="#cccccc", fontsize=8)
            ax_heat.set_yticklabels([f"{w*100:.1f}%" for w in wacc_range], color="#cccccc", fontsize=8)
            ax_heat.set_xlabel("Terminal Growth Rate", color="#cccccc")
            ax_heat.set_ylabel("WACC (Discount Rate)", color="#cccccc")
            ax_heat.set_title("Sensitivity: Enterprise Value ($M)", color="white", fontsize=11)

            for i in range(len(wacc_range)):
                for j in range(len(tg_range)):
                    val = sensitivity[i, j]
                    highlight = (i == 4 and j == 2)
                    ax_heat.text(j, i, f"${val:.0f}M",
                                 ha="center", va="center", fontsize=7,
                                 color="black" if not highlight else "white",
                                 fontweight="bold" if highlight else "normal",
                                 bbox=dict(boxstyle="round,pad=0.1", facecolor="white" if highlight else "none", alpha=0.3 if highlight else 0))

            plt.colorbar(im, ax=ax_heat, shrink=0.7)
            fig.suptitle(f"DCF Valuation Model{' — ' + ticker.upper() if ticker else ''}", color="white", fontsize=13)
            plt.tight_layout()
            chart_md = _fig_to_base64(fig)

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        terminal_pct = pv_terminal / total_npv * 100 if total_npv else 0
        lines = [f"## DCF Valuation{' — ' + ticker.upper() if ticker else ''}\n"]
        lines.append(f"| | Value |")
        lines.append(f"|--|--|")
        lines.append(f"| PV of FCFs (Y1–Y{years}) | ${pv_fcfs_sum:,.0f}M |")
        lines.append(f"| Terminal Value (PV) | ${pv_terminal:,.0f}M ({terminal_pct:.0f}% of total) |")
        lines.append(f"| **Enterprise Value** | **${total_npv:,.0f}M** |")
        if per_share:
            lines.append(f"| Intrinsic Value / Share | **${per_share:,.2f}** |")
        if current_price and per_share:
            upside = (per_share / current_price - 1) * 100
            direction = "upside" if upside >= 0 else "downside"
            lines.append(f"| Current Market Price | ${current_price:.2f} ({abs(upside):.1f}% {direction}) |")
        lines.append(f"\n**Assumptions:** WACC {discount_rate*100:.1f}% | Growth {growth_rate*100:.1f}% | Terminal Growth {terminal_growth*100:.1f}% | {years}-year projection\n")

        return "\n".join(lines) + "\n" + chart_md

    async def monte_carlo_simulation(
        self,
        ticker: str,
        simulations: int = 500,
        days: int = 252,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Run a Monte Carlo stock price simulation using Geometric Brownian Motion (GBM) based on historical volatility.
        :param ticker: Stock ticker symbol (e.g. 'AAPL', 'TSLA', 'SPY', 'BTC-USD')
        :param simulations: Number of simulation paths (100–2000, default 500)
        :param days: Number of trading days to simulate (21=1mo, 63=3mo, 126=6mo, 252=1yr)
        :return: Fan chart showing simulation percentiles with probability distribution
        """
        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Fetching {ticker} historical data...", "done": False}})

        simulations = max(100, min(2000, simulations))
        days = max(21, min(504, days))

        try:
            closes = await _fetch_closes(ticker, "2y")
        except Exception as e:
            return f"Failed to fetch {ticker} data: {e}"

        if len(closes) < 30:
            return f"Insufficient historical data for {ticker}."

        closes_arr = np.array(closes)
        log_returns = np.diff(np.log(closes_arr))
        mu = log_returns.mean()
        sigma = log_returns.std()
        S0 = closes_arr[-1]

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Running {simulations} simulations...", "done": False}})

        np.random.seed(42)
        dt = 1.0
        drift = (mu - 0.5 * sigma ** 2) * dt
        shock = sigma * math.sqrt(dt)

        all_paths = np.zeros((simulations, days + 1))
        all_paths[:, 0] = S0

        for t in range(1, days + 1):
            z = np.random.standard_normal(simulations)
            all_paths[:, t] = all_paths[:, t - 1] * np.exp(drift + shock * z)

        final_prices = all_paths[:, -1]
        p5 = np.percentile(final_prices, 5)
        p25 = np.percentile(final_prices, 25)
        p50 = np.percentile(final_prices, 50)
        p75 = np.percentile(final_prices, 75)
        p95 = np.percentile(final_prices, 95)
        prob_profit = np.mean(final_prices > S0) * 100

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Rendering simulation chart...", "done": False}})

        with plt.style.context("dark_background"):
            fig = plt.figure(figsize=(14, 6), facecolor="#1a1a2e")
            gs = gridspec.GridSpec(1, 2, width_ratios=[2, 1])
            ax_fan = fig.add_subplot(gs[0])
            ax_hist = fig.add_subplot(gs[1])

            for ax in [ax_fan, ax_hist]:
                ax.set_facecolor("#1a1a2e")

            time_axis = np.arange(days + 1)

            pct_paths = np.percentile(all_paths, [5, 25, 50, 75, 95], axis=0)

            ax_fan.fill_between(time_axis, pct_paths[0], pct_paths[4], alpha=0.15, color="#42a5f5", label="5–95th pct")
            ax_fan.fill_between(time_axis, pct_paths[1], pct_paths[3], alpha=0.25, color="#42a5f5", label="25–75th pct")
            ax_fan.plot(time_axis, pct_paths[2], color="#f59e0b", linewidth=2, label="Median")
            ax_fan.plot(time_axis, pct_paths[0], color="#ef5350", linewidth=1, linestyle="--", alpha=0.7)
            ax_fan.plot(time_axis, pct_paths[4], color="#26a69a", linewidth=1, linestyle="--", alpha=0.7)
            ax_fan.axhline(S0, color="#666666", linewidth=1, linestyle=":", alpha=0.8, label=f"Current ${S0:.2f}")

            ax_fan.set_title(f"{ticker.upper()} — Monte Carlo ({simulations} paths, {days}d)", color="white", fontsize=11)
            ax_fan.set_xlabel("Trading Days", color="#cccccc")
            ax_fan.set_ylabel("Price ($)", color="#cccccc")
            ax_fan.legend(framealpha=0.3, labelcolor="white", fontsize=8)
            ax_fan.tick_params(colors="#cccccc")
            ax_fan.grid(alpha=0.15)

            n, bins, patches = ax_hist.hist(final_prices, bins=50, orientation="horizontal",
                                             color="#42a5f5", alpha=0.7, edgecolor="#1a1a2e")
            for patch, bin_left in zip(patches, bins):
                if bin_left < S0:
                    patch.set_facecolor("#ef5350")

            ax_hist.axhline(S0, color="#666666", linewidth=1.5, linestyle=":", label=f"Entry ${S0:.2f}")
            ax_hist.axhline(p50, color="#f59e0b", linewidth=1.5, linestyle="--", label=f"Median ${p50:.2f}")
            ax_hist.set_title(f"Final Price Distribution", color="white", fontsize=11)
            ax_hist.set_xlabel("Frequency", color="#cccccc")
            ax_hist.tick_params(colors="#cccccc")
            ax_hist.legend(framealpha=0.3, labelcolor="white", fontsize=8)
            ax_hist.grid(alpha=0.15)

            fig.suptitle(f"Monte Carlo Simulation: {ticker.upper()} — {days} Trading Days", color="white", fontsize=12)
            plt.tight_layout()
            chart_md = _fig_to_base64(fig)

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        annual_vol = sigma * math.sqrt(252) * 100
        period_label = f"{days//21}mo" if days % 21 == 0 else f"{days}d"

        return (
            f"## Monte Carlo Simulation: {ticker.upper()}\n\n"
            f"**Model:** Geometric Brownian Motion | **Paths:** {simulations:,} | **Horizon:** {days} trading days\n"
            f"**Historical annual volatility:** {annual_vol:.1f}% | **Daily drift (μ):** {mu*100:.3f}%\n\n"
            f"| Percentile | Price | Return |"
            f"\n|--|--|--|"
            f"\n| 5th (bear) | ${p5:.2f} | {(p5/S0-1)*100:+.1f}% |"
            f"\n| 25th | ${p25:.2f} | {(p25/S0-1)*100:+.1f}% |"
            f"\n| 50th (median) | **${p50:.2f}** | **{(p50/S0-1)*100:+.1f}%** |"
            f"\n| 75th | ${p75:.2f} | {(p75/S0-1)*100:+.1f}% |"
            f"\n| 95th (bull) | ${p95:.2f} | {(p95/S0-1)*100:+.1f}% |"
            f"\n\n**Probability of profit:** {prob_profit:.1f}%\n\n"
        ) + chart_md

    async def portfolio_efficient_frontier(
        self,
        tickers: str,
        period: str = "3y",
        num_portfolios: int = 3000,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Build a Markowitz Efficient Frontier for a set of assets, showing optimal portfolio weights for maximum Sharpe ratio.
        :param tickers: Comma-separated tickers (e.g. 'AAPL,MSFT,JPM,GLD,TLT,BTC-USD') — 2 to 8 assets
        :param period: Historical data period: '1y', '2y', '3y', '5y'
        :param num_portfolios: Random portfolios to simulate (1000–10000, default 3000)
        :return: Efficient frontier scatter plot with optimal portfolio highlighted and weights table
        """
        if not HAS_PANDAS:
            return "Error: pandas not available."

        ticker_list = [t.strip().upper() for t in tickers.split(",") if t.strip()][:8]
        if len(ticker_list) < 2:
            return "Please provide at least 2 tickers."

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Fetching historical prices...", "done": False}})

        closes_dict = {}
        for t in ticker_list:
            try:
                closes = await _fetch_closes(t, period)
                if closes and len(closes) > 50:
                    closes_dict[t] = closes
            except Exception:
                pass

        if len(closes_dict) < 2:
            return "Could not fetch enough data. Check ticker symbols."

        min_len = min(len(v) for v in closes_dict.values())
        df = pd.DataFrame({t: v[-min_len:] for t, v in closes_dict.items()})
        returns = df.pct_change().dropna()

        mean_returns = returns.mean() * 252
        cov_matrix = returns.cov() * 252
        n_assets = len(closes_dict)
        tickers_valid = list(closes_dict.keys())
        rf = self.valves.RISK_FREE_RATE

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Simulating {num_portfolios} portfolios...", "done": False}})

        num_portfolios = max(1000, min(10000, num_portfolios))
        np.random.seed(42)
        port_returns = []
        port_vols = []
        port_weights = []
        port_sharpes = []

        for _ in range(num_portfolios):
            w = np.random.random(n_assets)
            w /= w.sum()
            r = np.dot(w, mean_returns.values)
            v = math.sqrt(np.dot(w, np.dot(cov_matrix.values, w)))
            s = (r - rf) / v if v > 0 else 0
            port_returns.append(r)
            port_vols.append(v)
            port_sharpes.append(s)
            port_weights.append(w)

        port_returns = np.array(port_returns)
        port_vols = np.array(port_vols)
        port_sharpes = np.array(port_sharpes)

        max_sharpe_idx = np.argmax(port_sharpes)
        min_vol_idx = np.argmin(port_vols)

        opt_weights = port_weights[max_sharpe_idx]
        opt_return = port_returns[max_sharpe_idx]
        opt_vol = port_vols[max_sharpe_idx]
        opt_sharpe = port_sharpes[max_sharpe_idx]

        mv_weights = port_weights[min_vol_idx]
        mv_return = port_returns[min_vol_idx]
        mv_vol = port_vols[min_vol_idx]

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Rendering efficient frontier...", "done": False}})

        with plt.style.context("dark_background"):
            fig, ax = plt.subplots(figsize=(11, 7), facecolor="#1a1a2e")
            ax.set_facecolor("#1a1a2e")

            sc = ax.scatter(port_vols * 100, port_returns * 100, c=port_sharpes, cmap="viridis",
                            alpha=0.4, s=4, linewidths=0)
            plt.colorbar(sc, ax=ax, label="Sharpe Ratio", shrink=0.8)

            ax.scatter(opt_vol * 100, opt_return * 100, color="#f59e0b", s=200, zorder=5,
                       marker="*", label=f"Max Sharpe ({opt_sharpe:.2f})", edgecolors="white", linewidths=0.5)
            ax.scatter(mv_vol * 100, mv_return * 100, color="#26a69a", s=150, zorder=5,
                       marker="D", label=f"Min Volatility", edgecolors="white", linewidths=0.5)

            for t, wr, wv in zip(tickers_valid,
                                  mean_returns.values * 100,
                                  np.sqrt(np.diag(cov_matrix.values)) * 100):
                ax.scatter(wv, wr, s=80, zorder=4, edgecolors="white", linewidths=0.7)
                ax.annotate(t, (wv, wr), textcoords="offset points", xytext=(5, 3),
                            color="white", fontsize=8, alpha=0.9)

            ax.set_xlabel("Annual Volatility (%)", color="#cccccc")
            ax.set_ylabel("Expected Annual Return (%)", color="#cccccc")
            ax.set_title(f"Markowitz Efficient Frontier — {', '.join(tickers_valid)}", color="white", fontsize=12)
            ax.legend(framealpha=0.3, labelcolor="white", loc="upper left")
            ax.tick_params(colors="#cccccc")
            ax.grid(alpha=0.15)
            plt.tight_layout()
            chart_md = _fig_to_base64(fig)

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        lines = ["## Efficient Frontier — Optimal Portfolio (Max Sharpe)\n"]
        lines.append(f"**Expected Return:** {opt_return*100:.1f}% | **Volatility:** {opt_vol*100:.1f}% | **Sharpe:** {opt_sharpe:.2f}\n")
        lines.append("| Asset | Weight |")
        lines.append("|-------|--------|")
        for t, w in sorted(zip(tickers_valid, opt_weights), key=lambda x: -x[1]):
            lines.append(f"| {t} | {w*100:.1f}% |")
        lines.append(f"\n**Minimum Volatility Portfolio:** Return {mv_return*100:.1f}% | Vol {mv_vol*100:.1f}%")
        lines.append("| Asset | Weight |")
        lines.append("|-------|--------|")
        for t, w in sorted(zip(tickers_valid, mv_weights), key=lambda x: -x[1]):
            lines.append(f"| {t} | {w*100:.1f}% |")
        lines.append(f"\n_Risk-free rate used: {rf*100:.1f}% | Period: {period}_\n")

        return "\n".join(lines) + "\n" + chart_md

    async def scenario_analysis(
        self,
        ticker: str,
        bear_return: float = -0.30,
        base_return: float = 0.10,
        bull_return: float = 0.35,
        bear_prob: float = 0.25,
        base_prob: float = 0.50,
        bull_prob: float = 0.25,
        investment: float = 10000,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Run a bear/base/bull scenario analysis for a stock investment with probability-weighted expected value.
        :param ticker: Stock ticker symbol (e.g. 'AAPL', 'MSFT', 'SPY')
        :param bear_return: Bear scenario return (e.g. -0.30 = -30%)
        :param base_return: Base scenario return (e.g. 0.10 = 10%)
        :param bull_return: Bull scenario return (e.g. 0.35 = 35%)
        :param bear_prob: Bear scenario probability (e.g. 0.25 = 25%)
        :param base_prob: Base scenario probability (e.g. 0.50 = 50%)
        :param bull_prob: Bull scenario probability (e.g. 0.25 = 25%)
        :param investment: Dollar amount to invest (for P&L calculation)
        :return: Scenario comparison bar chart with expected value and probability-weighted returns
        """
        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Fetching {ticker} current price...", "done": False}})

        total_prob = bear_prob + base_prob + bull_prob
        if abs(total_prob - 1.0) > 0.01:
            bear_prob /= total_prob
            base_prob /= total_prob
            bull_prob /= total_prob

        current_price = None
        try:
            closes = await _fetch_closes(ticker, "5d")
            if closes:
                current_price = closes[-1]
        except Exception:
            pass

        scenarios = [
            ("Bear", bear_return, bear_prob, "#ef5350"),
            ("Base", base_return, base_prob, "#42a5f5"),
            ("Bull", bull_return, bull_prob, "#26a69a"),
        ]

        expected_return = sum(r * p for _, r, p, _ in scenarios)
        expected_value = investment * (1 + expected_return)
        pnl = expected_value - investment

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Rendering scenario chart...", "done": False}})

        with plt.style.context("dark_background"):
            fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 6), facecolor="#1a1a2e")
            ax1.set_facecolor("#1a1a2e")
            ax2.set_facecolor("#1a1a2e")

            labels = [s[0] for s in scenarios]
            returns = [s[1] * 100 for s in scenarios]
            probs = [s[2] * 100 for s in scenarios]
            colors = [s[3] for s in scenarios]
            pl_vals = [investment * s[1] for s in scenarios]

            x = np.arange(len(labels))
            width = 0.35
            bars1 = ax1.bar(x - width / 2, returns, width, label="Return (%)", color=colors, alpha=0.85, edgecolor="#333")
            bars2 = ax1.bar(x + width / 2, probs, width, label="Probability (%)", color=colors, alpha=0.45, edgecolor="#333", hatch="//")

            ax1.axhline(expected_return * 100, color="#f59e0b", linewidth=1.5, linestyle="--",
                        label=f"Expected Return: {expected_return*100:+.1f}%")
            ax1.axhline(0, color="#666666", linewidth=0.6)

            for bar, val in zip(bars1, returns):
                ax1.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.5,
                         f"{val:+.0f}%", ha="center", va="bottom", color="white", fontsize=9, fontweight="bold")

            ax1.set_xticks(x)
            ax1.set_xticklabels(labels, color="white")
            ax1.set_ylabel("Return (%) / Probability (%)", color="#cccccc")
            ax1.set_title(f"{ticker.upper()} — Scenario Returns & Probabilities", color="white")
            ax1.legend(framealpha=0.3, labelcolor="white", fontsize=8)
            ax1.tick_params(colors="#cccccc")
            ax1.grid(alpha=0.15, axis="y")

            pl_colors = ["#ef5350" if v < 0 else "#26a69a" for v in pl_vals]
            bars3 = ax2.bar(labels, pl_vals, color=pl_colors, alpha=0.85, edgecolor="#333")
            ax2.axhline(pnl, color="#f59e0b", linewidth=1.5, linestyle="--",
                        label=f"Expected P&L: ${pnl:+,.0f}")
            ax2.axhline(0, color="#999999", linewidth=0.8)

            for bar, val in zip(bars3, pl_vals):
                y_pos = bar.get_height() + (max(pl_vals) - min(pl_vals)) * 0.02
                if val < 0:
                    y_pos = bar.get_height() - (max(pl_vals) - min(pl_vals)) * 0.05
                ax2.text(bar.get_x() + bar.get_width() / 2, y_pos,
                         f"${val:+,.0f}", ha="center", va="bottom", color="white", fontsize=9, fontweight="bold")

            ax2.set_ylabel(f"P&L (${investment:,.0f} investment)", color="#cccccc")
            ax2.set_title("Profit / Loss by Scenario", color="white")
            ax2.legend(framealpha=0.3, labelcolor="white", fontsize=8)
            ax2.tick_params(colors="#cccccc")
            ax2.grid(alpha=0.15, axis="y")

            if current_price:
                fig.suptitle(f"{ticker.upper()} Scenario Analysis | Current: ${current_price:.2f}", color="white", fontsize=12)
            else:
                fig.suptitle(f"{ticker.upper()} — Scenario Analysis", color="white", fontsize=12)

            plt.tight_layout()
            chart_md = _fig_to_base64(fig)

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        lines = [f"## {ticker.upper()} Scenario Analysis\n"]
        if current_price:
            lines.append(f"**Current Price:** ${current_price:.2f} | **Investment:** ${investment:,.0f}\n")
        lines.append("| Scenario | Return | Probability | P&L |")
        lines.append("|----------|--------|------------|-----|")
        for name, ret, prob, _ in scenarios:
            lines.append(f"| {name} | {ret*100:+.0f}% | {prob*100:.0f}% | ${investment*ret:+,.0f} |")
        lines.append(f"| **Expected Value** | **{expected_return*100:+.1f}%** | 100% | **${pnl:+,.0f}** |")
        lines.append(f"\n**Expected portfolio value:** ${expected_value:,.0f}\n")

        return "\n".join(lines) + "\n" + chart_md
