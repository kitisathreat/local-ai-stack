# Public access — Cloudflare Tunnel + hardening

The project is **local-first**: by default everything listens on the
Docker compose network and nothing is exposed to the internet. When you
do want to share the chat UI publicly, the supported path is a
Cloudflare Tunnel — no router port forwarding, and Cloudflare Access
handles email-OTP auth as a second gate in front of the backend's own
magic-link flow.

## Architecture

```
browser (any network)
   │
   ▼   TLS
chat.yourdomain.com
   │
   ▼   Cloudflare Edge + Access (email OTP, allow-list)
   │
   ▼   Argo Tunnel (outbound-only connection from your host)
   │
  cloudflared container
   │
   ▼   compose network
   frontend (nginx → /api/* → backend:8000)
```

No inbound port is opened on your router. Cloudflare handles TLS
termination; the backend only ever sees plaintext HTTP from the
`cloudflared` container on the compose network.

## Two setup paths

### Path A — Token (simplest)

Good for a long-lived personal tunnel. Token goes in `.env.local`; it's
treated like an SSH key.

1. `bash scripts/setup-cloudflared.sh` walks through:
   - Logging in with `cloudflared tunnel login`
   - Creating a named tunnel
   - Creating a DNS route `chat.yourdomain.com → tunnel`
   - Writing `CLOUDFLARE_TUNNEL_TOKEN=…` to `.env.local`
2. `docker compose --profile public up -d cloudflared`

**Secret hygiene:**
- Token grants full tunnel control — rotate by deleting + recreating
  the tunnel in the Cloudflare dashboard.
- If `.env.local` is ever committed or pasted into chat, revoke
  immediately.
- The repo's `.gitignore` already excludes `.env.local`.

### Path B — Credentials.json (production)

Preferred for stable tunnels across host re-provisions because the
tunnel identity lives in a file you back up separately.

1. `cloudflared tunnel login` (interactive browser auth)
2. `cloudflared tunnel create local-ai-stack` → writes `~/.cloudflared/<uuid>.json`
3. Mount the file into the `cloudflared` service as a Docker secret:

```yaml
# docker-compose.override.yml (example; commit-safe if the secret is a path)
services:
  cloudflared:
    command: tunnel --no-autoupdate --config /etc/cloudflared/config.yml run
    volumes:
      - ./cf/credentials.json:/etc/cloudflared/credentials.json:ro
      - ./cf/config.yml:/etc/cloudflared/config.yml:ro
```

`config.yml`:

```yaml
tunnel: <uuid-from-create>
credentials-file: /etc/cloudflared/credentials.json
ingress:
  - hostname: chat.yourdomain.com
    service: http://frontend:80
  - service: http_status:404
```

## Cloudflare Access (the second auth gate)

In the CF dashboard:

1. Create an Access app for `chat.yourdomain.com`.
2. Policy = Allow; require email matching your allow-list.
3. Save.

The backend's magic-link flow then runs **inside** the Access-authed
session, which gives you:

- Audit trail of who visited (CF Access logs)
- A hard gate on unknown emails before any backend load
- Device posture options (certs, MFA) if you ever want them

## Environment variables

Set in `.env.local`:

```
PUBLIC_BASE_URL=https://chat.yourdomain.com
ALLOWED_ORIGINS=https://chat.yourdomain.com
CLOUDFLARE_TUNNEL_TOKEN=…                # Path A only
CLOUDFLARE_HOSTNAME=chat.yourdomain.com
CLOUDFLARE_TUNNEL_ENABLED=1              # trust CF-Connecting-IP as client IP
TRUSTED_PROXIES=127.0.0.1,::1            # the cloudflared container sits here
```

The backend's startup validator refuses to boot when:

- `PUBLIC_BASE_URL` is `http://` to a non-localhost host
- `PUBLIC_BASE_URL` is `https://` but `ALLOWED_ORIGINS=*`
- `cookie_secure=true` + `PUBLIC_BASE_URL=http://` (silent-cookie-drop bug)

so you'll see a clear error on `docker compose logs backend` if any of
these don't line up.

## Rate-limit key (X-Forwarded-For trust)

Anonymous callers are rate-limited by client IP. Blind trust of
`X-Forwarded-For` lets attackers either evade limits (by rotating the
header) or pin a victim's IP to lock them out.

The backend only trusts `X-Forwarded-For` / `CF-Connecting-IP` when
the immediate peer is in `TRUSTED_PROXIES` (CIDR list). With
`CLOUDFLARE_TUNNEL_ENABLED=1`, `127.0.0.1` / `::1` are additionally
auto-trusted because the `cloudflared` container runs alongside the
backend.

When trusted, the leftmost XFF hop that isn't itself in
`TRUSTED_PROXIES` is used as the client IP; otherwise we fall back to
`request.client.host`.

## Metrics + observability

`cloudflared` exposes Prometheus metrics when you set:

```
command: tunnel --metrics 0.0.0.0:2000 --no-autoupdate run
```

Scrape from inside the compose network at `http://cloudflared:2000/metrics`.
Useful counters: `cloudflared_tunnel_total_requests`,
`cloudflared_tunnel_active_connections`.

Backend-side, `GET /admin/errors` (admin only) merges chat-loop errors
with the `backend_errors` table so you can see failures that would
otherwise be silent (post-stream finalization, retrieval drops).

## Revocation

- **Drop the tunnel:** `cloudflared tunnel delete local-ai-stack` (path
  A) or delete in the CF dashboard. Traffic to your hostname 1016s
  immediately.
- **Drop a user:** remove their email from CF Access + your
  `allowed_email_domains` in `config/auth.yaml`. Any active sessions
  still validate until the JWT TTL expires — rotate `AUTH_SECRET_KEY`
  to force-expire every session.
- **Drop an admin:** pull their email from `ADMIN_EMAILS`. Effective on
  next request (no caching).

## HTTPS-native deployment (without Cloudflare)

Not first-class supported, but doable:

1. Terminate TLS at nginx/Caddy on the host.
2. `PUBLIC_BASE_URL=https://your-host`, `ALLOWED_ORIGINS=https://your-host`.
3. Put the proxy's IP into `TRUSTED_PROXIES` so the backend honors
   `X-Forwarded-For` from it.
4. Drop or comment out the `cloudflared` service.

Cloudflare buys the managed TLS + DDoS absorption + email-OTP gate; you
give those up if you replace it with a plain reverse proxy.
