"""Area E — GUI / wizard probes."""

from __future__ import annotations
import pathlib
import subprocess
import sys


def run() -> list[dict]:
    results = []
    repo = pathlib.Path(__file__).resolve().parents[3]

    def probe(name: str, fn) -> None:
        try:
            status, detail, fix_hint = fn()
        except Exception as e:
            status, detail, fix_hint = "FAIL", str(e), "Run LocalAIStack.ps1 -Setup"
        results.append({"area": "E", "test": name, "status": status,
                        "detail": detail, "fix_hint": fix_hint})

    venv_gui = repo / "vendor" / "venv-gui" / "Scripts"

    # venv-gui intact
    def _venv():
        pythonw = venv_gui / "pythonw.exe"
        if pythonw.exists():
            return "PASS", str(pythonw), ""
        return "FAIL", f"Not found: {pythonw}", "Run LocalAIStack.ps1 -Setup"

    probe("gui_venv_intact", _venv)

    # PySide6 importable
    def _pyside6():
        pythonw = venv_gui / "pythonw.exe"
        python = venv_gui / "python.exe"
        interpreter = python if python.exists() else (pythonw if pythonw.exists() else None)
        if interpreter is None:
            return "FAIL", "GUI venv not found", "Run LocalAIStack.ps1 -Setup"
        r = subprocess.run(
            [str(interpreter), "-c", "import PySide6; print(PySide6.__version__)"],
            capture_output=True, text=True, timeout=30, cwd=str(repo)
        )
        if r.returncode == 0:
            return "PASS", f"PySide6 {r.stdout.strip()}", ""
        return "FAIL", r.stderr.strip()[-300:], "Run LocalAIStack.ps1 -Setup"

    probe("pyside6_importable", _pyside6)

    # Setup wizard importable
    def _wizard():
        python = venv_gui / "python.exe"
        if not python.exists():
            return "WARN", "GUI venv not found — skipping", "Run LocalAIStack.ps1 -Setup"
        r = subprocess.run(
            [str(python), "-c",
             "from gui.windows.setup_wizard import SetupWizard; print('ok')"],
            capture_output=True, text=True, timeout=30, cwd=str(repo)
        )
        if r.returncode == 0:
            return "PASS", "SetupWizard importable", ""
        if "No module named" in r.stderr:
            return "WARN", "setup_wizard not yet implemented", "Pending Phase 2"
        return "FAIL", r.stderr.strip()[-300:], "Check GUI venv"

    probe("setup_wizard_importable", _wizard)

    # Chat window importable
    def _chat():
        python = venv_gui / "python.exe"
        if not python.exists():
            return "WARN", "GUI venv not found — skipping", "Run LocalAIStack.ps1 -Setup"
        r = subprocess.run(
            [str(python), "-c",
             "from gui.windows.chat import ChatWindow; print('ok')"],
            capture_output=True, text=True, timeout=30, cwd=str(repo)
        )
        if r.returncode == 0:
            return "PASS", "ChatWindow importable", ""
        return "FAIL", r.stderr.strip()[-300:], "Check GUI venv"

    probe("chat_window_importable", _chat)

    return results
