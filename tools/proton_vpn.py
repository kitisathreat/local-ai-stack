"""
title: ProtonVPN Control — Status / Reconnect / Cycle Adapter
author: local-ai-stack
description: Inspect and control ProtonVPN's Windows service + adapter to bypass IP-based rate limiting and geo-restrictions, and to recover from stuck local DNS proxies (the 127.0.2.x dead-resolver failure mode).
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT

Architecture notes:

  - ProtonVPN's Windows app is GUI-only — no documented CLI. We therefore
    drive it through three indirect levers:
      1. `Restart-Service ProtonVPN*` — recycles the service host that
         owns the SmartConnect logic + the local DNS proxy at
         127.0.2.2/.3. Cheapest action; usually fixes a stuck DNS proxy
         without forcing a reconnect.
      2. `Disable-NetAdapter` + `Enable-NetAdapter` on the ProtonVPN
         WireGuard / OpenVPN adapter — forces the client to re-establish
         the tunnel, which (with SmartConnect on) typically picks a
         different exit server and gives the user a different public IP.
         Heavier than (1); use when (1) didn't change the IP.
      3. Reading current state via Get-Service / Get-NetAdapter / IP probe.

  - ProtonVPN auth is OAuth-based and stored in the user profile; we don't
    try to drive the GUI's connect flow. The service has to already be
    logged in (the GUI has been at least once after install).

  - Both Disable-NetAdapter and Restart-Service typically need elevation
    OR a user-level service permission grant. The tool reports actionable
    errors when permissions are missing.

  - Auto-use hook: `auto_recover_dns()` is the entry point the watchdog
    + model_resolver call. It walks Restart-Service first, then
    adapter-cycle if DNS still broken, then gives up with diagnostics.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import time
from typing import Any, Callable, Optional

import httpx
from pydantic import BaseModel, Field


# RFC-1123 hostname regex. Matches the subset we let into PowerShell
# command strings — letters, digits, dots, hyphens. Anything else
# (single-quote, semicolon, backtick, …) is rejected to prevent the
# valve-driven PS-injection path that the auto_recover_dns hostname
# probe used to be vulnerable to.
_HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$", re.IGNORECASE)


def _is_elevated() -> bool:
    """Return True if the current process has Windows admin rights.

    `Restart-Service` and `Disable-/Enable-NetAdapter` both require an
    elevated token; without one they fail with cryptic SCM errors. We
    surface that as a clean message instead.
    """
    if sys.platform != "win32":
        # Non-Windows: tool is a no-op anyway, but answer truthfully.
        try:
            return os.geteuid() == 0  # type: ignore[attr-defined]
        except AttributeError:
            return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except (OSError, AttributeError):
        return False


_ELEVATION_HINT = (
    "Relaunch LocalAIStack as Administrator (right-click → Run as administrator) "
    "so the tool can drive Restart-Service / Disable-NetAdapter."
)


# All powershell calls go through this so we can centralise the
# windowless / non-blocking flags. Use pwsh if available (it ships
# with the launcher's prereqs), fall back to powershell.exe otherwise.
def _powershell_exe() -> str:
    for candidate in ("pwsh", "powershell"):
        try:
            r = subprocess.run(
                [candidate, "-NoProfile", "-Command", "$PSVersionTable.PSVersion.Major"],
                capture_output=True, text=True, timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if r.returncode == 0:
                return candidate
        except (OSError, subprocess.SubprocessError):
            continue
    return "powershell"   # final fallback; caller will see the error


def _run_ps(script: str, timeout: int = 15) -> tuple[int, str, str]:
    """Run a PowerShell snippet, hidden console, return (exit, stdout, stderr)."""
    exe = _powershell_exe()
    try:
        r = subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return r.returncode, r.stdout or "", r.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    except OSError as exc:
        return 127, "", str(exc)


def _run_ps_args(script: str, args: list[str], timeout: int = 15) -> tuple[int, str, str]:
    """Run a PowerShell `param(...)` script with positional args.

    The args travel as separate argv entries — PowerShell never sees
    them as command text, so they can't break out of quoting. Use this
    (not `_run_ps` with f-string interpolation) any time you're passing
    a value derived from a valve, a tool argument, or anything else
    not under our static control.
    """
    exe = _powershell_exe()
    try:
        r = subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-Command", script, *args],
            capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return r.returncode, r.stdout or "", r.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    except OSError as exc:
        return 127, "", str(exc)


async def _public_ip(client: httpx.AsyncClient) -> tuple[str, str]:
    """Return (ip, country_code) using ipinfo.io. Returns ('', '') on failure.
    Async so the LLM-driven tool flow doesn't block the event loop."""
    try:
        r = await client.get("https://ipinfo.io/json", timeout=8)
        r.raise_for_status()
        d = r.json()
        return d.get("ip", "") or "", d.get("country", "") or ""
    except (httpx.HTTPError, ValueError, KeyError):
        return "", ""


