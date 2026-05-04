"""
title: Windows-MCP — Native Windows Automation
author: local-ai-stack
description: Native Windows control surface — list and focus open windows, send keystrokes / mouse clicks, run PowerShell commands, list services / processes, manage scheduled tasks, query the registry. Mirrors the Claude `Windows-MCP` connector. Pure Win32 API + PowerShell — no extra binaries needed.
required_open_webui_version: 0.4.0
requirements: ""
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

import asyncio
import platform
import shlex
import sys
from typing import Any

from pydantic import BaseModel, Field


def _is_windows() -> bool:
    return sys.platform == "win32" or platform.system() == "Windows"


class Tools:
    class Valves(BaseModel):
        ALLOW_ARBITRARY_POWERSHELL: bool = Field(
            default=False,
            description=(
                "Gate the powershell() escape hatch. When OFF, only the "
                "structured methods on this tool can run. Flip ON only when "
                "the user trusts the model with full PowerShell access on the host."
            ),
        )
        ALLOW_REGISTRY_WRITE: bool = Field(
            default=False,
            description="Gate registry_set / registry_delete. Off by default.",
        )
        ALLOW_SCHEDULED_TASK_WRITE: bool = Field(
            default=False,
            description="Gate task_create / task_delete. Off by default.",
        )
        TIMEOUT_SEC: int = Field(default=60, description="Per-command timeout.")

    def __init__(self) -> None:
        self.valves = self.Valves()

    def _check_platform(self) -> None:
        if not _is_windows():
            raise RuntimeError("windows_mcp can only run on Windows hosts.")

    async def _ps(self, script: str, *, timeout: int | None = None) -> str:
        self._check_platform()
        proc = await asyncio.create_subprocess_exec(
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy", "Bypass",
            "-Command", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout or self.valves.TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(f"powershell timed out: {script[:120]}")
        if proc.returncode != 0:
            raise RuntimeError(
                f"powershell rc={proc.returncode}: "
                f"{(stderr or b'').decode('utf-8', errors='replace')[:500]}"
            )
        return (stdout or b"").decode("utf-8", errors="replace")

    # ── Windows / focus ────────────────────────────────────────────────────

    async def list_windows(self) -> str:
        """List visible top-level windows with title and PID."""
        out = await self._ps(
            "Get-Process | Where-Object {$_.MainWindowTitle -ne ''} | "
            "Select-Object Id,ProcessName,MainWindowTitle | Format-Table -AutoSize | Out-String"
        )
        return out.strip() or "No windows."

    async def focus_window(self, title_substring: str) -> str:
        """Bring the first window whose title contains ``title_substring`` to
        the foreground.

        :param title_substring: Case-insensitive substring of the target title.
        """
        safe = title_substring.replace('"', '`"')
        script = (
            "$sig = '[DllImport(\"user32.dll\")] public static extern bool SetForegroundWindow(IntPtr hWnd);"
            " [DllImport(\"user32.dll\")] public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);';"
            "$type = Add-Type -MemberDefinition $sig -Namespace LAI -Name Win32 -PassThru;"
            f"$p = Get-Process | Where-Object {{ $_.MainWindowTitle -like '*{safe}*' }} | Select-Object -First 1;"
            "if (-not $p) { 'NOT_FOUND' } else { "
            "  [void]$type::ShowWindowAsync($p.MainWindowHandle, 9);"
            "  [void]$type::SetForegroundWindow($p.MainWindowHandle);"
            "  $p.MainWindowTitle"
            "}"
        )
        out = (await self._ps(script)).strip()
        if out == "NOT_FOUND":
            return f"No window matched {title_substring!r}."
        return f"Focused: {out}"

    async def send_keys(self, keys: str) -> str:
        """Send keystrokes to the foreground window. Uses .NET SendKeys
        syntax — `^c` = Ctrl+C, `%{F4}` = Alt+F4, `{ENTER}` etc.

        :param keys: SendKeys-format string.
        """
        safe = keys.replace('"', '`"')
        await self._ps(
            'Add-Type -AssemblyName System.Windows.Forms;'
            f'[System.Windows.Forms.SendKeys]::SendWait("{safe}")'
        )
        return f"Sent {len(keys)} keys."

    async def click(self, x: int, y: int, button: str = "left") -> str:
        """Synthesize a mouse click at screen coordinates.

        :param x: X pixel.
        :param y: Y pixel.
        :param button: "left" | "right" | "middle".
        """
        if button not in {"left", "right", "middle"}:
            raise ValueError("button must be left/right/middle.")
        flags = {
            "left": (0x0002, 0x0004),
            "right": (0x0008, 0x0010),
            "middle": (0x0020, 0x0040),
        }[button]
        script = (
            "$sig = '[DllImport(\"user32.dll\")] public static extern bool SetCursorPos(int X, int Y);"
            " [DllImport(\"user32.dll\")] public static extern void mouse_event(int dwFlags, int dx, int dy, int dwData, int dwExtraInfo);';"
            "$t = Add-Type -MemberDefinition $sig -Namespace LAI -Name Mouse -PassThru;"
            f"[void]$t::SetCursorPos({int(x)}, {int(y)});"
            f"$t::mouse_event({flags[0]}, 0, 0, 0, 0);"
            f"$t::mouse_event({flags[1]}, 0, 0, 0, 0)"
        )
        await self._ps(script)
        return f"Clicked {button} at ({x}, {y})."

    # ── Processes / services ──────────────────────────────────────────────

    async def list_processes(self, name_filter: str = "") -> str:
        """List running processes.

        :param name_filter: Optional case-insensitive substring of the name.
        """
        script = "Get-Process | Sort-Object -Property WS -Descending | "
        if name_filter:
            safe = name_filter.replace("'", "''")
            script += f"Where-Object {{ $_.ProcessName -like '*{safe}*' }} | "
        script += "Select-Object -First 40 Id,ProcessName,CPU,WS | Format-Table -AutoSize | Out-String"
        return (await self._ps(script)).strip() or "No matches."

    async def list_services(self, status_filter: str = "") -> str:
        """List Windows services.

        :param status_filter: "Running" | "Stopped" | "" for all.
        """
        script = "Get-Service"
        if status_filter:
            safe = status_filter.replace("'", "''")
            script += f" | Where-Object {{ $_.Status -eq '{safe}' }}"
        script += " | Sort-Object Status,Name | Format-Table -AutoSize Name,Status,StartType,DisplayName | Out-String"
        return (await self._ps(script)).strip() or "No services."

    # ── Scheduled tasks ────────────────────────────────────────────────────

    async def list_scheduled_tasks(self, name_filter: str = "") -> str:
        """List scheduled tasks.

        :param name_filter: Optional substring filter on task name.
        """
        script = "Get-ScheduledTask"
        if name_filter:
            safe = name_filter.replace("'", "''")
            script += f" | Where-Object {{ $_.TaskName -like '*{safe}*' }}"
        script += " | Select-Object TaskName,State,TaskPath | Format-Table -AutoSize | Out-String"
        return (await self._ps(script)).strip() or "No tasks."

    async def task_create(
        self,
        name: str,
        command: str,
        argument: str = "",
        trigger: str = "onlogon",
    ) -> str:
        """Create a scheduled task.

        :param name: Task name.
        :param command: Executable path.
        :param argument: Optional argument string.
        :param trigger: "onlogon" | "atstartup" | "daily" | "hourly".
        """
        if not self.valves.ALLOW_SCHEDULED_TASK_WRITE:
            raise PermissionError("ALLOW_SCHEDULED_TASK_WRITE is OFF.")
        if trigger not in {"onlogon", "atstartup", "daily", "hourly"}:
            raise ValueError("trigger must be onlogon/atstartup/daily/hourly.")
        trigger_cmd = {
            "onlogon": "New-ScheduledTaskTrigger -AtLogOn",
            "atstartup": "New-ScheduledTaskTrigger -AtStartup",
            "daily": "New-ScheduledTaskTrigger -Daily -At 9am",
            "hourly": "New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Hours 1)",
        }[trigger]
        safe_name = name.replace("'", "''")
        safe_cmd = command.replace("'", "''")
        safe_arg = argument.replace("'", "''")
        script = (
            f"$action = New-ScheduledTaskAction -Execute '{safe_cmd}'"
            + (f" -Argument '{safe_arg}'" if argument else "") + ";"
            f"$trigger = {trigger_cmd};"
            f"Register-ScheduledTask -TaskName '{safe_name}' -Action $action -Trigger $trigger -Force | Out-Null;"
            f"'OK'"
        )
        await self._ps(script)
        return f"Created scheduled task {name}."

    async def task_delete(self, name: str) -> str:
        """Delete a scheduled task.

        :param name: Task name.
        """
        if not self.valves.ALLOW_SCHEDULED_TASK_WRITE:
            raise PermissionError("ALLOW_SCHEDULED_TASK_WRITE is OFF.")
        safe = name.replace("'", "''")
        await self._ps(f"Unregister-ScheduledTask -TaskName '{safe}' -Confirm:$false")
        return f"Deleted scheduled task {name}."

    # ── Registry ───────────────────────────────────────────────────────────

    async def registry_get(self, path: str, name: str = "") -> str:
        """Read a registry value (or list a key).

        :param path: Registry path (e.g. "HKCU:\\Software\\Foo").
        :param name: Optional value name; empty lists all values under the key.
        """
        safe_path = path.replace("'", "''")
        if name:
            safe_name = name.replace("'", "''")
            return (await self._ps(
                f"(Get-ItemProperty -Path '{safe_path}' -Name '{safe_name}').{safe_name}"
            )).strip()
        return (await self._ps(f"Get-ItemProperty -Path '{safe_path}' | Out-String")).strip()

    async def registry_set(
        self,
        path: str,
        name: str,
        value: str,
        type: str = "String",
    ) -> str:
        """Write a registry value.

        :param path: Registry path.
        :param name: Value name.
        :param value: Stringified value.
        :param type: "String" | "DWord" | "QWord" | "Binary" | "ExpandString" | "MultiString".
        """
        if not self.valves.ALLOW_REGISTRY_WRITE:
            raise PermissionError("ALLOW_REGISTRY_WRITE is OFF.")
        if type not in {"String", "DWord", "QWord", "Binary", "ExpandString", "MultiString"}:
            raise ValueError("Unsupported registry type.")
        safe_path = path.replace("'", "''")
        safe_name = name.replace("'", "''")
        safe_value = value.replace("'", "''")
        await self._ps(
            f"if (-not (Test-Path '{safe_path}')) {{ New-Item -Path '{safe_path}' -Force | Out-Null }};"
            f"Set-ItemProperty -Path '{safe_path}' -Name '{safe_name}' -Value '{safe_value}' -Type {type}"
        )
        return f"Set {path}\\{name}."

    # ── Files / clipboard ─────────────────────────────────────────────────

    async def get_clipboard(self) -> str:
        """Read the current text clipboard content."""
        return (await self._ps("Get-Clipboard -Raw")).rstrip("\r\n")

    async def set_clipboard(self, text: str) -> str:
        """Set the text clipboard.

        :param text: Plain text.
        """
        safe = text.replace("'", "''")
        await self._ps(f"Set-Clipboard -Value '{safe}'")
        return f"Clipboard set ({len(text)} chars)."

    # ── PowerShell escape hatch ───────────────────────────────────────────

    async def powershell(self, command: str) -> str:
        """Run an arbitrary PowerShell command. Disabled by default — flip
        ALLOW_ARBITRARY_POWERSHELL on Valves first.

        :param command: PowerShell command line.
        """
        if not self.valves.ALLOW_ARBITRARY_POWERSHELL:
            raise PermissionError("ALLOW_ARBITRARY_POWERSHELL is OFF.")
        return (await self._ps(command)).rstrip()
