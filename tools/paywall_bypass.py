"""
title: Paywall Bypass — Multi-Strategy Article Reader
author: local-ai-stack
description: Read articles from sites that block direct fetches by trying a sequence of well-known bypass paths in order: 12ft.io URL prefix, archive.ph snapshot lookup, Internet Archive Wayback most-recent capture, Googlebot user-agent fetch, AMP version (`/amp/`), and print version (`?print=1`). Returns the first response that yields readable article text plus the strategy that worked. Pair with `unpaywall` for academic paywalls (already in the suite) — this tool targets news / blog / magazine paywalls (NYT / WSJ / FT / WaPo / Bloomberg / Substack / Medium etc).
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

import re
from typing import Any, Callable, Optional
from urllib.parse import urlparse, urlunparse, quote

import httpx
from pydantic import BaseModel, Field


_GOOGLEBOT_UA = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
_FB_UA = "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"
_LAI_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 local-ai-stack/1.0"


def _looks_like_paywall(text: str) -> bool:
    """Heuristic for paywall pages — short body + presence of paywall keywords."""
    if len(text) < 1500:
        return True
    needles = (
        "subscribe to continue", "subscribe now", "create a free account",
        "sign in to read", "to continue reading", "this article is for subscribers",
        "premium content", "paywall", "metered access", "for subscribers only",
    )
    low = text.lower()
    return any(n in low for n in needles) and len(text) < 8000


def _strip_html(html: str, max_chars: int = 16000) -> str:
    """Quick-and-dirty HTML → text."""
    txt = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    txt = re.sub(r"(?s)<!--.*?-->", " ", txt)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = re.sub(r"&nbsp;|&#160;", " ", txt)
    txt = re.sub(r"&amp;", "&", txt)
    txt = re.sub(r"&lt;", "<", txt)
    txt = re.sub(r"&gt;", ">", txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt[:max_chars]


class Tools:
    class Valves(BaseModel):
        TIMEOUT: int = Field(default=20, description="HTTP timeout per strategy attempt.")
        MAX_BODY_CHARS: int = Field(
            default=16000,
            description="Hard cap on extracted text per response.",
        )
        TWELVE_FT_BASE: str = Field(
            default="https://12ft.io",
            description="12ft.io mirror. Set blank to skip the strategy.",
        )
        ARCHIVE_PH_BASE: str = Field(
            default="https://archive.ph",
            description="archive.today mirror. Set blank to skip.",
        )

    def __init__(self):
        self.valves = self.Valves()

    # ── Strategy primitives ──────────────────────────────────────────────

    async def _direct(self, client: httpx.AsyncClient, url: str, *, ua: str) -> tuple[int, str]:
        try:
            r = await client.get(url, headers={"User-Agent": ua}, follow_redirects=True)
            return r.status_code, r.text
        except Exception as e:
            return 0, f"error: {e}"

    async def _try_12ft(self, client: httpx.AsyncClient, url: str) -> tuple[int, str, str]:
        if not self.valves.TWELVE_FT_BASE:
            return 0, "skipped", "12ft"
        target = f"{self.valves.TWELVE_FT_BASE.rstrip('/')}/proxy?q={quote(url, safe='')}"
        sc, body = await self._direct(client, target, ua=_LAI_UA)
        return sc, body, "12ft.io"

    async def _try_archive_ph(self, client: httpx.AsyncClient, url: str) -> tuple[int, str, str]:
        if not self.valves.ARCHIVE_PH_BASE:
            return 0, "skipped", "archive.ph"
        # archive.ph supports newest-snapshot redirect via /newest/<url>
        target = f"{self.valves.ARCHIVE_PH_BASE.rstrip('/')}/newest/{url}"
        sc, body = await self._direct(client, target, ua=_LAI_UA)
        return sc, body, "archive.ph"

    async def _try_wayback(self, client: httpx.AsyncClient, url: str) -> tuple[int, str, str]:
        try:
            r = await client.get(
                "https://archive.org/wayback/available",
                params={"url": url}, headers={"User-Agent": _LAI_UA},
            )
            data = r.json() if r.status_code == 200 else {}
        except Exception as e:
            return 0, f"error: {e}", "wayback"
        snap = (data.get("archived_snapshots", {}) or {}).get("closest", {})
        if not snap.get("available"):
            return 404, "no snapshot", "wayback"
        sc, body = await self._direct(client, snap["url"], ua=_LAI_UA)
        return sc, body, "wayback"

    async def _try_googlebot(self, client: httpx.AsyncClient, url: str) -> tuple[int, str, str]:
        sc, body = await self._direct(client, url, ua=_GOOGLEBOT_UA)
        return sc, body, "googlebot-ua"

    async def _try_facebook(self, client: httpx.AsyncClient, url: str) -> tuple[int, str, str]:
        sc, body = await self._direct(client, url, ua=_FB_UA)
        return sc, body, "facebookbot-ua"

    async def _try_amp(self, client: httpx.AsyncClient, url: str) -> tuple[int, str, str]:
        p = urlparse(url)
        amp_path = p.path.rstrip("/") + "/amp/"
        target = urlunparse((p.scheme, p.netloc, amp_path, "", "", ""))
        sc, body = await self._direct(client, target, ua=_LAI_UA)
        return sc, body, "amp"

    async def _try_print(self, client: httpx.AsyncClient, url: str) -> tuple[int, str, str]:
        target = url + ("&print=1" if "?" in url else "?print=1")
        sc, body = await self._direct(client, target, ua=_LAI_UA)
        return sc, body, "print"

    # ── Public API ────────────────────────────────────────────────────────

    async def fetch(
        self,
        url: str,
        text_only: bool = True,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Try each bypass strategy in order until one returns readable
        article text. Returns the strategy that worked + the text.
        :param url: Article URL.
        :param text_only: When True (default), strip HTML before returning. Set False to keep raw HTML.
        :return: Multi-section response: which strategy worked, plus extracted text.
        """
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT, follow_redirects=True) as c:
            attempts = [
                self._try_12ft, self._try_archive_ph, self._try_wayback,
                self._try_googlebot, self._try_facebook, self._try_amp,
                self._try_print,
            ]
            log: list[str] = [f"target: {url}"]
            for fn in attempts:
                sc, body, label = await fn(c, url)
                if sc == 200 and not _looks_like_paywall(body):
                    extracted = _strip_html(body, self.valves.MAX_BODY_CHARS) if text_only else body
                    log.append(f"strategy: {label}  status: {sc}  bytes: {len(body)}")
                    return "\n".join(log) + "\n\n" + extracted
                log.append(f"strategy: {label}  status: {sc}  paywalled={_looks_like_paywall(body) if sc==200 else 'n/a'}")
            log.append("\nAll strategies returned a paywall, error, or empty body.")
            return "\n".join(log)

    async def fetch_via(
        self,
        url: str,
        strategy: str,
        text_only: bool = True,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Force a specific bypass strategy.
        :param url: Article URL.
        :param strategy: 12ft, archive_ph, wayback, googlebot, facebook, amp, print.
        :param text_only: Strip HTML before returning.
        :return: Strategy result + extracted text.
        """
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT, follow_redirects=True) as c:
            fn = {
                "12ft":       self._try_12ft,
                "archive_ph": self._try_archive_ph,
                "wayback":    self._try_wayback,
                "googlebot":  self._try_googlebot,
                "facebook":   self._try_facebook,
                "amp":        self._try_amp,
                "print":      self._try_print,
            }.get(strategy.lower())
            if fn is None:
                return f"unknown strategy: {strategy}"
            sc, body, label = await fn(c, url)
            extracted = _strip_html(body, self.valves.MAX_BODY_CHARS) if text_only else body
            return f"strategy: {label}  status: {sc}  bytes: {len(body)}\n\n{extracted}"

    def list_strategies(self, __user__: Optional[dict] = None) -> str:
        """
        Return the supported bypass strategies in priority order.
        :return: Newline-delimited strategy list.
        """
        return "\n".join([
            "12ft        — 12ft.io URL proxy",
            "archive_ph  — archive.today newest snapshot",
            "wayback     — Internet Archive Wayback Machine",
            "googlebot   — fetch with Googlebot User-Agent",
            "facebook    — fetch with FacebookExternalHit User-Agent",
            "amp         — append /amp/ to the path",
            "print       — append ?print=1 to the URL",
        ])
