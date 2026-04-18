"""
title: URL Reader
author: local-ai-stack
description: Fetch and extract readable text content from any URL. Lets the model read web pages, documentation, and articles.
required_open_webui_version: 0.4.0
requirements: httpx, beautifulsoup4
version: 1.0.0
licence: MIT
"""

import httpx
import re
from pydantic import BaseModel, Field
from typing import Callable, Any, Optional


class Tools:
    class Valves(BaseModel):
        MAX_CHARS: int = Field(
            default=8000,
            description="Maximum characters of page content to return",
        )
        TIMEOUT: int = Field(default=15, description="Request timeout in seconds")
        USER_AGENT: str = Field(
            default="Mozilla/5.0 (compatible; local-ai-stack/1.0; +https://github.com/kitisathreat/local-ai-stack)",
            description="User-Agent header for requests",
        )

    def __init__(self):
        self.valves = self.Valves()

    def _clean_html(self, html: str) -> str:
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form", "button"]):
                tag.decompose()
            text = soup.get_text(separator="\n", strip=True)
        except ImportError:
            # Fallback: regex strip
            text = re.sub(r"<[^>]+>", " ", html)
            text = re.sub(r"&[a-z]+;", " ", text)

        # Collapse whitespace
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r" {2,}", " ", text)
        return text.strip()

    async def read_url(
        self,
        url: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Fetch and read the text content of a web page or URL.
        Useful for reading documentation, articles, or any public web content.
        :param url: The full URL to fetch (must start with http:// or https://)
        :return: Extracted readable text from the page
        """
        if not url.startswith(("http://", "https://")):
            return f"Invalid URL: must start with http:// or https://"

        if __event_emitter__:
            await __event_emitter__(
                {"type": "status", "data": {"description": f"Reading: {url}", "done": False}}
            )

        try:
            headers = {
                "User-Agent": self.valves.USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9",
                "Accept-Language": "en-US,en;q=0.9",
            }
            async with httpx.AsyncClient(
                timeout=self.valves.TIMEOUT,
                follow_redirects=True,
                headers=headers,
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()

            content_type = resp.headers.get("content-type", "")
            if "text/html" in content_type:
                text = self._clean_html(resp.text)
            elif "application/json" in content_type:
                text = resp.text
            else:
                text = resp.text

            if len(text) > self.valves.MAX_CHARS:
                text = text[: self.valves.MAX_CHARS] + f"\n\n[...content truncated at {self.valves.MAX_CHARS} chars]"

            if __event_emitter__:
                await __event_emitter__(
                    {"type": "status", "data": {"description": f"Read {len(text)} chars", "done": True}}
                )

            return f"## Content from: {url}\n\n{text}"

        except httpx.HTTPStatusError as e:
            return f"HTTP error {e.response.status_code} fetching {url}"
        except httpx.ConnectError:
            return f"Cannot connect to {url}. Check URL and internet connection."
        except httpx.TimeoutException:
            return f"Timeout fetching {url} after {self.valves.TIMEOUT}s."
        except Exception as e:
            return f"Error reading URL: {str(e)}"

    async def read_multiple_urls(
        self,
        urls: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Fetch and read multiple URLs, returning combined content.
        :param urls: Newline or comma-separated list of URLs to fetch
        :return: Combined text from all URLs
        """
        url_list = [u.strip() for u in re.split(r"[,\n]", urls) if u.strip()]
        if not url_list:
            return "No valid URLs provided."

        results = []
        for url in url_list[:3]:  # Limit to 3 URLs
            content = await self.read_url(url, __event_emitter__, __user__)
            results.append(content)

        return "\n\n---\n\n".join(results)
