"""Username + password login dialog.

Modal QDialog with a single form. On submit, calls
``BackendClient.login()`` which POSTs ``/auth/login`` and stores the
returned ``lai_session`` cookie for the rest of the process.

When ``require_admin=True`` (admin window launch), a successful login
that isn't an admin account is surfaced inline — the dialog stays open
and refuses to accept()*.
"""
from __future__ import annotations

import asyncio

from PySide6.QtCore import Qt, QSettings
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QVBoxLayout, QWidget,
)

from gui.api_client import BackendClient


class LoginDialog(QDialog):
    def __init__(
        self,
        client: BackendClient,
        parent: QWidget | None = None,
        *,
        require_admin: bool = False,
        title: str = "Sign in — Local AI Stack",
    ):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(380)
        self._client = client
        self._require_admin = require_admin
        self._user: dict | None = None

        self._username = QLineEdit()
        self._password = QLineEdit()
        self._password.setEchoMode(QLineEdit.EchoMode.Password)
        self._remember = QCheckBox("Remember me on this machine")
        self._error = QLabel("")
        self._error.setWordWrap(True)
        self._error.setStyleSheet("color: #c44;")

        # Pre-fill last-used username if available.
        settings = QSettings("LocalAIStack", "LocalAIStack")
        last = settings.value("last_username", "", type=str)
        if last:
            self._username.setText(last)
            self._password.setFocus()
        else:
            self._username.setFocus()

        form = QFormLayout()
        form.addRow("Username:", self._username)
        form.addRow("Password:", self._password)
        form.addRow(self._remember)

        sign_in = QPushButton("Sign in")
        sign_in.setDefault(True)
        sign_in.clicked.connect(self._submit)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(sign_in)

        root = QVBoxLayout(self)
        if require_admin:
            hint = QLabel("Admin sign-in. Only users with admin privileges may continue.")
            hint.setStyleSheet("color: #888; font-size: 9pt;")
            hint.setWordWrap(True)
            root.addWidget(hint)
        root.addLayout(form)
        root.addWidget(self._error)
        root.addLayout(buttons)

        # Submitting via Enter key in the password box.
        self._password.returnPressed.connect(self._submit)
        self._username.returnPressed.connect(lambda: self._password.setFocus())

    # Public: returns the user dict on success, None if dialog was cancelled.
    def result_user(self) -> dict | None:
        return self._user

    # ── Private ────────────────────────────────────────────────────────

    def _submit(self) -> None:
        username = self._username.text().strip()
        password = self._password.text()
        if not username or not password:
            self._error.setText("Enter username and password.")
            return
        self._error.setText("Signing in…")
        asyncio.ensure_future(self._login_async(username, password))

    async def _login_async(self, username: str, password: str) -> None:
        try:
            info = await self._client.login(username, password)
        except Exception as exc:
            # Constant-time generic error regardless of cause — mirrors
            # the backend's 401.
            self._error.setText(f"Invalid username or password.")
            return
        if self._require_admin and not info.get("is_admin"):
            # Drop the session; we don't want a non-admin cookie bound
            # to the admin window.
            await self._client.logout()
            self._error.setText("This account is not an admin. Try another.")
            return
        if self._remember.isChecked():
            QSettings("LocalAIStack", "LocalAIStack").setValue("last_username", username)
        self._user = {"username": username, **(info or {})}
        self.accept()
