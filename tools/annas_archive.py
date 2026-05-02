"""
title: Anna's Archive — Shadow Library Search & Download
author: local-ai-stack
description: Search Anna's Archive (and equivalent mirrors) for books, papers, comics, magazines, and standards. Look up records by MD5, enumerate free mirrors (LibGen, Sci-Hub, IPFS, partner servers), pull a paid-member fast-download URL, and optionally save the file to disk. No key needed for search/lookup; member key needed for fast downloads.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import asyncio
import os
import re
from html import unescape
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote, urlparse

import httpx
from pydantic import BaseModel, Field


UA = "Mozilla/5.0 (compatible; local-ai-stack annas-archive-tool/1.0)"

# Default mirror rotation. Anna's Archive periodically loses domains to legal /
# DDoS pressure; users can override DOMAINS in the valve.
DEFAULT_DOMAINS = (
    "annas-archive.li,annas-archive.se,annas-archive.gd,"
    "annas-archive.gl,annas-archive.vg,annas-archive.pm"
)

# Members fast-download partner servers (rotate if one is unhealthy).
FAST_DOMAIN_INDICES = (0, 1, 2)

CONTENT_TYPES = {
    "book_fiction",
    "book_nonfiction",
    "book_unknown",
    "book_comic",
    "magazine",
    "standards_document",
    "journal_article",
    "musical_score",
    "other",
}

SORTS = {
    "",
    "newest",
    "oldest",
    "largest",
    "smallest",
    "newest_added",
    "oldest_added",
    "random",
}

MD5_RE = re.compile(r"\b([a-f0-9]{32})\b", re.IGNORECASE)
SLOW_RE = re.compile(r"""href=["'](/slow_download/[a-f0-9]{32}/\d+/\d+)["']""", re.IGNORECASE)
FAST_RE = re.compile(r"""href=["'](/fast_download/[a-f0-9]{32}/\d+/\d+)["']""", re.IGNORECASE)
EXT_LIBGEN_RE = re.compile(
    r"""href=["'](https?://(?:libgen|library\.lol|libstc|sci-hub|annas-blog)[^"']+)["']""",
    re.IGNORECASE,
)
IPFS_RE = re.compile(
    r"""href=["'](https?://[^"']+/ipfs/[A-Za-z0-9]+(?:\?[^"']*)?)["']""", re.IGNORECASE
)
ONION_RE = re.compile(
    r"""href=["'](https?://[^"']+\.onion[^"']*)["']""", re.IGNORECASE
)
HATHI_RE = re.compile(
    r"""href=["'](https?://hdl\.handle\.net/2027/[^"']+)["']""", re.IGNORECASE
)
SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(KB|MB|GB)\b", re.IGNORECASE)
YEAR_RE = re.compile(r"\b(1[5-9]\d{2}|20\d{2})\b")


def _strip_tags(s: str) -> str:
    return unescape(re.sub(r"<[^>]+>", "", s)).strip()


def _human_size(n: int) -> str:
    if not n:
        return "?"
    units = ["B", "KB", "MB", "GB", "TB"]
    f, i = float(n), 0
    while f >= 1024 and i < len(units) - 1:
        f /= 1024
        i += 1
    return f"{f:.1f} {units[i]}"


class Tools:
    class Valves(BaseModel):
        DOMAINS: str = Field(
            default=DEFAULT_DOMAINS,
            description="Comma-separated Anna's Archive mirror domains. Tried in order; first one that responds wins.",
        )
        AA_MEMBER_KEY: str = Field(
            default="",
            description="Anna's Archive members-only API key (for /dyn/api/fast_download.json). Donate at annas-archive.li/donate to obtain one. Search and lookup work without it.",
        )
        MAX_RESULTS: int = Field(
            default=10, description="Max search results to return."
        )
        TIMEOUT: int = Field(default=25, description="HTTP timeout (seconds).")
        DOWNLOAD_TIMEOUT: int = Field(
            default=600,
            description="Max seconds to wait for a file download to complete.",
        )
        DOWNLOAD_DIR: str = Field(
            default="/data/annas-archive",
            description="Directory where downloaded files are saved.",
        )
        ALLOW_TOR: bool = Field(
            default=False,
            description="Surface .onion mirrors in mirror lists (requires Tor; URLs not auto-fetched).",
        )

    def __init__(self):
        self.valves = self.Valves()

    # ── HTTP plumbing ────────────────────────────────────────────────────────
    def _domains(self) -> list[str]:
        return [d.strip() for d in self.valves.DOMAINS.split(",") if d.strip()]

    async def _get(
        self, path: str, params: Optional[dict] = None, *, want_json: bool = False
    ) -> tuple[str, str]:
        """GET path against each mirror domain in order until one succeeds.

        Returns (response_text, base_url_used). Raises last exception if all fail.
        """
        last: Exception | None = None
        async with httpx.AsyncClient(
            timeout=self.valves.TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": UA},
        ) as client:
            for dom in self._domains():
                base = f"https://{dom}"
                try:
                    r = await client.get(base + path, params=params)
                    r.raise_for_status()
                    if want_json:
                        # Trigger JSON decode to confirm content
                        r.json()
                    return r.text, base
                except Exception as e:
                    last = e
                    continue
        raise last or RuntimeError("No Anna's Archive mirrors reachable")

    async def _status(self, emitter, msg: str, done: bool = False):
        if emitter:
            await emitter(
                {"type": "status", "data": {"description": msg, "done": done}}
            )

    # ── Search ───────────────────────────────────────────────────────────────
    async def search(
        self,
        query: str,
        content_type: str = "",
        language: str = "",
        extension: str = "",
        sort: str = "",
        page: int = 1,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search Anna's Archive for books, papers, comics, magazines, and standards.
        :param query: Free-text query, ISBN, DOI, or title/author keywords.
        :param content_type: One of book_fiction, book_nonfiction, book_unknown, book_comic, magazine, standards_document, journal_article, musical_score, other. Empty = all.
        :param language: ISO-639 language code (e.g. "en", "ru", "zh"). Empty = all.
        :param extension: File extension filter (e.g. "pdf", "epub", "mobi", "azw3", "djvu", "cbz"). Empty = all.
        :param sort: One of "" (relevance), newest, oldest, largest, smallest, newest_added, oldest_added, random.
        :param page: 1-indexed result page.
        :return: Markdown list with title, author, year, language, ext, size, MD5, and a direct /md5/ link for each hit.
        """
        if content_type and content_type not in CONTENT_TYPES:
            return f"Invalid content_type `{content_type}`. Use one of: {', '.join(sorted(CONTENT_TYPES))}."
        if sort and sort not in SORTS:
            return f"Invalid sort `{sort}`. Use one of: {', '.join(s or 'relevance' for s in sorted(SORTS))}."

        params: list[tuple[str, str]] = [("q", query)]
        if content_type:
            params.append(("content", content_type))
        if language:
            params.append(("lang", language))
        if extension:
            params.append(("ext", extension))
        if sort:
            params.append(("sort", sort))
        if page and page > 1:
            params.append(("page", str(page)))
        params.append(("display", "table"))  # simpler markup

        await self._status(__event_emitter__, f"Anna's Archive: searching '{query}'")
        try:
            html, base = await self._get("/search", params=params)
        except httpx.HTTPStatusError as e:
            return f"Anna's Archive search failed: HTTP {e.response.status_code}"
        except Exception as e:
            return f"Anna's Archive unreachable: {e}. Try editing the DOMAINS valve."

        hits = self._parse_search(html)
        if not hits:
            return f"No Anna's Archive results for: {query}"

        hits = hits[: self.valves.MAX_RESULTS]
        await self._status(
            __event_emitter__, f"Found {len(hits)} results", done=True
        )

        lines = [f"## Anna's Archive: {query} ({len(hits)} of many)\n"]
        for h in hits:
            title = h.get("title") or "(untitled)"
            md5 = h.get("md5", "")
            meta_bits = []
            for key in ("author", "publisher", "year", "lang", "ext", "size"):
                if h.get(key):
                    meta_bits.append(str(h[key]))
            meta = " · ".join(meta_bits)
            lines.append(f"**{title}**")
            if meta:
                lines.append(f"   {meta}")
            if md5:
                lines.append(f"   MD5: `{md5}`")
                lines.append(f"   🔗 {base}/md5/{md5}")
            lines.append("")
        lines.append(f"_Mirror used: {base}_")
        return "\n".join(lines)

    def _parse_search(self, html: str) -> list[dict]:
        """Extract hits from the /search HTML.

        Anna's Archive wraps each result in an <a href="/md5/HASH">…</a> block
        inside `js-aarecord-list-outer`. We split on those anchors and pull
        title + the comma-joined "lang, ext, size, year, publisher, author"
        snippet that lives in the first inner div.
        """
        hits: list[dict] = []
        # Each result block starts with an <a href="/md5/...">.
        block_re = re.compile(
            r"""<a[^>]+href=["']/md5/([a-f0-9]{32})["'][^>]*>(.*?)</a>""",
            re.IGNORECASE | re.DOTALL,
        )
        seen: set[str] = set()
        for m in block_re.finditer(html):
            md5 = m.group(1).lower()
            if md5 in seen:
                continue
            seen.add(md5)
            block = m.group(2)
            text = _strip_tags(block)
            lines = [ln.strip() for ln in re.split(r"[\n\r]+|<br\s*/?>", text) if ln.strip()]

            def _looks_like_meta(ln: str) -> bool:
                return "," in ln and (
                    SIZE_RE.search(ln) is not None
                    or YEAR_RE.search(ln) is not None
                    or " [" in ln
                )

            meta_line = next((ln for ln in lines if _looks_like_meta(ln)), "")
            non_meta = [ln for ln in lines if ln != meta_line]
            title = max(non_meta, key=len) if non_meta else (lines[0] if lines else "")
            parts = [p.strip(" ;,\u00a0") for p in meta_line.split(",")] if meta_line else []
            # Heuristic field extraction.
            lang = ""
            ext = ""
            size = ""
            year = ""
            publisher = ""
            author = ""
            for p in parts:
                lp = p.lower()
                if not lang and re.fullmatch(r"[a-z]{2,3}(\s*\[[a-z-]+\])?", lp):
                    lang = p
                elif not lang and "[" in p and "]" in p and len(p) < 32:
                    lang = p  # e.g. "English [en]"
                elif not ext and re.fullmatch(r"(pdf|epub|mobi|azw3?|djvu|cbz|cbr|fb2|txt|rtf|doc[x]?|chm)", lp):
                    ext = lp
                elif not size and SIZE_RE.search(p):
                    size = SIZE_RE.search(p).group(0)
                elif not year and YEAR_RE.fullmatch(p):
                    year = p
                elif not publisher and len(p) < 80 and any(c.isalpha() for c in p):
                    publisher = p
                elif not author and len(p) < 120:
                    author = p
            hits.append(
                {
                    "md5": md5,
                    "title": title[:300],
                    "lang": lang,
                    "ext": ext,
                    "size": size,
                    "year": year,
                    "publisher": publisher,
                    "author": author,
                }
            )
        return hits

    # ── Lookup ───────────────────────────────────────────────────────────────
    async def get_record(
        self,
        md5: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Fetch the full record page for an Anna's Archive entry by its MD5 hash.
        :param md5: 32-character hex MD5 (from search results).
        :return: Markdown summary plus an enumerated list of every download mirror Anna's Archive surfaces (slow partner, fast partner, LibGen, Sci-Hub, IPFS, onion, HathiTrust).
        """
        md5 = md5.strip().lower()
        if not re.fullmatch(r"[a-f0-9]{32}", md5):
            return "Invalid MD5: must be 32 hex characters."

        try:
            html, base = await self._get(f"/md5/{md5}")
        except Exception as e:
            return f"Anna's Archive unreachable: {e}"

        title = self._extract_title(html)
        meta_line = self._extract_meta_line(html)
        mirrors = self._extract_mirrors(html, base)

        out = [f"## {title or md5}", "", f"**MD5:** `{md5}`"]
        if meta_line:
            out.append(f"**Metadata:** {meta_line}")
        out.append(f"**Record:** {base}/md5/{md5}")
        out.append("")
        out.append(f"### Mirrors ({len(mirrors)})")
        if not mirrors:
            out.append("_No mirrors surfaced; the page may require login or this MD5 is unavailable._")
        else:
            for kind, url in mirrors:
                out.append(f"- **{kind}** — {url}")
        return "\n".join(out)

    @staticmethod
    def _extract_title(html: str) -> str:
        # The big title block on /md5/.
        m = re.search(
            r"""<div[^>]+class=["'][^"']*text-3xl[^"']*["'][^>]*>(.*?)</div>""",
            html,
            re.IGNORECASE | re.DOTALL,
        )
        if m:
            return _strip_tags(m.group(1))[:300]
        m = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        return _strip_tags(m.group(1))[:300] if m else ""

    @staticmethod
    def _extract_meta_line(html: str) -> str:
        # Look for the small grey metadata strip near the top of /md5/.
        m = re.search(
            r"""<div[^>]+class=["'][^"']*text-sm[^"']*text-gray[^"']*["'][^>]*>(.*?)</div>""",
            html,
            re.IGNORECASE | re.DOTALL,
        )
        return _strip_tags(m.group(1))[:400] if m else ""

    def _extract_mirrors(self, html: str, base: str) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for href in SLOW_RE.findall(html):
            out.append(("Anna's slow (free, throttled)", base + href))
        for href in FAST_RE.findall(html):
            out.append(("Anna's fast (members-only)", base + href))
        for url in EXT_LIBGEN_RE.findall(html):
            label = "External"
            low = url.lower()
            if "libgen" in low:
                label = "LibGen"
            elif "sci-hub" in low:
                label = "Sci-Hub"
            elif "libstc" in low:
                label = "Nexus/STC"
            out.append((label, url))
        for url in IPFS_RE.findall(html):
            out.append(("IPFS gateway", url))
        for url in HATHI_RE.findall(html):
            out.append(("HathiTrust", url))
        if self.valves.ALLOW_TOR:
            for url in ONION_RE.findall(html):
                out.append(("Tor onion", url))
        # Dedupe preserving order.
        seen: set[str] = set()
        uniq: list[tuple[str, str]] = []
        for kind, url in out:
            if url in seen:
                continue
            seen.add(url)
            uniq.append((kind, url))
        return uniq

    async def list_mirrors(
        self,
        md5: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Return only the download mirrors for an MD5, without the full record page header.
        :param md5: 32-character hex MD5.
        :return: Bulleted markdown list of mirror URLs grouped by source.
        """
        md5 = md5.strip().lower()
        if not re.fullmatch(r"[a-f0-9]{32}", md5):
            return "Invalid MD5."
        try:
            html, base = await self._get(f"/md5/{md5}")
        except Exception as e:
            return f"Anna's Archive unreachable: {e}"
        mirrors = self._extract_mirrors(html, base)
        if not mirrors:
            return f"No mirrors found for {md5}."
        groups: dict[str, list[str]] = {}
        for kind, url in mirrors:
            groups.setdefault(kind, []).append(url)
        lines = [f"## Mirrors for `{md5}`\n"]
        for kind, urls in groups.items():
            lines.append(f"### {kind} ({len(urls)})")
            for u in urls:
                lines.append(f"- {u}")
            lines.append("")
        return "\n".join(lines)

    # ── Members fast download ────────────────────────────────────────────────
    async def get_fast_download_url(
        self,
        md5: str,
        path_index: int = 0,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Resolve a direct, signed members-only "fast" download URL from /dyn/api/fast_download.json.
        Requires the AA_MEMBER_KEY valve to be set. Iterates partner domains until one returns 200.
        :param md5: 32-character hex MD5.
        :param path_index: Optional path variant (default 0). Try 0..N if a path is unhealthy.
        :return: Direct download URL plus quota info, or a clear error explaining 401/403/429.
        """
        md5 = md5.strip().lower()
        if not re.fullmatch(r"[a-f0-9]{32}", md5):
            return "Invalid MD5."
        if not self.valves.AA_MEMBER_KEY:
            return (
                "AA_MEMBER_KEY valve is empty. The fast-download API requires a "
                "paid Anna's Archive donor key (https://annas-archive.li/donate). "
                "Use `list_mirrors` for free LibGen/IPFS/slow-download links instead."
            )

        await self._status(
            __event_emitter__, f"Resolving fast download for {md5}…"
        )
        last_err = ""
        async with httpx.AsyncClient(
            timeout=self.valves.TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": UA},
        ) as client:
            for dom in self._domains():
                for didx in FAST_DOMAIN_INDICES:
                    url = f"https://{dom}/dyn/api/fast_download.json"
                    params = {
                        "md5": md5,
                        "key": self.valves.AA_MEMBER_KEY,
                        "path_index": str(path_index),
                        "domain_index": str(didx),
                    }
                    try:
                        r = await client.get(url, params=params)
                    except Exception as e:
                        last_err = f"{dom}: {e}"
                        continue
                    if r.status_code == 401:
                        return "Anna's Archive rejected the member key (HTTP 401). Re-check AA_MEMBER_KEY."
                    if r.status_code == 403:
                        return "AA_MEMBER_KEY is not a fast-download tier (HTTP 403)."
                    if r.status_code == 429:
                        try:
                            data = r.json()
                        except Exception:
                            data = {}
                        info = data.get("account_fast_download_info", {})
                        return (
                            "Daily fast-download quota exhausted (HTTP 429). "
                            f"Used today, {info.get('downloads_per_day','?')}/day cap."
                        )
                    if r.status_code == 404:
                        last_err = f"{dom} d{didx}: md5 not found"
                        continue
                    if r.status_code >= 500:
                        last_err = f"{dom} d{didx}: HTTP {r.status_code}"
                        continue
                    try:
                        data = r.json()
                    except Exception:
                        last_err = f"{dom} d{didx}: non-JSON response"
                        continue
                    durl = data.get("download_url")
                    if not durl:
                        last_err = f"{dom} d{didx}: {data.get('error','no url')}"
                        continue
                    info = data.get("account_fast_download_info", {})
                    left = info.get("downloads_left", "?")
                    cap = info.get("downloads_per_day", "?")
                    return (
                        f"## Fast Download URL\n"
                        f"**MD5:** `{md5}`\n"
                        f"**URL:** {durl}\n"
                        f"**Quota:** {left}/{cap} remaining today\n"
                        f"_Mirror: {dom}, domain_index={didx}, path_index={path_index}_"
                    )
        return f"Could not resolve fast download. Last error: {last_err or 'unknown'}"

    # ── Actual file download ────────────────────────────────────────────────
    async def download_file(
        self,
        md5: str,
        filename: str = "",
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Resolve the members fast-download URL, then stream the file to DOWNLOAD_DIR on disk.
        Requires AA_MEMBER_KEY. For free downloads, use `list_mirrors` and let the user pick a LibGen / IPFS link.
        :param md5: 32-character hex MD5.
        :param filename: Optional override for the saved filename. Default: derived from URL or "<md5>.<ext>".
        :return: Path to the saved file plus byte size, or an error.
        """
        url_msg = await self.get_fast_download_url(md5, __event_emitter__=__event_emitter__)
        m = re.search(r"\*\*URL:\*\*\s+(\S+)", url_msg)
        if not m:
            return url_msg  # already an error or quota message
        durl = m.group(1)

        out_dir = Path(self.valves.DOWNLOAD_DIR).expanduser()
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return f"Cannot create DOWNLOAD_DIR `{out_dir}`: {e}"

        # Pick a filename.
        if not filename:
            parsed = urlparse(durl)
            qs = dict(p.split("=", 1) for p in parsed.query.split("&") if "=" in p)
            filename = qs.get("filename", "")
            if filename:
                filename = filename.replace("/", "_")
            if not filename:
                tail = Path(parsed.path).name or md5
                filename = tail
        # Always anchor the file by md5 to prevent overwrites.
        if md5 not in filename:
            filename = f"{md5}_{filename}"
        target = out_dir / filename

        await self._status(
            __event_emitter__, f"Downloading {md5} → {target}"
        )
        try:
            async with httpx.AsyncClient(
                timeout=self.valves.DOWNLOAD_TIMEOUT,
                follow_redirects=True,
                headers={"User-Agent": UA},
            ) as client:
                async with client.stream("GET", durl) as r:
                    if r.status_code != 200:
                        return f"Download failed: HTTP {r.status_code} from {durl}"
                    written = 0
                    with open(target, "wb") as fh:
                        async for chunk in r.aiter_bytes(chunk_size=64 * 1024):
                            fh.write(chunk)
                            written += len(chunk)
        except asyncio.TimeoutError:
            return f"Download timed out after {self.valves.DOWNLOAD_TIMEOUT}s."
        except Exception as e:
            return f"Download error: {e}"

        await self._status(__event_emitter__, "Download complete", done=True)
        return (
            f"## Saved\n"
            f"**File:** `{target}`\n"
            f"**Size:** {_human_size(written)} ({written:,} bytes)\n"
            f"**Source:** {durl}"
        )

    # ── Misc public JSON ─────────────────────────────────────────────────────
    async def recent_downloads(
        self,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Show the most recently downloaded MD5s across Anna's Archive (public JSON, no key).
        Useful as a "what's hot right now" feed.
        :return: Markdown list with MD5 + record link for each.
        """
        try:
            text, base = await self._get("/dyn/recent_downloads/", want_json=True)
        except Exception as e:
            return f"Recent downloads unavailable: {e}"
        try:
            import json

            data = json.loads(text)
        except Exception:
            return "Could not parse recent_downloads JSON."
        rows = data if isinstance(data, list) else data.get("recent", [])
        if not rows:
            return "No recent downloads reported."
        lines = ["## Recent Anna's Archive downloads\n"]
        for row in rows[: self.valves.MAX_RESULTS]:
            md5 = row.get("md5") if isinstance(row, dict) else str(row)
            if not md5:
                continue
            title = row.get("title", "") if isinstance(row, dict) else ""
            label = f"{title} — `{md5}`" if title else f"`{md5}`"
            lines.append(f"- {label}")
            lines.append(f"   {base}/md5/{md5}")
        return "\n".join(lines)

    async def download_and_organize(
        self,
        md5: str,
        filename: str = "",
        kind: str = "books",
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Download a record by MD5 and immediately organize the resulting
        file into the media library
        (`<LIBRARY_ROOT>/Books/<Author>/<Title>.<ext>` for books, or the
        audiobooks subdir when `kind="audiobooks"`).
        :param md5: 32-char hex MD5 of the Anna's Archive record.
        :param filename: Optional explicit filename for the download.
        :param kind: "books" (default) or "audiobooks".
        :return: Combined download + organize log.
        """
        import importlib.util
        from pathlib import Path as _P
        spec = importlib.util.spec_from_file_location(
            "_lai_organize_helper", _P(__file__).parent / "_organize_helper.py",
        )
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        organize = mod.organize

        downloaded = await self.download_file(
            md5, filename=filename,
            __event_emitter__=__event_emitter__, __user__=__user__,
        )
        target = self.valves.DOWNLOAD_DIR
        organized = organize(target, kind=kind)
        return f"── download ──\n{downloaded}\n\n── organize ──\n{organized}"
