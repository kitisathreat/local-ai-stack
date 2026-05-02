"""
title: Torrent Aggregators — Knaben, Solid Torrents, 1337x, TorrentGalaxy, BTDigg
author: local-ai-stack
description: Crawl meta-search aggregators that index dozens of public trackers at once. Knaben (api.knaben.eu) and Solid Torrents (solidtorrents.to) expose JSON endpoints; 1337x and TorrentGalaxy require HTML parsing but cover the long tail of TV/movie/anime releases that the YTS/EZTV/Nyaa-shaped tools miss; BTDigg surfaces DHT-discovered torrents that aren't on any conventional tracker. Returns name, seeders/leechers, size, source, and a magnet URI / .torrent link to hand to the qBittorrent tool. This is a *discovery* layer — the user's torrent client is what actually downloads.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

import asyncio
import re
import urllib.parse
from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field


_UA = "Mozilla/5.0 (X11; Linux x86_64) local-ai-stack/1.0 torrent-aggregators"

# Default well-known trackers added to bare info-hash magnets.
_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://tracker.dler.org:6969/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.tiny-vps.com:6969/announce",
]


def _hash_to_magnet(infohash: str, name: str = "") -> str:
    if not infohash or len(infohash) not in (40, 32):
        return ""
    qs = "&".join(
        ["xt=urn:btih:" + infohash]
        + ([f"dn={urllib.parse.quote(name)}"] if name else [])
        + [f"tr={urllib.parse.quote(t)}" for t in _TRACKERS]
    )
    return f"magnet:?{qs}"


def _human(n: Any) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    units = ["B", "KB", "MB", "GB", "TB"]
    i, f = 0, float(n)
    while f >= 1024 and i < len(units) - 1:
        f /= 1024
        i += 1
    return f"{f:.2f} {units[i]}"


class Tools:
    class Valves(BaseModel):
        KNABEN_URL: str = Field(
            default="https://api.knaben.eu/v1",
            description="Knaben meta-search JSON API base URL.",
        )
        SOLIDTORRENTS_URL: str = Field(
            default="https://solidtorrents.to",
            description="Solid Torrents base URL (single-site, JSON API at /api/v1/search).",
        )
        L337X_URL: str = Field(
            default="https://1337x.to",
            description="1337x mirror base URL. Try 1337x.to, 1337xx.to, 1337x.is.",
        )
        TORRENTGALAXY_URL: str = Field(
            default="https://torrentgalaxy.to",
            description="TorrentGalaxy mirror base URL. Try torrentgalaxy.to, tgx.rs.",
        )
        BTDIGG_URL: str = Field(
            default="https://btdig.com",
            description="BTDigg DHT-search base URL. Try btdig.com, btdigg.com.",
        )
        DEFAULT_LIMIT: int = Field(default=15, description="Max results returned per indexer.")
        TIMEOUT: int = Field(default=20, description="HTTP timeout per source, seconds.")
        MIN_SEEDERS: int = Field(default=1, description="Filter floor for seeders.")

    def __init__(self):
        self.valves = self.Valves()

    # ── Knaben (meta) ─────────────────────────────────────────────────────

    async def search_knaben(
        self,
        query: str,
        category: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search Knaben — a meta-aggregator that pools 30+ public trackers
        through one JSON endpoint. Fastest "everything at once" path.
        :param query: Free-text query.
        :param category: Optional Knaben category — e.g. "movie", "tv", "anime", "audio".
                         Empty = all categories.
        :return: Title, source, size, seeders, magnet/link.
        """
        body: dict[str, Any] = {
            "search_type": "score",
            "search_field": "title",
            "query": query,
            "order_by": "seeders",
            "order_direction": "desc",
            "size": self.valves.DEFAULT_LIMIT,
        }
        if category:
            body["categories"] = [category]
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT) as c:
            try:
                r = await c.post(
                    self.valves.KNABEN_URL,
                    json=body,
                    headers={"User-Agent": _UA, "Accept": "application/json", "Content-Type": "application/json"},
                )
            except Exception as e:
                return f"Knaben request failed: {e}"
            if r.status_code >= 400:
                return f"Knaben error {r.status_code}: {r.text[:200]}"
            try:
                data = r.json()
            except Exception:
                return "Knaben returned non-JSON"

        hits = (data.get("hits") or data.get("results") or data.get("data") or [])
        if not hits:
            return f"Knaben: no results for {query}"

        out = [f"## Knaben (meta-search): {query}\n"]
        for it in hits[: self.valves.DEFAULT_LIMIT]:
            seeds = it.get("seeders", 0) or 0
            if seeds < self.valves.MIN_SEEDERS:
                continue
            ih = (it.get("hash") or it.get("infohash") or "").lower()
            magnet = it.get("magnet") or _hash_to_magnet(ih, it.get("title", ""))
            out.append(
                f"**{(it.get('title') or '')[:70]}**  [{it.get('tracker', '?')}]\n"
                f"   S={seeds}  L={it.get('leechers', 0)}  size={_human(it.get('bytes') or it.get('size', 0))}  cat={it.get('category', '?')}\n"
                f"   {magnet or it.get('details', '')}\n"
            )
        return "\n".join(out) if len(out) > 1 else f"Knaben: no results above MIN_SEEDERS={self.valves.MIN_SEEDERS}"

    # ── Solid Torrents (JSON) ─────────────────────────────────────────────

    async def search_solid_torrents(
        self,
        query: str,
        category: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search Solid Torrents via its public JSON endpoint.
        :param query: Free-text query.
        :param category: Optional category — "movie", "tv", "anime", "audio".
        :return: Title, size, seeders, magnet.
        """
        params = {"q": query, "sort": "seeders", "order": "desc", "skip": 0, "limit": self.valves.DEFAULT_LIMIT}
        if category:
            params["category"] = category
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT, follow_redirects=True) as c:
            try:
                r = await c.get(
                    f"{self.valves.SOLIDTORRENTS_URL.rstrip('/')}/api/v1/search",
                    params=params,
                    headers={"User-Agent": _UA, "Accept": "application/json"},
                )
            except Exception as e:
                return f"Solid Torrents request failed: {e}"
            if r.status_code >= 400:
                return f"Solid Torrents error {r.status_code}: {r.text[:200]}"
            try:
                data = r.json()
            except Exception:
                return "Solid Torrents returned non-JSON"

        items = data.get("results") if isinstance(data, dict) else data
        items = items or []
        if not items:
            return f"Solid Torrents: no results for {query}"

        out = [f"## Solid Torrents: {query}\n"]
        for it in items[: self.valves.DEFAULT_LIMIT]:
            seeds = it.get("swarm", {}).get("seeders") or it.get("seeders") or 0
            if seeds < self.valves.MIN_SEEDERS:
                continue
            ih = (it.get("infohash") or it.get("info_hash") or "").lower()
            magnet = it.get("magnet") or _hash_to_magnet(ih, it.get("title", ""))
            out.append(
                f"**{(it.get('title') or '')[:70]}**\n"
                f"   S={seeds}  L={it.get('swarm', {}).get('leechers', it.get('leechers', 0))}  size={_human(it.get('size', 0))}\n"
                f"   {magnet}\n"
            )
        return "\n".join(out) if len(out) > 1 else f"Solid Torrents: no results above MIN_SEEDERS={self.valves.MIN_SEEDERS}"

    # ── 1337x (HTML) ──────────────────────────────────────────────────────

    async def search_1337x(
        self,
        query: str,
        category: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search 1337x. No JSON API; this scrapes the search-results page and
        each detail page for the magnet URI.
        :param query: Free-text query.
        :param category: One of: Movies, TV, Anime, Music, Games, Apps, Documentaries.
                         Empty = all.
        :return: Title, size, seeders, magnet.
        """
        base = self.valves.L337X_URL.rstrip("/")
        q = urllib.parse.quote(query, safe="")
        if category:
            url = f"{base}/category-search/{q}/{category}/1/"
        else:
            url = f"{base}/search/{q}/1/"

        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT, follow_redirects=True) as c:
            try:
                r = await c.get(url, headers={"User-Agent": _UA})
            except Exception as e:
                return f"1337x request failed: {e}"
            if r.status_code == 404:
                return f"1337x: no results for {query}"
            if r.status_code >= 400:
                return f"1337x error {r.status_code}"
            html = r.text

            # Each result row: <td class="coll-1 name"><a href="/torrent/...">title</a></td>
            row_re = re.compile(
                r'<td class="coll-1 name">.*?<a href="(/torrent/[^"]+)"[^>]*>([^<]+)</a>.*?'
                r'<td class="coll-2 seeds">(\d+)</td>.*?'
                r'<td class="coll-3 leeches">(\d+)</td>.*?'
                r'<td class="coll-4 size[^"]*">([^<]+)</td>',
                re.DOTALL,
            )
            rows = row_re.findall(html)
            if not rows:
                return f"1337x: no parseable rows for {query}"

            # Walk the top results in parallel and grab their magnet links.
            sem = asyncio.Semaphore(4)

            async def magnet_of(detail_path: str) -> str:
                async with sem:
                    try:
                        rr = await c.get(f"{base}{detail_path}", headers={"User-Agent": _UA})
                        if rr.status_code != 200:
                            return ""
                        m = re.search(r'href="(magnet:\?[^"]+)"', rr.text)
                        return m.group(1).replace("&amp;", "&") if m else ""
                    except Exception:
                        return ""

            top = rows[: self.valves.DEFAULT_LIMIT]
            magnets = await asyncio.gather(*(magnet_of(r[0]) for r in top))

        out = [f"## 1337x: {query}\n"]
        for (path, title, seeds, leech, size), mag in zip(top, magnets):
            if int(seeds) < self.valves.MIN_SEEDERS:
                continue
            out.append(
                f"**{title.strip()}**\n"
                f"   S={seeds}  L={leech}  size={size.split()[0]} {size.split()[1] if len(size.split())>1 else ''}\n"
                f"   {mag or base + path}\n"
            )
        return "\n".join(out) if len(out) > 1 else f"1337x: no results above MIN_SEEDERS={self.valves.MIN_SEEDERS}"

    # ── TorrentGalaxy (HTML) ──────────────────────────────────────────────

    async def search_torrentgalaxy(
        self,
        query: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search TorrentGalaxy. Strong on movies, TV, anime; HTML scrape.
        :param query: Free-text query.
        :return: Title, size, seeders, magnet.
        """
        base = self.valves.TORRENTGALAXY_URL.rstrip("/")
        q = urllib.parse.quote(query, safe="")
        url = f"{base}/torrents.php?search={q}&sort=seeders&order=desc"
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT, follow_redirects=True) as c:
            try:
                r = await c.get(url, headers={"User-Agent": _UA})
            except Exception as e:
                return f"TorrentGalaxy request failed: {e}"
            if r.status_code >= 400:
                return f"TorrentGalaxy error {r.status_code}"
            html = r.text

        # TG inlines magnet links on the listing page.
        block_re = re.compile(
            r'(?is)<div class="tgxtablerow[^"]*">(.*?)</div>\s*</div>'
        )
        magnet_re = re.compile(r'(magnet:\?[^"\']+)')
        title_re = re.compile(r'<a[^>]+href="/torrent/[^"]+"[^>]*title="([^"]+)"')
        seeders_re = re.compile(r'<font color="green"[^>]*>\s*<b>\s*(\d+)\s*</b>', re.IGNORECASE)
        size_re = re.compile(r'<span class="badge[^"]*">\s*([\d.]+\s*[KMGT]?B)\s*</span>', re.IGNORECASE)

        rows = []
        for blk in block_re.findall(html):
            t = title_re.search(blk)
            m = magnet_re.search(blk)
            s = seeders_re.search(blk)
            sz = size_re.search(blk)
            if not (t and m):
                continue
            seeds = int(s.group(1)) if s else 0
            if seeds < self.valves.MIN_SEEDERS:
                continue
            rows.append({
                "title": t.group(1),
                "magnet": m.group(1).replace("&amp;", "&"),
                "seeds": seeds,
                "size": sz.group(1) if sz else "?",
            })
            if len(rows) >= self.valves.DEFAULT_LIMIT:
                break

        if not rows:
            return f"TorrentGalaxy: no parseable results for {query}"
        out = [f"## TorrentGalaxy: {query}\n"]
        for r in rows:
            out.append(
                f"**{r['title']}**\n"
                f"   S={r['seeds']}  size={r['size']}\n"
                f"   {r['magnet']}\n"
            )
        return "\n".join(out)

    # ── BTDigg (DHT) ──────────────────────────────────────────────────────

    async def search_btdigg(
        self,
        query: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search BTDigg — a DHT-crawled index. Surfaces torrents that aren't on
        any conventional tracker (long-tail, foreign-language). No seeder
        counts.
        :param query: Free-text query.
        :return: Title, size, hash, magnet.
        """
        base = self.valves.BTDIGG_URL.rstrip("/")
        q = urllib.parse.quote(query, safe="")
        url = f"{base}/search?q={q}"
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT, follow_redirects=True) as c:
            try:
                r = await c.get(url, headers={"User-Agent": _UA})
            except Exception as e:
                return f"BTDigg request failed: {e}"
            if r.status_code >= 400:
                return f"BTDigg error {r.status_code}"
            html = r.text

        # Each hit: <div class="one_result"> ... <a href="magnet:?xt=...">... <td>files=N</td><td>size=...</td>
        block_re = re.compile(r'(?is)<div class="one_result">(.*?)</div>\s*</div>')
        title_re = re.compile(r'<div class="torrent_name">\s*<h5>\s*<a[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
        magnet_re = re.compile(r'(magnet:\?[^"\']+)')
        size_re = re.compile(r'<span class="torrent_size"[^>]*>([^<]+)</span>')

        rows = []
        for blk in block_re.findall(html):
            t = title_re.search(blk)
            m = magnet_re.search(blk)
            sz = size_re.search(blk)
            if not (t and m):
                continue
            title = re.sub(r"<[^>]+>", "", t.group(1)).strip()
            rows.append({"title": title, "magnet": m.group(1).replace("&amp;", "&"), "size": sz.group(1) if sz else "?"})
            if len(rows) >= self.valves.DEFAULT_LIMIT:
                break

        if not rows:
            return f"BTDigg: no parseable results for {query}"
        out = [f"## BTDigg (DHT): {query}\n_(no live seeder counts; many results may be dead)_\n"]
        for r in rows:
            out.append(f"**{r['title']}**\n   size={r['size']}\n   {r['magnet']}\n")
        return "\n".join(out)

    # ── Aggregator ────────────────────────────────────────────────────────

    async def search_all(
        self,
        query: str,
        category: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Run every aggregator above in parallel and stitch results into one
        digest, sorted/grouped by source. Good when you don't already know
        which tracker has the title.
        :param query: Free-text query.
        :param category: Knaben/Solid Torrents category hint
                         ("movie", "tv", "anime", "audio"); ignored by the
                         HTML scrapers.
        :return: Combined Markdown digest.
        """
        coros = [
            self.search_knaben(query, category=category),
            self.search_solid_torrents(query, category=category),
            self.search_1337x(query),
            self.search_torrentgalaxy(query),
            self.search_btdigg(query),
        ]
        labels = ["Knaben (meta)", "Solid Torrents", "1337x", "TorrentGalaxy", "BTDigg"]
        results = await asyncio.gather(*coros, return_exceptions=True)
        out = [f"# Torrent aggregator digest: {query}"]
        for label, res in zip(labels, results):
            if isinstance(res, Exception):
                out.append(f"\n## {label}\n_(failed: {res})_")
            else:
                out.append("\n" + str(res))
        return "\n".join(out)
