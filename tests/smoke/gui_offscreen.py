"""Offscreen construction smoke for the PySide6 GUI.

Invoked from the pre-deploy-smoke workflow on a fresh Windows runner.
Builds a QApplication + ChatWindow + (optional) tray under
QT_QPA_PLATFORM=offscreen, processes events for ~2 s, and exits 0.

We are *not* trying to render anything — only proving:
  * gui/requirements.txt is installable on a clean Windows box
  * the import graph (gui.api_client, gui.windows.chat, gui.widgets.tray)
    is sound
  * ChatWindow constructs without raising

Backend reachability is irrelevant here; BackendClient is given a base URL
but no requests are issued during construction.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Make `import gui...` work regardless of cwd.
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from gui.api_client import BackendClient
from gui.widgets.tray import build_tray
from gui.windows.chat import ChatWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    client = BackendClient(base_url="http://127.0.0.1:18000")
    chat = ChatWindow(client)
    chat.show()

    try:
        tray = build_tray(app, chat, client)
        tray.show()
    except Exception as exc:
        print(f"[gui-smoke] tray unavailable (non-fatal): {exc}")

    QTimer.singleShot(2000, app.quit)
    rc = app.exec()
    print(f"[gui-smoke] exec returned {rc}")
    return 0 if rc == 0 else rc


if __name__ == "__main__":
    sys.exit(main())
