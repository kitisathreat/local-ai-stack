"""
title: Free & Archived Games — GamerPower, Epic, Steam, GOG, itch.io, IA Software, MyAbandonware, libregamewiki, configurable marketplaces
author: local-ai-stack
description: Find games that are legitimately free or legally archived. Three layers — (1) currently-free promotions across every storefront via GamerPower's aggregator + per-store endpoints (Epic weekly free, Steam free-to-play, GOG free-game promo, itch.io free indies); (2) game-preservation archives — Internet Archive's software library (DOS, Apple II, NES, browser-playable emulator), MyAbandonware, OldGamesDownload — for titles whose rightsholders have ceased commercial activity; (3) FOSS / open-source games via libregamewiki. Plus a configurable marketplace layer — drop URL templates + regex selectors for arbitrary game sites into MARKETPLACES and the tool will search them, extract download links, and (with WRITE_ENABLED) stream them to DOWNLOAD_DIR. The aggregator runs every layer in parallel for one query. Configured marketplaces are the user's responsibility — they should reflect sources the user has the right to access.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.1.0
licence: MIT
"""

from __future__ import annotations

import asyncio
import html as html_mod
import json
import os
import re
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urlencode, urljoin, urlparse

import httpx
from pydantic import BaseModel, Field


_UA = "Mozilla/5.0 (X11; Linux x86_64) local-ai-stack/1.0 free-games"
IA_API = "https://archive.org/advancedsearch.php"
IA_DOWNLOAD = "https://archive.org/download"
IA_DETAILS = "https://archive.org/details"
GAMERPOWER = "https://www.gamerpower.com/api"
EPIC_FREE = "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions"
STEAM_FEATURED = "https://store.steampowered.com/api/featuredcategories"
STEAM_SEARCH = "https://store.steampowered.com/api/storesearch"
GOG_CATALOG = "https://catalog.gog.com/v1/catalog"
ITCH_SEARCH = "https://itch.io/games/free"
LIBREGW_API = "https://libregamewiki.org/api.php"
FREEGAMEDEV_API = "https://freegamedev.net/wiki/api.php"
MYABANDON = "https://www.myabandonware.com"
OLDGAMES = "https://www.oldgamesdownload.com"
ABANDONIA = "https://www.abandonia.com"
GAMEJOLT = "https://gamejolt.com"


def _strip_html(s: str, n: int = 600) -> str:
    s = html_mod.unescape(s or "")
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:n]


def _safe_filename(name: str, default_ext: str = "bin") -> str:
    """Sanitise a string into a safe single-segment filename."""
    name = re.sub(r'[\\/:*?"<>|]+', "_", name).strip()
    name = re.sub(r"\s+", " ", name)[:200]
    if not name:
        name = f"download.{default_ext}"
    return name


# Generic regex patterns for "find downloadable artifacts on a page" — used as
# the fallback when a marketplace config doesn't supply its own download_pattern.
_GENERIC_DL_REGEX = re.compile(
    r"""(?ix)
    (?:href|src)\s*=\s*["']
    (
       magnet:\?[^"']+
     | https?://[^"']+\.(?:zip|7z|rar|iso|bin|cue|torrent|exe|dmg|pkg|tar(?:\.[gx]z)?|tgz)(?:\?[^"']*)?
    )
    ["']
    """
)


