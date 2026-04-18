"""
title: Network Tools — IP, DNS, WHOIS
author: local-ai-stack
description: Look up IP address geolocation and ASN info, resolve DNS records, and query WHOIS domain registration data. All free, no API key required.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
import socket
import re
from pydantic import BaseModel, Field
from typing import Callable, Any, Optional


class Tools:
    class Valves(BaseModel):
        pass

    def __init__(self):
        self.valves = self.Valves()

    async def lookup_ip(
        self,
        ip: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Look up geolocation, ISP, ASN, and network info for an IP address.
        :param ip: IPv4 or IPv6 address, or 'me' to look up your own public IP
        :return: Country, region, city, ISP, ASN, timezone, and coordinates
        """
        ip = ip.strip()
        if ip.lower() == "me":
            ip = ""  # ipinfo returns caller's IP when empty

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"https://ipinfo.io/{ip}/json",
                    headers={"Accept": "application/json"},
                )
                resp.raise_for_status()
                data = resp.json()

            addr     = data.get("ip", "?")
            hostname = data.get("hostname", "")
            city     = data.get("city", "")
            region   = data.get("region", "")
            country  = data.get("country", "")
            loc      = data.get("loc", "")
            org      = data.get("org", "")       # "AS15169 Google LLC"
            timezone = data.get("timezone", "")
            bogon    = data.get("bogon", False)

            lines = [f"## IP Lookup: {addr}\n"]
            if bogon:
                lines.append("⚠️ This is a private/reserved IP address (not routable on the public internet)")
            else:
                if hostname:
                    lines.append(f"- **Hostname:** {hostname}")
                lines.append(f"- **Location:** {city}, {region}, {country}")
                if loc:
                    lat, lon = loc.split(",")
                    lines.append(f"- **Coordinates:** {lat.strip()}°N, {lon.strip()}°E")
                lines.append(f"- **Organization/ASN:** {org}")
                lines.append(f"- **Timezone:** {timezone}")

            return "\n".join(lines)

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                return "Rate limit reached for ipinfo.io. Try again in a moment."
            return f"IP lookup error: HTTP {e.response.status_code}"
        except Exception as e:
            return f"IP lookup error: {str(e)}"

    def resolve_dns(
        self,
        hostname: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Resolve a domain name to its IP address(es) using system DNS.
        :param hostname: Domain name to resolve (e.g. "google.com", "api.github.com")
        :return: IP addresses the domain resolves to
        """
        hostname = hostname.strip().lower()
        hostname = re.sub(r"^https?://", "", hostname).split("/")[0]

        try:
            addrs = socket.getaddrinfo(hostname, None)
            ips = list(dict.fromkeys(a[4][0] for a in addrs))  # dedupe

            lines = [f"## DNS Resolution: {hostname}\n"]
            for ip in ips:
                ip_type = "IPv6" if ":" in ip else "IPv4"
                lines.append(f"- **{ip_type}:** {ip}")

            return "\n".join(lines)

        except socket.gaierror as e:
            return f"DNS resolution failed for '{hostname}': {e}\nThe domain may not exist or DNS may be unreachable."
        except Exception as e:
            return f"DNS error: {str(e)}"

    async def lookup_domain(
        self,
        domain: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get WHOIS-like registration info and basic metadata for a domain.
        :param domain: Domain name to look up (e.g. "example.com", "openai.com")
        :return: Registrar, creation date, expiry, and name servers
        """
        domain = domain.strip().lower()
        domain = re.sub(r"^https?://", "", domain).split("/")[0]

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                # Use RDAP (modern WHOIS replacement) via ICANN
                resp = await client.get(
                    f"https://rdap.org/domain/{domain}",
                    follow_redirects=True,
                )

                if resp.status_code == 404:
                    return f"Domain not found or not in RDAP: {domain}"
                resp.raise_for_status()
                data = resp.json()

            status = data.get("status", [])
            events = {e.get("eventAction", ""): e.get("eventDate", "") for e in data.get("events", [])}
            entities = data.get("entities", [])
            name_servers = [ns.get("ldhName", "") for ns in data.get("nameservers", [])]
            registrar = ""
            for entity in entities:
                roles = entity.get("roles", [])
                if "registrar" in roles:
                    vcard = entity.get("vcardArray", [None, []])[1]
                    for field in vcard:
                        if field[0] == "fn":
                            registrar = field[3]
                            break

            lines = [f"## Domain Info: {domain}\n"]
            if registrar:
                lines.append(f"- **Registrar:** {registrar}")
            if events.get("registration"):
                lines.append(f"- **Registered:** {events['registration'][:10]}")
            if events.get("expiration"):
                lines.append(f"- **Expires:** {events['expiration'][:10]}")
            if events.get("last changed"):
                lines.append(f"- **Last updated:** {events['last changed'][:10]}")
            if name_servers:
                lines.append(f"- **Name servers:** {', '.join(name_servers)}")
            if status:
                lines.append(f"- **Status:** {', '.join(status[:3])}")

            # Also get IP
            try:
                addrs = socket.getaddrinfo(domain, None)
                ips = list(dict.fromkeys(a[4][0] for a in addrs))[:3]
                lines.append(f"- **Resolves to:** {', '.join(ips)}")
            except Exception:
                pass

            return "\n".join(lines)

        except httpx.HTTPStatusError as e:
            return f"Domain lookup error: HTTP {e.response.status_code}"
        except Exception as e:
            return f"Domain lookup error: {str(e)}"
