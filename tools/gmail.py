"""
title: Gmail — Search, Read, Draft, Send, Label
author: local-ai-stack
description: Talk to a Gmail mailbox via the official REST API. Search threads / messages, read message bodies, create drafts, send mail, manage labels. Auth via OAuth 2.0 — supply client_id, client_secret, and a refresh_token minted with the gmail.modify scope (https://developers.google.com/gmail/api/auth/scopes).
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

import base64
from email.message import EmailMessage
from typing import Any, Optional

from pydantic import BaseModel, Field

from ._google_oauth import GoogleAuth, google_request


_API = "https://gmail.googleapis.com/gmail/v1/users/me"


class Tools:
    class Valves(BaseModel):
        CLIENT_ID: str = Field(default="", description="Google OAuth client_id (Installed-app type).")
        CLIENT_SECRET: str = Field(default="", description="Google OAuth client_secret.")
        REFRESH_TOKEN: str = Field(
            default="",
            description=(
                "Long-lived refresh token. Mint with scopes "
                "https://www.googleapis.com/auth/gmail.modify (or .readonly "
                "for read-only access)."
            ),
        )
        DEFAULT_MAX_RESULTS: int = Field(default=15, description="Default max results per listing.")

    def __init__(self) -> None:
        self.valves = self.Valves()
        self._auth: GoogleAuth | None = None

    def _ensure_auth(self) -> GoogleAuth:
        if (
            self._auth is None
            or self._auth.client_id != self.valves.CLIENT_ID
            or self._auth.refresh_token != self.valves.REFRESH_TOKEN
        ):
            self._auth = GoogleAuth(
                client_id=self.valves.CLIENT_ID,
                client_secret=self.valves.CLIENT_SECRET,
                refresh_token=self.valves.REFRESH_TOKEN,
            )
        return self._auth

    async def _request(self, method: str, path: str, **kw: Any) -> dict:
        auth = self._ensure_auth()
        r = await google_request(auth, method, f"{_API}{path}", **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"Gmail {method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}

    async def search_threads(self, query: str = "", max_results: int = 0) -> str:
        """Search threads with the standard Gmail query language (e.g.
        "from:foo@bar.com newer_than:7d label:inbox").

        :param query: Gmail query string. Empty = recent inbox.
        :param max_results: 1-100. 0 means use the configured default.
        """
        n = max_results or self.valves.DEFAULT_MAX_RESULTS
        body = await self._request(
            "GET", "/threads",
            params={"q": query, "maxResults": min(max(int(n), 1), 100)},
        )
        threads = body.get("threads", [])
        if not threads:
            return "No threads."
        return "\n".join(
            f"- thread {t.get('id')}  {(t.get('snippet') or '')[:120]}"
            for t in threads
        )

    async def get_thread(self, thread_id: str) -> str:
        """Fetch every message in a thread with subject, from, date, snippet.

        :param thread_id: Thread id (from search_threads).
        """
        body = await self._request("GET", f"/threads/{thread_id}", params={"format": "metadata"})
        messages = body.get("messages", [])
        if not messages:
            return "Empty thread."
        out = [f"# Thread {thread_id}"]
        for m in messages:
            headers = {h["name"].lower(): h["value"] for h in (m.get("payload", {}).get("headers") or [])}
            out.append(
                f"\n— {headers.get('from','?')}  [{headers.get('date','')}]\n"
                f"  Subject: {headers.get('subject','(no subject)')}\n"
                f"  {(m.get('snippet') or '')[:240]}"
            )
        return "\n".join(out)

    async def search_messages(self, query: str = "", max_results: int = 0) -> str:
        """Search individual messages (vs whole threads).

        :param query: Gmail query language.
        :param max_results: 1-100; 0 uses the configured default.
        """
        n = max_results or self.valves.DEFAULT_MAX_RESULTS
        body = await self._request(
            "GET", "/messages",
            params={"q": query, "maxResults": min(max(int(n), 1), 100)},
        )
        messages = body.get("messages", [])
        if not messages:
            return "No messages."
        return "\n".join(f"- {m.get('id')}  thread={m.get('threadId')}" for m in messages)

    async def get_message(self, message_id: str) -> str:
        """Fetch a single message with the body decoded to plain text.

        :param message_id: Message id.
        """
        body = await self._request("GET", f"/messages/{message_id}", params={"format": "full"})
        return _format_message(body)

    async def create_draft(
        self,
        to: str,
        subject: str,
        body: str,
        cc: str = "",
        bcc: str = "",
        thread_id: str = "",
    ) -> str:
        """Create a draft (does not send).

        :param to: Recipient (comma-separated for multiple).
        :param subject: Subject line.
        :param body: Plain-text body.
        :param cc: Optional Cc.
        :param bcc: Optional Bcc.
        :param thread_id: Optional thread id to reply within.
        """
        raw = _build_raw(to, subject, body, cc=cc, bcc=bcc)
        payload: dict[str, Any] = {"message": {"raw": raw}}
        if thread_id:
            payload["message"]["threadId"] = thread_id
        out = await self._request("POST", "/drafts", json_body=payload)
        return f"Created draft {out.get('id', '?')}  message={out.get('message', {}).get('id', '?')}"

    async def send_message(
        self,
        to: str,
        subject: str,
        body: str,
        cc: str = "",
        bcc: str = "",
        thread_id: str = "",
    ) -> str:
        """Send a new message immediately. NB: this writes to the recipient's
        inbox — only enable when the user explicitly opted in.

        :param to: Recipient.
        :param subject: Subject.
        :param body: Plain-text body.
        :param cc: Optional Cc.
        :param bcc: Optional Bcc.
        :param thread_id: Optional thread id (for in-thread replies).
        """
        raw = _build_raw(to, subject, body, cc=cc, bcc=bcc)
        payload: dict[str, Any] = {"raw": raw}
        if thread_id:
            payload["threadId"] = thread_id
        out = await self._request("POST", "/messages/send", json_body=payload)
        return f"Sent message {out.get('id', '?')}"

    async def list_labels(self) -> str:
        """List the user's labels (system + custom)."""
        body = await self._request("GET", "/labels")
        labels = body.get("labels", [])
        return "\n".join(f"- {l.get('id')}  {l.get('name')}  ({l.get('type', 'user')})" for l in labels) or "No labels."

    async def label_message(self, message_id: str, add: list[str] = None, remove: list[str] = None) -> str:
        """Add or remove labels on a message.

        :param message_id: Message id.
        :param add: Label ids to add.
        :param remove: Label ids to remove.
        """
        payload = {
            "addLabelIds": add or [],
            "removeLabelIds": remove or [],
        }
        await self._request("POST", f"/messages/{message_id}/modify", json_body=payload)
        return f"Updated labels on {message_id}: +{payload['addLabelIds']} -{payload['removeLabelIds']}"


