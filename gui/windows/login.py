"""Username + password login dialog.

Modal QDialog with a single form. On submit, runs
``BackendClient.login_sync()`` on a worker QThread — NOT an asyncio
task — because QDialog.exec() runs Qt's modal event loop synchronously,
which suspends qasync's asyncio scheduler. Any `await` inside the
dialog deadlocks ("Signing in…" shows forever). The QThread approach
keeps the UI responsive and cleanly surfaces the result via signals.

When ``require_admin=True`` (admin window launch), a successful login
that isn't an admin account is surfaced inline — the dialog stays open
and refuses to accept().
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QSettings, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QVBoxLayout, QWidget,
)

from gui.api_client import BackendClient


class _LoginWorker(QThread):
    """Runs BackendClient.login_sync() off the UI thread.

    Two outcomes, surfaced via signals so the dialog can update UI on
    Qt's main thread:
      success(dict) — backend returned a user dict
      failure(str)  — human-readable error string
    """
    success = Signal(dict)
    failure = Signal(str)

    def __init__(self, client: BackendClient, username: str, password: str):
        super().__init__()
        self._client = client
        self._username = username
        self._password = password

    def run(self) -> None:
        try:
            info = self._client.login_sync(self._username, self._password)
            self.success.emit(info or {})
        except ValueError:
            # Bad credentials — generic message (no info-leak).
            self.failure.emit("Invalid username or password.")
        except ConnectionError as exc:
            self.failure.emit(str(exc))
        except Exception as exc:
            # Unexpected — show the type so the user can report it.
            self.failure.emit(f"Login failed: {type(exc).__name__}: {exc}")


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
            self._error.setStyleSheet("color: #c44;")
            return
        # Don't double-submit if a worker is already running.
        if getattr(self, "_worker", None) and self._worker.isRunning():
            return
        self._error.setText("Signing in…")
        self._error.setStyleSheet("color: #888;")
        self._worker = _LoginWorker(self._client, username, password)
        self._worker.success.connect(lambda info, u=username: self._on_login_ok(u, info))
        self._worker.failure.connect(self._on_login_failed)
        self._worker.start()

    def _on_login_ok(self, username: str, info: dict) -> None:
        if self._require_admin and not info.get("is_admin"):
            # Non-admin account on the admin window — log out and reset
            # so a different account can try.
            try:
                self._client.logout_sync()
            except Exception:
                pass
            self._error.setText("This account does not have admin privileges.")
            self._error.setStyleSheet("color: #c44;")
            return
        if self._remember.isChecked():
            QSettings("LocalAIStack", "LocalAIStack").setValue("last_username", username)
        self._user = {"username": username, **(info or {})}
        self.accept()

    def _on_login_failed(self, msg: str) -> None:
        # Distinct messages for credential vs connectivity vs other so the
        # user knows whether to retype or to start the backend.
        self._error.setText(msg)
        self._error.setStyleSheet("color: #c44;")
        # Re-focus the password field for easy retry.
        self._password.selectAll()
        self._password.setFocus()
