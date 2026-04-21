# Auth — magic-link sign-in and session cookies

Authentication is **magic link only** — there are no passwords. A user
submits an email, the backend emails them a one-time link, they click it,
and a signed JWT cookie is set for `cookie_ttl_days`. No stored secret per
user.

## The flow

```
1. POST /auth/request {email}
     → backend validates email + domain allow-list
     → backend writes a row to `magic_links` (email, token, expires_at, ip)
     → backend sends email via SMTP (or logs a redacted notice — see below)

2. User clicks the emailed URL:  GET /auth/verify?token=…
     → backend atomically consumes the token (used_at)
     → upserts the users row, mints a JWT, sets lai_session cookie
     → 302 to PUBLIC_BASE_URL

3. Subsequent requests carry the cookie.
     → backend.auth.current_user decodes + validates on every request
```

## SMTP setup

Set these in `.env.local`:

```
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USER=noreply@example.com
SMTP_PASS=…
SMTP_STARTTLS=true
AUTH_EMAIL_FROM=noreply@example.com
```

Helper: `bash scripts/setup-smtp.sh` walks through the common providers.

When SMTP isn't configured the backend falls back to logging a redacted
notice. To actually see the magic-link URL (local dev only), set
`AUTH_DEV_LINK_LOG=1`. **Do not** enable this in any environment where
log access isn't fully trusted.

## Cookies + session TTL

Configured in `config/auth.yaml` under `session:`:

| Field | Default | Notes |
|---|---|---|
| `cookie_name` | `lai_session` | |
| `cookie_ttl_days` | 30 | Session rotates after this many days |
| `cookie_secure` | `true` | Set to false only for localhost http:// dev |
| `cookie_samesite` | `lax` | `strict` breaks the magic-link redirect |
| `jwt_algorithm` | `HS256` | Symmetric; secret is `AUTH_SECRET_KEY` |

The backend refuses to boot when `cookie_secure=true` but
`PUBLIC_BASE_URL` is `http://` (see `docs/public-access.md`).

## Email domain allow-list

```yaml
# config/auth.yaml
allowed_email_domains:
  - mydomain.tld
  - partner-org.com
```

Empty list → any email accepts. Non-empty → anything else gets a clean
400.

## Rate limits

Set in `config/auth.yaml::rate_limits`:

| Field | Default | Purpose |
|---|---|---|
| `requests_per_hour_per_email` | 5 | Caps /auth/request per target |
| `requests_per_hour_per_ip` | 30 | Caps /auth/request per caller IP (anti-enumeration) |
| `requests_per_minute_per_user` | 30 | Chat-endpoint per-user throttle |
| `requests_per_day_per_user` | 500 | Daily ceiling for chat |

The 429 response is intentionally generic regardless of which bucket
tripped, so attackers can't distinguish "this email is known" from "my
IP is rate-limited."

## Secrets — rotation and generation

```bash
python -c 'import secrets; print(secrets.token_urlsafe(48))'
```

- `AUTH_SECRET_KEY` — JWT signing key. Rotate by replacing the value
  and restarting; all existing sessions become invalid on next request.
- `HISTORY_SECRET_KEY` — optional separate KEK for the encrypted
  chat-history store. When unset, `history_store.py` derives from
  `AUTH_SECRET_KEY` via HKDF so rotating the JWT key also invalidates
  encrypted history. Set explicitly if you want independent lifecycles.

Both belong in `.env.local` (gitignored). Anyone who reads either key
can forge sessions or decrypt history at rest.

## Per-user preferences

`GET /preferences` + `PATCH /preferences` let signed-in users toggle
their own middleware stack:

```json
{
  "inject_datetime": true,
  "inject_clarification": true,
  "auto_web_search": true,
  "inject_memories": true,
  "inject_rag": true,
  "rag_top_k": 3,
  "rag_min_score": 0.55,
  "memory_top_k": 3,
  "memory_cadence": 5
}
```

Unknown keys are ignored; numeric fields are clamped to 0..1 (scores)
or 1..20 (top-K). Defaults match the module-level constants so an
un-configured user sees no behavior change.

## Admin

Admin-gated endpoints live under `/admin/*` and require the user's
email to be in `ADMIN_EMAILS` (comma-separated, env var). Unset
`ADMIN_EMAILS` disables the dashboard entirely (503). See
`docs/public-access.md` for the production ACL story.
