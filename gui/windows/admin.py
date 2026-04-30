"""Admin dashboard window.

Phase 4: full CRUD parity with the old Preact admin panel — Users,
Models, Tools, Airgap, Config (read-only), VRAM. Requires an admin
session; callers are expected to show LoginDialog(require_admin=True)
first.
"""
from __future__ import annotations

import asyncio

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QFormLayout,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QTabWidget,
    QVBoxLayout, QWidget,
)

from gui.api_client import BackendClient


class _NewUserDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New user")
        self.setModal(True)
        self._username = QLineEdit()
        self._email = QLineEdit()
        self._password = QLineEdit()
        self._password.setEchoMode(QLineEdit.EchoMode.Password)
        self._is_admin = QCheckBox("Admin account")
        form = QFormLayout()
        form.addRow("Username:", self._username)
        form.addRow("Email:", self._email)
        form.addRow("Password:", self._password)
        form.addRow(self._is_admin)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root = QVBoxLayout(self)
        root.addLayout(form)
        root.addWidget(buttons)

    def values(self) -> dict:
        return {
            "username": self._username.text().strip(),
            "email": self._email.text().strip(),
            "password": self._password.text(),
            "is_admin": self._is_admin.isChecked(),
        }


class AdminWindow(QMainWindow):
    def __init__(self, client: BackendClient):
        super().__init__()
        self.setWindowTitle("Local AI Stack — Admin")
        self.resize(960, 640)
        self._client = client
        self._airgap_enabled = False

        tabs = QTabWidget()
        tabs.addTab(self._build_users_tab(), "Users")
        tabs.addTab(self._build_models_tab(), "Models")
        tabs.addTab(self._build_tools_tab(), "Tools")
        tabs.addTab(self._build_airgap_tab(), "Airgap")
        tabs.addTab(self._build_vram_tab(), "VRAM")

        root = QVBoxLayout()
        root.addWidget(tabs)
        container = QWidget()
        container.setLayout(root)
        self.setCentralWidget(container)

        asyncio.ensure_future(self._refresh_all())

    # ── Users tab ─────────────────────────────────────────────────

    def _build_users_tab(self) -> QWidget:
        self._users_table = QTableWidget(0, 6)
        self._users_table.setHorizontalHeaderLabels(
            ["ID", "Username", "Email", "Admin", "Chats", "Last login"]
        )
        self._users_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._users_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._users_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )

        add = QPushButton("Add user")
        add.clicked.connect(self._on_add_user)
        edit = QPushButton("Change password")
        edit.clicked.connect(self._on_change_password)
        promote = QPushButton("Toggle admin")
        promote.clicked.connect(self._on_toggle_admin)
        delete = QPushButton("Delete")
        delete.clicked.connect(self._on_delete_user)

        bar = QHBoxLayout()
        bar.addWidget(add); bar.addWidget(edit); bar.addWidget(promote); bar.addWidget(delete)
        bar.addStretch(1)

        w = QWidget()
        lay = QVBoxLayout()
        lay.addLayout(bar)
        lay.addWidget(self._users_table)
        w.setLayout(lay)
        return w

    def _selected_user_id(self) -> int | None:
        rows = self._users_table.selectionModel().selectedRows()
        if not rows:
            return None
        item = self._users_table.item(rows[0].row(), 0)
        if not item:
            return None
        try:
            return int(item.text())
        except ValueError:
            return None

    def _on_add_user(self) -> None:
        dlg = _NewUserDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        v = dlg.values()
        if not v["username"] or not v["email"] or len(v["password"]) < 8:
            QMessageBox.warning(self, "Invalid", "Username, email, and 8+ char password required.")
            return
        async def do():
            try:
                await self._client.admin_create_user(**v)
                await self._refresh_users()
            except Exception as exc:
                QMessageBox.warning(self, "Create failed", str(exc))
        asyncio.ensure_future(do())

    def _on_change_password(self) -> None:
        uid = self._selected_user_id()
        if uid is None:
            return
        pw, ok = QInputDialog.getText(
            self, "Change password", "New password:", echo=QLineEdit.EchoMode.Password,
        )
        if not ok or len(pw) < 8:
            if ok:
                QMessageBox.warning(self, "Invalid", "Password must be at least 8 characters.")
            return
        async def do():
            try:
                await self._client.admin_patch_user(uid, password=pw)
                QMessageBox.information(self, "OK", "Password changed.")
            except Exception as exc:
                QMessageBox.warning(self, "Failed", str(exc))
        asyncio.ensure_future(do())

    def _on_toggle_admin(self) -> None:
        uid = self._selected_user_id()
        if uid is None:
            return
        # Flip the flag shown in the table.
        rows = self._users_table.selectionModel().selectedRows()
        if not rows: return
        row = rows[0].row()
        current = self._users_table.item(row, 3).text() == "yes"
        async def do():
            try:
                await self._client.admin_patch_user(uid, is_admin=not current)
                await self._refresh_users()
            except Exception as exc:
                QMessageBox.warning(self, "Failed", str(exc))
        asyncio.ensure_future(do())

    def _on_delete_user(self) -> None:
        uid = self._selected_user_id()
        if uid is None:
            return
        if QMessageBox.question(
            self, "Delete user",
            f"Hard-delete user {uid}? This also removes their chats and memories.",
        ) != QMessageBox.StandardButton.Yes:
            return
        async def do():
            try:
                await self._client.admin_delete_user(uid)
                await self._refresh_users()
            except Exception as exc:
                QMessageBox.warning(self, "Failed", str(exc))
        asyncio.ensure_future(do())

    async def _refresh_users(self) -> None:
        try:
            users = await self._client.admin_users()
        except Exception:
            users = []
        self._users_table.setRowCount(0)
        for row, u in enumerate(users):
            self._users_table.insertRow(row)
            self._users_table.setItem(row, 0, QTableWidgetItem(str(u.get("id", ""))))
            self._users_table.setItem(row, 1, QTableWidgetItem(u.get("username", "")))
            self._users_table.setItem(row, 2, QTableWidgetItem(u.get("email", "")))
            self._users_table.setItem(row, 3, QTableWidgetItem("yes" if u.get("is_admin") else ""))
            self._users_table.setItem(row, 4, QTableWidgetItem(str(u.get("conversations", 0))))
            last = u.get("last_login_at") or ""
            self._users_table.setItem(row, 5, QTableWidgetItem(str(last)[:19] if last else ""))

    # ── Models tab ────────────────────────────────────────────────

    def _build_models_tab(self) -> QWidget:
        self._models_table = QTableWidget(0, 5)
        self._models_table.setHorizontalHeaderLabels(
            ["Tier", "Source", "Identifier", "Origin", "Update?"]
        )
        self._models_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._models_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        w = QWidget()
        lay = QVBoxLayout()
        lay.addWidget(self._models_table)
        w.setLayout(lay)
        return w

    async def _refresh_models(self) -> None:
        try:
            data = await self._client.resolved_models()
        except Exception:
            data = {}
        tiers = (data.get("tiers") or {}).items()
        self._models_table.setRowCount(0)
        for row, (tier, info) in enumerate(tiers):
            self._models_table.insertRow(row)
            for col, val in enumerate([
                tier,
                info.get("source", ""),
                info.get("identifier", ""),
                info.get("origin", ""),
                "yes" if info.get("update_available") else "",
            ]):
                self._models_table.setItem(row, col, QTableWidgetItem(str(val)))

    # ── Tools tab ─────────────────────────────────────────────────

    def _build_tools_tab(self) -> QWidget:
        self._tools_table = QTableWidget(0, 3)
        self._tools_table.setHorizontalHeaderLabels(["Name", "Enabled", "Description"])
        self._tools_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._tools_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self._tools_table.cellClicked.connect(self._on_tool_clicked)
        w = QWidget()
        lay = QVBoxLayout()
        hint = QLabel("Click a checkbox to toggle a tool.")
        hint.setStyleSheet("color: #888;")
        lay.addWidget(hint)
        lay.addWidget(self._tools_table)
        w.setLayout(lay)
        return w

    def _on_tool_clicked(self, row: int, col: int) -> None:
        if col != 1:
            return
        name_item = self._tools_table.item(row, 0)
        if not name_item:
            return
        name = name_item.text()
        current = self._tools_table.item(row, 1).text() == "yes"
        async def do():
            try:
                await self._client.admin_set_tool_enabled(name, not current)
                await self._refresh_tools()
            except Exception as exc:
                QMessageBox.warning(self, "Failed", str(exc))
        asyncio.ensure_future(do())

    async def _refresh_tools(self) -> None:
        try:
            tools = await self._client.admin_tools()
        except Exception:
            tools = []
        self._tools_table.setRowCount(0)
        for row, t in enumerate(tools):
            self._tools_table.insertRow(row)
            self._tools_table.setItem(row, 0, QTableWidgetItem(t.get("name", "")))
            self._tools_table.setItem(row, 1, QTableWidgetItem("yes" if t.get("enabled") else ""))
            self._tools_table.setItem(row, 2, QTableWidgetItem(t.get("description", "")))

    # ── Airgap tab ─────────────────────────────────────────────────

    def _build_airgap_tab(self) -> QWidget:
        self._airgap_label = QLabel("Airgap: (loading…)")
        self._airgap_label.setStyleSheet("font-size: 14pt;")
        self._airgap_toggle = QPushButton("Enable airgap mode")
        self._airgap_toggle.clicked.connect(self._on_airgap_toggle)

        warning = QLabel(
            "<b>Airgap mode</b> closes the <code>chat.mylensandi.com</code> subdomain "
            "and enables the local Qt chat window for any logged-in user. "
            "Web-search tools, RAG uploads, and external API calls are blocked."
        )
        warning.setWordWrap(True)
        warning.setStyleSheet("color: #a60; padding: 8px; border: 1px solid #a60;")

        w = QWidget()
        lay = QVBoxLayout()
        lay.addWidget(self._airgap_label)
        lay.addWidget(self._airgap_toggle)
        lay.addSpacing(16)
        lay.addWidget(warning)
        lay.addStretch(1)
        w.setLayout(lay)
        return w

    def _on_airgap_toggle(self) -> None:
        new_state = not self._airgap_enabled
        async def do():
            try:
                result = await self._client.admin_set_airgap(new_state)
                self._airgap_enabled = bool(result.get("enabled"))
                self._update_airgap_label()
            except Exception as exc:
                QMessageBox.warning(self, "Failed", str(exc))
        asyncio.ensure_future(do())

    def _update_airgap_label(self) -> None:
        if self._airgap_enabled:
            self._airgap_label.setText("Airgap: ON (outbound blocked)")
            self._airgap_toggle.setText("Disable airgap mode")
        else:
            self._airgap_label.setText("Airgap: OFF (chat via chat.mylensandi.com)")
            self._airgap_toggle.setText("Enable airgap mode")

    async def _refresh_airgap(self) -> None:
        try:
            state = await self._client.airgap_state()
            self._airgap_enabled = bool(state.get("enabled"))
        except Exception:
            self._airgap_enabled = False
        self._update_airgap_label()

    # ── VRAM tab ──────────────────────────────────────────────────

    def _build_vram_tab(self) -> QWidget:
        self._vram_table = QTableWidget(0, 3)
        self._vram_table.setHorizontalHeaderLabels(["Tier", "Loaded", "Estimated GB"])
        self._vram_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        w = QWidget()
        lay = QVBoxLayout()
        lay.addWidget(self._vram_table)
        w.setLayout(lay)
        return w

    async def _refresh_vram(self) -> None:
        try:
            data = await self._client.vram_status()
        except Exception:
            data = {}
        self._vram_table.setRowCount(0)
        for row, tier in enumerate(data.get("tiers") or []):
            self._vram_table.insertRow(row)
            self._vram_table.setItem(row, 0, QTableWidgetItem(str(tier.get("name", ""))))
            self._vram_table.setItem(row, 1, QTableWidgetItem("yes" if tier.get("loaded") else ""))
            self._vram_table.setItem(row, 2, QTableWidgetItem(str(tier.get("estimated_vram_gb", ""))))

    # ── Refresh orchestration ─────────────────────────────────────

    async def _refresh_all(self) -> None:
        await asyncio.gather(
            self._refresh_users(),
            self._refresh_models(),
            self._refresh_tools(),
            self._refresh_airgap(),
            self._refresh_vram(),
        )
