# Streaming (stream.mylensandi.com)

Jellyfin runs as a Windows service alongside the local-ai-stack and is
exposed at `https://stream.mylensandi.com` through the existing
cloudflared tunnel. Public access is gated by a Cloudflare Access
email-allowlist policy; only `kitisathreat@gmail.com` can reach the
origin.

## What's already wired up

| Piece | State | Notes |
|---|---|---|
| Jellyfin Server 10.11.x | installed via winget, running as service `JellyfinServer` | listens on `127.0.0.1:8096` |
| cloudflared ingress | `stream.mylensandi.com → http://localhost:8096` | added before the `http_status:404` catch-all |
| DNS | `stream.mylensandi.com` CNAME → tunnel `5bf79db7-…` | created via `cloudflared tunnel route dns` |

> **Heads-up on cloudflared config path on Windows.** The Windows
> service runs as `LocalSystem` and reads
> `C:\ProgramData\Cloudflare\cloudflared\config.yml`, **not**
> `C:\Users\Kit\.cloudflared\config.yml` (which is what the CLI uses).
> Windows' cloudflared installer keeps the two in sync on edits, but if
> you ever see an ingress change "not sticking" after a restart, edit
> `ProgramData` directly. Confirm which one the service is reading with:
> ```powershell
> Get-CimInstance Win32_Process -Filter "Name='cloudflared.exe'" |
>   Select-Object CommandLine
> ```

## What still needs the dashboard (one-time)

### 1. Cloudflare Access policy

This is the gate that enforces "admin only" at the edge — traffic that
fails the policy never reaches Jellyfin.

**Preferred — script path.** Run
[`scripts/setup-stream-cf-access.ps1`](../scripts/setup-stream-cf-access.ps1).
It's idempotent: creates the self-hosted app + email-allowlist policy, or
updates them in place if they already exist.

```powershell
# Token: dash.cloudflare.com/profile/api-tokens
# Permissions: Account -> Access: Apps and Policies -> Edit
$env:CF_API_TOKEN = '<paste-token>'
pwsh .\scripts\setup-stream-cf-access.ps1
```

The script verifies by hitting `https://stream.mylensandi.com` anonymously
and expecting a 302 to `cloudflareaccess.com`.

**Fallback — dashboard path.** If you'd rather click through:

1. <https://one.dash.cloudflare.com> → pick the `mylensandi.com` account →
   **Access → Applications → Add an application**.
2. Pick **Self-hosted**.
3. Application: name `Jellyfin (stream)`, session duration 30 days,
   subdomain `stream`, domain `mylensandi.com`, path blank.
4. **Identity providers**: enable Google (or One-time PIN).
5. **Add a policy**: name `kit only`, action **Allow**, Include → Emails →
   `kitisathreat@gmail.com`.
6. Save.

Either way, hitting `https://stream.mylensandi.com` from a fresh browser
will send you to a Cloudflare login page; sign in as
`kitisathreat@gmail.com` and CF forwards the request to Jellyfin.

### 2. Jellyfin first-run wizard

First time you reach Jellyfin, it walks you through a wizard:

1. **Display language**: English.
2. **Create your admin user**: pick a username/password. This is
   independent of the lai_session login — keep the password in 1Password
   or wherever.
3. **Set up media libraries**: click **Add Media Library**, pick a
   content type (Movies / Shows / Music), then **+** next to "Folders"
   and point at the local path you want indexed. Examples:
   - Movies: `D:\Media\Movies` (or wherever your library lives)
   - TV: `D:\Media\TV`
   - Music: `D:\Media\Music`

   Folder layout that scans cleanly:
   ```
   Movies\
     The Matrix (1999)\
       The Matrix (1999).mkv
       The Matrix (1999).en.srt
   TV\
     Severance\
       Season 01\
         Severance - S01E01 - Good News About Hell.mkv
   ```
   (Jellyfin reads sidecar `.srt` / `.ass` / `.vtt` files automatically;
   embedded subtitle tracks are also picked up.)
4. **Metadata language / country**: English / United States (or your
   pick).
5. **Configure remote access**:
   - **Allow remote connections**: ✅ (cloudflared is the WAN edge)
   - **Enable automatic port mapping**: ❌ (no UPnP — cloudflared is
     handling ingress)

