"""System tray icon and menu.

Matches the UX of the legacy ``launcher/LocalAIStack.ps1`` tray: open
chat, open admin, open metrics, view logs, restart services, quit.
"""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from gui.api_client import BackendClient


def _log_dir() -> Path:
    return Path(os.environ.get("APPDATA", str(Path.home()))) / "LocalAIStack" / "logs"


def build_tray(app: QApplication, chat_window, client: BackendClient) -> QSystemTrayIcon:
    icon_path = Path(__file__).resolve().parent.parent.parent / "assets" / "icon.ico"
    icon = QIcon(str(icon_path)) if icon_path.exists() else app.windowIcon()
    tray = QSystemTrayIcon(icon, app)
    tray.setToolTip("Local AI Stack")

    menu = QMenu()

    def _open_chat():
        from gui.windows.chat import ChatWindow
        if isinstance(chat_window, ChatWindow):
            chat_window.showNormal()
            chat_window.raise_()
            chat_window.activateWindow()

    def _open_admin():
        from gui.windows.admin import AdminWindow
        w = AdminWindow(client)
        w.show()
        app._admin_win = w  # type: ignore[attr-defined]  # keep a ref

    def _open_metrics():
        from gui.windows.metrics import MetricsWindow
        w = MetricsWindow(client)
        w.show()
        app._metrics_win = w  # type: ignore[attr-defined]

    def _open_logs():
        import subprocess
        logdir = _log_dir()
        logdir.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(logdir))  # type: ignore[attr-defined]  # Windows
        except AttributeError:
            subprocess.Popen(["xdg-open", str(logdir)])

    def _quit():
        app.quit()

    for label, handler in [
        ("Open Chat", _open_chat),
        ("Open Admin", _open_admin),
        ("Open Metrics", _open_metrics),
        ("View Logs", _open_logs),
        (None, None),
        ("Quit", _quit),
    ]:
        if label is None:
            menu.addSeparator()
            continue
        act = QAction(label, menu)
        act.triggered.connect(handler)
        menu.addAction(act)

    tray.setContextMenu(menu)
    tray.activated.connect(lambda reason: _open_chat() if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
    return tray
