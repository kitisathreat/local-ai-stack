"""
title: Google Drive — Search, Read, Upload, Share
author: local-ai-stack
description: Search Drive, fetch file metadata, read file content (with smart Doc / Sheet / Slide export to markdown / CSV / PDF), upload files, manage permissions. OAuth refresh-token pattern shared with gmail / google_calendar.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from ._google_oauth import GoogleAuth, google_request


_API = "https://www.googleapis.com/drive/v3"
_UPLOAD = "https://www.googleapis.com/upload/drive/v3"


# Mapping of Google native MIME types -> the export format we'll request
# when reading content. Markdown for docs, CSV for sheets, PDF for slides.
_EXPORT_MAP = {
    "application/vnd.google-apps.document": "text/markdown",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "application/pdf",
    "application/vnd.google-apps.drawing": "image/png",
}


class Tools:
    class Valves(BaseModel):
        CLIENT_ID: str = Field(default="")
        CLIENT_SECRET: str = Field(default="")
        REFRESH_TOKEN: str = Field(
            default="",
            description=(
                "Refresh token. Mint with scopes "
                "https://www.googleapis.com/auth/drive (full) or "
                ".file / .readonly for narrower access."
            ),
        )
        DEFAULT_PAGE_SIZE: int = Field(default=25, description="Default search page size.")

    def __init__(self) -> None:
        self.valves = self.Valves()
        self._auth: GoogleAuth | None = None

    def _ensure_auth(self) -> GoogleAuth:
        if self._auth is None or self._auth.refresh_token != self.valves.REFRESH_TOKEN:
            self._auth = GoogleAuth(
                client_id=self.valves.CLIENT_ID,
                client_secret=self.valves.CLIENT_SECRET,
                refresh_token=self.valves.REFRESH_TOKEN,
            )
        return self._auth

    async def _json(self, method: str, path: str, **kw: Any) -> dict:
        r = await google_request(self._ensure_auth(), method, f"{_API}{path}", **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"Drive {method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}

    async def search(
        self,
        query: str = "",
        mime_type: str = "",
        order_by: str = "modifiedTime desc",
        page_size: int = 0,
    ) -> str:
        """Search Drive. Combine free-text with mime-type filtering.

        :param query: Free-text fragment matched against name + fullText.
        :param mime_type: Optional mime-type filter (e.g. "application/pdf").
        :param order_by: Drive ordering string.
        :param page_size: 1-1000; 0 uses the configured default.
        """
        n = page_size or self.valves.DEFAULT_PAGE_SIZE
        clauses: list[str] = ["trashed = false"]
        if query:
            safe = query.replace("'", "\\'")
            clauses.append(f"(name contains '{safe}' or fullText contains '{safe}')")
        if mime_type:
            clauses.append(f"mimeType = '{mime_type}'")
        params: dict[str, Any] = {
            "q": " and ".join(clauses),
            "orderBy": order_by,
            "pageSize": min(max(int(n), 1), 1000),
            "fields": "files(id,name,mimeType,modifiedTime,owners(displayName),webViewLink,size)",
        }
        body = await self._json("GET", "/files", params=params)
        files = body.get("files", [])
        if not files:
            return "No files."
        return "\n".join(
            f"- {f.get('id')}  {f.get('name')}  ({f.get('mimeType','?')})  modified={f.get('modifiedTime','')}"
            for f in files
        )

    async def get_metadata(self, file_id: str) -> str:
        """Fetch a file's metadata.

        :param file_id: Drive file id.
        """
        body = await self._json(
            "GET", f"/files/{file_id}",
            params={"fields": "id,name,mimeType,modifiedTime,createdTime,owners,parents,webViewLink,size,description"},
        )
        out = [f"# {body.get('name')}  ({body.get('id')})"]
        for k in ("mimeType", "size", "createdTime", "modifiedTime", "webViewLink"):
            if body.get(k):
                out.append(f"{k}: {body[k]}")
        owners = body.get("owners") or []
        if owners:
            out.append("owners: " + ", ".join(o.get("displayName", "?") for o in owners))
        if body.get("description"):
            out.append(f"\n{body['description']}")
        return "\n".join(out)

    async def read_file(self, file_id: str, max_chars: int = 8000) -> str:
        """Read a file's content. Google Docs / Sheets / Slides are exported
        to markdown / CSV / PDF (PDF returned as a notice — use download_pdf
        for the bytes).

        :param file_id: Drive file id.
        :param max_chars: Truncate the returned text to this many characters.
        """
        meta = await self._json(
            "GET", f"/files/{file_id}",
            params={"fields": "id,name,mimeType,size"},
        )
        mime = meta.get("mimeType", "")
        if mime in _EXPORT_MAP:
            export_mime = _EXPORT_MAP[mime]
            if export_mime == "application/pdf":
                return f"{meta.get('name')} is a Slides deck. Use export_to(file_id, 'application/pdf') to download as PDF."
            r = await google_request(
                self._ensure_auth(), "GET",
                f"{_API}/files/{file_id}/export",
                params={"mimeType": export_mime},
                accept=export_mime,
            )
            if r.status_code >= 400:
                raise RuntimeError(f"Drive export -> {r.status_code}: {r.text[:300]}")
            text = r.text
        else:
            r = await google_request(
                self._ensure_auth(), "GET",
                f"{_API}/files/{file_id}",
                params={"alt": "media"},
                accept="*/*",
            )
            if r.status_code >= 400:
                raise RuntimeError(f"Drive read -> {r.status_code}: {r.text[:300]}")
            # If it looks like text, decode; otherwise stub.
            try:
                text = r.text if r.encoding else r.content.decode("utf-8")
            except UnicodeDecodeError:
                return f"{meta.get('name')} is binary ({mime}); fetch with a download client."
        if len(text) > max_chars:
            return text[:max_chars] + f"\n\n... [truncated; {len(text) - max_chars} more chars]"
        return text

    async def upload_text(
        self,
        name: str,
        content: str,
        mime_type: str = "text/plain",
        parent_id: str = "",
    ) -> str:
        """Upload a small text file (≤ 5 MB).

        :param name: File name.
        :param content: Plain-text content.
        :param mime_type: MIME for the new file.
        :param parent_id: Optional folder id to put it in.
        """
        # Step 1: create the metadata.
        metadata: dict[str, Any] = {"name": name, "mimeType": mime_type}
        if parent_id: metadata["parents"] = [parent_id]
        # Step 2: simple multipart upload.
        boundary = "----lai-stack-boundary"
        body_bytes = (
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{_json_str(metadata)}\r\n"
            f"--{boundary}\r\nContent-Type: {mime_type}\r\n\r\n"
            f"{content}\r\n--{boundary}--"
        ).encode()
        r = await google_request(
            self._ensure_auth(), "POST",
            f"{_UPLOAD}/files?uploadType=multipart",
            data=body_bytes,
            accept="application/json",
        )
        if r.status_code >= 400:
            raise RuntimeError(f"Drive upload -> {r.status_code}: {r.text[:300]}")
        # The Content-Type header from headers() sets json; override here.
        body = r.json()
        return f"Uploaded {body.get('id')}  {body.get('name')}"

    async def share_file(
        self,
        file_id: str,
        role: str = "reader",
        type: str = "anyone",
        email: str = "",
    ) -> str:
        """Add a permission to a file.

        :param file_id: Drive file id.
        :param role: "reader" | "commenter" | "writer" | "owner".
        :param type: "user" | "group" | "domain" | "anyone".
        :param email: Required when type ∈ {user, group}.
        """
        if role not in {"reader", "commenter", "writer", "owner"}:
            raise ValueError("role must be reader/commenter/writer/owner.")
        if type not in {"user", "group", "domain", "anyone"}:
            raise ValueError("type must be user/group/domain/anyone.")
        payload: dict[str, Any] = {"role": role, "type": type}
        if type in {"user", "group"}:
            if not email:
                raise ValueError("email required for user/group permissions.")
            payload["emailAddress"] = email
        body = await self._json(
            "POST", f"/files/{file_id}/permissions",
            json_body=payload, params={"sendNotificationEmail": "false"},
        )
        return f"Granted {role}/{type} -> {file_id}  permission_id={body.get('id','?')}"

    async def list_recent(self, limit: int = 20) -> str:
        """List recently modified files.

        :param limit: 1-100.
        """
        return await self.search(query="", order_by="modifiedTime desc", page_size=limit)


def _json_str(d: dict) -> str:
    import json
    return json.dumps(d)