### 3. Hardware acceleration (NVENC for the RTX PRO 4000 Blackwell)

After the wizard, in the Jellyfin web UI:

1. Click the user icon (top right) → **Dashboard** → **Playback** (left
   nav, under "Server").
2. **Hardware acceleration**: **Nvidia NVENC**.
3. **Enable hardware decoding for**: tick H264, HEVC, VP9, AV1 (Blackwell
   supports all four).
4. **Enable hardware encoding**: ✅
5. **Enable Tone mapping**: ✅ (for 4K HDR → SDR transcodes)
6. **Throttle transcodes**: ✅
7. Save. Test by playing a file, opening **Dashboard → Playback** while
   it streams; the active session should show "Transcoding (h264 NVENC)"
   instead of CPU.

### 4. Subtitles

Jellyfin handles subtitles three ways and you don't have to choose
manually:

- **Embedded text tracks (SRT/ASS/SSA)** — streamed directly to the
  player, toggleable per-stream.
- **Embedded image tracks (PGS/VOBSUB)** — burned into the video during
  transcode (NVENC handles this fine).
- **Sidecar files** — picked up if named `<filename>.<lang>.srt` next to
  the video.

To download missing subs automatically, install the **OpenSubtitles**
plugin: Dashboard → Plugins → Catalog → OpenSubtitles. You'll need a free
opensubtitles.com account; the plugin asks for credentials on first
configure.

### 5. Stream-quality controls (already built in)

The web player has a quality picker (gear icon during playback). Bitrate
caps are configurable per-user:

Dashboard → Users → (your user) → **Streaming** tab:
- **Internet streaming bitrate limit**: cap remote sessions (e.g.
  10 Mbps) so cloudflared bandwidth doesn't get pegged. Local LAN
  sessions ignore this.
- **Maximum allowed video bitrate during transcoding**: keep at default
  unless you see CPU/GPU pressure during peak transcoding.

## How the auth model works

You asked for "gated behind the user login, for now only accessible to
admin users (kitisathreat)". The implementation here uses **two
independent gates** that together produce that effect:

1. **Cloudflare Access** at the edge — only requests proving they're
   from `kitisathreat@gmail.com` ever reach your machine. This is the
   admin-only gate.
2. **Jellyfin's own user** behind it — your second password, scoped to
   Jellyfin only.

This is *not* unified with the `lai_session` cookie used by
`chat.mylensandi.com`. Functionally identical for now (only you are
authorized either way), but if you later add other admins, you have two
options:

- **Easy**: extend the Cloudflare Access email allowlist; add a Jellyfin
  user for each.
- **Unified**: write a small forward-auth shim in the FastAPI backend
  that validates `lai_session` + `is_admin`, then proxies to Jellyfin.
  CF Access goes away. More code to maintain — only worth it once you
  have ≥3 admins.

## Useful operations

| Goal | Command |
|---|---|
| Stop Jellyfin | `Stop-Service JellyfinServer` |
| Start Jellyfin | `Start-Service JellyfinServer` |
| View logs | `notepad "$env:LOCALAPPDATA\jellyfin\log\jellyfin*.log"` |
| Config dir | `$env:LOCALAPPDATA\jellyfin\config\` |
| Library DB | `$env:LOCALAPPDATA\jellyfin\data\library.db` (sqlite) |
| Reload cloudflared after editing config.yml | `Restart-Service cloudflared` |
| Rotate the CF Access policy (e.g. add a new email) | dashboard → Access → Applications → `Jellyfin (stream)` → Policies → edit |

## Mobile / TV apps

Once CF Access is in place, the official Jellyfin mobile + TV apps
(iOS, Android, AndroidTV, tvOS) won't work out-of-the-box — they don't
go through a browser and so can't complete the CF Access challenge.

Two options:

1. **Use the web app on mobile** (works fine; PWA-installable).
2. **Issue a Cloudflare Access service token** and configure the
   client to send it. CF docs: <https://developers.cloudflare.com/cloudflare-one/identity/service-tokens/>.
   Heavier setup; only do this once you actually want a TV app.
