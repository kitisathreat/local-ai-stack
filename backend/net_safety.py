"""SSRF guard for outbound HTTP made on behalf of users / models.

Used anywhere the backend follows a URL it didn't originate itself —
RAG ingest (URL fetch), web-search middleware, and any tool that takes
a user-supplied URL (#66).

The gate resolves the hostname, rejects private / link-local / loopback
/ cloud-metadata ranges, and rejects schemes other than http/https.
Service URLs the operator set themselves (OLLAMA_URL, QDRANT_URL, etc.)
bypass this because the operator is explicitly naming internal hosts.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

_BLOCKED_NETS = [
    ipaddress.ip_network(n) for n in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",     # CGNAT
        "127.0.0.0/8",
        "169.254.0.0/16",    # link-local incl. cloud metadata (169.254.169.254)
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "::1/128",
        "fc00::/7",          # unique local
        "fe80::/10",         # link-local v6
    )
]


class UnsafeURLError(ValueError):
    """Raised when a URL resolves to a disallowed destination."""


def _host_addresses(host: str) -> list[str]:
    """Resolve a hostname to every A/AAAA record. Empty list on failure."""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return []
    return [info[4][0] for info in infos]


def _address_is_blocked(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return True  # unparseable → treat as unsafe
    return any(ip in net for net in _BLOCKED_NETS)


def check_url(url: str, *, allow_schemes: tuple[str, ...] = ("http", "https")) -> str:
    """Validate `url` and return it unchanged, or raise UnsafeURLError.

    Checks:
      - scheme is http/https
      - hostname present
      - every A/AAAA record resolves OUTSIDE blocked ranges
      - literal IPs in the hostname are also range-checked
    """
    if not url or not isinstance(url, str):
        raise UnsafeURLError("URL must be a non-empty string")
    parts = urlsplit(url)
    if parts.scheme.lower() not in allow_schemes:
        raise UnsafeURLError(f"Disallowed scheme: {parts.scheme!r}")
    host = parts.hostname or ""
    if not host:
        raise UnsafeURLError("URL has no host")

    # Literal IP — range-check directly.
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if any(ip in net for net in _BLOCKED_NETS):
            raise UnsafeURLError(f"Blocked IP {host}")
        return url

    addrs = _host_addresses(host)
    if not addrs:
        raise UnsafeURLError(f"Could not resolve {host}")
    for addr in addrs:
        if _address_is_blocked(addr):
            raise UnsafeURLError(
                f"{host} resolves to blocked address {addr}"
            )
    return url
