"""Area C — Cloudflare Tunnel probes."""

from __future__ import annotations
import os
import pathlib
import re
import subprocess
import time


def _cf_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("USERPROFILE", os.environ.get("HOME", "~"))) \
                  .expanduser() / ".cloudflared"


def _read_env_key(key: str) -> str:
    """Read a single key from the .env file."""
    local_appdata = os.environ.get("LOCALAPPDATA")
    candidates = []
    if local_appdata:
        candidates.append(pathlib.Path(local_appdata) / "LocalAIStack" / ".env")
    candidates.append(pathlib.Path(__file__).resolve().parents[3] / ".env")
    for p in candidates:
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def run() -> list[dict]:
    results = []

    def probe(name: str, fn) -> None:
        try:
            status, detail, fix_hint = fn()
        except Exception as e:
            status, detail, fix_hint = "FAIL", str(e), "Re-run setup wizard"
        results.append({"area": "C", "test": name, "status": status,
                        "detail": detail, "fix_hint": fix_hint})

    cf = _cf_dir()
    cert = cf / "cert.pem"
    config_yml = cf / "config.yml"

    # cert.pem exists
    def _cert_exists():
        if cert.exists():
            return "PASS", str(cert), ""
        return "FAIL", f"Not found: {cert}", "Re-run setup wizard → Cloudflare page"

    probe("cert_pem_exists", _cert_exists)

    # cert.pem not expired (< 90 days old)
    def _cert_fresh():
        if not cert.exists():
            return "FAIL", "cert.pem missing", "Re-run setup wizard → Cloudflare page"
        age_days = (time.time() - cert.stat().st_mtime) / 86400
        if age_days < 90:
            return "PASS", f"cert age {age_days:.0f} days", ""
        return "FAIL", f"cert is {age_days:.0f} days old (> 90)", \
               "Re-run setup wizard → Cloudflare page to re-authorize"

    probe("cert_pem_fresh", _cert_fresh)

    # config.yml exists and is valid YAML
    def _config_valid():
        import yaml
        if not config_yml.exists():
            return "FAIL", f"Not found: {config_yml}", "Re-run setup wizard → Cloudflare page"
        try:
            data = yaml.safe_load(config_yml.read_text(encoding="utf-8"))
            return "PASS", f"tunnel={data.get('tunnel', '?')}", ""
        except Exception as e:
            return "FAIL", str(e), "Re-run setup wizard to regenerate config.yml"

    probe("tunnel_config_exists", _config_valid)

    # Ingress order: chat hostname before http_status:404
    def _ingress_order():
        import yaml
        if not config_yml.exists():
            return "FAIL", "config.yml missing", "Re-run setup wizard"
        data = yaml.safe_load(config_yml.read_text(encoding="utf-8"))
        ingress = data.get("ingress", [])
        chat_idx = next((i for i, r in enumerate(ingress) if r.get("hostname")), None)
        fallback_idx = next(
            (i for i, r in enumerate(ingress) if "404" in str(r.get("service", ""))),
            None
        )
        if chat_idx is None:
            return "WARN", "No hostname rule found in ingress", \
                   "Re-run setup wizard → Cloudflare page"
        if fallback_idx is None:
            return "WARN", "No http_status:404 fallback rule found", \
                   "Edit config.yml to add a fallback rule"
        if chat_idx < fallback_idx:
            return "PASS", f"hostname at idx {chat_idx}, fallback at {fallback_idx}", ""
        return "FAIL", \
               f"Fallback (idx {fallback_idx}) is BEFORE hostname (idx {chat_idx})", \
               "Re-run setup wizard or manually reorder ingress rules in config.yml"

    probe("ingress_order_correct", _ingress_order)

    # cloudflared service running (Windows)
    def _service_running():
        if os.name != "nt":
            return "SKIP", "Windows-only check", ""
        r = subprocess.run(
            ["sc", "query", "cloudflared"],
            capture_output=True, text=True, timeout=10
        )
        if "RUNNING" in r.stdout:
            return "PASS", "cloudflared service is running", ""
        if "STOPPED" in r.stdout:
            return "WARN", "cloudflared service is stopped", \
                   "Run: sc start cloudflared  OR  LocalAIStack.ps1 -EnableTunnel"
        return "FAIL", r.stdout.strip() or "cloudflared service not found", \
               "Run: cloudflared service install  OR  re-run setup wizard"

    probe("cloudflared_service_running", _service_running)

    # DNS resolves for CHAT_HOSTNAME
    def _dns():
        import socket
        hostname = _read_env_key("CHAT_HOSTNAME") or _read_env_key("CLOUDFLARE_HOSTNAME")
        if not hostname or hostname in ("localhost", "127.0.0.1"):
            return "SKIP", "Local-only mode — no public DNS to check", ""
        try:
            addrs = socket.getaddrinfo(hostname, 443)
            ip = addrs[0][4][0]
            return "PASS", f"{hostname} → {ip}", ""
        except socket.gaierror as e:
            return "WARN", f"DNS lookup failed: {e}", \
                   "Wait up to 5 min for DNS propagation after tunnel creation"

    probe("dns_resolves", _dns)

    # Chat reachable externally
    def _external():
        import httpx
        hostname = _read_env_key("CHAT_HOSTNAME") or _read_env_key("CLOUDFLARE_HOSTNAME")
        if not hostname or hostname in ("localhost", "127.0.0.1"):
            return "SKIP", "Local-only mode", ""
        url = f"https://{hostname}/healthz"
        try:
            r = httpx.get(url, timeout=15, follow_redirects=True)
            if r.status_code == 200:
                return "PASS", f"GET {url} → 200", ""
            return "WARN", f"GET {url} → {r.status_code}", \
                   "Check tunnel ingress and backend status"
        except Exception as e:
            return "FAIL", str(e), "Check cloudflared service and ingress config"

    probe("chat_reachable_externally", _external)

    return results
