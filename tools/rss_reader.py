"""
title: RSS / Atom Feed Reader
author: local-ai-stack
description: Fetch and read any RSS or Atom news feed. Subscribe to news sources, blogs, podcasts, and research journals by URL. No API key needed.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
import xml.etree.ElementTree as ET
import re
from pydantic import BaseModel, Field
from typing import Callable, Any, Optional


# Useful pre-configured feeds
PRESET_FEEDS = {
    "hacker news": "https://news.ycombinator.com/rss",
    "hn": "https://news.ycombinator.com/rss",
    "arxiv ai": "https://rss.arxiv.org/rss/cs.AI",
    "arxiv ml": "https://rss.arxiv.org/rss/cs.LG",
    "bbc world": "https://feeds.bbci.co.uk/news/world/rss.xml",
    "bbc tech": "https://feeds.bbci.co.uk/news/technology/rss.xml",
    "techcrunch": "https://techcrunch.com/feed/",
    "ars technica": "https://feeds.arstechnica.com/arstechnica/index",
    "the verge": "https://www.theverge.com/rss/index.xml",
    "wired": "https://www.wired.com/feed/rss",
    "mit tech review": "https://www.technologyreview.com/feed/",
    "nature": "https://www.nature.com/nature.rss",
    "science": "https://www.science.org/action/showFeed?type=etoc&feed=rss&jc=science",
    "pubmed latest": "https://pubmed.ncbi.nlm.nih.gov/rss/search/1bDB_YqUKaxGOCPzGtYg3MOh0RA3VtmxFNzO_hOUzAeicWZV5F/?limit=15&utm_campaign=pubmed-2&fc=20231005062413",
    "slashdot": "https://rss.slashdot.org/Slashdot/slashdotMain",
    "nasa news": "https://www.nasa.gov/news-release/feed/",
    "github trending": "https://github.com/trending.atom",
    "reddit technology": "https://www.reddit.com/r/technology/.rss",
    "reddit machinelearning": "https://www.reddit.com/r/MachineLearning/.rss",
    "openai blog": "https://openai.com/blog/rss.xml",
    "google ai blog": "https://blog.research.google/atom.xml",
    "huggingface": "https://huggingface.co/blog/feed.xml",
}


class Tools:
    class Valves(BaseModel):
        MAX_ITEMS: int = Field(default=8, description="Maximum feed items to return")
        TIMEOUT: int = Field(default=10, description="Request timeout in seconds")

    def __init__(self):
        self.valves = self.Valves()

    def _clean(self, text: str) -> str:
        text = re.sub(r"<[^>]+>", "", text or "")
        text = re.sub(r"&[a-z]+;|&#\d+;", " ", text)
        return text.strip()

    def _parse_feed(self, xml_text: str) -> list:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return []

        items = []
        ns = {}

        # RSS 2.0
        for item in root.findall(".//item")[:self.valves.MAX_ITEMS]:
            title = self._clean(item.findtext("title") or "")
            link = item.findtext("link") or ""
            desc = self._clean(item.findtext("description") or "")[:200]
            pub = item.findtext("pubDate") or item.findtext("published") or ""
            if title:
                items.append({"title": title, "link": link, "desc": desc, "date": pub})

        # Atom
        atom_ns = "http://www.w3.org/2005/Atom"
        if not items:
            for entry in root.findall(f".//{{{atom_ns}}}entry")[:self.valves.MAX_ITEMS]:
                title = self._clean(entry.findtext(f"{{{atom_ns}}}title") or "")
                link_el = entry.find(f"{{{atom_ns}}}link")
                link = link_el.get("href", "") if link_el is not None else ""
                summary = self._clean(entry.findtext(f"{{{atom_ns}}}summary") or entry.findtext(f"{{{atom_ns}}}content") or "")[:200]
                date = entry.findtext(f"{{{atom_ns}}}updated") or entry.findtext(f"{{{atom_ns}}}published") or ""
                if title:
                    items.append({"title": title, "link": link, "desc": summary, "date": date[:10]})

        return items

    async def read_feed(
        self,
        feed: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Read a RSS or Atom feed by URL or preset name. Returns the latest items.
        :param feed: A feed URL (https://...) or a preset name like "hacker news", "arxiv ai", "bbc tech", "techcrunch", "nature", "nasa news", "reddit technology", "openai blog"
        :return: Latest news items with titles, summaries, dates, and links
        """
        # Resolve preset names
        url = PRESET_FEEDS.get(feed.lower().strip(), feed.strip())

        if not url.startswith("http"):
            available = ", ".join(sorted(PRESET_FEEDS.keys()))
            return f"Unknown feed: '{feed}'\nAvailable presets: {available}\nOr provide a direct RSS/Atom URL."

        if __event_emitter__:
            await __event_emitter__(
                {"type": "status", "data": {"description": f"Fetching feed: {url}", "done": False}}
            )

        try:
            async with httpx.AsyncClient(timeout=self.valves.TIMEOUT, follow_redirects=True) as client:
                resp = await client.get(
                    url,
                    headers={"User-Agent": "Mozilla/5.0; local-ai-stack/1.0", "Accept": "application/rss+xml,application/atom+xml,application/xml,text/xml"},
                )
                resp.raise_for_status()

            items = self._parse_feed(resp.text)

            if not items:
                return f"No items found in feed: {url}\nThe feed may be empty or in an unsupported format."

            if __event_emitter__:
                await __event_emitter__(
                    {"type": "status", "data": {"description": f"{len(items)} items retrieved", "done": True}}
                )

            lines = [f"## Feed: {url}\n"]
            for item in items:
                lines.append(f"**{item['title']}**")
                if item.get("date"):
                    lines.append(f"   📅 {item['date'][:25]}")
                if item.get("desc"):
                    lines.append(f"   {item['desc']}...")
                if item.get("link"):
                    lines.append(f"   🔗 {item['link']}")
                lines.append("")

            return "\n".join(lines)

        except httpx.HTTPStatusError as e:
            return f"Feed error: HTTP {e.response.status_code} — {url}"
        except httpx.ConnectError:
            return f"Cannot connect to feed: {url}"
        except Exception as e:
            return f"RSS feed error: {str(e)}"

    def list_presets(self, __user__: Optional[dict] = None) -> str:
        """
        List all available preset RSS feed names that can be used without a URL.
        :return: Table of preset names and their sources
        """
        lines = ["## Available RSS Feed Presets\n"]
        for name, url in sorted(PRESET_FEEDS.items()):
            lines.append(f"- **{name}** → {url}")
        lines.append("\nUsage: `read_feed('hacker news')` or provide any direct RSS/Atom URL.")
        return "\n".join(lines)
