"""
title: Free & Archived Games — GamerPower, Epic, Steam, GOG, itch.io, IA Software, MyAbandonware, libregamewiki
author: local-ai-stack
description: Find games that are legitimately free or legally archived. Three layers — (1) currently-free promotions across every storefront via GamerPower's aggregator + per-store endpoints (Epic weekly free, Steam free-to-play, GOG free-game promo, itch.io free indies); (2) game-preservation archives — Internet Archive's software library (DOS, Apple II, NES, browser-playable emulator), MyAbandonware, OldGamesDownload — for titles whose rightsholders have ceased commercial activity; (3) FOSS / open-source games via libregamewiki. Returns store / download URLs for each result. The aggregator runs every layer in parallel for one query.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

import asyncio
import html as html_mod
import json
import re
from typing import Any, Optional
from urllib.parse import quote, urlencode

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
MYABANDON = "https://www.myabandonware.com"
OLDGAMES = "https://www.oldgamesdownload.com"


def _strip_html(s: str, n: int = 600) -> str:
    s = html_mod.unescape(s or "")
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:n]


class Tools:
    class Valves(BaseModel):
        MAX_RESULTS: int = Field(default=10, description="Max items per source.")
        TIMEOUT: int = Field(default=20, description="HTTP timeout per request, seconds.")
        STEAM_REGION: str = Field(default="us", description="Country code for Steam endpoints.")
        STEAM_LANGUAGE: str = Field(default="english", description="Language for Steam endpoints.")
        GOG_LOCALE: str = Field(default="en-US", description="GOG locale.")
        ARCHIVE_ORG_PREFER_BROWSER_PLAYABLE: bool = Field(
            default=True,
            description="Prefer Internet Archive items that are browser-playable via emulator.",
        )

    def __init__(self):
        self.valves = self.Valves()

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

    # ── Aggregator ────────────────────────────────────────────────────────

    async def find_free(
        self,
        query: str = "",
        include_archives: bool = True,
        include_foss: bool = True,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        One-shot search: currently-free promos (GamerPower + Epic + Steam +
        GOG + itch.io) plus, when `include_archives`, preservation archives
        (Internet Archive software library + MyAbandonware +
        OldGamesDownload), plus FOSS games (libregamewiki) when
        `include_foss`.
        :param query: Title or keyword. When empty, returns only the
                      promotional sources (which list whatever's free now).
        :param include_archives: Include preservation archives (only useful
                                 when `query` is set).
        :param include_foss: Include libregamewiki FOSS-games search.
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
            labels.append("itch.io (free indies — query-filtered)")
        if query and include_archives:
            coros.append(self.ia_software(query, collection="softwarelibrary"))
            coros.append(self.myabandonware(query))
            coros.append(self.oldgamesdownload(query))
            labels += [
                "Internet Archive Software Library (preservation, often browser-playable)",
                "MyAbandonware (abandoned commercial titles)",
                "OldGamesDownload (abandoned commercial titles)",
            ]
        if query and include_foss:
            coros.append(self.libregamewiki(query))
            labels.append("libregamewiki (FOSS games)")

        results = await asyncio.gather(*coros, return_exceptions=True)
        out = [f"# Free / archived games digest" + (f": {query}" if query else "")]
        for label, res in zip(labels, results):
            if isinstance(res, Exception):
                out.append(f"\n## {label}\n_(failed: {res})_")
            else:
                out.append("\n" + str(res))
        return "\n".join(out)
