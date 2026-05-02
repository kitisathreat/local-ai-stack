"""Desktop chat window — embeds the browser chat UI in a native frame.

Loads the same ``backend/static/chat.html`` page that ``chat.mylensandi.com``
serves, so users on the backend host get a pixel-equivalent experience
without opening a browser. Independent of airgap mode: the host gate
already permits ``/static/`` and the chat API endpoints from loopback,
so this works whether airgap is on or off.

Spawned from:
    * the admin window's "View" menu  ("Open Desktop Chat")
    * ``gui/main.py --mode desktop-chat``
    * ``LocalAIStack.ps1 -DesktopChat``
"""
from __future__ import annotations

from PySide6.QtCore import QUrl
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QMainWindow, QMessageBox


def _resolve_chat_url(api_base: str) -> str:
    base = api_base.rstrip("/")
    return f"{base}/static/chat.html"


class DesktopChatWindow(QMainWindow):
    """QMainWindow hosting a QWebEngineView pointed at /static/chat.html."""

    def __init__(self, api_base: str = "http://127.0.0.1:18000"):
        super().__init__()
        self.setWindowTitle("Local AI Stack — Chat")
        self.resize(1100, 780)
        self._api_base = api_base.rstrip("/")

        try:
            from PySide6.QtWebEngineWidgets import QWebEngineView
        except ImportError as exc:
            raise RuntimeError(
                "QtWebEngine is not installed. Install with "
                "`pip install PySide6-Addons` or reinstall the GUI venv."
            ) from exc

        self._view = QWebEngineView(self)
        self._view.load(QUrl(_resolve_chat_url(self._api_base)))
        self.setCentralWidget(self._view)

        # Reload on F5 / Ctrl+R, like a browser.
        QShortcut(QKeySequence("F5"), self, self._view.reload)
        QShortcut(QKeySequence("Ctrl+R"), self, self._view.reload)

    @staticmethod
    def show_unavailable(parent=None) -> None:
        QMessageBox.warning(
            parent,
            "Desktop chat unavailable",
            "QtWebEngine is not installed in this GUI venv.\n\n"
            "Reinstall dependencies with:\n"
            "    vendor\\venv-gui\\Scripts\\pip install -r gui\\requirements.txt",
        )
