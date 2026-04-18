"""
title: Open Library — Books & Literature
author: local-ai-stack
description: Search Open Library (Internet Archive) for 20M+ books. Get publication info, author bios, covers, and links to free full-text where available. No API key required.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from pydantic import BaseModel, Field
from typing import Callable, Any, Optional


OL_API = "https://openlibrary.org"


class Tools:
    class Valves(BaseModel):
        MAX_RESULTS: int = Field(default=5, description="Maximum results to return")

    def __init__(self):
        self.valves = self.Valves()

    async def search_books(
        self,
        query: str,
        author: str = "",
        subject: str = "",
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search Open Library for books by title, author, or subject.
        :param query: Book title or keywords (e.g. "Dune", "machine learning textbook", "history of Rome")
        :param author: Optional author name filter (e.g. "Frank Herbert")
        :param subject: Optional subject filter (e.g. "science fiction", "mathematics", "biography")
        :return: Book titles, authors, publication year, edition count, and availability
        """
        if __event_emitter__:
            await __event_emitter__(
                {"type": "status", "data": {"description": f"Searching Open Library: {query}", "done": False}}
            )

        params = {
            "q": query,
            "limit": self.valves.MAX_RESULTS,
            "fields": "key,title,author_name,first_publish_year,edition_count,subject,ia,has_fulltext,cover_i,number_of_pages_median",
        }
        if author:
            params["author"] = author
        if subject:
            params["subject"] = subject

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(f"{OL_API}/search.json", params=params)
                resp.raise_for_status()
                data = resp.json()

            books = data.get("docs", [])
            total = data.get("numFound", 0)

            if not books:
                return f"No books found for: {query}"

            if __event_emitter__:
                await __event_emitter__(
                    {"type": "status", "data": {"description": f"Found {total:,} books", "done": True}}
                )

            lines = [f"## Open Library: {query} ({total:,} results)\n"]
            for b in books:
                title = b.get("title", "Unknown")
                authors = b.get("author_name", [])[:2]
                author_str = ", ".join(authors) + (" et al." if len(b.get("author_name", [])) > 2 else "")
                year = b.get("first_publish_year", "?")
                editions = b.get("edition_count", 1)
                pages = b.get("number_of_pages_median", "?")
                has_full = b.get("has_fulltext", False)
                ia_ids = b.get("ia", [])
                key = b.get("key", "")
                url = f"https://openlibrary.org{key}" if key else ""
                ia_url = f"https://archive.org/details/{ia_ids[0]}" if ia_ids else ""
                availability = "📖 Free to read" if has_full else "📚 Catalog entry"

                subjects = b.get("subject", [])[:4]
                subject_str = ", ".join(subjects) if subjects else ""

                lines.append(f"**{title}** ({year})")
                lines.append(f"   {author_str} | {editions} edition(s) | {pages} pages | {availability}")
                if subject_str:
                    lines.append(f"   Subjects: {subject_str}")
                if ia_url:
                    lines.append(f"   📄 Read: {ia_url}")
                elif url:
                    lines.append(f"   🔗 {url}")
                lines.append("")

            return "\n".join(lines)

        except Exception as e:
            return f"Open Library error: {str(e)}"

    async def get_book(
        self,
        identifier: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get full details for a book by ISBN, Open Library ID, or Internet Archive ID.
        :param identifier: ISBN-13, ISBN-10, OLID (e.g. "OL7353617M"), or IA ID
        :return: Full book metadata, description, and access links
        """
        identifier = identifier.strip()

        # Determine identifier type
        if identifier.startswith("OL"):
            url = f"{OL_API}/works/{identifier}.json"
        elif identifier.startswith("/works/"):
            url = f"{OL_API}{identifier}.json"
        else:
            # Assume ISBN
            url = f"{OL_API}/isbn/{identifier}.json"

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(url)
                if resp.status_code == 404:
                    return f"Book not found: {identifier}"
                resp.raise_for_status()
                book = resp.json()

                # Get author if available
                author_name = "Unknown"
                authors = book.get("authors", [])
                if authors:
                    author_key = authors[0].get("author", {}).get("key") or authors[0].get("key", "")
                    if author_key:
                        a_resp = await client.get(f"{OL_API}{author_key}.json")
                        if a_resp.status_code == 200:
                            author_name = a_resp.json().get("name", "Unknown")

            title = book.get("title", "Unknown")
            desc = book.get("description", "")
            if isinstance(desc, dict):
                desc = desc.get("value", "")
            desc = desc[:500] if desc else "No description available."
            subjects = book.get("subjects", [])[:6]
            first_pub = book.get("first_publish_date", "")
            ol_key = book.get("key", "")
            ol_url = f"https://openlibrary.org{ol_key}" if ol_key else ""

            lines = [f"## {title}\n"]
            lines.append(f"**Author:** {author_name}")
            if first_pub:
                lines.append(f"**First published:** {first_pub}")
            if subjects:
                lines.append(f"**Subjects:** {', '.join(subjects)}")
            lines.append(f"\n{desc}")
            if ol_url:
                lines.append(f"\n🔗 {ol_url}")

            return "\n".join(lines)

        except Exception as e:
            return f"Book lookup error: {str(e)}"

    async def get_author(
        self,
        author_name: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Look up an author on Open Library — bio, birth/death dates, and notable works.
        :param author_name: Author's full name (e.g. "Isaac Asimov", "Ursula K. Le Guin")
        :return: Author biography and list of notable works
        """
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                s = await client.get(
                    f"{OL_API}/search/authors.json",
                    params={"q": author_name, "limit": 1}
                )
                s.raise_for_status()
                results = s.json().get("docs", [])
                if not results:
                    return f"Author not found: {author_name}"

                a = results[0]
                key = a.get("key", "")
                detail = {}
                if key:
                    d = await client.get(f"{OL_API}/authors/{key}.json")
                    if d.status_code == 200:
                        detail = d.json()

            name = a.get("name", author_name)
            birth = a.get("birth_date", detail.get("birth_date", ""))
            death = a.get("death_date", detail.get("death_date", ""))
            work_count = a.get("work_count", 0)
            top_work = a.get("top_work", "")
            bio = detail.get("bio", "")
            if isinstance(bio, dict):
                bio = bio.get("value", "")
            bio = bio[:400] if bio else ""

            lines = [f"## Author: {name}\n"]
            if birth:
                lines.append(f"**Born:** {birth}" + (f"  **Died:** {death}" if death else ""))
            lines.append(f"**Works in Open Library:** {work_count:,}")
            if top_work:
                lines.append(f"**Notable work:** {top_work}")
            if bio:
                lines.append(f"\n{bio}...")
            lines.append(f"\n🔗 https://openlibrary.org/authors/{key}")
            return "\n".join(lines)

        except Exception as e:
            return f"Author lookup error: {str(e)}"
