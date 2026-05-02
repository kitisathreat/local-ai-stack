"""
title: Code Troubleshoot — Stack Overflow + GitHub Issues/Discussions/Wikis
author: local-ai-stack
description: Crawl Stack Overflow and GitHub (issues, discussions, wiki, code) for fixes to a specific error message, traceback, or bug. Aggregates across sources, ranks by acceptance/upvotes/recency, and returns full answer/issue bodies so the model can read the resolution rather than just a link. Built around a `find_solutions` aggregator plus per-source primitives — search GitHub issues across one repo or all of GitHub, fetch a full issue thread with its comments, query the GraphQL discussions index, search a repo's wiki, and pull the top N upvoted Stack Overflow answers for a question.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

import asyncio
import html as html_mod
import re
from typing import Any, Callable, Optional
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field


GITHUB_API = "https://api.github.com"
GITHUB_GRAPHQL = "https://api.github.com/graphql"
SE_API = "https://api.stackexchange.com/2.3"
_UA = "local-ai-stack/1.0 code-troubleshoot"


def _strip_html(s: str, max_chars: int = 4000) -> str:
    s = html_mod.unescape(s or "")
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"(?s)<!--.*?-->", " ", s)
    s = s.replace("<pre><code>", "\n```\n").replace("</code></pre>", "\n```\n")
    s = s.replace("<code>", "`").replace("</code>", "`")
    s = re.sub(r"<br\s*/?>", "\n", s)
    s = re.sub(r"</p>\s*<p>", "\n\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    return s[:max_chars]


def _short(text: str, n: int = 280) -> str:
    text = (text or "").strip().replace("\r", "")
    return (text[:n] + "…") if len(text) > n else text


class Tools:
    class Valves(BaseModel):
        GITHUB_TOKEN: str = Field(
            default="",
            description="GitHub Personal Access Token. Required for discussions (GraphQL) and recommended for issue search (raises rate limit from 10 to 30 req/min).",
        )
        STACKEXCHANGE_KEY: str = Field(
            default="",
            description="Optional Stack Exchange app key. Raises the unauthenticated 300/day quota to 10000/day.",
        )
        DEFAULT_SITE: str = Field(
            default="stackoverflow",
            description="Stack Exchange site slug to query by default (stackoverflow, serverfault, superuser, askubuntu, dba, unix, ...)",
        )
        MAX_RESULTS_PER_SOURCE: int = Field(
            default=5,
            description="Max items returned per source in find_solutions and individual searches.",
        )
        ANSWER_BODY_CHARS: int = Field(
            default=4000,
            description="Per-answer / per-issue body character cap when fetching threads.",
        )
        TIMEOUT: int = Field(default=20, description="HTTP timeout per request, seconds.")

    def __init__(self):
        self.valves = self.Valves()

    # ── HTTP helpers ──────────────────────────────────────────────────────

    def _gh_headers(self) -> dict:
        h = {"Accept": "application/vnd.github+json", "User-Agent": _UA}
        if self.valves.GITHUB_TOKEN:
            h["Authorization"] = f"Bearer {self.valves.GITHUB_TOKEN}"
        return h

    def _se_params(self, extra: dict, site: str) -> dict:
        p = {"site": site or self.valves.DEFAULT_SITE, **extra}
        if self.valves.STACKEXCHANGE_KEY:
            p["key"] = self.valves.STACKEXCHANGE_KEY
        return p

    # ── GitHub: issues ────────────────────────────────────────────────────

    async def search_github_issues(
        self,
        query: str,
        repo: str = "",
        state: str = "all",
        is_pr: bool = False,
        language: str = "",
        sort: str = "best-match",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search GitHub Issues (or PRs) — bug reports, traceback dumps, "this doesn't
        work" threads. Use this when an error message likely matches an existing
        report.
        :param query: Free-text query — paste the error message, exception type, or symptom.
        :param repo: Limit to a single repo, "owner/name". Empty = whole of GitHub.
        :param state: "open", "closed", or "all". Closed issues often hold the resolution.
        :param is_pr: When True, search pull requests instead of issues.
        :param language: Optional language qualifier (e.g. "python", "typescript").
        :param sort: "best-match" (default), "reactions", "comments", "updated", "created".
        :return: Ranked list with title, state, comments, body excerpt, link.
        """
        kind = "pr" if is_pr else "issue"
        q = f"{query} is:{kind}"
        if repo:
            q += f" repo:{repo}"
        if state in ("open", "closed"):
            q += f" state:{state}"
        if language:
            q += f" language:{language}"

        params: dict[str, Any] = {"q": q, "per_page": self.valves.MAX_RESULTS_PER_SOURCE}
        if sort and sort != "best-match":
            params["sort"] = "reactions" if sort == "reactions" else sort
            params["order"] = "desc"

        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT) as c:
            try:
                r = await c.get(f"{GITHUB_API}/search/issues", params=params, headers=self._gh_headers())
            except Exception as e:
                return f"GitHub issues request failed: {e}"
            if r.status_code == 403:
                return "GitHub rate limit exceeded — set GITHUB_TOKEN in this tool's Valves."
            if r.status_code == 422:
                return f"GitHub rejected the query (likely too long or syntactically invalid):\n  {q}"
            if r.status_code >= 400:
                return f"GitHub error {r.status_code}: {r.text[:300]}"
            data = r.json()

        items = data.get("items", [])
        if not items:
            return f"No GitHub {'PRs' if is_pr else 'issues'} matched: {query}" + (f" in {repo}" if repo else "")

        lines = [f"## GitHub {'PRs' if is_pr else 'Issues'}: {query}" + (f" (repo: {repo})" if repo else "") + "\n"]
        for it in items:
            full = it.get("repository_url", "").replace(GITHUB_API + "/repos/", "")
            state_icon = "🟢" if it.get("state") == "open" else "🔴"
            labels = ", ".join(l.get("name", "") for l in it.get("labels", [])[:5]) or "—"
            body = _short(it.get("body") or "", 240)
            lines.append(
                f"{state_icon} **{it.get('title', '')}**  [#{it.get('number')} in {full}]\n"
                f"   reactions {it.get('reactions', {}).get('total_count', 0)} · "
                f"comments {it.get('comments', 0)} · "
                f"labels: {labels}\n"
                f"   {body}\n"
                f"   {it.get('html_url', '')}\n"
            )
        return "\n".join(lines)

    async def get_github_issue_thread(
        self,
        repo: str,
        number: int,
        include_comments: bool = True,
        max_comments: int = 10,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Fetch a single GitHub issue (or PR) with its full body and the most
        relevant comments — so the model can read the resolution rather than
        guess from the title.
        :param repo: "owner/name".
        :param number: Issue or PR number.
        :param include_comments: When True (default), pull the comment thread too.
        :param max_comments: Cap on comments returned (most-reacted first).
        :return: Title, state, body, top comments, link.
        """
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT) as c:
            try:
                r = await c.get(
                    f"{GITHUB_API}/repos/{repo}/issues/{number}",
                    headers=self._gh_headers(),
                )
            except Exception as e:
                return f"GitHub request failed: {e}"
            if r.status_code == 404:
                return f"Issue not found: {repo}#{number}"
            if r.status_code >= 400:
                return f"GitHub error {r.status_code}: {r.text[:300]}"
            issue = r.json()

            comments: list[dict] = []
            if include_comments and issue.get("comments", 0):
                try:
                    cr = await c.get(
                        f"{GITHUB_API}/repos/{repo}/issues/{number}/comments",
                        params={"per_page": 100},
                        headers=self._gh_headers(),
                    )
                    if cr.status_code == 200:
                        comments = cr.json()
                except Exception:
                    pass

        state = issue.get("state", "?")
        body = _short(issue.get("body") or "", self.valves.ANSWER_BODY_CHARS)
        labels = ", ".join(l.get("name", "") for l in issue.get("labels", [])[:8]) or "—"
        out = [
            f"## {repo}#{number} — {issue.get('title', '')}",
            f"state: **{state}** · reactions {issue.get('reactions', {}).get('total_count', 0)} · "
            f"comments {issue.get('comments', 0)} · labels: {labels}",
            f"{issue.get('html_url', '')}",
            "",
            "### Issue body",
            body or "_(empty)_",
        ]
        if comments:
            comments.sort(
                key=lambda x: x.get("reactions", {}).get("total_count", 0),
                reverse=True,
            )
            out.append("\n### Top comments")
            for cm in comments[:max_comments]:
                user = cm.get("user", {}).get("login", "?")
                rcount = cm.get("reactions", {}).get("total_count", 0)
                cb = _short(cm.get("body") or "", self.valves.ANSWER_BODY_CHARS // 2)
                out.append(f"\n— **@{user}**  +{rcount}\n{cb}\n{cm.get('html_url', '')}")
        return "\n".join(out)

    # ── GitHub: discussions (GraphQL) ────────────────────────────────────

    async def search_github_discussions(
        self,
        query: str,
        repo: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search GitHub Discussions — Q&A category posts and community threads
        that often hold setup gotchas, "how do I…" answers, and workarounds
        that never become issues. Requires GITHUB_TOKEN (GraphQL endpoint).
        :param query: Free-text query.
        :param repo: Optional "owner/name" filter.
        :return: Ranked list with title, category, answer status, snippet, link.
        """
        if not self.valves.GITHUB_TOKEN:
            return "GitHub Discussions search needs a GITHUB_TOKEN in this tool's Valves (GraphQL endpoint)."

        q = query
        if repo:
            q += f" repo:{repo}"
        gql = """
        query($q: String!, $n: Int!) {
          search(query: $q, type: DISCUSSION, first: $n) {
            discussionCount
            nodes {
              ... on Discussion {
                title
                url
                isAnswered
                upvoteCount
                createdAt
                category { name }
                repository { nameWithOwner }
                body
              }
            }
          }
        }
        """
        payload = {"query": gql, "variables": {"q": q, "n": self.valves.MAX_RESULTS_PER_SOURCE}}
        headers = self._gh_headers()
        headers["Accept"] = "application/json"

        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT) as c:
            try:
                r = await c.post(GITHUB_GRAPHQL, json=payload, headers=headers)
            except Exception as e:
                return f"GitHub Discussions request failed: {e}"
            if r.status_code >= 400:
                return f"GitHub GraphQL error {r.status_code}: {r.text[:300]}"
            data = r.json()
        if "errors" in data:
            return f"GitHub GraphQL errors: {data['errors']}"

        nodes = (data.get("data", {}) or {}).get("search", {}).get("nodes", []) or []
        if not nodes:
            return f"No GitHub discussions matched: {query}" + (f" in {repo}" if repo else "")

        lines = [f"## GitHub Discussions: {query}" + (f" (repo: {repo})" if repo else "") + "\n"]
        for d in nodes:
            answered = "✅ answered" if d.get("isAnswered") else "❓ unanswered"
            cat = (d.get("category") or {}).get("name", "—")
            owner_name = (d.get("repository") or {}).get("nameWithOwner", "—")
            body = _short(d.get("body") or "", 240)
            lines.append(
                f"**{d.get('title', '')}**  [{owner_name}]\n"
                f"   {answered} · upvotes {d.get('upvoteCount', 0)} · category: {cat}\n"
                f"   {body}\n"
                f"   {d.get('url', '')}\n"
            )
        return "\n".join(lines)

    # ── GitHub: wiki ──────────────────────────────────────────────────────

    async def search_repo_wiki(
        self,
        repo: str,
        query: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search a repo's wiki by fetching the rendered wiki Home + sidebar and
        scanning for `query`. GitHub doesn't expose a wiki search API, so this
        clones-by-HTTP the wiki and greps; works on public wikis only.
        :param repo: "owner/name".
        :param query: Search term.
        :return: Matching wiki pages with snippet and URL.
        """
        # The wiki lives at https://github.com/<repo>/wiki and individual pages at
        # /<repo>/wiki/<Page-Slug>. We scrape the sidebar, fetch each page, grep.
        base = f"https://github.com/{repo}/wiki"
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT, follow_redirects=True) as c:
            try:
                home = await c.get(base, headers={"User-Agent": _UA})
            except Exception as e:
                return f"Wiki fetch failed: {e}"
            if home.status_code == 404:
                return f"No wiki found at {base}"
            if home.status_code >= 400:
                return f"Wiki fetch error {home.status_code}"
            # Pull page slugs from anchor tags.
            slugs = sorted({
                m.group(1)
                for m in re.finditer(rf'href="/{re.escape(repo)}/wiki/([^"#?]+)"', home.text)
                if m.group(1) not in ("_history",)
            })

            if not slugs:
                # Wiki exists but is empty or only Home page rendered.
                slugs = ["Home"]

            sem = asyncio.Semaphore(4)

            async def fetch(slug: str) -> tuple[str, str]:
                async with sem:
                    try:
                        rr = await c.get(f"{base}/{slug}", headers={"User-Agent": _UA})
                        return slug, rr.text if rr.status_code == 200 else ""
                    except Exception:
                        return slug, ""

            pages = await asyncio.gather(*(fetch(s) for s in slugs[:30]))

        ql = query.lower()
        hits: list[tuple[str, str]] = []
        for slug, html in pages:
            if not html:
                continue
            # Reduce to the rendered body so navigation chrome doesn't dominate.
            body = re.search(r'(?is)<div\s+id="wiki-body"[^>]*>(.*?)</div>\s*</div>', html)
            text = _strip_html(body.group(1) if body else html, max_chars=20000)
            if ql in text.lower():
                idx = text.lower().find(ql)
                lo = max(0, idx - 120)
                hi = min(len(text), idx + 240)
                hits.append((slug, text[lo:hi].strip()))

        if not hits:
            return f"No wiki pages in {repo} mentioned: {query}"

        lines = [f"## Wiki hits in {repo} for: {query}\n"]
        for slug, snippet in hits[: self.valves.MAX_RESULTS_PER_SOURCE]:
            title = slug.replace("-", " ")
            lines.append(f"**{title}**\n   …{snippet}…\n   {base}/{slug}\n")
        return "\n".join(lines)

    # ── Stack Overflow / Stack Exchange ──────────────────────────────────

    async def search_stack_overflow(
        self,
        query: str,
        tags: str = "",
        site: str = "",
        answered_only: bool = True,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search Stack Overflow (or any Stack Exchange site) for questions
        relevant to a problem, ranked by relevance.
        :param query: Free-text query — paste the error or symptom.
        :param tags: Semicolon-separated tag filter, e.g. "python;asyncio".
        :param site: Site slug override (default: stackoverflow).
        :param answered_only: When True, hide unanswered questions.
        :return: Ranked list with title, score, accepted-answer marker, tags, link.
        """
        site = site or self.valves.DEFAULT_SITE
        params = self._se_params(
            {
                "order": "desc",
                "sort": "relevance",
                "q": query,
                "pagesize": self.valves.MAX_RESULTS_PER_SOURCE,
                "filter": "default",
            },
            site,
        )
        if tags:
            params["tagged"] = tags
        if answered_only:
            params["accepted"] = "True"

        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT) as c:
            try:
                r = await c.get(f"{SE_API}/search/advanced", params=params)
            except Exception as e:
                return f"Stack Exchange request failed: {e}"
            if r.status_code >= 400:
                return f"Stack Exchange error {r.status_code}: {r.text[:300]}"
            items = r.json().get("items", [])
        if not items:
            return f"No Stack Exchange ({site}) results for: {query}"

        lines = [f"## Stack Exchange [{site}]: {query}\n"]
        for it in items:
            title = html_mod.unescape(it.get("title", ""))
            tag_list = ", ".join(it.get("tags", [])[:4])
            accepted = "✅ accepted" if it.get("accepted_answer_id") else (
                "💬 answered" if it.get("is_answered") else "❓ unanswered"
            )
            lines.append(
                f"**{title}**\n"
                f"   score {it.get('score', 0)} · {it.get('answer_count', 0)} answers · "
                f"{accepted} · tags: {tag_list}\n"
                f"   {it.get('link', '')}  (qid {it.get('question_id')})\n"
            )
        return "\n".join(lines)

    async def get_stack_overflow_answers(
        self,
        question_id: int,
        site: str = "",
        top_n: int = 3,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Fetch the top N upvoted answers for a Stack Exchange question — accepted
        first, then by score. Each answer body is converted from HTML to text.
        :param question_id: Question ID (integer).
        :param site: Site slug (default: stackoverflow).
        :param top_n: How many answers to return (default 3).
        :return: Question title + ranked answer bodies.
        """
        site = site or self.valves.DEFAULT_SITE

        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT) as c:
            try:
                qr = await c.get(
                    f"{SE_API}/questions/{question_id}",
                    params=self._se_params({"filter": "withbody"}, site),
                )
                ar = await c.get(
                    f"{SE_API}/questions/{question_id}/answers",
                    params=self._se_params(
                        {
                            "order": "desc",
                            "sort": "votes",
                            "pagesize": max(top_n, 1),
                            "filter": "withbody",
                        },
                        site,
                    ),
                )
            except Exception as e:
                return f"Stack Exchange request failed: {e}"
            if qr.status_code >= 400 or ar.status_code >= 400:
                return f"Stack Exchange error: q={qr.status_code} a={ar.status_code}"
            qitems = qr.json().get("items", [])
            aitems = ar.json().get("items", [])
        if not qitems:
            return f"Question {question_id} not found on {site}."

        q = qitems[0]
        title = html_mod.unescape(q.get("title", ""))
        out = [
            f"## {title}",
            f"score {q.get('score', 0)} · {q.get('answer_count', 0)} answers · "
            f"tags: {', '.join(q.get('tags', [])[:5])}",
            f"https://{site}.com/q/{question_id}",
            "",
            "### Question",
            _strip_html(q.get("body", ""), self.valves.ANSWER_BODY_CHARS),
        ]
        # Accepted first, then by score.
        aitems.sort(
            key=lambda a: (1 if a.get("is_accepted") else 0, a.get("score", 0)),
            reverse=True,
        )
        for i, a in enumerate(aitems[:top_n], start=1):
            tag = "**Accepted**" if a.get("is_accepted") else f"#{i}"
            out.append(
                f"\n### Answer {tag} — score {a.get('score', 0)}\n"
                + _strip_html(a.get("body", ""), self.valves.ANSWER_BODY_CHARS)
                + f"\n🔗 https://{site}.com/a/{a.get('answer_id', '')}"
            )
        return "\n".join(out)

    # ── Aggregator ────────────────────────────────────────────────────────

    async def find_solutions(
        self,
        query: str,
        repo: str = "",
        language: str = "",
        site: str = "",
        include_discussions: bool = True,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        One-shot troubleshooting search. Runs Stack Overflow, GitHub closed
        issues, and (when GITHUB_TOKEN is set) GitHub Discussions in parallel,
        then stitches them into a single ranked digest. Use this first; fall
        back to per-source primitives if you need to drill in.
        :param query: Error message, exception type, or symptom.
        :param repo: Optional "owner/name" to scope GitHub searches to one project.
        :param language: Optional Stack Overflow tag / GitHub language hint.
        :param site: Stack Exchange site override.
        :param include_discussions: When True and a token is set, include Discussions.
        :return: Combined Markdown digest.
        """
        if __event_emitter__:
            await __event_emitter__(
                {"type": "status", "data": {"description": f"Searching SO + GitHub for: {query}", "done": False}}
            )

        tag_arg = language if language else ""
        coros: list[Any] = [
            self.search_stack_overflow(query, tags=tag_arg, site=site, answered_only=True),
            self.search_github_issues(
                query, repo=repo, state="closed", language=language, sort="reactions"
            ),
        ]
        if include_discussions and self.valves.GITHUB_TOKEN:
            coros.append(self.search_github_discussions(query, repo=repo))

        results = await asyncio.gather(*coros, return_exceptions=True)

        sections: list[str] = [f"# Troubleshooting digest: {query}"]
        if repo:
            sections[0] += f"  (repo: {repo})"
        labels = ["Stack Overflow", "GitHub Issues (closed)", "GitHub Discussions"]
        for label, res in zip(labels, results):
            if isinstance(res, Exception):
                sections.append(f"\n## {label}\n_(failed: {res})_")
            else:
                sections.append("\n" + str(res))

        if __event_emitter__:
            await __event_emitter__(
                {"type": "status", "data": {"description": "Done", "done": True}}
            )
        return "\n".join(sections)
