"""
title: Android-MCP — adb Bridge for Connected Android Devices
author: local-ai-stack
description: Drive a connected Android phone or emulator through `adb`. List devices, install / uninstall APKs, send keyevents, dump UI hierarchy, take screenshots, push / pull files, run shell commands. Mirrors the Claude `Android-MCP` connector. Requires Android Platform Tools on PATH (https://developer.android.com/tools/releases/platform-tools).
required_open_webui_version: 0.4.0
requirements: ""
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

import asyncio
import os
import shlex
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


_ALLOWED_KEYEVENTS = {
    "HOME", "BACK", "MENU", "POWER", "VOLUME_UP", "VOLUME_DOWN", "VOLUME_MUTE",
    "ENTER", "ESCAPE", "TAB", "DEL", "FORWARD_DEL", "CAPS_LOCK",
    "DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT", "DPAD_CENTER",
    "APP_SWITCH", "RECENT_APPS", "ASSIST", "WAKEUP", "SLEEP",
    "MEDIA_PLAY_PAUSE", "MEDIA_NEXT", "MEDIA_PREVIOUS", "MEDIA_STOP",
    "BRIGHTNESS_UP", "BRIGHTNESS_DOWN", "NOTIFICATION", "SETTINGS",
}


class Tools:
    class Valves(BaseModel):
        ADB_PATH: str = Field(
            default="adb",
            description=(
                "Path to the adb binary. Default 'adb' resolves via PATH. "
                "Install via Android SDK Platform Tools: "
                "https://developer.android.com/tools/releases/platform-tools."
            ),
        )
        DEFAULT_SERIAL: str = Field(
            default="",
            description=(
                "Default device serial (from `adb devices`). When unset and "
                "more than one device is connected, callers must pass `serial`."
            ),
        )
        ALLOW_ARBITRARY_SHELL: bool = Field(
            default=False,
            description=(
                "When false, the `shell` method refuses unknown commands. "
                "When true, anything is forwarded to `adb shell` — keep this "
                "OFF unless you trust the model with full root-equivalent access."
            ),
        )
        ALLOW_INSTALL_UNINSTALL: bool = Field(
            default=False,
            description="Gate install_apk / uninstall_app. Off by default.",
        )
        TIMEOUT_SEC: int = Field(default=30, description="Per-command timeout.")

    def __init__(self) -> None:
        self.valves = self.Valves()

    def _serial_args(self, serial: str | None) -> list[str]:
        s = (serial or self.valves.DEFAULT_SERIAL).strip()
        return ["-s", s] if s else []

    async def _run(self, args: list[str], *, capture: bool = True, timeout: int | None = None) -> dict[str, Any]:
        cmd = [self.valves.ADB_PATH, *args]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE if capture else None,
            stderr=asyncio.subprocess.PIPE if capture else None,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout or self.valves.TIMEOUT_SEC)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(f"adb timed out: {' '.join(shlex.quote(c) for c in cmd)}")
        return {
            "rc": proc.returncode,
            "stdout": (stdout or b"").decode("utf-8", errors="replace"),
            "stderr": (stderr or b"").decode("utf-8", errors="replace"),
        }

    # ── Devices ────────────────────────────────────────────────────────────

    async def list_devices(self) -> str:
        """List connected devices and emulators."""
        out = await self._run(["devices", "-l"])
        if out["rc"] != 0:
            raise RuntimeError(out["stderr"] or "adb devices failed")
        return out["stdout"].strip() or "No devices."

    async def device_info(self, serial: str = "") -> str:
        """Read brand, model, OS version, and battery level.

        :param serial: Device serial; defaults to DEFAULT_SERIAL.
        """
        rows = []
        for prop in ("ro.product.brand", "ro.product.model", "ro.build.version.release"):
            out = await self._run(self._serial_args(serial) + ["shell", "getprop", prop])
            rows.append(f"{prop} = {out['stdout'].strip()}")
        bat = await self._run(self._serial_args(serial) + ["shell", "dumpsys", "battery"])
        for line in bat["stdout"].splitlines():
            if "level:" in line or "status:" in line:
                rows.append(line.strip())
        return "\n".join(rows)

    # ── UI control ─────────────────────────────────────────────────────────

    async def keyevent(self, key: str, serial: str = "") -> str:
        """Send a keyevent. Restricted to a safe allow-list.

        :param key: Keyevent name (e.g. HOME, BACK, ENTER).
        :param serial: Device serial.
        """
        k = key.upper().replace("KEYCODE_", "")
        if k not in _ALLOWED_KEYEVENTS:
            raise PermissionError(f"keyevent {k!r} not in allow-list ({sorted(_ALLOWED_KEYEVENTS)}).")
        out = await self._run(self._serial_args(serial) + ["shell", "input", "keyevent", k])
        if out["rc"] != 0:
            raise RuntimeError(out["stderr"])
        return f"Sent keyevent {k}."

    async def tap(self, x: int, y: int, serial: str = "") -> str:
        """Tap at screen coordinates.

        :param x: X pixel.
        :param y: Y pixel.
        :param serial: Device serial.
        """
        out = await self._run(self._serial_args(serial) + ["shell", "input", "tap", str(int(x)), str(int(y))])
        if out["rc"] != 0:
            raise RuntimeError(out["stderr"])
        return f"Tapped ({x},{y})."

    async def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 250, serial: str = "") -> str:
        """Swipe / drag between two points.

        :param x1: Start X.
        :param y1: Start Y.
        :param x2: End X.
        :param y2: End Y.
        :param duration_ms: Duration in ms.
        :param serial: Device serial.
        """
        out = await self._run(
            self._serial_args(serial)
            + ["shell", "input", "swipe", str(int(x1)), str(int(y1)), str(int(x2)), str(int(y2)), str(int(duration_ms))],
        )
        if out["rc"] != 0:
            raise RuntimeError(out["stderr"])
        return f"Swiped ({x1},{y1}) → ({x2},{y2}) over {duration_ms}ms."

    async def type_text(self, text: str, serial: str = "") -> str:
        """Type text into the focused field.

        :param text: Text to type. Spaces are escaped to %s for adb.
        :param serial: Device serial.
        """
        # adb's `input text` doesn't take spaces directly — escape to %s.
        encoded = text.replace(" ", "%s")
        out = await self._run(self._serial_args(serial) + ["shell", "input", "text", encoded])
        if out["rc"] != 0:
            raise RuntimeError(out["stderr"])
        return f"Typed {len(text)} chars."

    async def dump_ui(self, serial: str = "") -> str:
        """Dump the current UI hierarchy as XML (uiautomator dump).

        :param serial: Device serial.
        """
        await self._run(self._serial_args(serial) + ["shell", "uiautomator", "dump", "/sdcard/ui.xml"])
        out = await self._run(self._serial_args(serial) + ["shell", "cat", "/sdcard/ui.xml"])
        if out["rc"] != 0:
            raise RuntimeError(out["stderr"])
        return out["stdout"]

    async def screenshot(self, save_to: str, serial: str = "") -> str:
        """Capture a screenshot and pull it to the host filesystem.

        :param save_to: Local path to write the PNG.
        :param serial: Device serial.
        """
        local = Path(save_to).expanduser().resolve()
        local.parent.mkdir(parents=True, exist_ok=True)
        await self._run(self._serial_args(serial) + ["shell", "screencap", "-p", "/sdcard/_lai_shot.png"])
        out = await self._run(self._serial_args(serial) + ["pull", "/sdcard/_lai_shot.png", str(local)])
        if out["rc"] != 0:
            raise RuntimeError(out["stderr"])
        return f"Saved {local}"

    # ── Apps ───────────────────────────────────────────────────────────────

    async def list_packages(self, filter_substring: str = "", serial: str = "") -> str:
        """List installed packages.

        :param filter_substring: Optional substring filter.
        :param serial: Device serial.
        """
        args = self._serial_args(serial) + ["shell", "pm", "list", "packages"]
        out = await self._run(args)
        if out["rc"] != 0:
            raise RuntimeError(out["stderr"])
        rows = [l.replace("package:", "").strip() for l in out["stdout"].splitlines()]
        if filter_substring:
            rows = [r for r in rows if filter_substring.lower() in r.lower()]
        return "\n".join(rows) or "No packages."

    async def launch_app(self, package: str, activity: str = "", serial: str = "") -> str:
        """Launch an app by package name.

        :param package: e.g. "com.android.chrome".
        :param activity: Optional activity (defaults to the launcher activity).
        :param serial: Device serial.
        """
        if activity:
            target = f"{package}/{activity}"
            args = self._serial_args(serial) + ["shell", "am", "start", "-n", target]
        else:
            args = self._serial_args(serial) + ["shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"]
        out = await self._run(args)
        if out["rc"] != 0:
            raise RuntimeError(out["stderr"])
        return f"Launched {package}."

    async def install_apk(self, apk_path: str, replace: bool = True, serial: str = "") -> str:
        """Install an APK from the host filesystem.

        :param apk_path: Local path to the .apk.
        :param replace: True to upgrade an existing installation in place.
        :param serial: Device serial.
        """
        if not self.valves.ALLOW_INSTALL_UNINSTALL:
            raise PermissionError("Install gated — flip ALLOW_INSTALL_UNINSTALL on the tool's Valves.")
        local = Path(apk_path).expanduser().resolve()
        if not local.exists():
            raise FileNotFoundError(str(local))
        args = self._serial_args(serial) + ["install"] + (["-r"] if replace else []) + [str(local)]
        out = await self._run(args, timeout=180)
        if out["rc"] != 0:
            raise RuntimeError(out["stderr"] or out["stdout"])
        return out["stdout"].strip()

    async def uninstall_app(self, package: str, serial: str = "") -> str:
        """Uninstall a package.

        :param package: Package id.
        :param serial: Device serial.
        """
        if not self.valves.ALLOW_INSTALL_UNINSTALL:
            raise PermissionError("Uninstall gated — flip ALLOW_INSTALL_UNINSTALL on the tool's Valves.")
        out = await self._run(self._serial_args(serial) + ["uninstall", package])
        if out["rc"] != 0:
            raise RuntimeError(out["stderr"] or out["stdout"])
        return out["stdout"].strip()

    # ── Files ──────────────────────────────────────────────────────────────

    async def push_file(self, local_path: str, device_path: str, serial: str = "") -> str:
        """Push a file from the host to the device.

        :param local_path: Host path.
        :param device_path: Device path (e.g. /sdcard/Download/foo.txt).
        :param serial: Device serial.
        """
        local = Path(local_path).expanduser().resolve()
        if not local.exists():
            raise FileNotFoundError(str(local))
        out = await self._run(self._serial_args(serial) + ["push", str(local), device_path])
        if out["rc"] != 0:
            raise RuntimeError(out["stderr"])
        return out["stdout"].strip() or f"Pushed {local} → {device_path}"

    async def pull_file(self, device_path: str, local_path: str, serial: str = "") -> str:
        """Pull a file from the device to the host.

        :param device_path: Device path.
        :param local_path: Host destination.
        :param serial: Device serial.
        """
        local = Path(local_path).expanduser().resolve()
        local.parent.mkdir(parents=True, exist_ok=True)
        out = await self._run(self._serial_args(serial) + ["pull", device_path, str(local)])
        if out["rc"] != 0:
            raise RuntimeError(out["stderr"])
        return out["stdout"].strip() or f"Pulled {device_path} → {local}"

    # ── Shell escape hatch ────────────────────────────────────────────────

    async def shell(self, command: str, serial: str = "") -> str:
        """Run an arbitrary `adb shell` command. Disabled by default; flip
        ALLOW_ARBITRARY_SHELL on Valves to enable.

        :param command: Shell line (no surrounding quotes; tokens are split).
        :param serial: Device serial.
        """
        if not self.valves.ALLOW_ARBITRARY_SHELL:
            raise PermissionError("ALLOW_ARBITRARY_SHELL is OFF — explicit opt-in required.")
        tokens = shlex.split(command)
        out = await self._run(self._serial_args(serial) + ["shell", *tokens])
        if out["rc"] != 0:
            raise RuntimeError(out["stderr"] or out["stdout"])
        return out["stdout"].rstrip()
