"""Qt application entry point.

Launched by LocalAIStack.ps1 -Start after the backend reports healthy:

    vendor\\venv-gui\\Scripts\\pythonw.exe gui/main.py --api http://127.0.0.1:18000

No browser is opened, ever. All UI is native Qt.
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QIcon
from qasync import QEventLoop

from gui.api_client import BackendClient
from gui.widgets.tray import build_tray
from gui.windows.chat import ChatWindow


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="LocalAIStack GUI")
    p.add_argument("--api", default="http://127.0.0.1:18000", help="Backend base URL")
    p.add_argument("--token", default=None, help="Optional pre-authenticated bearer token")
    return p.parse_args()


def _icon_path() -> Path:
    return Path(__file__).resolve().parent.parent / "assets" / "icon.ico"


def main() -> int:
    args = _parse_args()

    app = QApplication(sys.argv)
    app.setApplicationName("LocalAIStack")
    app.setQuitOnLastWindowClosed(False)        # tray keeps the app alive
    icon_path = _icon_path()
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)

    client = BackendClient(base_url=args.api, token=args.token)

    chat = ChatWindow(client)
    chat.show()

    tray = build_tray(app, chat, client)
    tray.show()

    # Graceful SIGINT from the launcher stopping us.
    signal.signal(signal.SIGINT, lambda *_: app.quit())

    with loop:
        return loop.run_forever()


if __name__ == "__main__":
    sys.exit(main())
