"""Cloudflare Tunnel provisioning helpers — called from the setup wizard.

Flow:
  1. _needs_login()         → check cert.pem age
  2. run_login()            → spawn `cloudflared tunnel login` (opens system browser)
  3. wait_for_cert()        → QFileSystemWatcher polls until cert.pem appears
  4. create_tunnel()        → `cloudflared tunnel create <name>`
  5. route_dns()            → `cloudflared tunnel route dns <uuid> <hostname>`
  6. write_config_yml()     → write ~/.cloudflared/config.yml
  7. install_service()      → `cloudflared service install`

All subprocess calls raise CloudflareSetupError on non-zero exit.
"""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import re
import subprocess
import yaml

CERT_MAX_AGE_DAYS = 90

# When this module runs under pythonw.exe (no console), subprocess calls
# inherit no stdin/stdout handles. Without CREATE_NO_WINDOW the child
# briefly flashes a console window AND on some Windows versions fails
# silently when it tries to write to its inherited (null) stdout. The
# flag is a Windows-only constant so we gate the lookup.
_NO_WINDOW = (
    getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
)


class CloudflareSetupError(Exception):
    """Raised when a cloudflared subprocess exits non-zero."""
    def __init__(self, message: str, stderr: str = ""):
        super().__init__(message)
        self.stderr = stderr


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _cloudflared_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("USERPROFILE", pathlib.Path.home())) / ".cloudflared"


def _cert_path() -> pathlib.Path:
    return _cloudflared_dir() / "cert.pem"


def _config_path() -> pathlib.Path:
    return _cloudflared_dir() / "config.yml"


def _needs_login(max_age_days: int = CERT_MAX_AGE_DAYS) -> bool:
    """Return True if cert.pem is missing or older than *max_age_days*."""
    cert = _cert_path()
    if not cert.exists():
        return True
    age = datetime.datetime.now() - datetime.datetime.fromtimestamp(cert.stat().st_mtime)
    return age.days >= max_age_days


def _run_cloudflared(
    args: list[str],
    cloudflared: pathlib.Path | None = None,
    cloudflared_dir: pathlib.Path | None = None,
    timeout: int = 120,
) -> str:
    """Run cloudflared with *args*, return stdout. Raises CloudflareSetupError on failure.

    Uses CREATE_NO_WINDOW on Windows so the wizard (running under pythonw.exe)
    doesn't flash a console window and doesn't silently break when the child
    inherits a null console handle.
    """
    exe = str(cloudflared or find_cloudflared())
    cmd = [exe] + list(args)
    # cloudflared writes its banner to stderr; we capture both and return
    # stdout. Some subcommands (`tunnel route dns`) emit only on stderr.
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=_NO_WINDOW,
    )
    if result.returncode != 0:
        # Surface stderr to the caller — that's where cloudflared writes
        # its actionable error messages (DNS conflicts, auth errors).
        msg = (result.stderr or result.stdout or "").strip()
        raise CloudflareSetupError(
            f"cloudflared {' '.join(args[:2])} exited {result.returncode}: "
            f"{msg[-500:]}",
            stderr=result.stderr,
        )
    # Combine stdout + stderr so callers that parse for the tunnel UUID
    # find it regardless of which stream cloudflared used.
    return (result.stdout or "") + "\n" + (result.stderr or "")


def _parse_tunnel_uuid(stdout: str) -> str:
    """Extract tunnel UUID from `cloudflared tunnel create` stdout.

    Raises CloudflareSetupError if no UUID is found.
    """
    match = re.search(
        r"Created tunnel\s+\S+\s+with id\s+([0-9a-f-]{36})",
        stdout,
        re.IGNORECASE,
    )
    if match:
        return match.group(1)
    # Fallback: bare UUID anywhere in output
    match = re.search(
        r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b",
        stdout,
    )
    if match:
        return match.group(1)
    raise CloudflareSetupError(
        f"Could not extract tunnel UUID from cloudflared output:\n{stdout}"
    )


def _write_config_yml(
    tunnel_id: str,
    hostname: str,
    backend_url: str = "http://localhost:18000",
    cloudflared_dir: pathlib.Path | None = None,
) -> pathlib.Path:
    """Write ~/.cloudflared/config.yml with correct ingress order.

    The chat hostname entry MUST come before the http_status:404 fallback —
    cloudflared evaluates ingress rules top-to-bottom.
    """
    cf_dir = cloudflared_dir or _cloudflared_dir()
    cf_dir.mkdir(parents=True, exist_ok=True)

    creds_file = str(cf_dir / f"{tunnel_id}.json")

    config = {
        "tunnel": tunnel_id,
        "credentials-file": creds_file,
        "ingress": [
            {"hostname": hostname, "service": backend_url},
            {"service": "http_status:404"},
        ],
    }
    config_path = cf_dir / "config.yml"
    config_path.write_text(yaml.safe_dump(config, default_flow_style=False), encoding="utf-8")
    return config_path