class Tools:
    class Valves(BaseModel):
        MAX_RESULTS: int = Field(default=10, description="Max items per source.")
        TIMEOUT: int = Field(default=30, description="HTTP timeout per request, seconds.")
        STEAM_REGION: str = Field(default="us", description="Country code for Steam endpoints.")
        STEAM_LANGUAGE: str = Field(default="english", description="Language for Steam endpoints.")
        GOG_LOCALE: str = Field(default="en-US", description="GOG locale.")
        ARCHIVE_ORG_PREFER_BROWSER_PLAYABLE: bool = Field(
            default=True,
            description="Prefer Internet Archive items that are browser-playable via emulator.",
        )

        # ── Configurable marketplace layer ───────────────────────────────
        MARKETPLACES: str = Field(
            default="[]",
            description=(
                "JSON list of marketplace configs. Each entry is a dict with: "
                "name (str), search_url (str with {query} placeholder, url-encoded), "
                "result_pattern (regex with two capture groups: 1=item URL, 2=item title), "
                "download_pattern (optional regex with one capture group for direct download URLs / magnets — "
                "if omitted, a generic .zip/.iso/.7z/.torrent/magnet link extractor is used). "
                "You are responsible for the legality of sources you configure here."
            ),
        )
        DOWNLOAD_DIR: str = Field(
            default=str(Path.home() / "Games" / "Downloads"),
            description="Destination directory for marketplace downloads.",
        )
        WRITE_ENABLED: bool = Field(
            default=False,
            description="Master switch — file writes only happen when this is on.",
        )
        REQUEST_HEADERS: str = Field(
            default="{}",
            description='Extra HTTP headers (JSON object) sent with marketplace requests, e.g. {"Cookie":"...","Referer":"https://..."}',
        )
        USER_AGENT: str = Field(
            default="Mozilla/5.0 (X11; Linux x86_64) local-ai-stack/1.0 free-games",
            description="User-Agent for marketplace and download requests.",
        )
        FOLLOW_REDIRECTS: bool = Field(
            default=True,
            description="Follow HTTP redirects on marketplace requests.",
        )

    def __init__(self):
        self.valves = self.Valves()

    # ── Marketplace plumbing ──────────────────────────────────────────────

    def _marketplaces(self) -> list[dict]:
        try:
            entries = json.loads(self.valves.MARKETPLACES or "[]")
        except json.JSONDecodeError as e:
            raise ValueError(f"MARKETPLACES is not valid JSON: {e}") from None
        if not isinstance(entries, list):
            raise ValueError("MARKETPLACES must be a JSON array.")
        out = []
        for i, e in enumerate(entries):
            if not isinstance(e, dict):
                raise ValueError(f"MARKETPLACES[{i}] must be an object.")
            for k in ("name", "search_url", "result_pattern"):
                if not e.get(k):
                    raise ValueError(f"MARKETPLACES[{i}] missing required field '{k}'.")
            out.append(e)
        return out

    def _extra_headers(self) -> dict:
        try:
            extra = json.loads(self.valves.REQUEST_HEADERS or "{}")
        except json.JSONDecodeError:
            extra = {}
        h = {"User-Agent": self.valves.USER_AGENT, "Accept": "*/*"}
        if isinstance(extra, dict):
            h.update({k: str(v) for k, v in extra.items()})
        return h

    @staticmethod
    def _detect_blockers(html: str, headers: dict) -> str:
        """Surface obvious anti-bot signatures so the user knows when to give
        up on a regex fix and reach for a headless browser instead."""
        hits: list[str] = []
        h = (html or "")[:5000].lower()
        srv = (headers.get("server") or "").lower()
        if "cloudflare" in srv or "cf-ray" in {k.lower() for k in headers}:
            hits.append("Cloudflare (likely JS challenge / Turnstile)")
        if "just a moment" in h or "checking your browser" in h:
            hits.append("Cloudflare interstitial detected in body")
        if "captcha" in h or "g-recaptcha" in h or "h-captcha" in h:
            hits.append("CAPTCHA challenge")
        if "ddos-guard" in srv or "ddos-guard" in h:
            hits.append("DDoS-Guard")
        if "incapsula" in h or "imperva" in srv:
            hits.append("Imperva / Incapsula")
        if "<script" in h and ("__nuxt__" in h or "__next_data__" in h or "ng-version" in h or "react-root" in h):
            hits.append("JS-rendered SPA — server returned shell HTML, real content loads via JS")
        if not h.strip():
            hits.append("empty response body")
        return "; ".join(hits)

    # ── GamerPower (cross-platform free-game aggregator) ─────────────────

    async def gamerpower(
        self,
        platform: str = "",
        type: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Fetch all currently-active free game / DLC / loot promotions across
        every major storefront, via GamerPower's free public JSON API. The
        single best source for "what's free right now."
        :param platform: Optional filter — "epic-games-store", "steam",
                         "gog", "itch.io", "ubisoft", "ea", "battlenet",
                         "ps4", "xbox-one", "switch", "pc", "android", "ios".
                         Empty = all.
        :param type: Optional filter — "game", "loot", "beta". Empty = all.
        :return: Markdown list with title, end date, redeem URL, original price.
        """
        params: dict[str, Any] = {}
        if platform:
            params["platform"] = platform
        if type:
            params["type"] = type
        url = f"{GAMERPOWER}/giveaways"
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT) as c:
            try:
                r = await c.get(url, params=params, headers={"User-Agent": _UA, "Accept": "application/json"})
            except Exception as e:
                return f"GamerPower request failed: {e}"
            if r.status_code == 201 and not r.content:
                return "GamerPower: no active giveaways match those filters."
            if r.status_code >= 400:
                return f"GamerPower error {r.status_code}: {r.text[:200]}"
            try:
                items = r.json()
            except Exception:
                return "GamerPower returned non-JSON."

        if not isinstance(items, list) or not items:
            return f"GamerPower: no active giveaways for platform='{platform}' type='{type}'."

        out = [f"## GamerPower — currently free  (platform: {platform or 'any'} · type: {type or 'any'})\n"]
        for it in items[: max(self.valves.MAX_RESULTS, 30)]:  # show more — these are current giveaways
            kind = (it.get("type") or "").lower()
            badge = {"game": "🎮", "loot": "🎁", "beta": "🧪"}.get(kind, "•")
            worth = it.get("worth", "")
            ends = it.get("end_date", "—")
            users = it.get("users", 0)
            plats = it.get("platforms", "")
            out.append(
                f"{badge} **{it.get('title', '—')}**  _{kind}_  "
                f"(worth {worth or '?'})\n"
                f"   platforms: {plats}  ·  ends: {ends}  ·  claimed by: {users:,}\n"
                f"   {it.get('open_giveaway_url') or it.get('open_giveaway') or it.get('gamerpower_url', '')}\n"
            )
        return "\n".join(out)

    # ── Epic Games (weekly free) ──────────────────────────────────────────

    async def epic_free(
        self,
        country: str = "US",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Fetch Epic Games Store's weekly free-game promo via their public
        storefront endpoint.
        :param country: ISO country code for regional pricing/availability.
        :return: Markdown list of currently-free + upcoming-free games.
        """
        params = {"locale": "en-US", "country": country, "allowCountries": country}
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT) as c:
            try:
                r = await c.get(EPIC_FREE, params=params, headers={"User-Agent": _UA, "Accept": "application/json"})
            except Exception as e:
                return f"Epic request failed: {e}"
            if r.status_code >= 400:
                return f"Epic error {r.status_code}"
            try:
                data = r.json()
            except Exception:
                return "Epic returned non-JSON."

        elems = (data.get("data") or {}).get("Catalog", {}).get("searchStore", {}).get("elements") or []
        if not elems:
            return "Epic: no free-game promotions returned."

        now_free, soon_free = [], []
        for el in elems:
            promos = (el.get("promotions") or {}) or {}
            current = promos.get("promotionalOffers") or []
            upcoming = promos.get("upcomingPromotionalOffers") or []
            title = el.get("title", "—")
            slug = el.get("productSlug") or (el.get("catalogNs") or {}).get("mappings", [{}])[0].get("pageSlug") or el.get("urlSlug")
            url = f"https://store.epicgames.com/en-US/p/{slug}" if slug else "https://store.epicgames.com/en-US/free-games"
            desc = _strip_html(el.get("description", ""), 200)
            # Detect $0 promo to confirm 'free'.
            free_now = any(
                (po.get("discountSetting") or {}).get("discountPercentage") == 0
                for offer in current for po in (offer.get("promotionalOffers") or [])
            )
            if free_now or current:
                now_free.append((title, desc, url))
            elif upcoming:
                soon_free.append((title, desc, url))

        out = [f"## Epic Games Store ({country})\n"]
        out.append("### Free now")
        if now_free:
            for t, d, u in now_free:
                out.append(f"- 🟢 **{t}**\n   {d}\n   {u}")
        else:
            out.append("- _(none active)_")
        out.append("\n### Upcoming")
        if soon_free:
            for t, d, u in soon_free:
                out.append(f"- 🟡 **{t}**\n   {d}\n   {u}")
        else:
            out.append("- _(none scheduled)_")
        return "\n".join(out)

    # ── Steam (free-to-play / on-sale to $0) ─────────────────────────────

    async def steam_free(
        self,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        List Steam's currently-featured free titles (free-to-play, free
        weekends, and 100%-off specials). Uses Steam's public storefront
        featured-categories endpoint — no key.
        :return: Markdown list with appid + Steam page URL.
        """
        params = {"cc": self.valves.STEAM_REGION, "l": self.valves.STEAM_LANGUAGE}
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT) as c:
            try:
                r = await c.get(STEAM_FEATURED, params=params, headers={"User-Agent": _UA, "Accept": "application/json"})
            except Exception as e:
                return f"Steam request failed: {e}"
            if r.status_code >= 400:
                return f"Steam error {r.status_code}"
            try:
                data = r.json()
            except Exception:
                return "Steam returned non-JSON."

        out = [f"## Steam — free / temporarily-free titles ({self.valves.STEAM_REGION})\n"]
        any_hit = False
        for label, key in [("Free to Play", "specials"), ("New Releases", "new_releases")]:
            section = (data.get(key) or {}).get("items") or []
            free_items = [
                it for it in section
                if it.get("discount_percent") == 100 or it.get("final_price") == 0
            ]
            if not free_items:
                continue
            out.append(f"### {label}")
            for it in free_items[: self.valves.MAX_RESULTS]:
                out.append(
                    f"- **{it.get('name', '—')}**  "
                    f"(discount: {it.get('discount_percent', 0)}%)\n"
                    f"   https://store.steampowered.com/app/{it.get('id')}/"
                )
                any_hit = True
            out.append("")

        # Also surface explicit free-to-play tag pages.
        try:
            r2 = await httpx.AsyncClient(timeout=self.valves.TIMEOUT).get(
                STEAM_SEARCH,
                params={"term": "free", "cc": self.valves.STEAM_REGION, "l": self.valves.STEAM_LANGUAGE},
                headers={"User-Agent": _UA, "Accept": "application/json"},
            )
            sd = r2.json() if r2.status_code == 200 else {}
            free_search = [it for it in (sd.get("items") or []) if (it.get("price") or {}).get("final", 1) == 0]
            if free_search:
                out.append("### Free-to-play matches")
                for it in free_search[: self.valves.MAX_RESULTS]:
                    out.append(
                        f"- **{it.get('name', '—')}**\n"
                        f"   https://store.steampowered.com/app/{it.get('id')}/"
                    )
                any_hit = True
        except Exception:
            pass

        if not any_hit:
            return "Steam: no free / 100%-off titles featured right now."
        return "\n".join(out)

    # ── GOG (legitimate giveaways + $0 catalogue) ────────────────────────

    async def gog_free(
        self,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Pull GOG.com's catalogue filtered to price=0. Includes their current
        giveaway (if any) and games the publishers have permanently set
        free.
        :return: Markdown list with GOG store URL.
        """
        params = {
            "limit": self.valves.MAX_RESULTS,
            "order": "desc:trending",
            "price": "between:0,0",
            "productType": "in:game,pack,dlc,extras",
            "page": 1,
            "locale": self.valves.GOG_LOCALE,
        }
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT, follow_redirects=True) as c:
            try:
                r = await c.get(GOG_CATALOG, params=params, headers={"User-Agent": _UA, "Accept": "application/json"})
            except Exception as e:
                return f"GOG request failed: {e}"
            if r.status_code >= 400:
                return f"GOG error {r.status_code}"
            try:
                data = r.json()
            except Exception:
                return "GOG returned non-JSON."

        items = data.get("products") or []
        if not items:
            return "GOG: no $0 titles right now (giveaway + permanently-free)."
        out = ["## GOG.com — currently $0\n"]
        for it in items[: self.valves.MAX_RESULTS]:
            slug = it.get("slug") or it.get("id")
            url = f"https://www.gog.com/en/game/{slug}" if isinstance(slug, str) else f"https://www.gog.com/en/games?id={slug}"
            out.append(
                f"- **{it.get('title', '—')}**  _{it.get('productType', '?')}_\n"
                f"   developer: {', '.join(d.get('name', '?') for d in (it.get('developers') or [])) or '—'}\n"
                f"   {url}"
            )
        return "\n".join(out)

    # ── itch.io (free / NYP indies) ──────────────────────────────────────

    async def itchio_free(
        self,
        query: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search itch.io's free-games browse page (HTML scrape — itch.io's
        public JSON API requires a key for arbitrary search but the browse
        page is free).
        :param query: Optional free-text filter on titles.
        :return: Markdown list with title, dev, itch.io URL.
        """
        url = f"https://itch.io/games/free?q={quote(query, safe='')}" if query else "https://itch.io/games/free"
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT, follow_redirects=True) as c:
            try:
                r = await c.get(url, headers={"User-Agent": _UA})
            except Exception as e:
                return f"itch.io request failed: {e}"
            if r.status_code >= 400:
                return f"itch.io error {r.status_code}"
            html = r.text

        # itch.io renders each game as <div class="game_cell"...> with a title link
        # and author link inside.
        rows = re.findall(
            r'<div class="game_cell[^"]*"[^>]*>(.*?)</div>\s*</div>',
            html,
            flags=re.DOTALL,
        )
        title_re = re.compile(r'<a class="title game_link"[^>]*href="([^"]+)"[^>]*>([^<]+)</a>')
        author_re = re.compile(r'<a class="game_author[^"]*"[^>]*>([^<]+)</a>')
        out = [f"## itch.io — free games" + (f" matching '{query}'" if query else "") + "\n"]
        kept = 0
        for blk in rows:
            tm = title_re.search(blk)
            if not tm:
                continue
            au = author_re.search(blk)
            out.append(
                f"- **{tm.group(2).strip()}** by _{au.group(1).strip() if au else 'unknown'}_\n"
                f"   {tm.group(1)}"
            )
            kept += 1
            if kept >= self.valves.MAX_RESULTS:
                break
        return "\n".join(out) if kept else f"itch.io: no free-games-page matches for '{query}'"

    # ── Internet Archive Software Library (preservation) ─────────────────

    async def ia_software(
        self,
        query: str,
        collection: str = "softwarelibrary",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search the Internet Archive's Software Library — the single largest
        legitimate game-preservation effort. Many entries are
        browser-playable via the emulator built into archive.org.
        :param query: Title or platform keyword.
        :param collection: Which IA software collection to search. Useful values:
                           - "softwarelibrary" (whole library)
                           - "softwarelibrary_msdos" (MS-DOS, browser-playable)
                           - "softwarelibrary_apple" (Apple II / Mac)
                           - "internetarcade" (arcade emulator, browser-playable)
                           - "consolelivingroom" (cartridge consoles)
                           - "softwarelibrary_zxspectrum"
                           - "softwarelibrary_c64"
                           - "classicpcgames"
                           - "softwarelibrary_amiga"
        :return: Markdown list with year, downloads, IA emulator/details URL.
        """
        params: dict[str, Any] = {
            "q": f"collection:({collection}) AND ({query})",
            "rows": self.valves.MAX_RESULTS,
            "page": 1,
            "output": "json",
            "sort[]": "downloads desc",
        }
        for i, fld in enumerate(["identifier", "title", "creator", "year", "downloads", "format"]):
            params[f"fl[{i}]"] = fld

        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT) as c:
            try:
                r = await c.get(IA_API, params=params, headers={"User-Agent": _UA})
            except Exception as e:
                return f"IA request failed: {e}"
            if r.status_code >= 400:
                return f"IA returned {r.status_code}"
            docs = (r.json().get("response") or {}).get("docs") or []
        if not docs:
            return f"IA Software [{collection}]: no results for {query}"

        out = [f"## Internet Archive Software [{collection}]: {query}\n"]
        for d in docs:
            ident = d.get("identifier", "")
            fmts = d.get("format", []) or []
            if isinstance(fmts, str):
                fmts = [fmts]
            playable = self.valves.ARCHIVE_ORG_PREFER_BROWSER_PLAYABLE and any(
                "emulator" in f.lower() or "wasm" in f.lower() for f in fmts
            )
            out.append(
                f"**{d.get('title', '—')}**  ({d.get('year', '—')})\n"
                f"   creator: {d.get('creator', '—')}  ·  downloads: {int(d.get('downloads', 0)):,}\n"
                f"   formats: {', '.join(fmts) or '—'}" + ("  ·  🎮 browser-playable" if playable else "") + "\n"
                f"   play/details: {IA_DETAILS}/{ident}\n"
                f"   download: {IA_DOWNLOAD}/{ident}/\n"
            )
        return "\n".join(out)

    # ── MyAbandonware ────────────────────────────────────────────────────

    async def myabandonware(
        self,
        query: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search MyAbandonware — focused on titles whose rightsholders have
        ceased commercial activity. Each result page lists DOS/Win/Mac
        installers for direct download.
        :param query: Title query.
        :return: Markdown list with year, platforms, MyAbandonware URL.
        """
        url = f"{MYABANDON}/search/q/{quote(query, safe='')}/"
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT, follow_redirects=True) as c:
            try:
                r = await c.get(url, headers={"User-Agent": _UA})
            except Exception as e:
                return f"MyAbandonware request failed: {e}"
            if r.status_code >= 400:
                return f"MyAbandonware error {r.status_code}"
            html = r.text

        # Each hit: <div class="item-game">…<a href="/game/SLUG">title</a>…<span class="year">YYYY</span>
        block_re = re.compile(r'(?is)<div class="item-game[^"]*">(.*?)</div>\s*</div>')
        title_re = re.compile(r'<a[^>]+href="(/game/[^"]+)"[^>]*>([^<]+)</a>')
        year_re = re.compile(r'(\d{4})')
        platforms_re = re.compile(r'class="game-platform[^"]*"[^>]*title="([^"]+)"')

        rows = []
        for blk in block_re.findall(html):
            tm = title_re.search(blk)
            if not tm:
                continue
            ym = year_re.search(blk)
            plats = platforms_re.findall(blk)
            rows.append({
                "title": tm.group(2).strip(),
                "url": f"{MYABANDON}{tm.group(1)}",
                "year": ym.group(1) if ym else "—",
                "platforms": ", ".join(plats) or "—",
            })
            if len(rows) >= self.valves.MAX_RESULTS:
                break

        if not rows:
            # Fallback: simpler anchor scan
            simple = re.findall(r'<a[^>]+href="(/game/[^"]+)"[^>]*>([^<]+)</a>', html)
            for path, title in simple[: self.valves.MAX_RESULTS]:
                rows.append({"title": title.strip(), "url": f"{MYABANDON}{path}", "year": "—", "platforms": "—"})

        if not rows:
            return f"MyAbandonware: no results for {query}"
        out = [f"## MyAbandonware: {query}\n_(rightsholder-abandoned titles; download from each game's page)_\n"]
        for r in rows:
            out.append(f"- **{r['title']}** ({r['year']})  ·  {r['platforms']}\n   {r['url']}")
        return "\n".join(out)

    # ── OldGamesDownload ─────────────────────────────────────────────────

    async def oldgamesdownload(
        self,
        query: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search OldGamesDownload — similar archive focus to MyAbandonware,
        DOS/Win/Mac installers with patches.
        :param query: Title query.
        :return: Markdown list of titles with detail-page URLs.
        """
        url = f"{OLDGAMES}/?s={quote(query, safe='')}"
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT, follow_redirects=True) as c:
            try:
                r = await c.get(url, headers={"User-Agent": _UA})
            except Exception as e:
                return f"OldGamesDownload request failed: {e}"
            if r.status_code >= 400:
                return f"OldGamesDownload error {r.status_code}"
            html = r.text

        # Posts are <article> with an <h2 class="entry-title"><a href=…>Title</a></h2>
        rows = re.findall(
            r'<h2[^>]*class="[^"]*entry-title[^"]*"[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>([^<]+)</a>',
            html,
        )
        if not rows:
            return f"OldGamesDownload: no results for {query}"
        out = [f"## OldGamesDownload: {query}\n_(rightsholder-abandoned titles)_\n"]
        for url2, title in rows[: self.valves.MAX_RESULTS]:
            out.append(f"- **{html_mod.unescape(title.strip())}**\n   {url2}")
        return "\n".join(out)

    # ── libregamewiki (FOSS / open-source games) ────────────────────────

    async def libregamewiki(
        self,
        query: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search libregamewiki — fully FOSS games (0 A.D., Battle for Wesnoth,
        OpenTTD, SuperTuxKart, OpenMW, etc.). Source is included; truly
        free in every sense.
        :param query: Title or genre keyword.
        :return: Markdown list with libregamewiki page URLs.
        """
        params = {
            "action": "query",
            "list": "search",
            "srsearch": query,
            "srlimit": self.valves.MAX_RESULTS,
            "format": "json",
        }
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT) as c:
            try:
                r = await c.get(LIBREGW_API, params=params, headers={"User-Agent": _UA, "Accept": "application/json"})
            except Exception as e:
                return f"libregamewiki request failed: {e}"
            if r.status_code >= 400:
                return f"libregamewiki error {r.status_code}"
            try:
                data = r.json()
            except Exception:
                return "libregamewiki returned non-JSON."

        hits = (data.get("query") or {}).get("search") or []
        if not hits:
            return f"libregamewiki: no FOSS-game results for {query}"
        out = [f"## libregamewiki — FOSS games: {query}\n"]
        for h in hits[: self.valves.MAX_RESULTS]:
            title = h.get("title", "—")
            slug = title.replace(" ", "_")
            snippet = _strip_html(h.get("snippet", ""), 240)
            out.append(
                f"- **{title}**\n"
                f"   {snippet}\n"
                f"   https://libregamewiki.org/{slug}"
            )
        return "\n".join(out)

    # ── FreeGameDev wiki (FOSS / open-source games — second source) ──────

    async def freegamedev(
        self,
        query: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search the FreeGameDev wiki — companion FOSS-games index to
        libregamewiki. Same MediaWiki search pattern. Use both: their
        coverage overlaps but each has titles the other doesn't.
        :param query: Title or genre keyword.
        :return: Markdown list with FreeGameDev wiki page URLs.
        """
        params = {
            "action": "query",
            "list": "search",
            "srsearch": query,
            "srlimit": self.valves.MAX_RESULTS,
            "format": "json",
        }
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT) as c:
            try:
                r = await c.get(FREEGAMEDEV_API, params=params, headers={"User-Agent": _UA, "Accept": "application/json"})
            except Exception as e:
                return f"FreeGameDev request failed: {e}"
            if r.status_code >= 400:
                return f"FreeGameDev error {r.status_code}"
            try:
                data = r.json()
            except Exception:
                return "FreeGameDev returned non-JSON."

        hits = (data.get("query") or {}).get("search") or []
        if not hits:
            return f"FreeGameDev: no results for {query}"
        out = [f"## FreeGameDev wiki — FOSS games: {query}\n"]
        for h in hits[: self.valves.MAX_RESULTS]:
            title = h.get("title", "—")
            slug = title.replace(" ", "_")
            snippet = _strip_html(h.get("snippet", ""), 240)
            out.append(
                f"- **{title}**\n"
                f"   {snippet}\n"
                f"   https://freegamedev.net/wiki/{slug}"
            )
        return "\n".join(out)

    # ── Game Jolt (free indies) ──────────────────────────────────────────

    async def gamejolt_free(
        self,
        query: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Browse Game Jolt's free indie catalogue. When `query` is empty
        returns a slice of currently-popular free games; otherwise scans
        the free-games browse page for matching titles.
        :param query: Optional title substring filter.
        :return: Markdown list with title, dev, Game Jolt URL.
        """
        url = f"{GAMEJOLT}/games/free"
        if query:
            url = f"{GAMEJOLT}/games?q={quote(query, safe='')}&maxprice=0"
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT, follow_redirects=True) as c:
            try:
                r = await c.get(url, headers={"User-Agent": _UA})
            except Exception as e:
                return f"Game Jolt request failed: {e}"
            if r.status_code >= 400:
                return f"Game Jolt error {r.status_code}"
            html = r.text

        # Game Jolt embeds its current state as a JSON payload in the page;
        # also has anchor hrefs of the form /games/<slug>/<id>.
        seen: set[str] = set()
        rows: list[dict] = []
        anchor_re = re.compile(r'<a[^>]+href="(/games/([^/"]+)/(\d+))"[^>]*>', re.IGNORECASE)
        title_after_re = re.compile(r'>([^<]{2,100})</a>')
        for m in anchor_re.finditer(html):
            full_path = m.group(1)
            slug = m.group(2)
            gid = m.group(3)
            if gid in seen:
                continue
            seen.add(gid)
            # Try to grab the visible title near the anchor.
            tail = html[m.end(): m.end() + 400]
            tm = title_after_re.search(tail)
            title = (tm.group(1).strip() if tm else slug.replace("-", " ")).strip()
            rows.append({
                "title": html_mod.unescape(title),
                "url": f"{GAMEJOLT}{full_path}",
                "id": gid,
            })
            if len(rows) >= self.valves.MAX_RESULTS * 2:
                break

        if query:
            ql = query.lower()
            rows = [r for r in rows if ql in r["title"].lower()] or rows

        if not rows:
            return f"Game Jolt: no free games surfaced" + (f" for '{query}'" if query else "")

        out = [f"## Game Jolt — free indies" + (f": {query}" if query else "") + "\n"]
        for r in rows[: self.valves.MAX_RESULTS]:
            out.append(f"- **{r['title']}**\n   {r['url']}")
        return "\n".join(out)

    # ── Abandonia (abandoned commercial titles, third source) ────────────

    async def abandonia(
        self,
        query: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search Abandonia — long-running archive of abandoned DOS/Windows
        commercial games (rightsholders ceased commercial activity). Each
        result page hosts its own download. Companion to MyAbandonware /
        OldGamesDownload — different catalogues, different curation.
        :param query: Title query.
        :return: Markdown list with year, platform, Abandonia URL.
        """
        url = f"{ABANDONIA}/en/games/list/{quote(query, safe='')}"
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT, follow_redirects=True) as c:
            try:
                r = await c.get(url, headers={"User-Agent": _UA})
            except Exception as e:
                return f"Abandonia request failed: {e}"
            if r.status_code == 404:
                # Fall back to their search endpoint.
                try:
                    r = await c.get(
                        f"{ABANDONIA}/en/index.php",
                        params={"op": "search", "q": query},
                        headers={"User-Agent": _UA},
                    )
                except Exception as e:
                    return f"Abandonia request failed: {e}"
            if r.status_code >= 400:
                return f"Abandonia error {r.status_code}"
            html = r.text

        # Listing rows: each game card has <a href="/en/games/NNN/Title.html">Title</a>
        rows = re.findall(
            r'<a[^>]+href="(/en/games/\d+/[^"]+\.html)"[^>]*>([^<]{2,120})</a>',
            html,
        )
        # Deduplicate while preserving order.
        seen: set[str] = set()
        deduped = []
        for path, title in rows:
            if path in seen:
                continue
            seen.add(path)
            deduped.append((path, title))
            if len(deduped) >= self.valves.MAX_RESULTS:
                break

        if not deduped:
            return f"Abandonia: no results for {query}"
        out = [f"## Abandonia: {query}\n_(rightsholder-abandoned titles; download from each game's page)_\n"]
        for path, title in deduped:
            out.append(f"- **{html_mod.unescape(title.strip())}**\n   {ABANDONIA}{path}")
        return "\n".join(out)

    # ── Configurable marketplace layer ───────────────────────────────────

    async def list_marketplaces(self, __user__: Optional[dict] = None) -> str:
        """
        Show every marketplace currently configured in the MARKETPLACES
        valve. Reading this is the fastest way to confirm the JSON parsed
        and to see which `name` values the search/extract methods will
        accept.
        :return: Markdown table of name + search URL template.
        """
        try:
            mps = self._marketplaces()
        except ValueError as e:
            return f"MARKETPLACES configuration error: {e}"
        if not mps:
            return (
                "No marketplaces configured. Set the MARKETPLACES valve to a JSON list, e.g.:\n\n"
                '  [{"name":"Example","search_url":"https://example.com/?s={query}",'
                '"result_pattern":"<h2 class=\\"entry-title\\"><a[^>]+href=\\"([^\\"]+)\\"[^>]*>([^<]+)</a>",'
                '"download_pattern":"href=\\"(https?://[^\\"]+\\\\.(?:zip|iso|7z))\\""}]'
            )
        out = ["## Configured marketplaces\n"]
        for m in mps:
            out.append(
                f"- **{m['name']}**\n"
                f"   search: `{m['search_url']}`\n"
                f"   result_pattern set: yes  ·  custom download_pattern: {'yes' if m.get('download_pattern') else 'no (using generic)'}"
            )
        return "\n".join(out)

    # ── Diagnostic helpers ───────────────────────────────────────────────
    #
    # The honest answer to "will this work on any site you point it at?"
    # is no — generic web scraping is brittle. These methods exist so the
    # user can verify a marketplace config in one round-trip instead of
    # guessing why nothing came back.

    async def probe_marketplace(
        self,
        name: str,
        query: str = "test",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Diagnostic version of `search_marketplace`. Runs the search exactly
        as `search_marketplace` would, but instead of returning hits, dumps
        every signal needed to debug a config: HTTP status, response
        headers, byte count, content-type, anti-bot signatures, regex
        match count, the first three matches with their positions, and a
        sample of the response body. Use this whenever a marketplace
        returns "no matches" so you can see why.
        :param name: Marketplace name from the MARKETPLACES valve.
        :param query: Test query (defaults to "test"; pass something likely
                      to have results).
        :return: Markdown diagnostic report.
        """
        try:
            mps = self._marketplaces()
        except ValueError as e:
            return f"MARKETPLACES configuration error: {e}"
        match = next((m for m in mps if m["name"].lower() == name.lower()), None)
        if not match:
            names = ", ".join(m["name"] for m in mps) or "(none)"
            return f"No marketplace named '{name}'. Configured: {names}"
        return await self._probe_config(match, query)

    async def test_marketplace_config(
        self,
        config_json: str,
        query: str = "test",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Try a marketplace config WITHOUT saving it to the MARKETPLACES
        valve. Pass the same JSON object you'd add to MARKETPLACES; this
        runs a probe with `query` and reports what worked and what didn't.
        Iterate on the regex here, then commit the working config to the
        valve.
        :param config_json: JSON object string with name, search_url,
                            result_pattern, optional download_pattern.
        :param query: Test query.
        :return: Markdown diagnostic report.
        """
        try:
            cfg = json.loads(config_json)
        except json.JSONDecodeError as e:
            return f"config_json is not valid JSON: {e}"
        if not isinstance(cfg, dict):
            return "config_json must be a JSON object."
        for k in ("name", "search_url", "result_pattern"):
            if not cfg.get(k):
                return f"config missing required field '{k}'."
        return await self._probe_config(cfg, query)

    async def _probe_config(self, cfg: dict, query: str) -> str:
        """Shared probe logic for probe_marketplace + test_marketplace_config."""
        name = cfg.get("name", "<unnamed>")
        try:
            search_url = cfg["search_url"].format(query=quote(query, safe=""))
        except (KeyError, IndexError):
            return f"search_url for '{name}' must contain {{query}} placeholder."
        try:
            pat = re.compile(cfg["result_pattern"], flags=re.IGNORECASE | re.DOTALL)
        except re.error as e:
            return f"result_pattern for '{name}' is not a valid regex: {e}"
        dl_pat: Optional[re.Pattern] = None
        if cfg.get("download_pattern"):
            try:
                dl_pat = re.compile(cfg["download_pattern"], flags=re.IGNORECASE | re.DOTALL)
            except re.error as e:
                return f"download_pattern for '{name}' is not a valid regex: {e}"

        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT, follow_redirects=self.valves.FOLLOW_REDIRECTS) as c:
            try:
                r = await c.get(search_url, headers=self._extra_headers())
            except Exception as e:
                return f"PROBE — request failed: {e}"
            html = r.text
            hops = " → ".join(str(h.url) for h in r.history) + (" → " if r.history else "")
            ctype = r.headers.get("content-type", "—")
            blockers = self._detect_blockers(html, dict(r.headers))

            hits = list(pat.finditer(html))
            sample_matches: list[str] = []
            for h in hits[:3]:
                groups = h.groups()
                pos = f"@{h.start()}"
                if len(groups) >= 2:
                    sample_matches.append(f"  - {pos}  url=`{groups[0][:120]}`  title=`{groups[1][:120]}`")
                elif groups:
                    sample_matches.append(f"  - {pos}  group(1)=`{groups[0][:200]}`")
                else:
                    sample_matches.append(f"  - {pos}  full match=`{h.group(0)[:200]}`")

            # Generic-DL probe on the first hit's page (if any), to also
            # validate download extraction works.
            dl_diag = "  (skipped — no result_pattern hits)"
            if hits:
                first = hits[0]
                first_url = first.group(1) if first.groups() else first.group(0)
                if first_url:
                    base = f"{urlparse(search_url).scheme}://{urlparse(search_url).netloc}"
                    full = first_url if first_url.startswith("http") else urljoin(base + "/", first_url)
                    try:
                        rr = await c.get(full, headers=self._extra_headers())
                        dlhtml = rr.text
                        use_pat = dl_pat or _GENERIC_DL_REGEX
                        dl_hits = list(use_pat.finditer(dlhtml))
                        dl_blockers = self._detect_blockers(dlhtml, dict(rr.headers))
                        sample_dl: list[str] = []
                        for m in dl_hits[:5]:
                            try:
                                u = m.group(1)
                            except IndexError:
                                u = m.group(0)
                            sample_dl.append(f"      - {u[:200]}")
                        dl_diag = (
                            f"  fetched first result page: {full}\n"
                            f"    status: {rr.status_code}  ·  bytes: {len(dlhtml):,}  ·  ctype: {rr.headers.get('content-type','—')}\n"
                            f"    blocker hints: {dl_blockers or 'none'}\n"
                            f"    download_pattern: {'custom' if dl_pat else 'generic'}\n"
                            f"    download links matched: {len(dl_hits)}\n"
                            + ("    sample download URLs:\n" + "\n".join(sample_dl) if sample_dl else "    (no download links — see body sample below)\n"
                               + f"      body sample: {_strip_html(dlhtml, 600)}")
                        )
                    except Exception as e:
                        dl_diag = f"  download-page fetch failed: {e}"

        verdict = (
            "✅ search_pattern works" if hits else
            ("⚠️  blocker detected — generic regex probably won't help" if blockers else "❌ search_pattern matched 0 times")
        )
        return (
            f"# Probe: {name}  (query='{query}')\n"
            f"## Search\n"
            f"  url:        {search_url}\n"
            f"  redirects:  {hops}{r.url}\n"
            f"  status:     {r.status_code}\n"
            f"  ctype:      {ctype}\n"
            f"  bytes:      {len(html):,}\n"
            f"  blocker hints: {blockers or 'none'}\n"
            f"  result_pattern matches: {len(hits)}  →  {verdict}\n"
            + (("  sample matches:\n" + "\n".join(sample_matches)) if sample_matches else "")
            + f"\n\n## Download extraction (first hit)\n{dl_diag}\n"
            f"\n## Body sample (first 1500 chars, HTML-stripped)\n  {_strip_html(html, 1500)}"
        )

    async def probe_download(
        self,
        url: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        HEAD a download URL (with a ranged-GET fallback for servers that
        don't support HEAD) and report what would be downloaded — final
        URL after redirects, content-type, content-length, server,
        last-modified. Use to validate an extracted download link before
        committing to a real download.
        :param url: Direct download URL.
        :return: Markdown report.
        """
        return await self.download(url, dry_run=True)

    def recipe_templates(self, __user__: Optional[dict] = None) -> str:
        """
        Return ready-to-paste regex starter templates for common CMS / site
        layouts. Copy the closest one into a MARKETPLACES entry and adapt.
        :return: Markdown with named templates and JSON snippets.
        """
        templates = [
            ("WordPress (entry-title pattern — most blogs / news / many download sites)", {
                "name": "Example WordPress",
                "search_url": "https://example.com/?s={query}",
                "result_pattern": r'<h2[^>]*class="[^"]*entry-title[^"]*"[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>([^<]+)</a>',
                "download_pattern": r'href="(https?://[^"]+\.(?:zip|7z|rar|iso|exe|dmg|tar(?:\.[gx]z)?|tgz))"',
            }),
            ("Generic article cards (h2/h3 → a)", {
                "name": "Example",
                "search_url": "https://example.com/search?q={query}",
                "result_pattern": r'<(?:h2|h3)[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>([^<]+)</a>',
            }),
            ("Game-style listing — figure/card with title under it", {
                "name": "Example",
                "search_url": "https://example.com/?s={query}",
                "result_pattern": r'<a[^>]+href="(/games?/[^"]+)"[^>]*>\s*(?:<img[^>]*>)?\s*<(?:span|div|h\d)[^>]*>([^<]+)</',
            }),
            ("Discourse forum (topic listing)", {
                "name": "Example Discourse",
                "search_url": "https://forum.example.com/search?q={query}",
                "result_pattern": r'<a[^>]+class="[^"]*search-link[^"]*"[^>]+href="([^"]+)"[^>]*>([^<]+)</a>',
            }),
            ("MediaWiki (use the API instead — more reliable than scraping)", {
                "name": "Example MediaWiki",
                "search_url": "https://wiki.example.com/api.php?action=query&list=search&srsearch={query}&format=json&srlimit=10",
                "result_pattern": r'"title":"([^"]+)".*?"snippet":"([^"]*)"',
            }),
            ("Direct-download magnet pages (TPB-style)", {
                "name": "Example tracker",
                "search_url": "https://example.com/search?q={query}",
                "result_pattern": r'<a[^>]+href="(/torrent/\d+/[^"]+)"[^>]*>([^<]+)</a>',
                "download_pattern": r'(magnet:\?xt=urn:btih:[a-fA-F0-9]+[^"\']*)',
            }),
        ]
        out = ["## Marketplace recipe templates\n",
               "Copy the closest template into a MARKETPLACES entry, adapt the URL/regex.",
               "After editing, run `test_marketplace_config(json_str, query='something')` to verify.",
               "When the probe returns ✅, commit the config to the MARKETPLACES valve.\n"]
        for label, cfg in templates:
            out.append(f"### {label}")
            out.append("```json")
            out.append(json.dumps(cfg, indent=2))
            out.append("```\n")
        out.append("## Limits to be honest about")
        out.append("- **JS-rendered SPAs** (Next.js, Nuxt, React, Angular) won't work — the server returns an empty shell. Use the headless_browser tool to render first, then feed HTML to extract_downloads.")
        out.append("- **Cloudflare / DDoS-Guard / CAPTCHA** challenges will return interstitials. probe_marketplace flags these in `blocker hints`.")
        out.append("- **Auth-walled sites** need cookies in the REQUEST_HEADERS valve.")
        out.append("- **POST search forms** aren't supported here — use a site that has a GET endpoint.")
        out.append("- **Pagination** isn't automatic — bake the page number into search_url if needed.")
        return "\n".join(out)

    async def search_marketplace(
        self,
        name: str,
        query: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search a single configured marketplace by `name`. Pulls
        search_url.format(query=...), runs result_pattern across the response,
        returns matched (URL, title) pairs.
        :param name: The `name` of one entry in the MARKETPLACES valve.
        :param query: Title to search for.
        :return: Markdown list of result pages.
        """
        try:
            mps = self._marketplaces()
        except ValueError as e:
            return f"MARKETPLACES configuration error: {e}"
        match = next((m for m in mps if m["name"].lower() == name.lower()), None)
        if not match:
            names = ", ".join(m["name"] for m in mps) or "(none)"
            return f"No marketplace named '{name}'. Configured: {names}"
        try:
            search_url = match["search_url"].format(query=quote(query, safe=""))
        except (KeyError, IndexError):
            return f"search_url for '{name}' must contain {{query}} placeholder."
        try:
            pat = re.compile(match["result_pattern"], flags=re.IGNORECASE | re.DOTALL)
        except re.error as e:
            return f"result_pattern for '{name}' is not a valid regex: {e}"

        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT, follow_redirects=self.valves.FOLLOW_REDIRECTS) as c:
            try:
                r = await c.get(search_url, headers=self._extra_headers())
            except Exception as e:
                return f"{name}: request failed: {e}"
            if r.status_code >= 400:
                return f"{name}: HTTP {r.status_code} from {search_url}"
            html = r.text

        hits = pat.findall(html)
        if not hits:
            # Make the failure diagnosable instead of silent: dump enough of
            # the response that the user can see what their regex needs to
            # match, plus surface obvious anti-bot signatures.
            blockers = self._detect_blockers(html, dict(r.headers))
            sample = _strip_html(html, 1200) if html else "(empty body)"
            return (
                f"{name}: no result_pattern matches for '{query}'.\n"
                f"  search_url: {search_url}\n"
                f"  status: {r.status_code}  ·  bytes: {len(html):,}  ·  ctype: {r.headers.get('content-type','—')}\n"
                f"  blocker hints: {blockers or 'none detected'}\n\n"
                f"  --- response sample (HTML stripped, first 1200 chars) ---\n  {sample}\n"
                f"  --- raw HTML head (first 600 chars) ---\n  {html[:600]}\n\n"
                f"  Try `probe_marketplace('{name}', '{query}')` for a richer diagnostic, "
                f"or `recipe_templates()` for known-good regex starting points."
            )

        base = f"{urlparse(search_url).scheme}://{urlparse(search_url).netloc}"
        out = [f"## {name}: {query}\n"]
        for h in hits[: self.valves.MAX_RESULTS]:
            url, title = (h[0], h[1]) if isinstance(h, tuple) else (h, "")
            full = url if url.startswith("http") else urljoin(base + "/", url)
            out.append(f"- **{html_mod.unescape(title or full)}**\n   {full}")
        return "\n".join(out)

    async def search_all_marketplaces(
        self,
        query: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Run `search_marketplace` against every configured marketplace in
        parallel and return one combined digest.
        :param query: Title to search for.
        :return: Combined Markdown.
        """
        try:
            mps = self._marketplaces()
        except ValueError as e:
            return f"MARKETPLACES configuration error: {e}"
        if not mps:
            return "No marketplaces configured (see list_marketplaces)."
        results = await asyncio.gather(
            *(self.search_marketplace(m["name"], query) for m in mps),
            return_exceptions=True,
        )
        out = [f"# Configured-marketplace search: {query}"]
        for m, res in zip(mps, results):
            if isinstance(res, Exception):
                out.append(f"\n## {m['name']}\n_(failed: {res})_")
            else:
                out.append("\n" + str(res))
        return "\n".join(out)

    async def extract_downloads(
        self,
        page_url: str,
        marketplace_name: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Fetch a marketplace item page and extract direct-download links from
        it. If `marketplace_name` is set and the marketplace has a custom
        `download_pattern` regex, that's used; otherwise a generic regex
        matches `<a href="…">` URLs ending in zip/7z/rar/iso/torrent and
        magnet: links.
        :param page_url: Item / detail page URL.
        :param marketplace_name: Optional — use this marketplace's custom
                                 download_pattern if set.
        :return: Markdown list of extracted download URLs.
        """
        custom_pat: Optional[re.Pattern] = None
        if marketplace_name:
            try:
                mps = self._marketplaces()
            except ValueError as e:
                return f"MARKETPLACES configuration error: {e}"
            mp = next((m for m in mps if m["name"].lower() == marketplace_name.lower()), None)
            if mp and mp.get("download_pattern"):
                try:
                    custom_pat = re.compile(mp["download_pattern"], flags=re.IGNORECASE | re.DOTALL)
                except re.error as e:
                    return f"download_pattern for '{marketplace_name}' is not a valid regex: {e}"

        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT, follow_redirects=self.valves.FOLLOW_REDIRECTS) as c:
            try:
                r = await c.get(page_url, headers=self._extra_headers())
            except Exception as e:
                return f"page fetch failed: {e}"
            if r.status_code >= 400:
                return f"HTTP {r.status_code} from {page_url}"
            html = r.text

        pat = custom_pat or _GENERIC_DL_REGEX
        urls = []
        for m in pat.finditer(html):
            try:
                u = m.group(1)
            except IndexError:
                u = m.group(0)
            if not u:
                continue
            if not u.startswith(("http", "magnet:")):
                u = urljoin(page_url, u)
            if u not in urls:
                urls.append(u)
        if not urls:
            blockers = self._detect_blockers(html, dict(r.headers))
            return (
                f"No download links matched on {page_url} (pattern: {'custom' if custom_pat else 'generic'}).\n"
                f"  status: {r.status_code}  ·  bytes: {len(html):,}  ·  ctype: {r.headers.get('content-type','—')}\n"
                f"  blocker hints: {blockers or 'none detected'}\n"
                f"  Tip: many sites hide the download URL behind a JS click-handler or a "
                f"second redirect page. If so, point download_pattern at the JS-bridge URL "
                f"or open the link with the headless_browser tool first."
            )
        out = [f"## Download links extracted from {page_url}\n"]
        for u in urls[:30]:
            out.append(f"- {u}")
        return "\n".join(out)

    async def download(
        self,
        url: str,
        filename: str = "",
        dry_run: bool = False,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Stream-download a single URL into DOWNLOAD_DIR. Gated behind
        WRITE_ENABLED. Magnet links are not downloaded here — copy them to
        the qBittorrent tool instead. When `dry_run=True`, runs a HEAD
        request and reports what *would* be downloaded (final URL after
        redirects, content-type, content-length, server) without writing.
        :param url: Direct download URL (http(s)://).
        :param filename: Optional override; defaults to the URL's basename.
        :param dry_run: When True, HEAD only — no file written.
        :return: Final path on disk + bytes written.
        """
        if url.startswith("magnet:"):
            return "Magnet links are not downloaded by this tool — pass to the qBittorrent tool's add_torrent."
        if not url.startswith("http"):
            return f"Refusing non-http(s) URL: {url}"

        # Dry-run: HEAD only, no write, no WRITE_ENABLED check.
        if dry_run:
            async with httpx.AsyncClient(timeout=self.valves.TIMEOUT, follow_redirects=self.valves.FOLLOW_REDIRECTS) as c:
                try:
                    h = await c.head(url, headers=self._extra_headers())
                except Exception as e:
                    return f"dry-run HEAD failed: {e}"
                # Some servers return 405 for HEAD; fall back to a 1-byte ranged GET.
                if h.status_code in (405, 501):
                    headers = dict(self._extra_headers())
                    headers["Range"] = "bytes=0-0"
                    try:
                        h = await c.get(url, headers=headers)
                    except Exception as e:
                        return f"dry-run ranged-GET failed: {e}"
                ctype = h.headers.get("content-type", "—")
                clen = h.headers.get("content-length")
                final_url = str(h.url)
                hops = " → ".join(str(rh.url) for rh in h.history) + (" → " if h.history else "")
                warn = ""
                if clen and int(clen) < 4096 and "html" in ctype.lower():
                    warn = "\n  ⚠️  small payload + HTML content-type — likely an interstitial / login wall, not the actual file."
                return (
                    f"DRY RUN — would download:\n"
                    f"  url:        {url}\n"
                    f"  redirects:  {hops}{final_url}\n"
                    f"  status:     {h.status_code}\n"
                    f"  ctype:      {ctype}\n"
                    f"  size:       {int(clen):,} bytes" if clen else f"  size:       (server didn't send Content-Length)"
                ) + (f"\n  server:     {h.headers.get('server', '—')}\n  modified:   {h.headers.get('last-modified', '—')}{warn}")

        if not self.valves.WRITE_ENABLED:
            return "Download blocked: flip WRITE_ENABLED in this tool's Valves first (or call with dry_run=True to preview)."
        out_dir = Path(self.valves.DOWNLOAD_DIR).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)

        if not filename:
            base = os.path.basename(urlparse(url).path) or "download"
            ext = (base.rsplit(".", 1) + ["bin"])[-1] if "." in base else "bin"
            filename = _safe_filename(base, default_ext=ext)
        else:
            filename = _safe_filename(filename)
        dest = out_dir / filename

        bytes_written = 0
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT, follow_redirects=self.valves.FOLLOW_REDIRECTS) as c:
            try:
                async with c.stream("GET", url, headers=self._extra_headers()) as resp:
                    if resp.status_code >= 400:
                        return f"HTTP {resp.status_code} from {url}"
                    ctype = resp.headers.get("content-type", "")
                    if "text/html" in ctype.lower() and resp.headers.get("content-length") and int(resp.headers["content-length"]) < 8192:
                        return (
                            f"Refused: server returned tiny HTML ({resp.headers.get('content-length')} bytes, ctype={ctype}) "
                            f"— that's almost certainly an interstitial, not the file. "
                            f"Try `download(url, dry_run=True)` to inspect, or use the headless_browser tool."
                        )
                    with dest.open("wb") as fh:
                        async for chunk in resp.aiter_bytes(chunk_size=128 * 1024):
                            fh.write(chunk)
                            bytes_written += len(chunk)
            except Exception as e:
                return f"download failed after {bytes_written:,} bytes: {e}"

        return f"Wrote {bytes_written:,} bytes → {dest}"

    async def search_and_extract(
        self,
        name: str,
        query: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Convenience: search a marketplace, then for each result page also
        run extract_downloads. Returns one digest with item titles plus
        every download URL discovered on each item's page.
        :param name: Marketplace name (must exist in MARKETPLACES).
        :param query: Title to search.
        :return: Markdown digest.
        """
        try:
            mps = self._marketplaces()
        except ValueError as e:
            return f"MARKETPLACES configuration error: {e}"
        match = next((m for m in mps if m["name"].lower() == name.lower()), None)
        if not match:
            names = ", ".join(m["name"] for m in mps) or "(none)"
            return f"No marketplace named '{name}'. Configured: {names}"
        try:
            search_url = match["search_url"].format(query=quote(query, safe=""))
        except (KeyError, IndexError):
            return f"search_url for '{name}' must contain {{query}} placeholder."
        try:
            pat = re.compile(match["result_pattern"], flags=re.IGNORECASE | re.DOTALL)
        except re.error as e:
            return f"result_pattern for '{name}' is not a valid regex: {e}"

        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT, follow_redirects=self.valves.FOLLOW_REDIRECTS) as c:
            r = await c.get(search_url, headers=self._extra_headers())
            if r.status_code >= 400:
                return f"{name}: HTTP {r.status_code} from {search_url}"
            html = r.text
            hits = pat.findall(html)
        if not hits:
            return f"{name}: no result_pattern matches for '{query}'"

        base = f"{urlparse(search_url).scheme}://{urlparse(search_url).netloc}"
        items: list[tuple[str, str]] = []
        for h in hits[: self.valves.MAX_RESULTS]:
            url, title = (h[0], h[1]) if isinstance(h, tuple) else (h, "")
            full = url if url.startswith("http") else urljoin(base + "/", url)
            items.append((html_mod.unescape(title or full), full))

        # Walk each item page in parallel, extracting downloads.
        coros = [self.extract_downloads(u, marketplace_name=name) for _, u in items]
        extracts = await asyncio.gather(*coros, return_exceptions=True)
        out = [f"# {name}: {query}"]
        for (title, page), ex in zip(items, extracts):
            out.append(f"\n## {title}\n   page: {page}")
            if isinstance(ex, Exception):
                out.append(f"   _(extract failed: {ex})_")
            else:
                out.append(str(ex))
        return "\n".join(out)

    # ── Aggregator ────────────────────────────────────────────────────────

    async def find_free(
        self,
        query: str = "",
        include_archives: bool = True,
        include_foss: bool = True,
        include_marketplaces: bool = True,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        One-shot search across every layer:
          - currently-free promos: GamerPower, Epic, Steam, GOG, itch.io,
            Game Jolt
          - preservation archives (when `include_archives`): Internet
            Archive Software Library, MyAbandonware, OldGamesDownload,
            Abandonia
          - FOSS games (when `include_foss`): libregamewiki, FreeGameDev
            wiki
          - every entry in the MARKETPLACES valve (when
            `include_marketplaces` and one or more marketplaces are
            configured)
        :param query: Title or keyword. When empty, returns only the
                      promotional sources (which list whatever's free now).
        :param include_archives: Include preservation archives (only useful
                                 when `query` is set).
        :param include_foss: Include FOSS-game wiki searches.
        :param include_marketplaces: Include every configured marketplace.
        :return: Combined Markdown digest.
        """
        coros: list[Any] = [
            self.gamerpower(),
            self.epic_free(),
            self.steam_free(),
            self.gog_free(),
        ]
        labels = [
            "GamerPower (cross-platform giveaways)",
            "Epic Games Store (weekly free)",
            "Steam (free-to-play / 100%-off)",
            "GOG.com ($0 catalogue + giveaway)",
        ]
        if query:
            coros.append(self.itchio_free(query))
            coros.append(self.gamejolt_free(query))
            labels.append("itch.io (free indies — query-filtered)")
            labels.append("Game Jolt (free indies)")
        if query and include_archives:
            coros.append(self.ia_software(query, collection="softwarelibrary"))
            coros.append(self.myabandonware(query))
            coros.append(self.oldgamesdownload(query))
            coros.append(self.abandonia(query))
            labels += [
                "Internet Archive Software Library (preservation, often browser-playable)",
                "MyAbandonware (abandoned commercial titles)",
                "OldGamesDownload (abandoned commercial titles)",
                "Abandonia (abandoned commercial titles)",
            ]
        if query and include_foss:
            coros.append(self.libregamewiki(query))
            coros.append(self.freegamedev(query))
            labels.append("libregamewiki (FOSS games)")
            labels.append("FreeGameDev wiki (FOSS games)")
        if query and include_marketplaces:
            try:
                mps = self._marketplaces()
            except ValueError:
                mps = []
            for m in mps:
                coros.append(self.search_marketplace(m["name"], query))
                labels.append(f"Marketplace: {m['name']} (user-configured)")

        results = await asyncio.gather(*coros, return_exceptions=True)
        out = [f"# Free / archived games digest" + (f": {query}" if query else "")]
        for label, res in zip(labels, results):
            if isinstance(res, Exception):
                out.append(f"\n## {label}\n_(failed: {res})_")
            else:
                out.append("\n" + str(res))
        return "\n".join(out)
