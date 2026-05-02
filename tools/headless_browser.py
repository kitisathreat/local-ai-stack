"""
title: Headless Browser — Playwright-Based Page Fetcher
author: local-ai-stack
description: Fetch JavaScript-rendered pages that the basic `url_reader` tool can't handle (Cloudflare-protected, SPA-heavy, requires-cookie sites). Uses Playwright with Chromium when installed (`pip install playwright && playwright install chromium`). Returns extracted text + an optional screenshot path. The first call per process boots a browser context; subsequent calls reuse it. Pair with `paywall_bypass` for paywalled-and-Cloudflared sites.
required_open_webui_version: 0.4.0
requirements: playwright
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field


def _strip_html(html: str, max_chars: int) -> str:
    txt = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    txt = re.sub(r"(?s)<!--.*?-->", " ", txt)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = re.sub(r"&nbsp;|&#160;", " ", txt)
    txt = re.sub(r"&amp;", "&", txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt[:max_chars]


class Tools:
    class Valves(BaseModel):
        DEFAULT_TIMEOUT_MS: int = Field(default=30_000)
        MAX_BODY_CHARS: int = Field(default=24_000)
        SCREENSHOT_DIR: str = Field(
            default=str(Path.home() / ".cache" / "local-ai-stack" / "screenshots"),
            description="Where to save optional screenshots.",
        )
        UA: str = Field(
            default="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 local-ai-stack/1.0",
        )

    def __init__(self):
        self.valves = self.Valves()
        self._browser = None      # type: ignore[assignment]
        self._playwright = None   # type: ignore[assignment]

    async def _ensure_browser(self):
        if self._browser is not None:
            return self._browser
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise RuntimeError(
                "playwright not installed. Run: pip install playwright && playwright install chromium"
            ) from e
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=True)
        return self._browser

    async def fetch(
        self,
        url: str,
        wait_for: str = "domcontentloaded",
        wait_for_selector: str = "",
        timeout_ms: int = 0,
        screenshot: bool = False,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Render a page via headless Chromium and return the visible text.
        :param url: Page URL.
        :param wait_for: Lifecycle event — load, domcontentloaded, networkidle.
        :param wait_for_selector: Optional CSS selector to wait for after load (e.g. "article").
        :param timeout_ms: Navigation timeout. 0 = DEFAULT_TIMEOUT_MS.
        :param screenshot: When True, save a PNG and include the path in the response.
        :return: Multi-section response with status, page text, and optional screenshot path.
        """
        try:
            browser = await self._ensure_browser()
        except RuntimeError as e:
            return str(e)
        context = await browser.new_context(user_agent=self.valves.UA)
        page = await context.new_page()
        try:
            r = await page.goto(url, wait_until=wait_for,
                                timeout=timeout_ms or self.valves.DEFAULT_TIMEOUT_MS)
            status = r.status if r else 0
            if wait_for_selector:
                try:
                    await page.wait_for_selector(wait_for_selector,
                                                  timeout=timeout_ms or self.valves.DEFAULT_TIMEOUT_MS)
                except Exception:
                    pass
            html = await page.content()
            shot = ""
            if screenshot:
                d = Path(self.valves.SCREENSHOT_DIR).expanduser()
                d.mkdir(parents=True, exist_ok=True)
                shot_path = d / f"{re.sub(r'[^A-Za-z0-9]', '_', url)[:60]}.png"
                await page.screenshot(path=str(shot_path), full_page=True)
                shot = str(shot_path)
        finally:
            await context.close()

        text = _strip_html(html, self.valves.MAX_BODY_CHARS)
        head = f"url: {url}\nstatus: {status}\nbytes: {len(html)}"
        if shot:
            head += f"\nscreenshot: {shot}"
        return head + "\n\n" + text

    async def click_and_fetch(
        self,
        url: str,
        selector_chain: list[str],
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Navigate to a URL, click a sequence of selectors (e.g. cookie banner
        accept → "show more" → "load comments"), then return the resulting
        text. Each selector is clicked in order; missing selectors are
        ignored.
        :param url: Starting URL.
        :param selector_chain: List of CSS selectors to click sequentially.
        :return: Final page text.
        """
        try:
            browser = await self._ensure_browser()
        except RuntimeError as e:
            return str(e)
        context = await browser.new_context(user_agent=self.valves.UA)
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded",
                            timeout=self.valves.DEFAULT_TIMEOUT_MS)
            for sel in selector_chain:
                try:
                    await page.click(sel, timeout=5_000)
                    await page.wait_for_timeout(500)
                except Exception:
                    pass
            html = await page.content()
        finally:
            await context.close()
        return _strip_html(html, self.valves.MAX_BODY_CHARS)

    async def shutdown(self, __user__: Optional[dict] = None) -> str:
        """
        Tear down the cached browser process. Call this if the chromium
        process is misbehaving.
        :return: Confirmation.
        """
        try:
            if self._browser is not None:
                await self._browser.close()
            if self._playwright is not None:
                await self._playwright.stop()
        except Exception as e:
            return f"shutdown error: {e}"
        finally:
            self._browser = None
            self._playwright = None
        return "browser shut down"
