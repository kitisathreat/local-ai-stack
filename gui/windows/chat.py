"""Chat window.

Phase 6: airgap-aware. In normal mode the chat subdomain is the canonical
way in, so the Qt chat window just shows a guidance card. When airgap is
enabled the window prompts for login and the full chat UI takes over.

A QTimer polls /api/airgap every 5 s and swaps UI in place if the state
changes.
"""
from __future__ import annotations

import asyncio

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel, QMainWindow,
    QPlainTextEdit, QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)

from gui.api_client import BackendClient, ChatTurn
from gui.widgets.gauges import StatusGauges
from gui.widgets.markdown_view import MarkdownView


AIRGAP_POLL_MS = 5000
TELEMETRY_POLL_MS = 2000

# Rough character→token heuristic. Llama/Qwen BPEs land near 3.5–4.0
# chars/token for English; 4.0 keeps the gauge from over-reporting.
CHARS_PER_TOKEN = 4.0


class ChatWindow(QMainWindow):
    def __init__(self, client: BackendClient):
        super().__init__()
        self.setWindowTitle("Local AI Stack — Chat")
        self.resize(1000, 720)
        self._client = client
        self._history: list[ChatTurn] = []
        self._streaming = False
        self._airgap: bool | None = None
        self._authenticated = False
        # tier_id -> context_window tokens; populated by _load_models.
        self._ctx_windows: dict[str, int] = {}

        # Stacked widget: 0 = guidance card (airgap off), 1 = chat UI.
        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_guidance_view())
        self._stack.addWidget(self._build_chat_view())
        self.setCentralWidget(self._stack)

        # Airgap poll.
        self._poll = QTimer(self)
        self._poll.setInterval(AIRGAP_POLL_MS)
        self._poll.timeout.connect(self._poll_airgap)
        self._poll.start()
        asyncio.ensure_future(self._refresh_airgap())

        # Telemetry poll for VRAM gauge (and indirectly the context gauge,
        # since we redraw it on the same tick).
        self._telemetry = QTimer(self)
        self._telemetry.setInterval(TELEMETRY_POLL_MS)
        self._telemetry.timeout.connect(self._poll_telemetry)
        self._telemetry.start()

    # ── Views ─────────────────────────────────────────────────────

    def _build_guidance_view(self) -> QWidget:
        card = QLabel()
        card.setWordWrap(True)
        card.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card.setTextFormat(Qt.TextFormat.RichText)
        card.setOpenExternalLinks(True)
        card.setText(
            "<h2>Chat is hosted at <code>chat.mylensandi.com</code></h2>"
            "<p style='color:#888;max-width:520px;margin:12px auto;'>"
            "This window is idle while airgap mode is off. "
            "Sign in at <a href='https://chat.mylensandi.com'>chat.mylensandi.com</a> "
            "to talk to the models.</p>"
            "<p style='color:#888;margin-top:16px;'>"
            "To chat from this window instead, enable airgap mode from the admin panel.</p>"
        )
        open_admin = QPushButton("Open admin panel")
        open_admin.clicked.connect(self._open_admin_from_guidance)

        box = QVBoxLayout()
        box.addStretch(1)
        box.addWidget(card)
        box.addSpacing(12)
        box.addWidget(open_admin, 0, Qt.AlignmentFlag.AlignCenter)
        box.addStretch(1)
        w = QWidget()
        w.setLayout(box)
        return w

    def _build_chat_view(self) -> QWidget:
        # Toolbar
        self._model = QComboBox()
        self._model.setMinimumWidth(260)
        self._model.currentIndexChanged.connect(lambda _i: self._refresh_context_gauge())
        self._think = QCheckBox("Think")
        clear = QPushButton("Clear")
        clear.clicked.connect(self._clear)
        self._status = QLabel("")
        self._status.setStyleSheet("color: #888;")

        top = QHBoxLayout()
        top.addWidget(self._model, 1)
        top.addWidget(self._think)
        top.addWidget(clear)
        top.addWidget(self._status)

        # Hollow-ring gauges for context window + VRAM.
        self._gauges = StatusGauges()
        self._refresh_context_gauge()

        # Conversation
        self._view = MarkdownView()

        # Composer
        self._composer = QPlainTextEdit()
        self._composer.setPlaceholderText("Type a message. Ctrl+Enter to send.")
        self._composer.setFixedHeight(120)
        self._send_button = QPushButton("Send")
        self._send_button.clicked.connect(self._send_clicked)
        self._send_shortcut = QShortcut(QKeySequence("Ctrl+Return"), self._composer, self._send_clicked)

        bottom = QHBoxLayout()
        bottom.addWidget(self._composer, 1)
        bottom.addWidget(self._send_button)

        root = QVBoxLayout()
        root.addLayout(top)
        root.addWidget(self._gauges)
        root.addWidget(self._view, 1)
        root.addLayout(bottom)

        w = QWidget()
        w.setLayout(root)
        return w

    # ── Airgap state ──────────────────────────────────────────────

    def _poll_airgap(self) -> None:
        asyncio.ensure_future(self._refresh_airgap())

    async def _refresh_airgap(self) -> None:
        try:
            state = await self._client.airgap_state()
            enabled = bool(state.get("enabled"))
        except Exception:
            # Be conservative — if we can't reach /api/airgap, treat as
            # "not airgap" and leave the guidance card visible.
            enabled = False
        if self._airgap == enabled:
            return
        self._airgap = enabled
        if enabled:
            await self._switch_to_chat_mode()
        else:
            self._switch_to_guidance_mode()

    async def _switch_to_chat_mode(self) -> None:
        # Airgap just came on — require login.
        if not self._authenticated:
            if not await self._prompt_login():
                # User cancelled; stay on guidance card.
                return
        self._stack.setCurrentIndex(1)
        await self._load_models()

    def _switch_to_guidance_mode(self) -> None:
        # Airgap flipped off; freeze any in-flight stream and swap views.
        self._set_streaming(True)  # disable composer before clearing streaming flag
        self._streaming = False
        self._send_button.setEnabled(False)
        self._send_shortcut.setEnabled(False)
        self._composer.setReadOnly(True)
        self._stack.setCurrentIndex(0)

    async def _prompt_login(self) -> bool:
        from gui.windows.login import LoginDialog
        dlg = LoginDialog(self._client, require_admin=False, parent=self,
                          title="Sign in — airgap chat")
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return False
        self._authenticated = True
        return True

    def _open_admin_from_guidance(self) -> None:
        from gui.windows.admin import AdminWindow
        # Admin window spawns with its own login prompt via the tray's
        # subprocess path; for in-process convenience here we open the
        # AdminWindow directly but gate by a require_admin login first.
        from gui.windows.login import LoginDialog
        dlg = LoginDialog(self._client, require_admin=True, parent=self,
                          title="Admin sign-in")
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._admin_win = AdminWindow(self._client)
        self._admin_win.show()

    # ── Chat actions ──────────────────────────────────────────────

    def _clear(self) -> None:
        self._history.clear()
        self._view.set_markdown("")
        self._refresh_context_gauge()

    # ── Gauges / telemetry ────────────────────────────────────────

    def _poll_telemetry(self) -> None:
        if self._airgap is not True:
            return
        asyncio.ensure_future(self._refresh_vram_gauge())

    async def _refresh_vram_gauge(self) -> None:
        try:
            data = await self._client.vram_status()
        except Exception:
            return
        total = float(data.get("total_vram_gb") or 0.0)
        free = data.get("free_vram_gb_actual")
        if free is None:
            free = data.get("free_vram_gb_projected") or 0.0
        used = max(0.0, total - float(free))
        self._gauges.set_vram(used, total)

    def _estimate_history_tokens(self) -> int:
        chars = sum(len(t.content) for t in self._history)
        return int(chars / CHARS_PER_TOKEN)

    def _current_context_window(self) -> int:
        tier_id = self._model.currentData() if hasattr(self, "_model") else None
        if not tier_id:
            return 0
        return self._ctx_windows.get(str(tier_id), 0)

    def _refresh_context_gauge(self) -> None:
        if not hasattr(self, "_gauges"):
            return
        self._gauges.set_context(
            self._estimate_history_tokens(),
            self._current_context_window(),
        )

    def _send_clicked(self) -> None:
        if self._streaming or self._airgap is not True:
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
        self._refresh_context_gauge()
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
            self._refresh_context_gauge()
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
        self._ctx_windows.clear()
        for m in models:
            mid = m.get("id") or ""
            label = m.get("owned_by") or mid
            ctx = int(m.get("context_window") or 0)
            if mid and ctx:
                self._ctx_windows[mid] = ctx
            self._model.addItem(f"{label} — {mid}", mid)
        self._refresh_context_gauge()

    # Hide-on-close so the tray keeps things alive.
    def closeEvent(self, event):  # noqa: N802
        event.ignore()
        self.hide()
