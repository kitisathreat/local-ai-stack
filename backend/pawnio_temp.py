"""PawnIO-backed CPU temperature reader (Windows only).

Reads CPU package / Tctl temperature via the PawnIO kernel driver
(C:\\Program Files\\PawnIO\\PawnIOLib.dll). Stock Windows can't read CPU
temp without a third-party tool — PawnIO is the lightest path because
it's just a signed kernel driver + tiny library, no GUI app.

Two paths depending on CPU vendor (cached from cpuinfo at first call):

  * Intel  → IntelMSR.bin → MSR 0x1A2 (TJmax) + MSR 0x19C (digital
    readout below TJmax). T_pkg = TJmax − readout.
  * AMD    → AMDFamily17.bin → SMN 0x00059800. Decode:
        ((raw >> 21) & 0x7FF) * 0.125
    No offset on Zen 4+ desktop SKUs. AMDFamily17 also covers Zen 5
    (Family 1Ah / Granite Ridge etc.) since the SMN address didn't move.

Module-level singleton keeps the driver handle open so each poll is a
single ioctl, not load+ioctl. close() on shutdown is best-effort.

Returns ``None`` if PawnIO isn't installed, the driver isn't running,
the .bin module is missing, or the CPU vendor isn't supported.
"""

from __future__ import annotations

import ctypes
import logging
import os
import platform
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_PAWNIO_DLL_PATH = r"C:\Program Files\PawnIO\PawnIOLib.dll"
_MODULES_DIR = Path(__file__).resolve().parent.parent / "tools" / "pawnio-modules"

_state: dict = {
    "init_attempted": False,
    "ready": False,
    "vendor": None,         # "intel" | "amd" | None
    "handle": None,         # ctypes HANDLE
    "dll": None,
    "tjmax": None,          # cached for Intel
    "lock": threading.Lock(),
}


def _detect_vendor() -> Optional[str]:
    """AMD or Intel based on platform.processor / Win32_Processor."""
    try:
        proc = platform.processor() or ""
        if "Intel" in proc or "GenuineIntel" in proc:
            return "intel"
        if "AMD" in proc or "AuthenticAMD" in proc:
            return "amd"
    except Exception:
        pass
    # Fallback via WMI
    try:
        import subprocess
        out = subprocess.check_output(
            ["powershell.exe", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Processor | Select-Object -First 1).Manufacturer"],
            text=True, timeout=3,
        ).strip()
        if "Intel" in out:
            return "intel"
        if "AMD" in out:
            return "amd"
    except Exception:
        pass
    return None


def _init() -> bool:
    """One-shot: load the DLL, open a handle, load the right .bin module.
    Sets _state.ready to True on success. Idempotent (re-callable cheaply
    after a failure to retry — useful if PawnIO was started after backend)."""
    if _state["ready"]:
        return True
    if os.name != "nt":
        return False
    try:
        if not Path(_PAWNIO_DLL_PATH).exists():
            return False
        dll = ctypes.WinDLL(_PAWNIO_DLL_PATH)
        # Bind signatures
        dll.pawnio_open.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        dll.pawnio_open.restype = ctypes.c_long
        dll.pawnio_load.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t]
        dll.pawnio_load.restype = ctypes.c_long
        dll.pawnio_execute.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_uint64), ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_uint64), ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        dll.pawnio_execute.restype = ctypes.c_long
        dll.pawnio_close.argtypes = [ctypes.c_void_p]
        dll.pawnio_close.restype = ctypes.c_long

        vendor = _detect_vendor()
        if vendor not in ("intel", "amd"):
            logger.info("PawnIO temp: unknown CPU vendor, skipping")
            return False
        module_name = "IntelMSR.bin" if vendor == "intel" else "AMDFamily17.bin"
        blob_path = _MODULES_DIR / module_name
        if not blob_path.exists():
            logger.info("PawnIO temp: module %s not found at %s", module_name, blob_path)
            return False

        handle = ctypes.c_void_p()
        hr = dll.pawnio_open(ctypes.byref(handle))
        if hr != 0 or not handle.value:
            logger.info("PawnIO temp: pawnio_open failed hr=0x%08x", hr & 0xFFFFFFFF)
            return False
        with open(blob_path, "rb") as f:
            blob = f.read()
        hr = dll.pawnio_load(handle, blob, len(blob))
        if hr != 0:
            logger.info("PawnIO temp: pawnio_load(%s) failed hr=0x%08x", module_name, hr & 0xFFFFFFFF)
            dll.pawnio_close(handle)
            return False

        # Intel: cache TJmax once; it's static for the lifetime of the proc.
        tjmax = None
        if vendor == "intel":
            in_buf = (ctypes.c_uint64 * 1)(0x1A2)
            out_buf = (ctypes.c_uint64 * 1)()
            ret_sz = ctypes.c_size_t()
            hr = dll.pawnio_execute(
                handle, b"ioctl_read_msr", in_buf, 1, out_buf, 1, ctypes.byref(ret_sz),
            )
            if hr == 0 and ret_sz.value >= 1:
                tjmax = float((out_buf[0] >> 16) & 0xFF)

        _state["dll"] = dll
        _state["handle"] = handle
        _state["vendor"] = vendor
        _state["tjmax"] = tjmax
        _state["ready"] = True
        logger.info("PawnIO temp: ready (vendor=%s, module=%s, tjmax=%s)",
                    vendor, module_name, tjmax)
        return True
    except Exception as exc:
        logger.info("PawnIO temp: init failed: %s", exc)
        return False


