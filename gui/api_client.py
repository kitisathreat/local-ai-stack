"""Thin async HTTP client for the backend.

Uses ``httpx.AsyncClient`` with Qt via ``qasync``. Cookies (the
``lai_session`` JWT set by /auth/login) persist on a shared
``httpx.Cookies`` jar for the life of the process.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import AsyncIterator

import httpx


@dataclass(frozen=True)
class ChatTurn:
    role: str
    content: str


class BackendClient:
    def __init__(self, base_url: str, token: str | None = None, timeout: float = 60.0):
        self._base = base_url.rstrip("/")
        self._headers: dict[str, str] = {}
        # Shared cookie jar — the session cookie set by /auth/login
        # attaches automatically to every subsequent request.
        self._cookies: httpx.Cookies = httpx.Cookies()
        if token:
            self._headers["Authorization"] = f"Bearer {token}"
        self._timeout = timeout

    # ── Convenience ────────────────────────────────────────────────────

    def set_token(self, token: str | None) -> None:
        if token:
            self._headers["Authorization"] = f"Bearer {token}"
        else:
            self._headers.pop("Authorization", None)

    def has_session(self) -> bool:
        return any(c.name == "lai_session" for c in self._cookies.jar)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base,
            headers=self._headers,
            cookies=self._cookies,
            timeout=self._timeout,
        )

    # ── Auth ──────────────────────────────────────────────────────────

    async def login(self, username: str, password: str) -> dict:
        async with self._client() as c:
            r = await c.post(
                "/auth/login",
                json={"username": username, "password": password},
            )
            # Persist any cookies the server set.
            self._cookies.update(r.cookies)
            if r.status_code == 401:
                raise ValueError("Invalid username or password")
            r.raise_for_status()
            return r.json()

    def login_sync(self, username: str, password: str) -> dict:
        """Synchronous variant for dialogs running inside Qt's modal
        exec() loop — qasync's asyncio scheduler is suspended while
        QDialog.exec() is on the stack, so any `await` inside the
        dialog hangs forever. We do a one-shot blocking POST instead.

        Raises ValueError("Invalid username or password") on 401, and
        ConnectionError(str) on any networking failure (with a short
        human-readable message)."""
        try:
            with httpx.Client(
                base_url=self._base,
                headers=self._headers,
                cookies=self._cookies,
                timeout=self._timeout,
            ) as c:
                r = c.post(
                    "/auth/login",
                    json={"username": username, "password": password},
                )
                self._cookies.update(r.cookies)
                if r.status_code == 401:
                    raise ValueError("Invalid username or password")
                r.raise_for_status()
                return r.json()
        except ValueError:
            raise
        except httpx.ConnectError as exc:
            raise ConnectionError(
                f"Could not reach backend at {self._base}. Is it running?"
            ) from exc
        except httpx.TimeoutException as exc:
            raise ConnectionError(
                f"Login timed out talking to {self._base}."
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise ConnectionError(
                f"Backend returned {exc.response.status_code}: {exc.response.text[:200]}"
            ) from exc

    async def logout(self) -> None:
        async with self._client() as c:
            try:
                await c.post("/auth/logout")
            except Exception:
                pass
        self._cookies.clear()

    def logout_sync(self) -> None:
        """Sync logout — companion to login_sync, callable from QDialog
        worker threads where the asyncio loop is suspended."""
        try:
            with httpx.Client(
                base_url=self._base, headers=self._headers,
                cookies=self._cookies, timeout=self._timeout,
            ) as c:
                c.post("/auth/logout")
        except Exception:
            pass
        self._cookies.clear()

    async def me(self) -> dict:
        async with self._client() as c:
            r = await c.get("/admin/me")
            r.raise_for_status()
            return r.json()

    # ── Read endpoints ─────────────────────────────────────────────────

    async def healthz(self) -> bool:
        async with self._client() as c:
            r = await c.get("/healthz")
            return r.status_code == 200

    async def list_models(self) -> list[dict]:
        async with self._client() as c:
            r = await c.get("/v1/models")
            r.raise_for_status()
            return (r.json() or {}).get("data", [])

    async def vram_status(self) -> dict:
        async with self._client() as c:
            r = await c.get("/vram")
            r.raise_for_status()
            return r.json()

    async def resolved_models(self) -> dict:
        async with self._client() as c:
            r = await c.get("/resolved-models")
            if r.status_code == 404:
                return {}
            r.raise_for_status()
            return r.json()

    async def airgap_state(self) -> dict:
        async with self._client() as c:
            r = await c.get("/api/airgap")
            r.raise_for_status()
            return r.json()

    # ── Admin ─────────────────────────────────────────────────────────

    async def admin_users(self) -> list[dict]:
        async with self._client() as c:
            r = await c.get("/admin/users")
            r.raise_for_status()
            return (r.json() or {}).get("data", [])

    async def admin_create_user(
        self, *, username: str, email: str, password: str, is_admin: bool = False,
    ) -> dict:
        async with self._client() as c:
            r = await c.post("/admin/users", json={
                "username": username, "email": email,
                "password": password, "is_admin": is_admin,
            })
            r.raise_for_status()
            return r.json()

    async def admin_patch_user(self, user_id: int, **fields) -> dict:
        fields = {k: v for k, v in fields.items() if v is not None}
        async with self._client() as c:
            r = await c.patch(f"/admin/users/{user_id}", json=fields)
            r.raise_for_status()
            return r.json()

    async def admin_delete_user(self, user_id: int) -> None:
        async with self._client() as c:
            r = await c.delete(f"/admin/users/{user_id}")
            r.raise_for_status()

    async def admin_tools(self) -> dict:
        """Returns {"data": [...tools...], "groups": [...taxonomy tree...]}."""
        async with self._client() as c:
            r = await c.get("/admin/tools")
            r.raise_for_status()
            payload = r.json() or {}
            return {"data": payload.get("data", []), "groups": payload.get("groups", [])}

    async def admin_set_tool_enabled(self, name: str, enabled: bool) -> None:
        async with self._client() as c:
            r = await c.patch(f"/admin/tools/{name}", json={"enabled": enabled})
            r.raise_for_status()

    async def admin_bulk_set_tools(
        self, enabled: bool, *,
        tier: str | None = None, group: str | None = None,
        subgroup: str | None = None, names: list[str] | None = None,
    ) -> dict:
        """Flip many tools at once. Pass `names` for an explicit list, or
        any combination of tier/group/subgroup to use the taxonomy filter.
        Returns the server response with the changed-name list and count."""
        body: dict = {"enabled": enabled}
        if names is not None: body["names"] = list(names)
        if tier is not None:     body["tier"] = tier
        if group is not None:    body["group"] = group
        if subgroup is not None: body["subgroup"] = subgroup
        async with self._client() as c:
            r = await c.patch("/admin/tools", json=body)
            r.raise_for_status()
            return r.json() or {}

    async def admin_set_airgap(self, enabled: bool) -> dict:
        async with self._client() as c:
            r = await c.patch("/admin/airgap", json={"enabled": enabled})
            r.raise_for_status()
            return r.json()

    async def admin_get_config(self) -> dict:
        async with self._client() as c:
            r = await c.get("/admin/config")
            r.raise_for_status()
            return r.json()

    async def admin_patch_config(self, payload: dict) -> dict:
        async with self._client() as c:
            r = await c.patch("/admin/config", json=payload)
            r.raise_for_status()
            return r.json()

    async def admin_errors(self, limit: int = 25) -> list[dict]:
        async with self._client() as c:
            r = await c.get("/admin/errors", params={"limit": limit})
            r.raise_for_status()
            return r.json() or []

    async def admin_reload(self) -> dict:
        async with self._client() as c:
            r = await c.post("/admin/reload")
            r.raise_for_status()
            return r.json()

    async def airgap_state(self) -> dict:
        async with self._client() as c:
            r = await c.get("/admin/airgap")
            r.raise_for_status()
            return r.json()

    async def vram_status(self) -> dict:
        async with self._client() as c:
            r = await c.get("/admin/vram")
            r.raise_for_status()
            return r.json()

    async def admin_overview(self) -> dict:
        """Usage / counters for the admin dashboard header."""
        async with self._client() as c:
            r = await c.get("/admin/overview")
            r.raise_for_status()
            return r.json()

    async def model_pull_status(self) -> dict:
        """Per-tier download progress for the Models tab progress bars.

        Returns {tier_name: {downloaded_bytes, expected_bytes, percent,
        complete, in_progress, repo, filename}}. Endpoint reads the
        on-disk GGUF size + the largest partial blob in the HF cache,
        so it surfaces in-flight downloads even before they're symlinked
        into snapshots/."""
        async with self._client() as c:
            r = await c.get("/admin/model-pull-status")
            if r.status_code == 404:
                return {}
            r.raise_for_status()
            return r.json()

    # ── Chat streaming ─────────────────────────────────────────────────

    async def stream_chat(
        self,
        messages: list[ChatTurn],
        model: str,
        *,
        think: bool | None = None,
    ) -> AsyncIterator[str]:
        """Yields incremental text tokens from the SSE response."""
        payload = {
            "model": model,
            "stream": True,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
        }
        if think is not None:
            payload["think"] = think
        async with self._client() as c:
            async with c.stream("POST", "/v1/chat/completions", json=payload) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    body = line[5:].strip()
                    if body == "[DONE]":
                        return
                    try:
                        chunk = json.loads(body)
                    except json.JSONDecodeError:
                        continue
                    for choice in chunk.get("choices", []):
                        delta = (choice.get("delta") or {}).get("content")
                        if delta:
                            yield delta
