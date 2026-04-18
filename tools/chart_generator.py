"""
title: Financial Chart Generator — Price History, Candlesticks & Technical Charts
author: local-ai-stack
description: Generate beautiful financial charts embedded directly in chat. Plot price history (line or candlestick), compare multiple assets normalized to the same start, overlay RSI/MACD/Bollinger Bands as subplots, and visualize return correlations as a heatmap. All charts rendered server-side and returned as inline images. No API key required.
required_open_webui_version: 0.4.0
requirements: httpx matplotlib mplfinance pandas numpy
version: 1.0.0
licence: MIT
"""

import io
import base64
import asyncio
from datetime import datetime, timedelta
from typing import Callable, Any, Optional
from pydantic import BaseModel, Field

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.gridspec as gridspec
import numpy as np

try:
    import pandas as pd
    import mplfinance as mpf
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False

import httpx

YAHOO_BASE = "https://query1.finance.yahoo.com"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

PERIOD_MAP = {
    "1d": ("1d", "1m"), "5d": ("5d", "5m"), "1mo": ("1mo", "1d"),
    "3mo": ("3mo", "1d"), "6mo": ("6mo", "1d"), "1y": ("1y", "1d"),
    "2y": ("2y", "1wk"), "5y": ("5y", "1wk"), "10y": ("10y", "1mo"), "max": ("max", "1mo"),
}


def _fig_to_base64(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor=fig.get_facecolor())
    buf.seek(0)
    encoded = base64.b64encode(buf.read()).decode("utf-8")
    plt.close(fig)
    return f"![chart](data:image/png;base64,{encoded})"


async def _fetch_ohlcv(ticker: str, period: str = "1y", interval: str = "1d") -> dict:
    url = f"{YAHOO_BASE}/v8/finance/chart/{ticker.upper()}"
    params = {"period1": 0, "period2": 9999999999, "interval": interval, "range": period,
              "includePrePost": "false"}
    async with httpx.AsyncClient(timeout=20, headers=HEADERS) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()


def _parse_ohlcv(data: dict) -> "pd.DataFrame | None":
    if not HAS_PANDAS:
        return None
    result = data.get("chart", {}).get("result", [])
    if not result:
        return None
    r = result[0]
    timestamps = r.get("timestamp", [])
    q = r.get("indicators", {}).get("quote", [{}])[0]
    opens = q.get("open", [])
    highs = q.get("high", [])
    lows = q.get("low", [])
    closes = q.get("close", [])
    volumes = q.get("volume", [])

    rows = []
    for i, ts in enumerate(timestamps):
        if closes[i] is None:
            continue
        rows.append({
            "Date": pd.Timestamp(ts, unit="s"),
            "Open": opens[i] or closes[i],
            "High": highs[i] or closes[i],
            "Low": lows[i] or closes[i],
            "Close": closes[i],
            "Volume": volumes[i] or 0,
        })
    if not rows:
        return None
    df = pd.DataFrame(rows).set_index("Date")
    df.index = pd.DatetimeIndex(df.index)
    return df


