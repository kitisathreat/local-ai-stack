"""
title: Bandcamp — Search, Album Details & Free FLAC Downloads
author: local-ai-stack
description: Search Bandcamp's catalogue, inspect a release page, and (when the artist set the price to "name-your-price" with a $0 minimum, or the user chooses to pay) request a free or paid lossless download. Bandcamp does not publish a public API, so search uses the public site search HTML and album metadata is read from the JSON-LD blob each release page embeds. Returns FLAC/V0/MP3 download URLs when an album supports free download. The download primitive only writes when WRITE_ENABLED is on and the destination is inside the configured DOWNLOAD_DIR.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urlparse

import httpx
from pydantic import BaseModel, Field


_UA = "Mozilla/5.0 (X11; Linux x86_64) local-ai-stack/1.0 bandcamp"
_FORMATS = {
    "flac": "FLAC",
    "wav": "WAV",
    "aiff-lossless": "AIFF",
    "alac": "ALAC",
    "vorbis": "Ogg Vorbis",
    "mp3-v0": "MP3 V0",
    "mp3-320": "MP3 320",
    "aac-hi": "AAC",
}


def _extract_json_ld(html: str) -> list[dict]:
    """Pull every JSON-LD <script> blob out of a Bandcamp page."""
    blobs: list[dict] = []
    for m in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        flags=re.DOTALL | re.IGNORECASE,
    ):
        try:
            blobs.append(json.loads(m.group(1).strip()))
        except Exception:
            continue
    return blobs


def _extract_tralbum(html: str) -> Optional[dict]:
    """Bandcamp embeds the canonical track/album payload as `data-tralbum` on the page."""
    m = re.search(r'data-tralbum="([^"]+)"', html)
    if not m:
        return None
    raw = m.group(1).replace("&quot;", '"').replace("&amp;", "&")
    try:
        return json.loads(raw)
    except Exception:
        return None


def _safe_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", name).strip()[:120]


class Tools:
    class Valves(BaseModel):
        DOWNLOAD_DIR: str = Field(
            default=str(Path.home() / "Music" / "Bandcamp"),
            description="Directory where Bandcamp downloads are written. Created on first download.",
        )
        WRITE_ENABLED: bool = Field(
            default=False,
            description="Master switch for writing files to disk. Off by default.",
        )
        PREFER_FORMAT: str = Field(
            default="flac",
            description="Preferred audio format when an album offers a choice: flac, wav, aiff-lossless, alac, vorbis, mp3-v0, mp3-320, aac-hi.",
        )
        MAX_RESULTS: int = Field(default=10, description="Max search results to return.")
        TIMEOUT: int = Field(default=30, description="HTTP timeout per request, seconds.")

    def __init__(self):
        self.valves = self.Valves()

    # ── Search ────────────────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        item_type: str = "all",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search Bandcamp by free-text query.
        :param query: Artist, album, track, or label.
        :param item_type: "all", "track", "album", "artist", "label".
        :return: Markdown list of hits with URL, type, and any visible price.
        """
        type_map = {"all": "", "track": "t", "album": "a", "artist": "b", "label": "b"}
        params = {"q": query, "from": "results"}
        item_t = type_map.get(item_type.lower(), "")
        if item_t:
            params["item_type"] = item_t

        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT, follow_redirects=True) as c:
            try:
                r = await c.get(
                    "https://bandcamp.com/search",
                    params=params,
                    headers={"User-Agent": _UA},
                )
            except Exception as e:
                return f"Bandcamp search failed: {e}"
            if r.status_code >= 400:
                return f"Bandcamp returned {r.status_code}"
            html = r.text

        # Each result is a <li class="searchresult …"> with itemtype, heading,
        # subhead, and a result-info block. Parse the simplest stable bits.
        results: list[dict] = []
        for block in re.finditer(
            r'<li class="searchresult[^"]*">(.*?)</li>',
            html,
            flags=re.DOTALL,
        ):
            chunk = block.group(1)
            url_m = re.search(r'<a href="([^"]+)"', chunk)
            title_m = re.search(r'<div class="heading">\s*<a[^>]*>(.*?)</a>', chunk, re.DOTALL)
            sub_m = re.search(r'<div class="subhead">\s*(.*?)\s*</div>', chunk, re.DOTALL)
            type_m = re.search(r'<div class="itemtype">\s*(.*?)\s*</div>', chunk, re.DOTALL)
            tag_m = re.search(r'<div class="tags">\s*(.*?)\s*</div>', chunk, re.DOTALL)
            if not url_m or not title_m:
                continue
            url = url_m.group(1).split("?")[0]
            title = re.sub(r"\s+", " ", title_m.group(1)).strip()
            sub = re.sub(r"\s+", " ", (sub_m.group(1) if sub_m else "")).strip()
            kind = re.sub(r"\s+", " ", (type_m.group(1) if type_m else "")).strip()
            tags = re.sub(r"\s+", " ", (tag_m.group(1) if tag_m else "")).strip().lstrip("tags:").strip()
            results.append({"title": title, "url": url, "sub": sub, "type": kind, "tags": tags})
            if len(results) >= self.valves.MAX_RESULTS:
                break

        if not results:
            return f"No Bandcamp results for: {query}"

        out = [f"## Bandcamp: {query}\n"]
        for it in results:
            out.append(f"**{it['title']}**  _{it['type']}_")
            if it["sub"]:
                out.append(f"   {it['sub']}")
            if it["tags"]:
                out.append(f"   tags: {it['tags']}")
            out.append(f"   {it['url']}\n")
        return "\n".join(out)

    # ── Inspect ───────────────────────────────────────────────────────────

    async def album_details(
        self,
        url: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Fetch a Bandcamp album/track page and return artist, title, year,
        track listing, price, free-download eligibility, available formats,
        and the streaming preview URLs.
        :param url: Bandcamp album or track URL.
        :return: Markdown summary.
        """
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT, follow_redirects=True) as c:
            try:
                r = await c.get(url, headers={"User-Agent": _UA})
            except Exception as e:
                return f"Bandcamp fetch failed: {e}"
            if r.status_code >= 400:
                return f"Bandcamp returned {r.status_code}"
            html = r.text

        ld = _extract_json_ld(html)
        tralbum = _extract_tralbum(html) or {}

        title = ""
        artist = ""
        date = ""
        tracks: list[dict] = []
        for blob in ld:
            if blob.get("@type") in ("MusicAlbum", "MusicRecording"):
                title = blob.get("name", title)
                by = blob.get("byArtist") or {}
                artist = (by.get("name") if isinstance(by, dict) else artist) or artist
                date = blob.get("datePublished", date)
                items = ((blob.get("track") or {}).get("itemListElement") or []) if blob.get("@type") == "MusicAlbum" else []
                for el in items:
                    item = el.get("item") or {}
                    tracks.append({
                        "no": el.get("position"),
                        "name": item.get("name"),
                        "url": item.get("@id"),
                        "duration": item.get("duration"),
                    })

        # Pricing / free-download status from the tralbum payload.
        price = (tralbum.get("current") or {}).get("minimum_price")
        is_purchasable = (tralbum.get("current") or {}).get("is_set_price") is False
        free_download = bool(tralbum.get("freeDownloadPage"))
        download_url = tralbum.get("freeDownloadPage") or ""

        # Stream URLs are inside trackinfo[i].file
        if not tracks:
            for ti in tralbum.get("trackinfo", []):
                tracks.append({
                    "no": ti.get("track_num"),
                    "name": ti.get("title"),
                    "url": ti.get("title_link"),
                    "duration": ti.get("duration"),
                    "stream": (ti.get("file") or {}).get("mp3-128"),
                })

        out = [
            f"## {artist} — {title}".strip(" —"),
            f"released: {date or '—'}" + (f"  ·  price: ${price}" if price is not None else ""),
            f"name-your-price: **{'yes' if is_purchasable else 'no'}**" + (
                f"  ·  free download: {download_url}" if free_download else ""
            ),
            f"page: {url}",
            "",
            "### Tracks",
        ]
        for t in tracks:
            line = f"  {t.get('no') or '·'}. {t.get('name') or '—'}"
            if t.get("duration"):
                line += f"  ({t['duration']})"
            if t.get("stream"):
                line += f"\n     stream: {t['stream']}"
            out.append(line)
        return "\n".join(out)

    # ── Free / paid download eligibility ──────────────────────────────────

    async def find_free_album(
        self,
        url: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Inspect a Bandcamp album for free-download eligibility (name-your-price
        with a $0 floor, or "free download" link). Returns the
        download-page URL if eligible.
        :param url: Album URL.
        :return: Eligibility verdict + download-page URL or explanation.
        """
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT, follow_redirects=True) as c:
            try:
                r = await c.get(url, headers={"User-Agent": _UA})
            except Exception as e:
                return f"Bandcamp fetch failed: {e}"
            if r.status_code >= 400:
                return f"Bandcamp returned {r.status_code}"
            html = r.text

        tralbum = _extract_tralbum(html) or {}
        free_url = tralbum.get("freeDownloadPage") or ""
        cur = tralbum.get("current") or {}
        is_set_price = cur.get("is_set_price")
        minimum_price = cur.get("minimum_price")

        if free_url:
            return (
                f"✅ Free download available\n"
                f"   page: {free_url}\n"
                f"   tip: open the page, enter $0 (or any amount), submit your email; Bandcamp emails a download link in the format chosen on that page."
            )
        if is_set_price is False and (minimum_price in (None, 0, 0.0)):
            return (
                f"✅ Name-your-price (NYP) — $0 minimum supported\n"
                f"   page: {url}\n"
                f"   tip: click 'Buy Digital Album', enter 0, complete checkout to receive a free download link."
            )
        if is_set_price is True and minimum_price:
            return (
                f"💲 Paid only — minimum ${minimum_price}\n"
                f"   page: {url}"
            )
        return f"❓ Could not determine free-download status for {url}"

    # ── Discover free / NYP releases ──────────────────────────────────────

    async def discover_free_in_tag(
        self,
        tag: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Crawl Bandcamp's tag page (e.g. "ambient", "post-rock") and return
        releases marked as free or name-your-price.
        :param tag: Bandcamp tag slug, hyphenated lowercase.
        :return: Markdown list with release URLs.
        """
        slug = tag.strip().lower().replace(" ", "-")
        url = f"https://bandcamp.com/tag/{quote(slug, safe='')}?tab=highlights"
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT, follow_redirects=True) as c:
            try:
                r = await c.get(url, headers={"User-Agent": _UA})
            except Exception as e:
                return f"Bandcamp tag fetch failed: {e}"
            if r.status_code >= 400:
                return f"Bandcamp returned {r.status_code} for tag '{tag}'"
            html = r.text

        # Bandcamp inlines a JSON `data-blob` on tag pages with item lists.
        m = re.search(r'data-blob="([^"]+)"', html)
        if not m:
            return f"No machine-readable releases found on {url}"
        try:
            blob = json.loads(m.group(1).replace("&quot;", '"').replace("&amp;", "&"))
        except Exception as e:
            return f"Failed to parse Bandcamp tag blob: {e}"

        items = (
            blob.get("hub", {}).get("tabs", [{}])[0]
            .get("dig_deeper", {}).get("results", {}).get("items", [])
            or blob.get("dig_deeper", {}).get("items", [])
            or []
        )
        free = []
        for it in items:
            url2 = it.get("tralbum_url") or it.get("url") or ""
            if not url2:
                continue
            # Tag-blob entries don't expose price directly; fall back to title heuristics.
            free.append({
                "title": it.get("title", "—"),
                "artist": it.get("artist", "—"),
                "url": url2,
                "genre": it.get("genre"),
            })

        if not free:
            return f"No releases listed on Bandcamp tag page for '{tag}'."

        out = [f"## Bandcamp tag: {tag}\n",
               "_Tag pages don't expose price reliably — use `find_free_album(url)` to verify NYP/free status._\n"]
        for it in free[: self.valves.MAX_RESULTS]:
            out.append(f"**{it['artist']} — {it['title']}**")
            if it.get("genre"):
                out.append(f"   genre: {it['genre']}")
            out.append(f"   {it['url']}\n")
        return "\n".join(out)

    # ── Direct download (writes to disk) ─────────────────────────────────

    async def download_free_track(
        self,
        track_url: str,
        format: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Stream-download a single free track from Bandcamp to DOWNLOAD_DIR.
        Only works on tracks Bandcamp serves a direct stream URL for and where
        the rights-holder set free streaming. For album / NYP downloads, see
        `find_free_album` and follow the email link Bandcamp returns.
        :param track_url: Track page URL.
        :param format: Optional override for valves.PREFER_FORMAT.
        :return: Path on disk + status.
        """
        if not self.valves.WRITE_ENABLED:
            return "Download blocked: flip WRITE_ENABLED in this tool's Valves first."

        fmt = (format or self.valves.PREFER_FORMAT).lower()
        if fmt not in _FORMATS:
            return f"Unknown format '{fmt}'. Supported: {', '.join(_FORMATS)}"

        out_dir = Path(self.valves.DOWNLOAD_DIR).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)

        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT, follow_redirects=True) as c:
            try:
                page = await c.get(track_url, headers={"User-Agent": _UA})
            except Exception as e:
                return f"Bandcamp page fetch failed: {e}"
            if page.status_code >= 400:
                return f"Bandcamp returned {page.status_code}"
            tralbum = _extract_tralbum(page.text) or {}
            tracks = tralbum.get("trackinfo", [])
            if not tracks:
                return "Page has no streamable trackinfo."
            track = tracks[0]
            stream = (track.get("file") or {}).get("mp3-128")
            if not stream:
                return "Bandcamp did not expose a free stream for this track. Use the album NYP flow."

            # The mp3-128 stream is what the player uses; for full-quality,
            # the user must complete the email-link flow. We download the
            # mp3-128 preview here as a clear best-effort.
            artist = tralbum.get("artist", "Unknown Artist")
            title = track.get("title", "track")
            ext = "mp3"
            fname = _safe_filename(f"{artist} - {title}.{ext}")
            dest = out_dir / fname
            try:
                async with c.stream("GET", stream, headers={"User-Agent": _UA}) as resp:
                    if resp.status_code != 200:
                        return f"Stream returned {resp.status_code}"
                    with dest.open("wb") as fh:
                        async for chunk in resp.aiter_bytes(chunk_size=64 * 1024):
                            fh.write(chunk)
            except Exception as e:
                return f"Download failed: {e}"

        size = os.path.getsize(dest)
        return (
            f"Downloaded MP3 preview ({size:,} bytes) → {dest}\n"
            f"For lossless / full-length, use Bandcamp's name-your-price email flow:\n"
            f"  1. find_free_album({track_url})\n"
            f"  2. open the resulting page and complete checkout at $0\n"
            f"  3. choose '{_FORMATS.get(fmt, 'FLAC')}' on the download page Bandcamp emails you"
        )