def read_cpu_temp_c() -> Optional[float]:
    """Return current CPU package temperature in °C, or None if unavailable.

    Cheap (~50–200 µs per call once the handle is loaded). Thread-safe
    via a process-wide lock so concurrent admin requests don't race
    on the single PawnIO handle.
    """
    with _state["lock"]:
        if not _state["ready"] and not _state["init_attempted"]:
            _state["init_attempted"] = True
            _init()
        if not _state["ready"]:
            return None
        dll = _state["dll"]
        h = _state["handle"]
        vendor = _state["vendor"]
        try:
            if vendor == "amd":
                in_buf = (ctypes.c_uint64 * 1)(0x00059800)
                out_buf = (ctypes.c_uint64 * 1)()
                ret_sz = ctypes.c_size_t()
                hr = dll.pawnio_execute(
                    h, b"ioctl_read_smn", in_buf, 1, out_buf, 1, ctypes.byref(ret_sz),
                )
                if hr != 0 or ret_sz.value < 1:
                    return None
                raw = out_buf[0]
                # AMD PPR THM_TCON_CUR_TMP layout:
                #   bits 31:21 = CUR_TEMP (11-bit, 0.125 °C/LSB)
                #   bit 20     = CUR_TEMP_RANGE_SEL — when set, the chip
                #                reports in the high-range encoding and
                #                we subtract 49 °C to get true Tctl.
                # Zen 1-4 desktop SKUs typically ship with range_sel=0
                # (raw decode is correct as-is). Zen 5 / Ryzen 9000-series
                # (Family 1Ah / Granite Ridge) ships with range_sel=1, so
                # raw - 49 is the true Tctl. Reading bit 20 makes the
                # decode self-correcting across both gens — no SKU table.
                tctl_raw = ((raw >> 21) & 0x7FF) * 0.125
                range_sel = (raw >> 20) & 0x1
                t = tctl_raw - 49.0 if range_sel else tctl_raw
                if 0.0 <= t <= 150.0:
                    return t
                return None
            elif vendor == "intel":
                in_buf = (ctypes.c_uint64 * 1)(0x19C)
                out_buf = (ctypes.c_uint64 * 1)()
                ret_sz = ctypes.c_size_t()
                hr = dll.pawnio_execute(
                    h, b"ioctl_read_msr", in_buf, 1, out_buf, 1, ctypes.byref(ret_sz),
                )
                if hr != 0 or ret_sz.value < 1:
                    return None
                readout = (out_buf[0] >> 16) & 0x7F
                tjmax = _state["tjmax"]
                if tjmax is None:
                    return None
                return float(tjmax - readout)
        except Exception:
            return None
    return None


def close() -> None:
    """Best-effort handle close on shutdown."""
    with _state["lock"]:
        try:
            if _state["ready"] and _state["dll"] and _state["handle"]:
                _state["dll"].pawnio_close(_state["handle"])
        except Exception:
            pass
        _state["ready"] = False
        _state["handle"] = None
        _state["dll"] = None