class Tools:
    class Valves(BaseModel):
        # Cap on how aggressive auto_recover_dns gets before giving up.
        max_recovery_attempts: int = Field(
            default=2,
            description="Max times auto_recover_dns will try the full Restart→Cycle ladder before returning failure",
        )
        # When true, IP-rate-limit detection by upstream callers can
        # auto-cycle the adapter. When false, the rate-limit hint just
        # logs and asks for explicit consent.
        allow_auto_ip_rotate: bool = Field(
            default=False,
            description="When true, callers seeing HF / API rate-limit responses can auto-rotate the VPN exit server. Off by default since it disrupts the user's active VPN session.",
        )
        # The DNS host the auto-recovery health-check resolves against.
        # Defaults to HF's XetHub bridge since that's the most-broken
        # downstream during ProtonVPN DNS proxy stalls.
        recovery_test_host: str = Field(
            default="cas-bridge.xethub.hf.co",
            description="Hostname auto_recover_dns probes to determine whether DNS is healthy.",
        )

    def __init__(self) -> None:
        self.valves = self.Valves()

    # ── status ──────────────────────────────────────────────────────────

    async def status(
        self,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Report the current ProtonVPN service + adapter state plus the
        observed public IP and country.

        :return: Markdown-formatted status block.
        """
        services_script = """
        $svc = Get-Service | Where-Object { $_.Name -match 'Proton' } |
               Select-Object Name, DisplayName, Status
        $adapters = Get-NetAdapter -ErrorAction SilentlyContinue |
                    Where-Object { $_.InterfaceDescription -match 'Proton' -or $_.Name -match 'Proton' } |
                    Select-Object Name, InterfaceDescription, Status, MacAddress
        @{ services = @($svc); adapters = @($adapters) } | ConvertTo-Json -Depth 4 -Compress
        """
        rc, out, err = await asyncio.to_thread(_run_ps, services_script)
        if rc != 0:
            return f"[proton_vpn.status] ERROR: {err.strip() or out.strip()}"
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return f"[proton_vpn.status] PARSE ERROR\nRaw stdout: {out[:600]}"

        async with httpx.AsyncClient() as c:
            ip, cc = await _public_ip(c)

        lines = ["## ProtonVPN status"]
        services = data.get("services") or []
        if services:
            lines.append("\n### Services")
            for s in services if isinstance(services, list) else [services]:
                lines.append(f"  - **{s.get('Name')}** ({s.get('DisplayName')}): {s.get('Status')}")
        else:
            lines.append("\n### Services\n  *(none — ProtonVPN may not be installed)*")
        adapters = data.get("adapters") or []
        if adapters:
            lines.append("\n### Network adapters")
            for a in adapters if isinstance(adapters, list) else [adapters]:
                lines.append(
                    f"  - **{a.get('Name')}**: {a.get('Status')} "
                    f"({a.get('InterfaceDescription')})"
                )
        else:
            lines.append("\n### Network adapters\n  *(no Proton-named adapter found)*")
        lines.append("\n### Public IP")
        lines.append(f"  - **{ip or '?'}** ({cc or '?'})")
        return "\n".join(lines)

    # ── restart / reconnect levers ──────────────────────────────────────

    async def restart_service(
        self,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Restart all ProtonVPN-named Windows services. Cheapest recovery
        action — typically fixes a stuck local DNS proxy at 127.0.2.x
        without dropping the VPN tunnel itself.

        :return: One-line per service: 'Name: RESTARTED' / 'failed: <reason>'.
        """
        if not _is_elevated():
            return f"[proton_vpn.restart_service] requires admin. {_ELEVATION_HINT}"
        script = r"""
        $results = @()
        foreach ($svc in (Get-Service | Where-Object { $_.Name -match 'Proton' })) {
            try {
                Restart-Service -Name $svc.Name -Force -ErrorAction Stop
                $results += "$($svc.Name): RESTARTED"
            } catch {
                $results += "$($svc.Name): failed: $($_.Exception.Message)"
            }
        }
        if (-not $results) { $results = @('(no Proton services found)') }
        $results -join "`n"
        """
        rc, out, err = await asyncio.to_thread(_run_ps, script, 30)
        if rc != 0:
            return f"[proton_vpn.restart_service] ERROR\n{err.strip() or out.strip()}"
        # Give the DNS proxy a moment to come back up, then flush local cache.
        await asyncio.sleep(3)
        await asyncio.to_thread(_run_ps, "Clear-DnsClientCache", 5)
        return f"## restart_service\n```\n{out.strip()}\n```"

    async def cycle_adapter(
        self,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Disable then re-enable the ProtonVPN network adapter. Forces
        SmartConnect to pick a (potentially different) exit server, so
        the user gets a fresh public IP — useful for bypassing
        per-IP rate limits or geo restrictions on a stuck endpoint.

        Heavier than restart_service because it drops the tunnel
        completely; only use when restart_service didn't help.

        :return: Adapter cycle log + before/after public IP comparison.
        """
        if not _is_elevated():
            return f"[proton_vpn.cycle_adapter] requires admin. {_ELEVATION_HINT}"
        async with httpx.AsyncClient() as c:
            ip_before, cc_before = await _public_ip(c)
        script = r"""
        $a = Get-NetAdapter -ErrorAction SilentlyContinue |
             Where-Object { $_.InterfaceDescription -match 'Proton' -or $_.Name -match 'Proton' } |
             Sort-Object Name
        if (-not $a) { 'NO_PROTON_ADAPTER'; return }
        foreach ($adapter in $a) {
            try {
                Disable-NetAdapter -Name $adapter.Name -Confirm:$false -ErrorAction Stop
                Start-Sleep -Seconds 2
                Enable-NetAdapter -Name $adapter.Name -Confirm:$false -ErrorAction Stop
                "$($adapter.Name): cycled"
            } catch {
                "$($adapter.Name): failed: $($_.Exception.Message)"
            }
        }
        """
        rc, out, err = await asyncio.to_thread(_run_ps, script, 30)
        if rc != 0:
            return f"[proton_vpn.cycle_adapter] ERROR\n{err.strip() or out.strip()}"
        if "NO_PROTON_ADAPTER" in out:
            return "[proton_vpn.cycle_adapter] no Proton-named network adapter found"
        # Give SmartConnect ~10s to re-establish, then re-probe IP.
        await asyncio.sleep(10)
        async with httpx.AsyncClient() as c:
            ip_after, cc_after = await _public_ip(c)
        changed = (ip_before and ip_after and ip_before != ip_after)
        return (
            f"## cycle_adapter\n```\n{out.strip()}\n```\n"
            f"\n**Before**: {ip_before or '?'} ({cc_before or '?'})"
            f"\n**After** : {ip_after or '?'} ({cc_after or '?'})  "
            f"{'— IP CHANGED ✓' if changed else '— same IP'}"
        )

    # ── auto-recovery hook (called by watchdog + resolver) ──────────────

    async def auto_recover_dns(
        self,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Single-call recovery for the 'local DNS resolver wedged' failure
        mode. Tries Restart-Service first; if DNS still broken, escalates
        to a full adapter cycle. Idempotent + safe to call repeatedly.

        :return: Result + final DNS state (resolved | unresolved).
        """
        host = (self.valves.recovery_test_host or "").strip()
        # Strict validation: the hostname is interpolated into a
        # PowerShell single-quoted string further down. A value like
        # `evil.com'; Remove-Item -Recurse C:\; #` would otherwise break
        # out and run arbitrary PS in a process that may be elevated
        # (this whole tool requires admin). Reject anything that doesn't
        # match RFC-1123 hostname syntax.
        if not _HOSTNAME_RE.match(host):
            return (
                f"[proton_vpn.auto_recover_dns] refusing to probe — "
                f"`recovery_test_host` valve {host!r} is not a valid hostname. "
                f"Set it to something like `cas-bridge.xethub.hf.co`."
            )
        attempts = max(1, int(self.valves.max_recovery_attempts))
        log: list[str] = []

        # Pass the validated host via -Args so PowerShell never sees it
        # as part of the command text. Belt-and-suspenders with the
        # regex check above; either alone would close the injection
        # path, but together they survive a future regex regression.
        probe_script = (
            "param($h) "
            "try { Resolve-DnsName -Name $h -DnsOnly -QuickTimeout -Type A "
            "-ErrorAction Stop | Out-Null; 'OK' } catch { 'FAIL' }"
        )

        async def _dns_ok() -> bool:
            rc, out, _ = await asyncio.to_thread(_run_ps_args, probe_script, [host], 10)
            return "OK" in (out or "")

        if await _dns_ok():
            return f"[proton_vpn.auto_recover_dns] DNS for `{host}` already healthy — no action taken."

        for attempt in range(1, attempts + 1):
            log.append(f"### Attempt {attempt}/{attempts}")
            log.append("- Restart-Service ProtonVPN*")
            await self.restart_service()
            await asyncio.sleep(2)
            if await _dns_ok():
                log.append(f"- ✓ DNS recovered after restart_service")
                return "## auto_recover_dns: SUCCESS\n" + "\n".join(log)
            log.append("- ✗ DNS still broken — escalating to cycle_adapter")
            await self.cycle_adapter()
            await asyncio.sleep(2)
            if await _dns_ok():
                log.append("- ✓ DNS recovered after cycle_adapter")
                return "## auto_recover_dns: SUCCESS\n" + "\n".join(log)
            log.append("- ✗ DNS still broken after both")

        return (
            "## auto_recover_dns: GAVE UP\n"
            + "\n".join(log)
            + f"\n\nDNS for `{host}` still failing after {attempts} attempt(s). "
            "Probable causes: ProtonVPN account issue (re-login in the GUI), "
            "real upstream outage, or a non-Proton DNS misconfiguration."
        )

    # ── helper: rotate IP for rate-limit / geo-block bypass ─────────────

    async def rotate_ip(
        self,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Force the ProtonVPN client to acquire a new exit server (and
        therefore a new public IP). Useful for bypassing IP-based rate
        limits and geo-restrictions on third-party APIs. Disrupts the
        active VPN session for ~10s while the tunnel re-establishes.

        :return: Old IP → new IP comparison + country codes.
        """
        return await self.cycle_adapter()