def _find_existing_tunnel(cloudflared_dir: pathlib.Path | None = None) -> dict | None:
    """Read an existing config.yml and return {tunnel_id, config_path} if present."""
    cfg_path = (cloudflared_dir or _cloudflared_dir()) / "config.yml"
    if not cfg_path.exists():
        return None
    try:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    tunnel_id = data.get("tunnel")
    if not tunnel_id:
        return None
    return {"tunnel_id": str(tunnel_id), "config_path": str(cfg_path)}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def find_cloudflared() -> pathlib.Path:
    """Locate the cloudflared binary.  Returns the Path or raises FileNotFoundError."""
    import shutil
    repo = pathlib.Path(__file__).resolve().parents[1]
    vendor_exe = repo / "vendor" / "cloudflared" / "cloudflared.exe"
    if vendor_exe.exists():
        return vendor_exe
    found = shutil.which("cloudflared")
    if found:
        return pathlib.Path(found)
    raise FileNotFoundError("cloudflared binary not found. Run LocalAIStack.ps1 -Setup")


def run_login(cloudflared: pathlib.Path | None = None) -> subprocess.Popen:
    """Spawn `cloudflared tunnel login` and return the Popen handle.

    cloudflared opens the system browser automatically and writes cert.pem
    when the user completes the OAuth flow.

    Under pythonw.exe (no console) we MUST pass CREATE_NO_WINDOW; without
    it the child inherits a null console and silently exits before
    opening the browser.
    """
    exe = str(cloudflared or find_cloudflared())
    return subprocess.Popen(
        [exe, "tunnel", "login"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=_NO_WINDOW,
    )


def find_remote_tunnel_by_name(
    name: str, cloudflared: pathlib.Path | None = None,
) -> str | None:
    """Look up an existing tunnel by name in the user's Cloudflare account.

    Uses `cloudflared tunnel list --output json --name <n>`. Returns the
    tunnel UUID if a non-deleted tunnel with that name exists, else None.
    Lets `create_tunnel` be idempotent so re-running the wizard doesn't
    fail with "tunnel already exists".
    """
    try:
        out = _run_cloudflared(
            ["tunnel", "list", "--output", "json", "--name", name],
            cloudflared=cloudflared,
            timeout=30,
        )
    except CloudflareSetupError:
        return None
    # cloudflared prints a JSON array to stdout (sometimes preceded by a
    # banner line on stderr; we combine streams in _run_cloudflared).
    # Find the first '[' through the matching ']' so we ignore any noise.
    m = re.search(r"\[\s*(?:\{[\s\S]*?\}\s*,?\s*)*\]", out)
    if not m:
        return None
    try:
        tunnels = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    for t in tunnels or []:
        if (t.get("name") or "").lower() != name.lower():
            continue
        # Skip soft-deleted entries. cloudflared writes "0001-01-01T00:00:00Z"
        # as the zero-value timestamp for live tunnels — that's *not* deleted.
        deleted_at = (t.get("deleted_at") or "").strip()
        if deleted_at and not deleted_at.startswith("0001-01-01"):
            continue
        tid = t.get("id") or t.get("ID")
        if tid:
            return str(tid)
    return None


def create_tunnel(name: str, cloudflared: pathlib.Path | None = None) -> str:
    """Create a Cloudflare Tunnel named *name*. Returns the UUID string.

    Idempotent: if a tunnel with that name already exists in the account,
    its UUID is returned without recreating. This handles the "user
    clicked Connect, the create succeeded, then the wizard crashed before
    saving the UUID" replay case.
    """
    existing = find_remote_tunnel_by_name(name, cloudflared=cloudflared)
    if existing:
        return existing
    stdout = _run_cloudflared(
        ["tunnel", "create", name], cloudflared=cloudflared, timeout=60,
    )
    return _parse_tunnel_uuid(stdout)


def route_dns(
    tunnel_id: str,
    hostname: str,
    cloudflared: pathlib.Path | None = None,
    *,
    overwrite: bool = True,
) -> None:
    """Route *hostname* to tunnel *tunnel_id*.

    `overwrite=True` (the default) passes ``-f`` so a stale CNAME from a
    previous tunnel is replaced rather than triggering a 1003 error.
    """
    args = ["tunnel", "route", "dns"]
    if overwrite:
        args.append("-f")
    args += [tunnel_id, hostname]
    _run_cloudflared(args, cloudflared=cloudflared, timeout=60)


def write_config_yml(
    tunnel_id: str,
    hostname: str,
    backend_url: str = "http://localhost:18000",
    cloudflared_dir: pathlib.Path | None = None,
) -> pathlib.Path:
    """Public alias for _write_config_yml."""
    return _write_config_yml(tunnel_id, hostname, backend_url, cloudflared_dir)


def find_existing_tunnel(cloudflared_dir: pathlib.Path | None = None) -> dict | None:
    """Public alias for _find_existing_tunnel."""
    return _find_existing_tunnel(cloudflared_dir)


def install_service(cloudflared: pathlib.Path | None = None) -> None:
    """Install cloudflared as a Windows service (requires elevation)."""
    _run_cloudflared(["service", "install"], cloudflared=cloudflared, timeout=30)


def cloudflared_login_via_terminal(
    cloudflared: pathlib.Path | None = None,
) -> subprocess.Popen:
    """Launch `cloudflared tunnel login` in a NEW console window.

    Useful when the wizard is hosted by pythonw.exe and we want the user
    to see the URL/banner cloudflared prints. Falls back to `run_login`
    when CREATE_NEW_CONSOLE isn't available (non-Windows).
    """
    exe = str(cloudflared or find_cloudflared())
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) if os.name == "nt" else 0
    return subprocess.Popen(
        [exe, "tunnel", "login"],
        creationflags=flags,
    )
