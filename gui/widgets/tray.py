"""System tray icon and menu.

Matches the UX of the legacy ``launcher/LocalAIStack.ps1`` tray: open
chat, open admin, open metrics, view logs, restart services, quit.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from gui.api_client import BackendClient


AIRGAP_POLL_MS = 5000


def _log_dir() -> Path:
    return Path(os.environ.get("APPDATA", str(Path.home()))) / "LocalAIStack" / "logs"


def build_tray(app: QApplication, chat_window, client: BackendClient) -> QSystemTrayIcon:
    assets_dir = Path(__file__).resolve().parent.parent.parent / "assets"
    base_icon_path = assets_dir / "icon.ico"
    airgap_icon_path = assets_dir / "icon-airgap.ico"
    base_icon = QIcon(str(base_icon_path)) if base_icon_path.exists() else app.windowIcon()
    airgap_icon = QIcon(str(airgap_icon_path)) if airgap_icon_path.exists() else base_icon
    tray = QSystemTrayIcon(base_icon, app)
    tray.setToolTip("Local AI Stack — airgap OFF (chat at chat.mylensandi.com)")

    menu = QMenu()

    def _open_chat():
        from gui.windows.chat import ChatWindow
        if isinstance(chat_window, ChatWindow):
            chat_window.showNormal()
            chat_window.raise_()
            chat_window.activateWindow()

    def _open_admin():
        # Admin is a separate Qt process so it can enforce its own login
        # dialog without conflicting with the chat session cookies.
        import subprocess, sys as _sys
        from pathlib import Path
        gui_main = Path(__file__).resolve().parent.parent / "main.py"
        pythonw = Path(_sys.executable).with_name("pythonw.exe")
        exe = str(pythonw) if pythonw.exists() else _sys.executable
        try:
            subprocess.Popen([exe, str(gui_main), "--mode", "admin", "--api",
                              client._base if hasattr(client, "_base") else "http://127.0.0.1:18000"])
        except Exception:
            pass

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

    # ── Airgap poll for icon/tooltip swap ─────────────────────────────
    async def _poll_once():
        try:
            state = await client.airgap_state()
            enabled = bool(state.get("enabled"))
        except Exception:
            return
        if enabled:
            tray.setIcon(airgap_icon)
            tray.setToolTip("Local AI Stack — airgap ON (local chat enabled)")
        else:
            tray.setIcon(base_icon)
            tray.setToolTip("Local AI Stack — airgap OFF (chat at chat.mylensandi.com)")

    poll_timer = QTimer(tray)
    poll_timer.setInterval(AIRGAP_POLL_MS)
    poll_timer.timeout.connect(lambda: asyncio.ensure_future(_poll_once()))
    poll_timer.start()
    asyncio.ensure_future(_poll_once())

    return tray
