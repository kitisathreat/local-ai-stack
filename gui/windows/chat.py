"""Main chat window.

Layout:
    ┌────────────────────────────────┐
    │ [model ▼]      [Think] [Clear] │
    ├────────────────────────────────┤
    │                                │
    │  Conversation (MarkdownView)   │
    │                                │
    ├────────────────────────────────┤
    │  [Composer (QPlainTextEdit)]   │
    │                      [Send]    │
    └────────────────────────────────┘
"""

from __future__ import annotations

import asyncio

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QMainWindow, QPlainTextEdit,
    QPushButton, QVBoxLayout, QWidget,
)

from gui.api_client import BackendClient, ChatTurn
from gui.widgets.markdown_view import MarkdownView


class ChatWindow(QMainWindow):
    def __init__(self, client: BackendClient):
        super().__init__()
        self.setWindowTitle("Local AI Stack — Chat")
        self.resize(1000, 720)
        self._client = client
        self._history: list[ChatTurn] = []

        # ── Toolbar ───────────────────────────────────────────────
        self._model = QComboBox()
        self._model.setMinimumWidth(260)
        self._think = QCheckBox("Think")
        clear = QPushButton("Clear")
        clear.clicked.connect(self._clear)

        top = QHBoxLayout()
        top.addWidget(self._model, 1)
        top.addWidget(self._think)
        top.addWidget(clear)

        # ── Conversation view ─────────────────────────────────────
        self._view = MarkdownView()

        # ── Composer ──────────────────────────────────────────────
        self._composer = QPlainTextEdit()
        self._composer.setPlaceholderText("Type a message. Ctrl+Enter to send.")
        self._composer.setFixedHeight(120)
        self._send_button = QPushButton("Send")
        self._send_button.clicked.connect(self._send_clicked)
        self._send_shortcut = QShortcut(QKeySequence("Ctrl+Return"), self._composer, self._send_clicked)
        self._streaming = False

        bottom = QHBoxLayout()
        bottom.addWidget(self._composer, 1)
        bottom.addWidget(self._send_button)

        root = QVBoxLayout()
        root.addLayout(top)
        root.addWidget(self._view, 1)
        root.addLayout(bottom)

        container = QWidget()
        container.setLayout(root)
        self.setCentralWidget(container)

        asyncio.ensure_future(self._load_models())

    # ── Actions ───────────────────────────────────────────────────

    def _clear(self) -> None:
        self._history.clear()
        self._view.set_markdown("")

    def _send_clicked(self) -> None:
        if self._streaming:
            return
        text = self._composer.toPlainText().strip()
        if not text:
            return
        model = self._model.currentData() or self._model.currentText()
        if not model:
            self._view.append_markdown("\n*No model selected.*\n")
            return
        self._composer.clear()
        self._history.append(ChatTurn(role="user", content=text))
        self._view.append_markdown(f"\n\n**You:** {text}\n\n**Assistant:** ")
        self._set_streaming(True)
        asyncio.ensure_future(self._stream_reply(model))

    def _set_streaming(self, active: bool) -> None:
        self._streaming = active
        self._send_button.setEnabled(not active)
        self._send_shortcut.setEnabled(not active)
        self._composer.setReadOnly(active)

    async def _stream_reply(self, model: str) -> None:
        buffered = ""
        try:
            async for delta in self._client.stream_chat(
                self._history, model, think=self._think.isChecked() or None,
            ):
                buffered += delta
                self._view.append_markdown(delta)
            self._history.append(ChatTurn(role="assistant", content=buffered))
        except Exception as exc:
            self._view.append_markdown(f"\n\n*Error: {exc}*\n")
        finally:
            self._view.flush_now()
            self._set_streaming(False)

    async def _load_models(self) -> None:
        try:
            models = await self._client.list_models()
        except Exception as exc:
            self._view.append_markdown(f"*Could not load models: {exc}*\n")
            return
        self._model.clear()
        for m in models:
            mid = m.get("id") or ""
            label = m.get("owned_by") or mid
            self._model.addItem(f"{label} — {mid}", mid)

    # Hide-on-close so the tray keeps things alive.
    def closeEvent(self, event):  # noqa: N802
        event.ignore()
        self.hide()
