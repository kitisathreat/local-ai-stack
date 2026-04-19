# Cloudflare Tunnel + Access setup

One-time setup to publish the stack at `https://chat.mylensandi.com` with magic-link email auth.

## Prerequisites
- `mylensandi.com` is on Cloudflare DNS
- `cloudflared` installed on the host (`winget install --id Cloudflare.cloudflared`)

## Steps

```bash
# 1. Authorize cloudflared against your Cloudflare account (opens a browser).
cloudflared tunnel login

# 2. Create a named tunnel.
cloudflared tunnel create local-ai-stack
# Note the UUID printed. Credentials JSON is written to ~/.cloudflared/<UUID>.json.

# 3. Route DNS. This creates a CNAME in the Cloudflare dashboard.
cloudflared tunnel route dns local-ai-stack chat.mylensandi.com

# 4. Edit cloudflare/config.yml — replace TUNNEL_UUID_HERE (both occurrences) with the UUID.

# 5. The cloudflared container will mount ~/.cloudflared/ and use config.yml on startup.
```

## Cloudflare Access (magic-link email auth)

In the Cloudflare Zero Trust dashboard:

1. **Access → Applications → Add application → Self-hosted**
2. Application domain: `chat.mylensandi.com`
3. **Policies → Add policy**
   - Name: "Allowed emails"
   - Action: Allow
   - Include: `Emails` → paste your allowlist (comma-separated)
4. **Identity providers**: enable **One-time PIN** (this is the magic-link UX)
5. Save.

Visitors to `chat.mylensandi.com` will be asked for their email, receive a 6-digit code, and be granted access only if the email is on the allowlist.

## Environment variables for the `api` service

After creating the Access application, set these in `.env` at the repo root so the API can verify Cloudflare's JWT:

```
CF_ACCESS_TEAM_DOMAIN=<your-team>.cloudflareaccess.com
CF_ACCESS_AUD=<AUD tag from the Application's Overview page>
```

For local development (bypassing CF Access):
```
DEV_TRUSTED_EMAIL=you@example.com
```
