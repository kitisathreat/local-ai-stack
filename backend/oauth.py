"""OAuth flow for connector authentication.

End-to-end "Sign in with X" for connectors that authenticate via OAuth
(Notion, Airtable, Figma, GitHub, Hugging Face, Google, Canva, Zoom).

Flow:
  1. Frontend opens a popup at GET /auth/oauth/<slug>/start. We require
     a signed-in session here (Depends(auth.current_user)) so we know
     who's authorising.
  2. We mint a one-time signed `state` token (PKCE verifier kept
     server-side in `_pending_state` keyed by the state nonce — never
     leaves the backend, even though the user can decode the JWT).
  3. We redirect the popup to the provider's authorize URL with our
     callback URL + state.
  4. Provider redirects to GET /auth/oauth/<slug>/callback with `code` +
     `state`. We verify state, exchange code → access token via the
     provider's token endpoint, and save the token under the user's
     encrypted oauth blob at data/user_oauth/<user_id>.enc.
  5. Callback returns a small HTML page that posts a window.postMessage
     to the parent and self-closes.

OAuth tokens stay SERVER-SIDE — the backend uses them to call provider
APIs on the user's behalf. Frontend never sees the token; it only sees
status (connected / expired / scopes) via /admin/me/oauth_status.

Per-provider OAuth-app registration (one-time admin setup):

  Notion          https://www.notion.com/my-integrations  →  "+ New integration"
                  Type: Public, redirect: <BASE>/auth/oauth/notion/callback
  Airtable        https://airtable.com/create/oauth      →  "Register an integration"
                  Redirect: <BASE>/auth/oauth/airtable/callback
                  Scopes: data.records:read, data.records:write, schema.bases:read
  Figma           https://www.figma.com/developers/apps  →  "Create new app"
                  Redirect: <BASE>/auth/oauth/figma/callback
  GitHub          https://github.com/settings/applications/new
                  Auth callback URL: <BASE>/auth/oauth/github_integration/callback
  Hugging Face    https://huggingface.co/settings/connected-applications
                  Redirect: <BASE>/auth/oauth/huggingface/callback
                  Scopes: read-repos, write-repos
  Canva           https://www.canva.com/developers/integrations/
  Zoom            https://marketplace.zoom.us/develop/create

After registering, set these env vars (in .env or systemd unit):

  OAUTH_NOTION_CLIENT_ID, OAUTH_NOTION_CLIENT_SECRET
  OAUTH_AIRTABLE_CLIENT_ID, OAUTH_AIRTABLE_CLIENT_SECRET
  OAUTH_FIGMA_CLIENT_ID, OAUTH_FIGMA_CLIENT_SECRET
  OAUTH_GITHUB_CLIENT_ID, OAUTH_GITHUB_CLIENT_SECRET
  OAUTH_HUGGINGFACE_CLIENT_ID, OAUTH_HUGGINGFACE_CLIENT_SECRET
  OAUTH_GOOGLE_CLIENT_ID, OAUTH_GOOGLE_CLIENT_SECRET   (Gmail / Drive / Calendar)
  OAUTH_CANVA_CLIENT_ID, OAUTH_CANVA_CLIENT_SECRET
  OAUTH_ZOOM_CLIENT_ID, OAUTH_ZOOM_CLIENT_SECRET

  OAUTH_BASE_URL  = public URL of this backend, e.g. "https://chat.example.com".
                    Defaults to request.base_url; override when behind a
                    reverse proxy / Cloudflare Tunnel where the actual
                    public URL differs from what FastAPI sees.

Local-only stacks: GitHub and Notion accept http://localhost:<port>
callbacks; the others usually require HTTPS. Use Cloudflare Tunnel or
ngrok to expose the backend if you need OAuth with those providers.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx
from jose import JWTError, jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import auth

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Provider registry ─────────────────────────────────────────────────

OAUTH_PROVIDERS: dict[str, dict] = {
    "notion": {
        "name": "Notion",
        "authorize_url": "https://api.notion.com/v1/oauth/authorize",
        "token_url": "https://api.notion.com/v1/oauth/token",
        "scopes": [],   # Notion scopes are configured in the integration itself
        "client_id_env": "OAUTH_NOTION_CLIENT_ID",
        "client_secret_env": "OAUTH_NOTION_CLIENT_SECRET",
        "auth_method": "basic",  # client creds in Authorization: Basic
        "extra_authorize_params": {"owner": "user"},
    },
    "airtable": {
        "name": "Airtable",
        "authorize_url": "https://airtable.com/oauth2/v1/authorize",
        "token_url": "https://airtable.com/oauth2/v1/token",
        "scopes": ["data.records:read", "data.records:write", "schema.bases:read"],
        "client_id_env": "OAUTH_AIRTABLE_CLIENT_ID",
        "client_secret_env": "OAUTH_AIRTABLE_CLIENT_SECRET",
        "auth_method": "basic",
        "pkce": True,  # Airtable requires PKCE
    },
    "figma": {
        "name": "Figma",
        "authorize_url": "https://www.figma.com/oauth",
        "token_url": "https://api.figma.com/v1/oauth/token",
        "scopes": ["files:read"],
        "client_id_env": "OAUTH_FIGMA_CLIENT_ID",
        "client_secret_env": "OAUTH_FIGMA_CLIENT_SECRET",
        "auth_method": "post_body",
    },
    "github_integration": {
        "name": "GitHub",
        "authorize_url": "https://github.com/login/oauth/authorize",
        "token_url": "https://github.com/login/oauth/access_token",
        "scopes": ["repo", "read:user", "read:org"],
        "client_id_env": "OAUTH_GITHUB_CLIENT_ID",
        "client_secret_env": "OAUTH_GITHUB_CLIENT_SECRET",
        "auth_method": "post_body",
    },
    "huggingface": {
        "name": "Hugging Face",
        "authorize_url": "https://huggingface.co/oauth/authorize",
        "token_url": "https://huggingface.co/oauth/token",
        "scopes": ["read-repos", "write-repos", "openid", "profile"],
        "client_id_env": "OAUTH_HUGGINGFACE_CLIENT_ID",
        "client_secret_env": "OAUTH_HUGGINGFACE_CLIENT_SECRET",
        "auth_method": "basic",
        "pkce": True,
    },
    "gmail": {
        "name": "Gmail",
        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "scopes": ["https://www.googleapis.com/auth/gmail.modify"],
        "client_id_env": "OAUTH_GOOGLE_CLIENT_ID",
        "client_secret_env": "OAUTH_GOOGLE_CLIENT_SECRET",
        "auth_method": "post_body",
        "extra_authorize_params": {"access_type": "offline", "prompt": "consent"},
    },
    "google_drive": {
        "name": "Google Drive",
        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "scopes": [
            "https://www.googleapis.com/auth/drive.file",
            "https://www.googleapis.com/auth/drive.readonly",
        ],
        "client_id_env": "OAUTH_GOOGLE_CLIENT_ID",
        "client_secret_env": "OAUTH_GOOGLE_CLIENT_SECRET",
        "auth_method": "post_body",
        "extra_authorize_params": {"access_type": "offline", "prompt": "consent"},
    },
    "google_calendar": {
        "name": "Google Calendar",
        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "scopes": ["https://www.googleapis.com/auth/calendar.events"],
        "client_id_env": "OAUTH_GOOGLE_CLIENT_ID",
        "client_secret_env": "OAUTH_GOOGLE_CLIENT_SECRET",
        "auth_method": "post_body",
        "extra_authorize_params": {"access_type": "offline", "prompt": "consent"},
    },
    "canva": {
        "name": "Canva",
        "authorize_url": "https://www.canva.com/api/oauth/authorize",
        "token_url": "https://api.canva.com/rest/v1/oauth/token",
        "scopes": [
            "design:meta:read", "design:content:read", "design:content:write",
            "asset:read", "asset:write", "folder:read", "comment:read",
        ],
        "client_id_env": "OAUTH_CANVA_CLIENT_ID",
        "client_secret_env": "OAUTH_CANVA_CLIENT_SECRET",
        "auth_method": "basic",
        "pkce": True,
    },
    "zoom": {
        "name": "Zoom",
        "authorize_url": "https://zoom.us/oauth/authorize",
        "token_url": "https://zoom.us/oauth/token",
        "scopes": ["meeting:read", "meeting:write", "user:read"],
        "client_id_env": "OAUTH_ZOOM_CLIENT_ID",
        "client_secret_env": "OAUTH_ZOOM_CLIENT_SECRET",
        "auth_method": "basic",
    },
    "linear": {
        "name": "Linear",
        "authorize_url": "https://linear.app/oauth/authorize",
        "token_url": "https://api.linear.app/oauth/token",
        "scopes": ["read", "write", "issues:create"],
        "client_id_env": "OAUTH_LINEAR_CLIENT_ID",
        "client_secret_env": "OAUTH_LINEAR_CLIENT_SECRET",
        "auth_method": "post_body",
    },
    "lucid": {
        "name": "Lucid",
        "authorize_url": "https://lucid.app/oauth2/authorize",
        "token_url": "https://api.lucid.co/oauth2/token",
        "scopes": ["lucidchart.document.app", "lucidchart.document.content:readonly"],
        "client_id_env": "OAUTH_LUCID_CLIENT_ID",
        "client_secret_env": "OAUTH_LUCID_CLIENT_SECRET",
        "auth_method": "basic",
    },
    "box": {
        "name": "Box",
        "authorize_url": "https://account.box.com/api/oauth2/authorize",
        "token_url": "https://api.box.com/oauth2/token",
        "scopes": [],   # Box scopes are configured app-side
        "client_id_env": "OAUTH_BOX_CLIENT_ID",
        "client_secret_env": "OAUTH_BOX_CLIENT_SECRET",
        "auth_method": "post_body",
    },
    "spotify": {
        "name": "Spotify",
        "authorize_url": "https://accounts.spotify.com/authorize",
        "token_url": "https://accounts.spotify.com/api/token",
        "scopes": [
            "user-read-private", "user-read-email",
            "playlist-read-private", "playlist-modify-public",
            "playlist-modify-private", "user-library-read",
            "user-library-modify", "user-read-currently-playing",
        ],
        "client_id_env": "OAUTH_SPOTIFY_CLIENT_ID",
        "client_secret_env": "OAUTH_SPOTIFY_CLIENT_SECRET",
        "auth_method": "basic",
        "pkce": True,
    },
}


# ── Encrypted token storage ───────────────────────────────────────────


def _oauth_dir() -> Path:
    repo = Path(__file__).resolve().parent.parent
    p = repo / "data" / "user_oauth"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _oauth_path(user_id: int) -> Path:
    return _oauth_dir() / f"{int(user_id)}.enc"


def _oauth_fernet_key() -> bytes:
    """Reuse the user_settings Fernet key file. Same security model:
    a stolen .enc blob alone doesn't decrypt without the key file at
    data/.user_settings_key (mode 0o600)."""
    from cryptography.fernet import Fernet
    p = _oauth_dir().parent / ".user_settings_key"
    if not p.exists():
        p.write_bytes(Fernet.generate_key())
        try:
            import stat as _stat
            os.chmod(p, _stat.S_IRUSR | _stat.S_IWUSR)
        except (OSError, AttributeError):
            pass
    return p.read_bytes()


def _load_oauth_blob(user_id: int) -> dict:
    from cryptography.fernet import Fernet, InvalidToken
    p = _oauth_path(user_id)
    if not p.exists():
        return {}
    try:
        f = Fernet(_oauth_fernet_key())
        return json.loads(f.decrypt(p.read_bytes()).decode("utf-8"))
    except (InvalidToken, ValueError, OSError) as exc:
        logger.warning("Failed to decrypt oauth blob for %s: %s", user_id, exc)
        return {}


def _save_oauth_blob(user_id: int, blob: dict) -> None:
    from cryptography.fernet import Fernet
    f = Fernet(_oauth_fernet_key())
    enc = f.encrypt(json.dumps(blob, separators=(",", ":")).encode("utf-8"))
    _oauth_path(user_id).write_bytes(enc)


def get_user_token(user_id: int, slug: str) -> dict | None:
    """Public helper for the rest of the backend: get the stored OAuth
    token blob for a user × connector. Tools / API call handlers use
    this to get a bearer token they can attach to provider requests."""
    blob = _load_oauth_blob(user_id)
    return blob.get(slug)


def _save_token(user_id: int, slug: str, token_resp: dict) -> dict:
    """Persist a token-exchange response. Normalises the field names
    across providers (some return `access_token`, some `bot_access_token`
    for Notion's bot tokens, etc.)."""
    blob = _load_oauth_blob(user_id)
    entry: dict = {
        "configured_at": int(time.time()),
        "provider": OAUTH_PROVIDERS.get(slug, {}).get("name", slug),
    }
    access = (
        token_resp.get("access_token")
        or token_resp.get("bot_access_token")     # Notion bot tokens
    )
    if not access:
        raise HTTPException(502, f"Provider {slug} did not return an access_token")
    entry["access_token"] = access
    if token_resp.get("refresh_token"):
        entry["refresh_token"] = token_resp["refresh_token"]
    if token_resp.get("expires_in"):
        try:
            entry["expires_at"] = int(time.time()) + int(token_resp["expires_in"])
        except (TypeError, ValueError):
            pass
    if token_resp.get("scope"):
        entry["scopes"] = token_resp["scope"]
    elif token_resp.get("scopes"):
        entry["scopes"] = token_resp["scopes"]
    if token_resp.get("workspace_id"):
        entry["workspace_id"] = token_resp["workspace_id"]
    if token_resp.get("workspace_name"):
        entry["workspace_name"] = token_resp["workspace_name"]
    blob[slug] = entry
    _save_oauth_blob(user_id, blob)
    return entry


# ── State signing + PKCE ──────────────────────────────────────────────

# In-memory pending-state map: state_nonce → {user_id, slug, verifier,
# expires_at}. PKCE verifier MUST stay server-side; the JWT state token
# contains only the nonce so the user can't extract the verifier even
# though JWTs are base64-decodable.
_pending_state: dict[str, dict] = {}
_STATE_TTL_S = 600


def _gc_pending() -> None:
    now = time.time()
    expired = [k for k, v in _pending_state.items() if v.get("expires_at", 0) < now]
    for k in expired:
        _pending_state.pop(k, None)


def _auth_secret() -> str:
    secret = os.environ.get("AUTH_SECRET_KEY", "")
    if not secret:
        # The auth module already refuses to boot without this; this
        # check here is a defensive 503 in case OAuth is hit on a
        # misconfigured boot.
        raise HTTPException(503, "AUTH_SECRET_KEY not set; OAuth disabled")
    return secret


def _sign_state(user_id: int, slug: str, nonce: str) -> str:
    payload = {
        "user_id": int(user_id),
        "slug": slug,
        "nonce": nonce,
        "iat": int(time.time()),
        "exp": int(time.time()) + _STATE_TTL_S,
    }
    return jwt.encode(payload, _auth_secret(), algorithm="HS256")


def _verify_state(token: str) -> dict:
    try:
        return jwt.decode(token, _auth_secret(), algorithms=["HS256"])
    except JWTError as exc:
        raise HTTPException(400, f"Invalid OAuth state: {exc}")


def _pkce_verifier() -> str:
    return secrets.token_urlsafe(64)[:128]


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


# ── Callback URL ──────────────────────────────────────────────────────


def _callback_url(request: Request, slug: str) -> str:
    """Compute the public callback URL. Honors OAUTH_BASE_URL when set
    (needed when behind Cloudflare Tunnel / a reverse proxy where
    request.base_url differs from the user-facing URL)."""
    base = os.environ.get("OAUTH_BASE_URL", "").rstrip("/")
    if not base:
        base = str(request.base_url).rstrip("/")
    return f"{base}/auth/oauth/{slug}/callback"


# ── Endpoints ─────────────────────────────────────────────────────────


@router.get("/auth/oauth/{slug}/start")
async def oauth_start(
    slug: str,
    request: Request,
    user: dict = Depends(auth.current_user),
):
    """Kick off the OAuth dance. Redirects the popup to the provider's
    authorize URL with our callback URL + state."""
    cfg = OAUTH_PROVIDERS.get(slug)
    if not cfg:
        raise HTTPException(404, f"Unknown OAuth connector: {slug}")
    client_id = os.environ.get(cfg["client_id_env"])
    if not client_id:
        raise HTTPException(
            503,
            f"OAuth not configured for {slug}. "
            f"Set {cfg['client_id_env']} (and its _SECRET) in .env. "
            f"See backend/oauth.py docstring for provider-portal links.",
        )
    nonce = secrets.token_urlsafe(24)
    state_token = _sign_state(user["id"], slug, nonce)
    verifier = _pkce_verifier() if cfg.get("pkce") else None
    _gc_pending()
    _pending_state[nonce] = {
        "user_id": int(user["id"]),
        "slug": slug,
        "verifier": verifier,
        "expires_at": time.time() + _STATE_TTL_S,
    }

    params = {
        "client_id": client_id,
        "redirect_uri": _callback_url(request, slug),
        "response_type": "code",
        "state": state_token,
    }
    scopes = cfg.get("scopes") or []
    if scopes:
        params["scope"] = " ".join(scopes)
    if cfg.get("pkce") and verifier:
        params["code_challenge"] = _pkce_challenge(verifier)
        params["code_challenge_method"] = "S256"
    extra = cfg.get("extra_authorize_params") or {}
    for k, v in extra.items():
        params[k] = v

    url = f"{cfg['authorize_url']}?{urlencode(params)}"
    return RedirectResponse(url, status_code=302)


@router.get("/auth/oauth/{slug}/callback")
async def oauth_callback(
    slug: str,
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
):
    """Provider redirects here after the user signs in. We exchange the
    authorization code for an access token and persist it under the user's
    encrypted OAuth blob, then return a self-closing HTML page that
    notifies the parent window via postMessage."""
    if error:
        return HTMLResponse(_oauth_result_page(slug, ok=False,
            message=f"{error}: {error_description or ''}"))
    if not code or not state:
        return HTMLResponse(_oauth_result_page(slug, ok=False,
            message="Missing code or state in callback"))
    cfg = OAUTH_PROVIDERS.get(slug)
    if not cfg:
        return HTMLResponse(_oauth_result_page(slug, ok=False,
            message=f"Unknown connector slug: {slug}"))

    # Verify state JWT
    try:
        payload = _verify_state(state)
    except HTTPException as exc:
        return HTMLResponse(_oauth_result_page(slug, ok=False,
            message=str(exc.detail)))
    if payload.get("slug") != slug:
        return HTMLResponse(_oauth_result_page(slug, ok=False,
            message="State / slug mismatch"))

    nonce = payload.get("nonce")
    pending = _pending_state.pop(nonce, None) if nonce else None
    if not pending:
        # Either expired or already redeemed (single-use). Treat as
        # benign in case the provider double-fired the callback.
        return HTMLResponse(_oauth_result_page(slug, ok=False,
            message="OAuth state expired or already used"))
    if pending.get("expires_at", 0) < time.time():
        return HTMLResponse(_oauth_result_page(slug, ok=False,
            message="OAuth state expired"))

    user_id = int(payload["user_id"])
    client_id = os.environ.get(cfg["client_id_env"])
    client_secret = os.environ.get(cfg["client_secret_env"])
    if not client_id or not client_secret:
        return HTMLResponse(_oauth_result_page(slug, ok=False,
            message=f"OAuth client credentials missing on backend ({cfg['client_id_env']})"))

    # Token exchange
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": _callback_url(request, slug),
    }
    headers = {"Accept": "application/json"}
    auth_method = cfg.get("auth_method", "post_body")
    if auth_method == "basic":
        creds = f"{client_id}:{client_secret}".encode("utf-8")
        headers["Authorization"] = "Basic " + base64.b64encode(creds).decode("ascii")
    else:
        data["client_id"] = client_id
        data["client_secret"] = client_secret
    if cfg.get("pkce") and pending.get("verifier"):
        data["code_verifier"] = pending["verifier"]

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(cfg["token_url"], data=data, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("OAuth token exchange failed for %s: %s", slug, exc)
        return HTMLResponse(_oauth_result_page(slug, ok=False,
            message=f"Network error talking to {cfg.get('name', slug)}: {exc}"))

    if r.status_code >= 400:
        logger.warning("OAuth %s token exchange %s: %s", slug, r.status_code, r.text[:500])
        return HTMLResponse(_oauth_result_page(slug, ok=False,
            message=f"Provider returned {r.status_code}: {r.text[:200]}"))

    try:
        token_resp = r.json()
    except ValueError:
        # GitHub historically returned form-encoded by default; we set
        # Accept: application/json above which fixes that, but be defensive.
        from urllib.parse import parse_qs
        token_resp = {k: v[0] for k, v in parse_qs(r.text).items()}

    try:
        _save_token(user_id, slug, token_resp)
    except HTTPException as exc:
        return HTMLResponse(_oauth_result_page(slug, ok=False, message=str(exc.detail)))

    return HTMLResponse(_oauth_result_page(slug, ok=True,
        message=f"Connected to {cfg.get('name', slug)}"))


@router.get("/admin/me/oauth_status")
async def oauth_status(user: dict = Depends(auth.current_user)):
    """Return non-sensitive connection status for every OAuth-stored
    connector. Frontend uses this to decide which connectors render as
    "Connected" vs "Sign in"."""
    blob = _load_oauth_blob(user["id"])
    out: dict[str, dict] = {}
    now = time.time()
    for slug, entry in blob.items():
        item = {
            "connected": bool(entry.get("access_token")),
            "configured_at": entry.get("configured_at"),
            "provider": entry.get("provider"),
        }
        if entry.get("scopes"):
            item["scopes"] = entry["scopes"]
        if entry.get("expires_at"):
            item["expires_at"] = entry["expires_at"]
            if entry["expires_at"] < now:
                item["expired"] = True
        if entry.get("workspace_name"):
            item["workspace_name"] = entry["workspace_name"]
        out[slug] = item
    return {"ok": True, "connectors": out}


@router.post("/admin/me/oauth_disconnect/{slug}")
async def oauth_disconnect(
    slug: str,
    user: dict = Depends(auth.current_user),
):
    """Drop the stored access token for one connector. Idempotent —
    returns ok regardless of whether the connector was connected."""
    blob = _load_oauth_blob(user["id"])
    if slug in blob:
        del blob[slug]
        _save_oauth_blob(user["id"], blob)
    return {"ok": True}


@router.get("/admin/oauth/providers")
async def oauth_providers_list(user: dict = Depends(auth.current_user)):
    """List which OAuth providers are configured (have client_id+secret
    set in env). Used by the frontend to decide which "Sign in with X"
    buttons to enable vs disable-with-tooltip-explaining-admin-must-
    register."""
    out: dict[str, dict] = {}
    for slug, cfg in OAUTH_PROVIDERS.items():
        configured = bool(
            os.environ.get(cfg["client_id_env"])
            and os.environ.get(cfg["client_secret_env"])
        )
        out[slug] = {
            "provider": cfg["name"],
            "configured": configured,
            "scopes": cfg.get("scopes", []),
            "client_id_env": cfg["client_id_env"],
        }
    return {"ok": True, "providers": out}


# ── Self-closing callback page ────────────────────────────────────────


def _oauth_result_page(slug: str, *, ok: bool, message: str) -> str:
    """Tiny HTML page returned to the popup after the OAuth round-trip.
    Posts a message to the parent (window.opener) and self-closes."""
    safe_slug = (slug or "").replace("'", "").replace("\\", "")
    safe_msg = (message or "").replace("</", "<\\/")
    title = "Connected" if ok else "Connection failed"
    icon = "✅" if ok else "⚠️"
    color = "#00FF66" if ok else "#FF3333"
    payload_json = json.dumps({
        "type": "oauth_complete",
        "ok": ok,
        "slug": safe_slug,
        "message": safe_msg,
    })
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>{title} — {safe_slug}</title>
<style>
  body {{ font-family: system-ui, -apple-system, Segoe UI, sans-serif;
         background: #0f1014; color: #e8e8e8; margin: 0;
         display: flex; align-items: center; justify-content: center;
         min-height: 100vh; }}
  .box {{ padding: 28px 32px; text-align: center; max-width: 380px;
          background: #181a21; border: 1px solid #2a2d36; border-radius: 10px;
          box-shadow: 0 12px 40px rgba(0,0,0,0.4); }}
  h1 {{ margin: 0 0 10px; font-size: 18px; color: {color}; }}
  p  {{ margin: 0; color: #aaa; font-size: 13px; line-height: 1.5; }}
  .small {{ margin-top: 16px; font-size: 11px; color: #666; }}
</style></head><body>
<div class="box">
  <h1>{icon} {title}</h1>
  <p>{safe_msg}</p>
  <p class="small">This window will close automatically.</p>
</div>
<script>
  (function(){{
    try {{
      if (window.opener) {{
        window.opener.postMessage({payload_json}, window.location.origin);
      }}
    }} catch (e) {{}}
    setTimeout(function(){{ window.close(); }}, {1200 if ok else 4500});
  }})();
</script>
</body></html>"""
