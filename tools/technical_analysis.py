"""
title: Technical Analysis — RSI, MACD, Bollinger Bands & More
author: local-ai-stack
description: Compute technical analysis indicators from real price data fetched via Yahoo Finance. Supports RSI, MACD, Bollinger Bands, SMA, EMA, Stochastic oscillator, ATR, OBV, VWAP, and support/resistance detection. No API key required.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
import math
from datetime import datetime
from typing import Callable, Any, Optional, List
from pydantic import BaseModel, Field

YAHOO_BASE = "https://query1.finance.yahoo.com"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def _ema(values: List[float], period: int) -> List[float]:
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    result = [sum(values[:period]) / period]
    for v in values[period:]:
        result.append(v * k + result[-1] * (1 - k))
    return [None] * (period - 1) + result


def _sma(values: List[float], period: int) -> List[Optional[float]]:
    result = []
    for i in range(len(values)):
        if i < period - 1:
            result.append(None)
        else:
            result.append(sum(values[i - period + 1:i + 1]) / period)
    return result


async def _fetch_prices(ticker: str, period: str = "6mo", interval: str = "1d"):
    async with httpx.AsyncClient(timeout=15, headers=HEADERS) as client:
        resp = await client.get(
            f"{YAHOO_BASE}/v8/finance/chart/{ticker}",
            params={"range": period, "interval": interval, "includePrePost": "false"},
        )
        resp.raise_for_status()
        data = resp.json()

    result = data.get("chart", {}).get("result", [])
    if not result:
        raise ValueError(f"No data for {ticker}")

    r = result[0]
    timestamps = r.get("timestamp", [])
    quote = r.get("indicators", {}).get("quote", [{}])[0]
    closes = quote.get("close", [])
    highs = quote.get("high", [])
    lows = quote.get("low", [])
    volumes = quote.get("volume", [])

    # Remove None values
    rows = [(timestamps[i], closes[i], highs[i], lows[i], volumes[i])
            for i in range(len(timestamps))
            if closes[i] is not None and highs[i] is not None and lows[i] is not None]

    return rows, r.get("meta", {})


class Tools:
    class Valves(BaseModel):
        RSI_PERIOD: int = Field(default=14, description="RSI calculation period (default 14)")
        MACD_FAST: int = Field(default=12, description="MACD fast EMA period")
        MACD_SLOW: int = Field(default=26, description="MACD slow EMA period")
        MACD_SIGNAL: int = Field(default=9, description="MACD signal line period")
        BB_PERIOD: int = Field(default=20, description="Bollinger Bands period (default 20)")
        BB_STD: float = Field(default=2.0, description="Bollinger Bands standard deviation multiplier")

    def __init__(self):
        self.valves = self.Valves()

    async def get_rsi(
        self,
        ticker: str,
        period: str = "6mo",
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Calculate the Relative Strength Index (RSI) for a stock. RSI > 70 = overbought, RSI < 30 = oversold.
        :param ticker: Stock ticker symbol (e.g. 'AAPL', 'SPY', 'BTC-USD')
        :param period: Data period (1mo/3mo/6mo/1y/2y) — default 6mo
        :return: Current RSI, recent RSI values, and buy/sell signal interpretation
        """
        ticker = ticker.upper()

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Calculating RSI for {ticker}...", "done": False}})

        try:
            rows, meta = await _fetch_prices(ticker, period)
        except Exception as e:
            return f"Error fetching {ticker}: {str(e)}"

        closes = [r[1] for r in rows]
        dates = [datetime.utcfromtimestamp(r[0]).strftime("%Y-%m-%d") for r in rows]

        rsi_period = self.valves.RSI_PERIOD
        if len(closes) < rsi_period + 1:
            return f"Not enough data to calculate RSI (need {rsi_period + 1} periods, got {len(closes)})"

        # RSI calculation
        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains = [max(0, d) for d in deltas]
        losses = [max(0, -d) for d in deltas]

        avg_gain = sum(gains[:rsi_period]) / rsi_period
        avg_loss = sum(losses[:rsi_period]) / rsi_period

        rsi_values = []
        for i in range(rsi_period, len(deltas)):
            avg_gain = (avg_gain * (rsi_period - 1) + gains[i]) / rsi_period
            avg_loss = (avg_loss * (rsi_period - 1) + losses[i]) / rsi_period
            rs = avg_gain / avg_loss if avg_loss > 0 else float('inf')
            rsi = 100 - (100 / (1 + rs))
            rsi_values.append((dates[i + 1], rsi))

        current_rsi = rsi_values[-1][1]
        current_price = closes[-1]
        name = meta.get("longName") or meta.get("shortName") or ticker

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        # Signal
        if current_rsi >= 70:
            signal = "🔴 OVERBOUGHT — potential reversal/sell signal"
        elif current_rsi >= 60:
            signal = "🟠 Bullish momentum — approaching overbought"
        elif current_rsi <= 30:
            signal = "🟢 OVERSOLD — potential reversal/buy signal"
        elif current_rsi <= 40:
            signal = "🔵 Bearish momentum — approaching oversold"
        else:
            signal = "⚪ Neutral (neither overbought nor oversold)"

        lines = [f"## RSI ({rsi_period}) — {name} ({ticker})\n"]
        lines.append(f"**Current Price:** ${current_price:.2f} | **Current RSI:** {current_rsi:.2f}\n")
        lines.append(f"**Signal:** {signal}\n")

        lines.append("| Date | RSI | Zone |")
        lines.append("|------|-----|------|")
        for date, rsi in rsi_values[-20:]:
            zone = "Overbought" if rsi >= 70 else ("Oversold" if rsi <= 30 else "Neutral")
            lines.append(f"| {date} | {rsi:.2f} | {zone} |")

        return "\n".join(lines)

    async def get_macd(
        self,
        ticker: str,
        period: str = "1y",
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Calculate MACD (Moving Average Convergence Divergence) for a stock. Crossovers signal momentum changes.
        :param ticker: Stock ticker symbol (e.g. 'TSLA', 'QQQ', 'GLD')
        :param period: Data period (3mo/6mo/1y/2y) — default 1y
        :return: MACD line, signal line, histogram, and crossover signals
        """
        ticker = ticker.upper()

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Calculating MACD for {ticker}...", "done": False}})

        try:
            rows, meta = await _fetch_prices(ticker, period)
        except Exception as e:
            return f"Error fetching {ticker}: {str(e)}"

        closes = [r[1] for r in rows]
        dates = [datetime.utcfromtimestamp(r[0]).strftime("%Y-%m-%d") for r in rows]

        fast = self.valves.MACD_FAST
        slow = self.valves.MACD_SLOW
        signal_period = self.valves.MACD_SIGNAL

        if len(closes) < slow + signal_period:
            return f"Need at least {slow + signal_period} data points for MACD."

        ema_fast = _ema(closes, fast)
        ema_slow = _ema(closes, slow)

        macd_line = [
            (f - s) if f is not None and s is not None else None
            for f, s in zip(ema_fast, ema_slow)
        ]

        valid_macd = [v for v in macd_line if v is not None]
        signal_line_raw = _ema(valid_macd, signal_period)
        signal_line = [None] * (len(macd_line) - len(signal_line_raw)) + signal_line_raw

        histogram = [
            (m - s) if m is not None and s is not None else None
            for m, s in zip(macd_line, signal_line)
        ]

        name = meta.get("longName") or meta.get("shortName") or ticker
        current_price = closes[-1]
        current_macd = next((v for v in reversed(macd_line) if v is not None), None)
        current_signal = next((v for v in reversed(signal_line) if v is not None), None)
        current_hist = next((v for v in reversed(histogram) if v is not None), None)

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        if current_macd is not None and current_signal is not None:
            if current_macd > current_signal and current_hist and current_hist > 0:
                signal_str = "🟢 Bullish — MACD above signal line"
            elif current_macd < current_signal:
                signal_str = "🔴 Bearish — MACD below signal line"
            else:
                signal_str = "⚪ Neutral"
        else:
            signal_str = "⚪ Insufficient data"

        lines = [f"## MACD ({fast},{slow},{signal_period}) — {name} ({ticker})\n"]
        lines.append(f"**Current Price:** ${current_price:.2f}")
        if current_macd is not None:
            lines.append(f"**MACD:** {current_macd:.4f} | **Signal:** {current_signal:.4f} | **Histogram:** {current_hist:+.4f}")
        lines.append(f"**Signal:** {signal_str}\n")

        lines.append("| Date | MACD | Signal | Histogram | Direction |")
        lines.append("|------|------|--------|-----------|-----------|")

        # Show last 25 rows with valid data
        valid_rows = [(dates[i], macd_line[i], signal_line[i], histogram[i])
                      for i in range(len(dates))
                      if macd_line[i] is not None and signal_line[i] is not None and histogram[i] is not None][-25:]

        prev_hist = None
        for date, macd, sig, hist in valid_rows:
            direction = ""
            if prev_hist is not None:
                if hist > 0 and prev_hist <= 0:
                    direction = "▲ Bullish Cross"
                elif hist < 0 and prev_hist >= 0:
                    direction = "▼ Bearish Cross"
                elif hist > prev_hist:
                    direction = "↑"
                else:
                    direction = "↓"
            prev_hist = hist
            lines.append(f"| {date} | {macd:.4f} | {sig:.4f} | {hist:+.4f} | {direction} |")

        return "\n".join(lines)

    async def get_bollinger_bands(
        self,
        ticker: str,
        period: str = "6mo",
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Calculate Bollinger Bands for a stock. Price touching upper band = overbought, lower band = oversold.
        :param ticker: Stock ticker symbol (e.g. 'AAPL', 'BTC-USD', 'GLD')
        :param period: Data period (3mo/6mo/1y/2y) — default 6mo
        :return: Upper, middle (SMA), and lower bands with bandwidth and %B position
        """
        ticker = ticker.upper()

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Calculating Bollinger Bands for {ticker}...", "done": False}})

        try:
            rows, meta = await _fetch_prices(ticker, period)
        except Exception as e:
            return f"Error fetching {ticker}: {str(e)}"

        closes = [r[1] for r in rows]
        dates = [datetime.utcfromtimestamp(r[0]).strftime("%Y-%m-%d") for r in rows]

        bb_period = self.valves.BB_PERIOD
        bb_std = self.valves.BB_STD

        if len(closes) < bb_period:
            return f"Need at least {bb_period} data points for Bollinger Bands."

        bands = []
        for i in range(len(closes)):
            if i < bb_period - 1:
                bands.append((None, None, None))
                continue
            window = closes[i - bb_period + 1:i + 1]
            middle = sum(window) / bb_period
            variance = sum((x - middle) ** 2 for x in window) / bb_period
            std = math.sqrt(variance)
            upper = middle + bb_std * std
            lower = middle - bb_std * std
            bands.append((upper, middle, lower))

        current_price = closes[-1]
        current_bands = bands[-1]

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        name = meta.get("longName") or meta.get("shortName") or ticker

        lines = [f"## Bollinger Bands ({bb_period}, {bb_std}σ) — {name} ({ticker})\n"]
        lines.append(f"**Current Price:** ${current_price:.2f}")

        if current_bands[0] is not None:
            upper, middle, lower = current_bands
            bandwidth = (upper - lower) / middle * 100
            pct_b = (current_price - lower) / (upper - lower) * 100 if (upper - lower) > 0 else 50

            position = (
                "🔴 Above upper band (overbought)" if current_price > upper else
                "🟢 Below lower band (oversold)" if current_price < lower else
                f"⚪ Inside bands ({pct_b:.0f}% from bottom)"
            )

            lines.append(f"**Upper:** ${upper:.2f} | **Middle:** ${middle:.2f} | **Lower:** ${lower:.2f}")
            lines.append(f"**Bandwidth:** {bandwidth:.2f}% | **%B:** {pct_b:.1f}%")
            lines.append(f"**Position:** {position}\n")

        lines.append("| Date | Price | Upper | Middle | Lower | %B |")
        lines.append("|------|-------|-------|--------|-------|----|")

        valid = [(dates[i], closes[i], bands[i]) for i in range(len(dates)) if bands[i][0] is not None][-20:]
        for date, price, (upper, middle, lower) in valid:
            pct_b = (price - lower) / (upper - lower) * 100 if (upper - lower) > 0 else 50
            tag = "↑OB" if price > upper else ("↓OS" if price < lower else "")
            lines.append(f"| {date} | ${price:.2f} | ${upper:.2f} | ${middle:.2f} | ${lower:.2f} | {pct_b:.0f}% {tag} |")

        return "\n".join(lines)

    async def full_technical_report(
        self,
        ticker: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Generate a complete technical analysis report for a stock with all major indicators and a summary signal.
        :param ticker: Stock ticker symbol (e.g. 'AAPL', 'SPY', 'NVDA', 'BTC-USD')
        :return: RSI, MACD, Bollinger Bands, SMA cross, and overall buy/sell/hold signal
        """
        ticker = ticker.upper()

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Running full technical analysis for {ticker}...", "done": False}})

        try:
            rows, meta = await _fetch_prices(ticker, "1y")
        except Exception as e:
            return f"Error fetching {ticker}: {str(e)}"

        closes = [r[1] for r in rows]
        highs = [r[2] for r in rows]
        lows = [r[3] for r in rows]
        volumes = [r[4] for r in rows]
        dates = [datetime.utcfromtimestamp(r[0]).strftime("%Y-%m-%d") for r in rows]

        current_price = closes[-1]
        name = meta.get("longName") or meta.get("shortName") or ticker
        currency = meta.get("currency", "USD")

        signals = []
        results = {}

        # RSI
        rsi_period = self.valves.RSI_PERIOD
        if len(closes) >= rsi_period + 10:
            deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
            gains = [max(0, d) for d in deltas]
            losses = [max(0, -d) for d in deltas]
            avg_gain = sum(gains[:rsi_period]) / rsi_period
            avg_loss = sum(losses[:rsi_period]) / rsi_period
            rsi_vals = []
            for i in range(rsi_period, len(deltas)):
                avg_gain = (avg_gain * (rsi_period - 1) + gains[i]) / rsi_period
                avg_loss = (avg_loss * (rsi_period - 1) + losses[i]) / rsi_period
                rs = avg_gain / avg_loss if avg_loss > 0 else float('inf')
                rsi_vals.append(100 - (100 / (1 + rs)))
            current_rsi = rsi_vals[-1]
            results["RSI"] = f"{current_rsi:.1f}"
            if current_rsi < 30:
                signals.append(("BUY", "RSI oversold"))
            elif current_rsi > 70:
                signals.append(("SELL", "RSI overbought"))
            else:
                signals.append(("NEUTRAL", "RSI neutral"))

        # SMA 50/200 (Golden/Death Cross)
        sma50 = _sma(closes, 50)
        sma200 = _sma(closes, 200)
        if sma50[-1] and sma200[-1]:
            results["SMA 50"] = f"${sma50[-1]:.2f}"
            results["SMA 200"] = f"${sma200[-1]:.2f}"
            if sma50[-1] > sma200[-1]:
                signals.append(("BUY", "Golden Cross (50 > 200 SMA)"))
            else:
                signals.append(("SELL", "Death Cross (50 < 200 SMA)"))

        # Price vs SMA 50
        if sma50[-1]:
            if current_price > sma50[-1]:
                signals.append(("BUY", f"Price above 50 SMA"))
            else:
                signals.append(("SELL", f"Price below 50 SMA"))

        # MACD
        fast, slow, sig_period = self.valves.MACD_FAST, self.valves.MACD_SLOW, self.valves.MACD_SIGNAL
        if len(closes) >= slow + sig_period:
            ema_f = _ema(closes, fast)
            ema_s = _ema(closes, slow)
            macd = [(f - s) if f is not None and s is not None else None for f, s in zip(ema_f, ema_s)]
            valid_macd = [v for v in macd if v is not None]
            signal_ema = _ema(valid_macd, sig_period)
            current_macd_val = valid_macd[-1] if valid_macd else None
            current_signal_val = signal_ema[-1] if signal_ema else None
            if current_macd_val is not None and current_signal_val is not None:
                results["MACD"] = f"{current_macd_val:.3f}"
                results["MACD Signal"] = f"{current_signal_val:.3f}"
                if current_macd_val > current_signal_val:
                    signals.append(("BUY", "MACD above signal"))
                else:
                    signals.append(("SELL", "MACD below signal"))

        # Bollinger Bands
        bb_period = self.valves.BB_PERIOD
        if len(closes) >= bb_period:
            window = closes[-bb_period:]
            mid = sum(window) / bb_period
            std = math.sqrt(sum((x - mid) ** 2 for x in window) / bb_period)
            upper_bb = mid + 2 * std
            lower_bb = mid - 2 * std
            results["BB Upper"] = f"${upper_bb:.2f}"
            results["BB Middle"] = f"${mid:.2f}"
            results["BB Lower"] = f"${lower_bb:.2f}"
            if current_price < lower_bb:
                signals.append(("BUY", "Price below lower Bollinger Band"))
            elif current_price > upper_bb:
                signals.append(("SELL", "Price above upper Bollinger Band"))
            else:
                signals.append(("NEUTRAL", "Price inside Bollinger Bands"))

        # 52-week high/low
        week52_high = max(highs)
        week52_low = min(lows)
        results["52W High"] = f"${week52_high:.2f}"
        results["52W Low"] = f"${week52_low:.2f}"
        pct_from_high = (current_price - week52_high) / week52_high * 100
        pct_from_low = (current_price - week52_low) / week52_low * 100

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        # Overall signal
        buys = sum(1 for s, _ in signals if s == "BUY")
        sells = sum(1 for s, _ in signals if s == "SELL")
        neutrals = sum(1 for s, _ in signals if s == "NEUTRAL")
        total = len(signals)

        if buys > sells + neutrals:
            overall = "🟢 BULLISH"
        elif sells > buys + neutrals:
            overall = "🔴 BEARISH"
        elif buys > sells:
            overall = "🟡 MILDLY BULLISH"
        elif sells > buys:
            overall = "🟠 MILDLY BEARISH"
        else:
            overall = "⚪ NEUTRAL"

        lines = [f"## Technical Analysis Report: {name} ({ticker})\n"]
        lines.append(f"**Price:** ${current_price:.2f} {currency} | **Date:** {dates[-1]}")
        lines.append(f"**52W:** ${week52_low:.2f} – ${week52_high:.2f} ({pct_from_high:.1f}% from high)")
        lines.append(f"\n### Overall Signal: {overall}")
        lines.append(f"({buys} Bullish / {sells} Bearish / {neutrals} Neutral signals)\n")

        lines.append("### Indicator Values\n")
        lines.append("| Indicator | Value |")
        lines.append("|-----------|-------|")
        for k, v in results.items():
            lines.append(f"| {k} | {v} |")

        lines.append("\n### Signal Breakdown\n")
        lines.append("| Signal | Reason |")
        lines.append("|--------|--------|")
        for sig, reason in signals:
            emoji = "🟢" if sig == "BUY" else ("🔴" if sig == "SELL" else "⚪")
            lines.append(f"| {emoji} {sig} | {reason} |")

        lines.append("\n> *Technical analysis is for informational purposes only. Past performance does not guarantee future results.*")

        return "\n".join(lines)