def _build_raw(to: str, subject: str, body: str, cc: str = "", bcc: str = "") -> str:
    msg = EmailMessage()
    msg["To"] = to
    if cc: msg["Cc"] = cc
    if bcc: msg["Bcc"] = bcc
    msg["Subject"] = subject
    msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode().rstrip("=")


def _format_message(m: dict) -> str:
    payload = m.get("payload") or {}
    headers = {h["name"].lower(): h["value"] for h in (payload.get("headers") or [])}
    body_text = _extract_body(payload)
    out = [
        f"From: {headers.get('from','?')}",
        f"To: {headers.get('to','?')}",
        f"Subject: {headers.get('subject','(no subject)')}",
        f"Date: {headers.get('date','?')}",
        f"\n{body_text}",
    ]
    return "\n".join(out)


def _extract_body(payload: dict) -> str:
    """Recursively pull the first text/plain part. Falls back to text/html
    flattened, or the snippet."""
    mt = payload.get("mimeType", "")
    body = payload.get("body") or {}
    data = body.get("data")
    if mt == "text/plain" and data:
        return _b64url(data)
    parts = payload.get("parts") or []
    for p in parts:
        if p.get("mimeType") == "text/plain" and (p.get("body") or {}).get("data"):
            return _b64url(p["body"]["data"])
    for p in parts:
        text = _extract_body(p)
        if text:
            return text
    if data:
        return _b64url(data)
    return ""


def _b64url(s: str) -> str:
    pad = "=" * (-len(s) % 4)
    try:
        return base64.urlsafe_b64decode(s + pad).decode("utf-8", errors="replace")
    except Exception:
        return ""