class Tools:
    class Valves(BaseModel):
        CHART_STYLE: str = Field(
            default="dark_background",
            description="Matplotlib style: 'dark_background', 'seaborn-v0_8', 'ggplot', 'fivethirtyeight'",
        )

    def __init__(self):
        self.valves = self.Valves()

    async def plot_price_history(
        self,
        ticker: str,
        period: str = "1y",
        chart_type: str = "candlestick",
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Plot price history for a stock, ETF, crypto, or forex pair as a candlestick or line chart with volume bars.
        :param ticker: Ticker symbol (e.g. 'AAPL', 'BTC-USD', 'SPY', 'EURUSD=X', 'GC=F' for gold)
        :param period: Time period: '1mo', '3mo', '6mo', '1y', '2y', '5y', '10y', 'max'
        :param chart_type: 'candlestick' for OHLC bars or 'line' for closing price line
        :return: Embedded price chart image
        """
        if not HAS_PANDAS:
            return "Error: pandas not available. Run `pip install pandas mplfinance`."

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Fetching {ticker} price data...", "done": False}})

        period_key = period if period in PERIOD_MAP else "1y"
        range_val, interval = PERIOD_MAP[period_key]

        try:
            data = await _fetch_ohlcv(ticker, range_val, interval)
        except Exception as e:
            return f"Failed to fetch data for {ticker}: {e}"

        df = _parse_ohlcv(data)
        if df is None or df.empty:
            return f"No price data found for '{ticker}'."

        meta = data.get("chart", {}).get("result", [{}])[0].get("meta", {})
        currency = meta.get("currency", "USD")
        long_name = meta.get("longName") or meta.get("symbol", ticker)

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Rendering chart...", "done": False}})

        style = self.valves.CHART_STYLE

        if chart_type == "candlestick":
            mc = mpf.make_marketcolors(up="#26a69a", down="#ef5350", edge="inherit",
                                       wick="inherit", volume="inherit")
            s = mpf.make_mpf_style(base_mpf_style="nightclouds", marketcolors=mc,
                                   gridcolor="#333333", facecolor="#1a1a2e", figcolor="#1a1a2e",
                                   rc={"axes.labelcolor": "#cccccc", "xtick.color": "#cccccc",
                                       "ytick.color": "#cccccc", "axes.titlecolor": "#ffffff"})

            add_plots = [mpf.make_addplot(
                df["Close"].rolling(20).mean(), color="#f59e0b", width=1.2, label="SMA20"
            )]
            try:
                buf = io.BytesIO()
                mpf.plot(df, type="candle", style=s, volume=True, addplot=add_plots,
                         title=f"\n{long_name} ({ticker.upper()}) — {period}", ylabel=f"Price ({currency})",
                         ylabel_lower="Volume", figsize=(12, 7), savefig=dict(fname=buf, dpi=130, bbox_inches="tight"))
                buf.seek(0)
                encoded = base64.b64encode(buf.read()).decode("utf-8")
                chart_md = f"![chart](data:image/png;base64,{encoded})"
            except Exception as e:
                chart_type = "line"

        if chart_type == "line":
            with plt.style.context(style):
                fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 7), gridspec_kw={"height_ratios": [3, 1]},
                                               facecolor="#1a1a2e")
                ax1.set_facecolor("#1a1a2e")
                ax2.set_facecolor("#1a1a2e")

                closes = df["Close"].values
                dates_num = mdates.date2num(df.index.to_pydatetime())

                ax1.plot(df.index, closes, color="#26a69a", linewidth=1.5, label="Close")
                sma20 = pd.Series(closes).rolling(20).mean().values
                ax1.plot(df.index, sma20, color="#f59e0b", linewidth=1, linestyle="--", label="SMA 20", alpha=0.8)

                ax1.fill_between(df.index, closes, alpha=0.1, color="#26a69a")
                ax1.set_title(f"{long_name} ({ticker.upper()}) — {period}", color="white", fontsize=13)
                ax1.set_ylabel(f"Price ({currency})", color="#cccccc")
                ax1.legend(framealpha=0.3, labelcolor="white")
                ax1.tick_params(colors="#cccccc")
                ax1.grid(alpha=0.2)

                volumes = df["Volume"].values
                colors = ["#26a69a" if c >= o else "#ef5350"
                          for c, o in zip(df["Close"].values, df["Open"].values)]
                ax2.bar(df.index, volumes, color=colors, alpha=0.7, width=0.8)
                ax2.set_ylabel("Volume", color="#cccccc")
                ax2.tick_params(colors="#cccccc")
                ax2.grid(alpha=0.2)

                fig.tight_layout()
                chart_md = _fig_to_base64(fig)

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        pct = ((df["Close"].iloc[-1] / df["Close"].iloc[0]) - 1) * 100
        direction = "▲" if pct >= 0 else "▼"
        latest = df["Close"].iloc[-1]
        high = df["High"].max()
        low = df["Low"].min()

        summary = (
            f"**{long_name}** | Period: {period}\n"
            f"Latest: **{latest:.2f} {currency}** | Period return: {direction} {abs(pct):.1f}% "
            f"| High: {high:.2f} | Low: {low:.2f}\n\n"
        )
        return summary + chart_md

    async def plot_comparison(
        self,
        tickers: str,
        period: str = "1y",
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Compare multiple assets normalized to 100 at the start, showing relative performance over time.
        :param tickers: Comma-separated ticker symbols (e.g. 'AAPL,MSFT,GOOGL,AMZN' or 'SPY,QQQ,IWM')
        :param period: Time period: '1mo', '3mo', '6mo', '1y', '2y', '5y'
        :return: Normalized performance comparison chart
        """
        if not HAS_PANDAS:
            return "Error: pandas not available."

        ticker_list = [t.strip().upper() for t in tickers.split(",") if t.strip()][:8]
        if not ticker_list:
            return "Please provide at least one ticker symbol."

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Fetching data for {len(ticker_list)} assets...", "done": False}})

        period_key = period if period in PERIOD_MAP else "1y"
        range_val, interval = PERIOD_MAP[period_key]

        series_dict = {}
        errors = []
        for t in ticker_list:
            try:
                data = await _fetch_ohlcv(t, range_val, interval)
                df = _parse_ohlcv(data)
                if df is not None and not df.empty:
                    series_dict[t] = df["Close"]
            except Exception as e:
                errors.append(f"{t}: {e}")

        if not series_dict:
            return f"No data retrieved. Errors: {'; '.join(errors)}"

        colors = ["#26a69a", "#ef5350", "#f59e0b", "#42a5f5", "#ab47bc",
                  "#66bb6a", "#ff7043", "#26c6da"]

        with plt.style.context("dark_background"):
            fig, ax = plt.subplots(figsize=(12, 6), facecolor="#1a1a2e")
            ax.set_facecolor("#1a1a2e")

            returns_summary = []
            for i, (t, series) in enumerate(series_dict.items()):
                normalized = (series / series.dropna().iloc[0]) * 100
                color = colors[i % len(colors)]
                pct = (normalized.dropna().iloc[-1] - 100)
                label = f"{t} ({'+' if pct >= 0 else ''}{pct:.1f}%)"
                ax.plot(normalized.index, normalized.values, color=color, linewidth=1.8, label=label)
                returns_summary.append((t, pct))

            ax.axhline(100, color="#666666", linewidth=0.8, linestyle="--", alpha=0.5)
            ax.set_title(f"Normalized Performance Comparison — {period}", color="white", fontsize=13)
            ax.set_ylabel("Indexed Return (Base = 100)", color="#cccccc")
            ax.legend(framealpha=0.3, labelcolor="white", loc="upper left")
            ax.tick_params(colors="#cccccc")
            ax.grid(alpha=0.2)
            fig.tight_layout()

            chart_md = _fig_to_base64(fig)

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        returns_summary.sort(key=lambda x: -x[1])
        ranking = " | ".join(f"**{t}**: {'+' if r >= 0 else ''}{r:.1f}%" for t, r in returns_summary)

        return f"**Returns ({period}):** {ranking}\n\n" + chart_md

    async def plot_technical_analysis(
        self,
        ticker: str,
        period: str = "6mo",
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Plot a full technical analysis chart with price/SMA/Bollinger Bands, RSI, and MACD as separate panels.
        :param ticker: Stock ticker symbol (e.g. 'AAPL', 'TSLA', 'SPY')
        :param period: Time period: '3mo', '6mo', '1y', '2y'
        :return: Multi-panel technical analysis chart
        """
        if not HAS_PANDAS:
            return "Error: pandas not available."

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Fetching {ticker} for technical analysis...", "done": False}})

        period_key = period if period in PERIOD_MAP else "6mo"
        range_val, interval = PERIOD_MAP[period_key]

        try:
            data = await _fetch_ohlcv(ticker, range_val, interval)
        except Exception as e:
            return f"Failed to fetch data: {e}"

        df = _parse_ohlcv(data)
        if df is None or df.empty:
            return f"No data for '{ticker}'."

        meta = data.get("chart", {}).get("result", [{}])[0].get("meta", {})
        long_name = meta.get("longName") or ticker.upper()

        closes = df["Close"]
        sma20 = closes.rolling(20).mean()
        sma50 = closes.rolling(50).mean()
        std20 = closes.rolling(20).std()
        bb_upper = sma20 + 2 * std20
        bb_lower = sma20 - 2 * std20

        delta = closes.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.replace(0, float("nan"))
        rsi = 100 - 100 / (1 + rs)

        ema12 = closes.ewm(span=12, adjust=False).mean()
        ema26 = closes.ewm(span=26, adjust=False).mean()
        macd_line = ema12 - ema26
        signal_line = macd_line.ewm(span=9, adjust=False).mean()
        histogram = macd_line - signal_line

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Rendering chart...", "done": False}})

        with plt.style.context("dark_background"):
            fig = plt.figure(figsize=(13, 10), facecolor="#1a1a2e")
            gs = gridspec.GridSpec(4, 1, height_ratios=[3, 1, 1, 1], hspace=0.08)

            ax_price = fig.add_subplot(gs[0])
            ax_vol = fig.add_subplot(gs[1], sharex=ax_price)
            ax_rsi = fig.add_subplot(gs[2], sharex=ax_price)
            ax_macd = fig.add_subplot(gs[3], sharex=ax_price)

            for ax in [ax_price, ax_vol, ax_rsi, ax_macd]:
                ax.set_facecolor("#1a1a2e")

            ax_price.plot(df.index, closes, color="#26a69a", linewidth=1.5, label="Close")
            ax_price.plot(df.index, sma20, color="#f59e0b", linewidth=1, linestyle="--", label="SMA 20", alpha=0.8)
            ax_price.plot(df.index, sma50, color="#42a5f5", linewidth=1, linestyle="--", label="SMA 50", alpha=0.8)
            ax_price.fill_between(df.index, bb_upper, bb_lower, alpha=0.1, color="#ab47bc", label="BB Bands")
            ax_price.plot(df.index, bb_upper, color="#ab47bc", linewidth=0.7, linestyle=":")
            ax_price.plot(df.index, bb_lower, color="#ab47bc", linewidth=0.7, linestyle=":")
            ax_price.set_title(f"{long_name} ({ticker.upper()}) — Technical Analysis ({period})", color="white", fontsize=12)
            ax_price.legend(framealpha=0.3, labelcolor="white", fontsize=8, loc="upper left")
            ax_price.set_ylabel("Price", color="#cccccc")
            ax_price.grid(alpha=0.15)
            ax_price.tick_params(labelbottom=False, colors="#cccccc")

            colors = ["#26a69a" if c >= o else "#ef5350"
                      for c, o in zip(df["Close"].values, df["Open"].values)]
            ax_vol.bar(df.index, df["Volume"], color=colors, alpha=0.6)
            ax_vol.set_ylabel("Volume", color="#cccccc", fontsize=8)
            ax_vol.grid(alpha=0.15)
            ax_vol.tick_params(labelbottom=False, colors="#cccccc")

            ax_rsi.plot(df.index, rsi, color="#f59e0b", linewidth=1.2)
            ax_rsi.axhline(70, color="#ef5350", linewidth=0.8, linestyle="--", alpha=0.7)
            ax_rsi.axhline(30, color="#26a69a", linewidth=0.8, linestyle="--", alpha=0.7)
            ax_rsi.fill_between(df.index, rsi, 70, where=(rsi >= 70), alpha=0.2, color="#ef5350")
            ax_rsi.fill_between(df.index, rsi, 30, where=(rsi <= 30), alpha=0.2, color="#26a69a")
            ax_rsi.set_ylim(0, 100)
            ax_rsi.set_yticks([30, 50, 70])
            ax_rsi.set_ylabel("RSI", color="#cccccc", fontsize=8)
            ax_rsi.grid(alpha=0.15)
            ax_rsi.tick_params(labelbottom=False, colors="#cccccc")

            hist_colors = ["#26a69a" if v >= 0 else "#ef5350" for v in histogram.values]
            ax_macd.bar(df.index, histogram, color=hist_colors, alpha=0.6, width=1.0)
            ax_macd.plot(df.index, macd_line, color="#42a5f5", linewidth=1.2, label="MACD")
            ax_macd.plot(df.index, signal_line, color="#f59e0b", linewidth=1.0, label="Signal")
            ax_macd.axhline(0, color="#666666", linewidth=0.6)
            ax_macd.set_ylabel("MACD", color="#cccccc", fontsize=8)
            ax_macd.legend(framealpha=0.3, labelcolor="white", fontsize=7, loc="upper left")
            ax_macd.grid(alpha=0.15)
            ax_macd.tick_params(colors="#cccccc")

            plt.setp(ax_price.get_xticklabels(), visible=False)
            plt.setp(ax_vol.get_xticklabels(), visible=False)
            plt.setp(ax_rsi.get_xticklabels(), visible=False)

            chart_md = _fig_to_base64(fig)

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        latest_rsi = rsi.dropna().iloc[-1] if not rsi.dropna().empty else 0
        rsi_signal = "Overbought" if latest_rsi > 70 else ("Oversold" if latest_rsi < 30 else "Neutral")
        macd_signal = "Bullish crossover" if macd_line.iloc[-1] > signal_line.iloc[-1] else "Bearish crossover"

        summary = (
            f"**{ticker.upper()}** | RSI: **{latest_rsi:.1f}** ({rsi_signal}) | "
            f"MACD: **{macd_signal}** | SMA20: {sma20.iloc[-1]:.2f} | SMA50: {sma50.iloc[-1]:.2f}\n\n"
        )
        return summary + chart_md

    async def plot_correlation_matrix(
        self,
        tickers: str,
        period: str = "1y",
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Plot a correlation heatmap of daily returns for multiple assets — useful for portfolio diversification analysis.
        :param tickers: Comma-separated tickers (e.g. 'SPY,GLD,TLT,BTC-USD,DXY,VNQ')
        :param period: Time period: '6mo', '1y', '2y', '5y'
        :return: Correlation matrix heatmap
        """
        if not HAS_PANDAS:
            return "Error: pandas not available."

        ticker_list = [t.strip().upper() for t in tickers.split(",") if t.strip()][:10]
        if len(ticker_list) < 2:
            return "Please provide at least 2 tickers for correlation analysis."

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Fetching price data...", "done": False}})

        period_key = period if period in PERIOD_MAP else "1y"
        range_val, interval = PERIOD_MAP[period_key]

        series_dict = {}
        for t in ticker_list:
            try:
                data = await _fetch_ohlcv(t, range_val, interval)
                df = _parse_ohlcv(data)
                if df is not None and not df.empty:
                    series_dict[t] = df["Close"]
            except Exception:
                pass

        if len(series_dict) < 2:
            return "Could not retrieve data for enough tickers."

        combined = pd.DataFrame(series_dict).dropna()
        returns = combined.pct_change().dropna()
        corr = returns.corr()

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Rendering heatmap...", "done": False}})

        with plt.style.context("dark_background"):
            n = len(corr)
            fig, ax = plt.subplots(figsize=(max(6, n * 1.1), max(5, n * 1.0)), facecolor="#1a1a2e")
            ax.set_facecolor("#1a1a2e")

            cmap = plt.get_cmap("RdYlGn")
            im = ax.imshow(corr.values, cmap=cmap, vmin=-1, vmax=1, aspect="auto")

            ax.set_xticks(range(n))
            ax.set_yticks(range(n))
            ax.set_xticklabels(corr.columns, rotation=45, ha="right", color="white")
            ax.set_yticklabels(corr.columns, color="white")

            for i in range(n):
                for j in range(n):
                    val = corr.values[i, j]
                    text_color = "black" if abs(val) > 0.5 else "white"
                    ax.text(j, i, f"{val:.2f}", ha="center", va="center",
                            color=text_color, fontsize=10, fontweight="bold")

            plt.colorbar(im, ax=ax, shrink=0.8)
            ax.set_title(f"Return Correlation Matrix ({period})", color="white", fontsize=12, pad=10)
            fig.tight_layout()
            chart_md = _fig_to_base64(fig)

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        tickers_used = list(series_dict.keys())
        return (
            f"**Correlation Matrix** for {', '.join(tickers_used)} over {period}.\n"
            f"Values range from -1 (perfect inverse) to +1 (perfect positive). "
            f"Near 0 = uncorrelated (good for diversification).\n\n"
        ) + chart_md
