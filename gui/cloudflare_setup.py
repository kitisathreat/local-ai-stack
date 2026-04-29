"""Cloudflare Tunnel provisioning helpers — called from the setup wizard.

Flow:
  1. _needs_login()       → check cert.pem age
  2. run_login()          → spawn `cloudflared tunnel login` (opens system browser)
  3. wait_for_cert()      → QFileSystemWatcher polls until cert.pem appears
  4. create_tunnel()      → `cloudflared tunnel create <name>`
  5. route_dns()          → `cloudflared tunnel route dns <uuid> <hostname>`
  6. write_config_yml()   → write ~/.cloudflared/config.yml
  7. install_service()    → `cloudflared service install`

All subprocess calls raise CloudflareSetupError on non-zero exit with
the captured stderr included in the message.
"""
from __future__ import annotations

import datetime
import os
import pathlib
import re
import subprocess
import yaml


class CloudflareSetupError(Exception):
    """Raised when a cloudflared subprocess exits non-zero."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cloudflared_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("USERPROFILE", pathlib.Path.home())) / ".cloudflared"


def _cert_path() -> pathlib.Path:
    return _cloudflared_dir() / "cert.pem"


def _config_path() -> pathlib.Path:
    return _cloudflared_dir() / "config.yml"


def _needs_login(max_age_days: int = 90) -> bool:
    """Return True if cert.pem is missing or older than *max_age_days*."""
    cert = _cert_path()
    if not cert.exists():
        return True
    age = datetime.datetime.now() - datetime.datetime.fromtimestamp(cert.stat().st_mtime)
    return age.days >= max_age_days


def _run(cmd: list[str], cwd: str | None = None, timeout: int = 120) -> str:
    """Run *cmd*, return stdout. Raises CloudflareSetupError on failure."""
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=cwd,
    )
    if result.returncode != 0:
        raise CloudflareSetupError(
            f"Command {cmd[0]} exited {result.returncode}: {result.stderr.strip()[-500:]}"
        )
    return result.stdout


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def find_cloudflared() -> pathlib.Path:
    """Locate the cloudflared binary.  Returns the Path or raises FileNotFoundError."""
    repo = pathlib.Path(__file__).resolve().parents[1]
    candidates = [
        repo / "vendor" / "cloudflared" / "cloudflared.exe",
        pathlib.Path("cloudflared"),  # on PATH
    ]
    for c in candidates:
        if c.is_absolute():
            if c.exists():
                return c
        else:
            # Try resolving via PATH
            import shutil
            found = shutil.which("cloudflared")
            if found:
                return pathlib.Path(found)
    raise FileNotFoundError("cloudflared binary not found. Run LocalAIStack.ps1 -Setup")


def run_login(cloudflared: pathlib.Path | None = None) -> subprocess.Popen:
    """Spawn `cloudflared tunnel login` and return the Popen handle.

    The caller is responsible for waiting / terminating the process.
    cloudflared opens the system browser automatically and writes cert.pem
    when the user completes the OAuth flow.
    """
    exe = str(cloudflared or find_cloudflared())
    return subprocess.Popen(
        [exe, "tunnel", "login"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def parse_tunnel_uuid(stdout: str) -> str | None:
    """Extract the tunnel UUID from `cloudflared tunnel create` stdout."""
    match = re.search(
        r"Created tunnel\s+\S+\s+with id\s+([0-9a-f-]{36})",
        stdout,
        re.IGNORECASE,
    )
    if match:
        return match.group(1)
    # Fallback: bare UUID anywhere on a line
    match = re.search(r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b",
                      stdout)
    return match.group(1) if match else None


def create_tunnel(name: str, cloudflared: pathlib.Path | None = None) -> str:
    """Create a Cloudflare Tunnel named *name*.  Returns the UUID string."""
    exe = str(cloudflared or find_cloudflared())
    stdout = _run([exe, "tunnel", "create", name], timeout=60)
    uuid = parse_tunnel_uuid(stdout)
    if not uuid:
        raise CloudflareSetupError(f"Could not extract tunnel UUID from output:\n{stdout}")
    return uuid


def route_dns(uuid: str, hostname: str, cloudflared: pathlib.Path | None = None) -> None:
    """Route *hostname* to tunnel *uuid*."""
    exe = str(cloudflared or find_cloudflared())
    _run([exe, "tunnel", "route", "dns", uuid, hostname], timeout=60)


def write_config_yml(
    uuid: str,
    hostname: str,
    backend_port: int = 18000,
    cloudflared_dir: pathlib.Path | None = None,
) -> pathlib.Path:
    """Write ~/.cloudflared/config.yml with correct ingress order.

    The chat hostname entry MUST come before the http_status:404 fallback —
    cloudflared evaluates ingress rules top-to-bottom.
    """
    cf_dir = cloudflared_dir or _cloudflared_dir()
    cf_dir.mkdir(parents=True, exist_ok=True)

    userprofile = os.environ.get("USERPROFILE", str(pathlib.Path.home()))
    creds_file = str(cf_dir / f"{uuid}.json")

    config = {
        "tunnel": uuid,
        "credentials-file": creds_file,
        "ingress": [
            {"hostname": hostname, "service": f"http://localhost:{backend_port}"},
            {"service": "http_status:404"},
        ],
    }
    config_path = cf_dir / "config.yml"
    config_path.write_text(yaml.safe_dump(config, default_flow_style=False), encoding="utf-8")
    return config_path


def find_existing_tunnel(cloudflared_dir: pathlib.Path | None = None) -> dict | None:
    """Read an existing config.yml and return {uuid, name} if present, else None."""
    cfg_path = (cloudflared_dir or _cloudflared_dir()) / "config.yml"
    if not cfg_path.exists():
        return None
    try:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    uuid = data.get("tunnel")
    if not uuid:
        return None
    return {"uuid": str(uuid), "config_path": str(cfg_path)}


def install_service(cloudflared: pathlib.Path | None = None) -> None:
    """Install cloudflared as a Windows service (requires elevation)."""
    exe = str(cloudflared or find_cloudflared())
    _run([exe, "service", "install"], timeout=30)
