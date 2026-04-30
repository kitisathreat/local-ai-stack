"""Area A — Prerequisite probes."""

from __future__ import annotations
import shutil
import subprocess
import pathlib
import re
import sys
import os


def _run(cmd: list[str]) -> tuple[int, str, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return r.returncode, r.stdout, r.stderr
    except FileNotFoundError:
        return 1, "", f"{cmd[0]}: command not found"
    except subprocess.TimeoutExpired:
        return 1, "", f"{cmd[0]}: timed out"


def run() -> list[dict]:
    results = []

    def probe(name: str, fn) -> None:
        try:
            status, detail, fix_hint = fn()
        except Exception as e:
            status, detail, fix_hint = "FAIL", str(e), "Check installation"
        results.append({"area": "A", "test": name, "status": status,
                        "detail": detail, "fix_hint": fix_hint})

    # Python 3.12+
    def _python():
        code, out, err = _run([sys.executable, "--version"])
        ver = (out + err).strip()
        m = re.search(r"(\d+)\.(\d+)", ver)
        if code != 0 or not m:
            return "FAIL", f"python: {ver}", "Run LocalAIStack.ps1 -Setup"
        major, minor = int(m.group(1)), int(m.group(2))
        ok = major > 3 or (major == 3 and minor >= 12)
        return ("PASS" if ok else "FAIL"), ver, "Run LocalAIStack.ps1 -Setup"

    probe("python_312", _python)

    # cloudflared
    def _cloudflared():
        # Check vendor path first, then PATH
        vendor = pathlib.Path(__file__).resolve().parents[3] / "vendor" / "cloudflared" / "cloudflared.exe"
        binary = str(vendor) if vendor.exists() else "cloudflared"
        code, out, err = _run([binary, "--version"])
        ver = (out + err).strip()
        if code != 0:
            return "FAIL", ver or "cloudflared not found", "Run LocalAIStack.ps1 -Setup"
        return "PASS", ver, ""

    probe("cloudflared_installed", _cloudflared)

    # NVIDIA driver
    def _nvidia():
        code, out, err = _run(["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"])
        ver = (out + err).strip()
        if code != 0:
            return "FAIL", "nvidia-smi not found or failed", \
                   "Install NVIDIA driver ≥ 550 from nvidia.com/drivers"
        m = re.search(r"(\d+)", ver)
        if m and int(m.group(1)) >= 550:
            return "PASS", f"driver {ver}", ""
        return "WARN", f"driver {ver} may be too old (need ≥ 550)", \
               "Update driver from nvidia.com/drivers"

    probe("nvidia_driver_550", _nvidia)

    # CUDA 12
    def _cuda():
        code, out, err = _run(["nvidia-smi"])
        combined = (out + err)
        m = re.search(r"CUDA Version:\s*(\d+)\.(\d+)", combined)
        if code != 0 or not m:
            return "WARN", "Could not detect CUDA version", \
                   "Ensure NVIDIA driver is installed"
        major = int(m.group(1))
        return ("PASS" if major >= 12 else "WARN"), \
               f"CUDA {m.group(1)}.{m.group(2)}", \
               "Update driver for CUDA 12+" if major < 12 else ""

    probe("cuda_12", _cuda)

    # Vendored Qdrant binary
    def _qdrant():
        repo_root = pathlib.Path(__file__).resolve().parents[3]
        p = repo_root / "vendor" / "qdrant" / "qdrant.exe"
        if not p.exists():
            return "FAIL", str(p), "Run LocalAIStack.ps1 -Setup"
        return "PASS", str(p), ""

    probe("qdrant_binary", _qdrant)

    # Vendored llama-server binary
    def _llama():
        repo_root = pathlib.Path(__file__).resolve().parents[3]
        p = repo_root / "vendor" / "llama-server" / "llama-server.exe"
        if not p.exists():
            return "FAIL", str(p), "Run LocalAIStack.ps1 -Setup"
        return "PASS", str(p), ""

    probe("llama_server_binary", _llama)

    return results
