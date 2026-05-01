"""Qt application entry point.

Launched by LocalAIStack.ps1 in one of three modes:

    # default — chat window + tray; used on -Start
    vendor\\venv-gui\\Scripts\\pythonw.exe gui/main.py --api http://127.0.0.1:18000

    # admin window only; used on -Admin
    vendor\\venv-gui\\Scripts\\pythonw.exe gui/main.py --mode admin --api http://127.0.0.1:18000

    # setup wizard; used on -SetupGui (first-run + installer reconfigure)
    vendor\\venv-gui\\Scripts\\pythonw.exe gui/main.py --mode wizard

No browser is opened, ever. All UI is native Qt.
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
from PySide6.QtGui import QIcon
from qasync import QEventLoop

from gui.api_client import BackendClient
from gui.widgets.tray import build_tray
from gui.windows.chat import ChatWindow


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="LocalAIStack GUI")
    p.add_argument(
        "--mode",
        choices=("chat", "admin", "wizard"),
        default="chat",
        help="chat (default, tray + chat window) | admin (login + admin window) | wizard (first-run / reconfigure setup)",
    )
    p.add_argument("--api", default="http://127.0.0.1:18000", help="Backend base URL")
    p.add_argument("--token", default=None, help="Optional pre-authenticated bearer token")
    return p.parse_args()


def _icon_path() -> Path:
    return Path(__file__).resolve().parent.parent / "assets" / "icon.ico"


async def _run_admin_mode(app: QApplication, client: BackendClient) -> int:
    """Show LoginDialog(require_admin=True); on success, open AdminWindow."""
    from gui.windows.login import LoginDialog
    from gui.windows.admin import AdminWindow

    dlg = LoginDialog(client, require_admin=True, title="Admin sign-in")
    if dlg.exec() != QDialog.DialogCode.Accepted:
        app.quit()
        return 0

    window = AdminWindow(client)
    window.show()
    # Keep a strong ref on app so Qt doesn't garbage-collect the window.
    app._admin_window = window  # type: ignore[attr-defined]
    return 0


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

    if args.mode == "admin":
        # Admin-only: no chat window, no tray. Quit on dialog close.
        app.setQuitOnLastWindowClosed(True)
        asyncio.ensure_future(_run_admin_mode(app, client))
    elif args.mode == "wizard":
        # First-run / reconfigure: show the setup wizard, no tray.
        # Spawned by LocalAIStack.ps1 -SetupGui or the installer's
        # -Reconfigure path.
        from gui.windows.setup_wizard import SetupWizard
        app.setQuitOnLastWindowClosed(True)
        wiz = SetupWizard()
        wiz.show()
        # Hold a strong ref so Qt doesn't garbage-collect the window.
        app._setup_wizard = wiz  # type: ignore[attr-defined]
    else:
        chat = ChatWindow(client)
        chat.show()
        tray = build_tray(app, chat, client)
        tray.show()

    signal.signal(signal.SIGINT, lambda *_: app.quit())

    with loop:
        return loop.run_forever()


if __name__ == "__main__":
    sys.exit(main())
