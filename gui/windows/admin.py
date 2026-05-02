"""Admin dashboard window — full write-parity with deleted Preact admin panel.

Tabs: Users, Models, Tools, Airgap, VRAM, Router, Auth, Errors, Reload.
Requires an admin session; callers show LoginDialog(require_admin=True) first.
"""
from __future__ import annotations

import asyncio
import secrets
import string

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QFormLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QSpinBox, QDoubleSpinBox, QTableWidget, QTableWidgetItem, QTabWidget,
    QTextEdit, QVBoxLayout, QWidget,
)

from gui.api_client import BackendClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _generate_password(length: int = 16) -> str:
    """Cryptographically random password using a URL-safe alphabet."""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_"
    return "".join(secrets.choice(alphabet) for _ in range(length))


class _NewUserDialog(QDialog):
    """Dialog for creating a new user.

    Defaults to a non-admin local account. Email is optional — when blank a
    placeholder `<username>@local.lan` is sent so the DB's NOT NULL UNIQUE
    constraint is satisfied without forcing the admin to invent one.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add user")
        self.setModal(True)

        self._username = QLineEdit()
        self._username.setPlaceholderText("e.g. alice")

        self._email = QLineEdit()
        self._email.setPlaceholderText("optional — defaults to <username>@local.lan")

        self._password = QLineEdit()
        self._password.setEchoMode(QLineEdit.EchoMode.Password)
        self._password.setPlaceholderText("8+ characters")

        self._show_password = QCheckBox("Show")
        self._show_password.toggled.connect(self._on_show_password_toggled)

        gen_btn = QPushButton("Generate")
        gen_btn.setToolTip("Generate a strong random password and copy it to the clipboard.")
        gen_btn.clicked.connect(self._on_generate_password)

        pw_row = QHBoxLayout()
        pw_row.addWidget(self._password, 1)
        pw_row.addWidget(self._show_password)
        pw_row.addWidget(gen_btn)

        self._is_admin = QCheckBox("Admin account (grant dashboard access)")
        self._is_admin.setChecked(False)

        hint = QLabel(
            "Leave “Admin account” unchecked to create a regular (non-admin) user "
            "who can sign in to chat but cannot open the admin dashboard."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888;")

        form = QFormLayout()
        form.addRow("Username:", self._username)
        form.addRow("Email:", self._email)
        form.addRow("Password:", pw_row)
        form.addRow("", self._is_admin)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Create user")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.addLayout(form)
        root.addWidget(hint)
        root.addWidget(buttons)
        self.resize(460, self.sizeHint().height())

    def _on_show_password_toggled(self, checked: bool) -> None:
        self._password.setEchoMode(
            QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
        )

    def _on_generate_password(self) -> None:
        pw = _generate_password()
        self._password.setText(pw)
        # Reveal so the admin can copy it down before closing the dialog.
        self._show_password.setChecked(True)
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(pw)

    def values(self) -> dict:
        username = self._username.text().strip()
        email = self._email.text().strip()
        if not email and username:
            email = f"{username}@local.lan"
        return {
            "username": username,
            "email": email,
            "password": self._password.text(),
            "is_admin": self._is_admin.isChecked(),
        }


def _make_save_row(save_fn) -> tuple[QHBoxLayout, QPushButton]:
    """Return (layout, save_button) with a right-aligned Save button."""
    save_btn = QPushButton("Save")
    save_btn.setFixedWidth(90)
    save_btn.clicked.connect(save_fn)
    row = QHBoxLayout()
    row.addStretch()
    row.addWidget(save_btn)
    return row, save_btn


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class AdminWindow(QMainWindow):
    def __init__(self, client: BackendClient):
        super().__init__()
        self.setWindowTitle("Local AI Stack — Admin")
        self.resize(1000, 680)
        self._client = client
        self._airgap_enabled = False

        tabs = QTabWidget()
        tabs.addTab(self._build_users_tab(), "Users")
        tabs.addTab(self._build_models_tab(), "Models")
        tabs.addTab(self._build_tools_tab(), "Tools")
        tabs.addTab(self._build_airgap_tab(), "Airgap")
        tabs.addTab(self._build_vram_tab(), "VRAM")
        tabs.addTab(self._build_router_tab(), "Router")
        tabs.addTab(self._build_auth_tab(), "Auth")
        tabs.addTab(self._build_errors_tab(), "Errors")
        tabs.addTab(self._build_reload_tab(), "Reload")

        container = QWidget()
        QVBoxLayout(container).addWidget(tabs)
        self.setCentralWidget(container)

        self._build_menu_bar()

        asyncio.ensure_future(self._refresh_all())

    # ── Menu bar ──────────────────────────────────────────────────────────
    def _build_menu_bar(self) -> None:
        bar = self.menuBar()
        view = bar.addMenu("&View")

        chat_act = QAction("Open &Desktop Chat", self)
        chat_act.setShortcut("Ctrl+Shift+C")
        chat_act.setStatusTip(
            "Open the chat UI in a native desktop window (same look as the browser)."
        )
        chat_act.triggered.connect(self._open_desktop_chat)
        view.addAction(chat_act)

    def _open_desktop_chat(self) -> None:
        try:
            from gui.windows.desktop_chat import DesktopChatWindow
        except ImportError as exc:
            QMessageBox.critical(self, "Desktop chat unavailable", str(exc))
            return
        api_base = getattr(self._client, "_base", "http://127.0.0.1:18000")
        try:
            win = DesktopChatWindow(api_base=api_base)
        except RuntimeError as exc:
            QMessageBox.critical(self, "Desktop chat unavailable", str(exc))
            return
        win.show()
        win.raise_()
        win.activateWindow()
        # Hold a strong ref so Qt doesn't garbage-collect the window.
        self._desktop_chat = win  # type: ignore[attr-defined]

    # ── Users tab ──────────────────────────────────────────────────────────

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
        add.setToolTip(
            "Create a new account. Defaults to a non-admin user; tick the "
            "“Admin account” box in the dialog to grant dashboard access."
        )
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
        lay = QVBoxLayout(w)
        lay.addLayout(bar)
        lay.addWidget(self._users_table)
        return w

    def _selected_user_id(self) -> int | None:
        rows = self._users_table.selectionModel().selectedRows()
        if not rows:
            return None
        item = self._users_table.item(rows[0].row(), 0)
        return int(item.text()) if item else None

    def _on_add_user(self) -> None:
        dlg = _NewUserDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        v = dlg.values()
        if not v["username"]:
            QMessageBox.warning(self, "Invalid", "Username is required.")
            return
        if len(v["password"]) < 8:
            QMessageBox.warning(self, "Invalid", "Password must be at least 8 characters.")
            return
        # `values()` already fills in `<username>@local.lan` when the field
        # is blank, so this is just a defensive guard.
        if not v["email"]:
            v["email"] = f"{v['username']}@local.lan"
        kind = "admin" if v["is_admin"] else "non-admin"
        async def do():
            try:
                await self._client.admin_create_user(**v)
            except Exception as exc:
                QMessageBox.warning(self, "Create failed", str(exc))
                return
            await self._refresh_users()
            QMessageBox.information(
                self, "User created",
                f"Created {kind} account “{v['username']}”.",
            )
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
        rows = self._users_table.selectionModel().selectedRows()
        if not rows:
            return
        current = self._users_table.item(rows[0].row(), 3).text() == "yes"
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
            # last_login_at can be a unix epoch float (native mode) or
            # an ISO string (legacy). Normalize to "YYYY-MM-DD HH:MM:SS"
            # so the column is human-readable instead of raw seconds.
            last = u.get("last_login_at")
            if isinstance(last, (int, float)):
                from datetime import datetime
                last_str = datetime.fromtimestamp(last).strftime("%Y-%m-%d %H:%M:%S")
            elif isinstance(last, str) and last:
                last_str = last[:19]
            else:
                last_str = ""
            self._users_table.setItem(row, 5, QTableWidgetItem(last_str))

    # ── Models tab ─────────────────────────────────────────────────────────

    def _build_models_tab(self) -> QWidget:
        # Six-column layout: the new "Progress" column hosts a per-tier
        # QProgressBar that polls /admin/model-pull-status. The bar is
        # the user's at-a-glance view of "is everything downloaded yet?"
        # while the wizard / backend auto-pull runs in the background.
        self._models_table = QTableWidget(0, 6)
        self._models_table.setHorizontalHeaderLabels(
            ["Tier", "Source", "Identifier", "Origin", "Update?", "Progress"]
        )
        self._models_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._models_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        # Progress bars per tier, keyed by tier name. Reused across
        # refreshes so the bar instance — and its smooth animation —
        # survives the table re-population.
        self._progress_bars: dict[str, QProgressBar] = {}
        # Drive a 5-second poll while ANY tier is incomplete. Stops
        # itself once everything reaches 100%.
        self._pull_timer = QTimer(self)
        self._pull_timer.setInterval(5000)
        self._pull_timer.timeout.connect(
            lambda: asyncio.ensure_future(self._refresh_pull_progress())
        )
        w = QWidget()
        QVBoxLayout(w).addWidget(self._models_table)
        return w

    def _make_progress_bar(self, tier: str) -> QProgressBar:
        bar = self._progress_bars.get(tier)
        if bar is None:
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(0)
            bar.setTextVisible(True)
            bar.setFormat("…")
            self._progress_bars[tier] = bar
        return bar

    @staticmethod
    def _fmt_bytes(n: int | float) -> str:
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if n < 1024 or unit == "TB":
                return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
            n /= 1024
        return f"{n:.1f} TB"

    def _update_bar(self, tier: str, status: dict) -> None:
        bar = self._make_progress_bar(tier)
        complete = status.get("complete", False)
        in_progress = status.get("in_progress", False)
        pct = status.get("percent")
        downloaded = status.get("downloaded_bytes") or 0
        expected = status.get("expected_bytes")
        if complete:
            bar.setRange(0, 100); bar.setValue(100)
            bar.setFormat("✓ done")
        elif pct is not None:
            bar.setRange(0, 100); bar.setValue(int(pct))
            bar.setFormat(f"{pct:.1f}% — {self._fmt_bytes(downloaded)}"
                          + (f" / {self._fmt_bytes(expected)}" if expected else ""))
        elif in_progress:
            # No expected size yet (HF API hasn't answered) — show an
            # indeterminate spinner so the user sees activity.
            bar.setRange(0, 0); bar.setFormat(self._fmt_bytes(downloaded))
        else:
            bar.setRange(0, 100); bar.setValue(0)
            bar.setFormat("queued")

    async def _refresh_models(self) -> None:
        try:
            data = await self._client.resolved_models()
        except Exception:
            data = {}
        tiers = list((data.get("tiers") or {}).items())
        self._models_table.setRowCount(0)
        for row, (tier, info) in enumerate(tiers):
            self._models_table.insertRow(row)
            repo = info.get("repo") or info.get("model_id") or ""
            filename = info.get("filename") or ""
            if repo and filename:
                identifier = f"{repo}/{filename}"
            else:
                identifier = repo or filename or info.get("path") or ""
            for col, val in enumerate([
                tier,
                info.get("source", ""),
                identifier,
                info.get("origin", ""),
                "yes" if info.get("update_available") else "",
            ]):
                self._models_table.setItem(row, col, QTableWidgetItem(str(val)))
            # Progress bar in column 5 — set after insertRow so the
            # widget is parented to the right cell.
            bar = self._make_progress_bar(tier)
            # Seed from the available flag so the row isn't blank
            # before the first /admin/model-pull-status round trip.
            seed_complete = bool(info.get("available"))
            if seed_complete:
                bar.setRange(0, 100); bar.setValue(100); bar.setFormat("✓ done")
            elif bar.format() == "…":
                bar.setRange(0, 100); bar.setValue(0); bar.setFormat("checking…")
            self._models_table.setCellWidget(row, 5, bar)
        # Kick off the first detailed poll and start the timer if any
        # tier is still incomplete.
        asyncio.ensure_future(self._refresh_pull_progress())
        if hasattr(self, "_pull_timer") and not self._pull_timer.isActive():
            self._pull_timer.start()

    async def _refresh_pull_progress(self) -> None:
        """Poll /admin/model-pull-status and update each row's bar."""
        try:
            statuses = await self._client.model_pull_status()
        except Exception:
            return
        all_done = bool(statuses) and all(
            s.get("complete") for s in statuses.values()
        )
        for tier, status in statuses.items():
            self._update_bar(tier, status)
        if all_done and hasattr(self, "_pull_timer"):
            self._pull_timer.stop()

    # ── Tools tab ──────────────────────────────────────────────────────────

    def _build_tools_tab(self) -> QWidget:
        self._tools_table = QTableWidget(0, 3)
        self._tools_table.setHorizontalHeaderLabels(["Name", "Enabled", "Description"])
        self._tools_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._tools_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self._tools_table.cellClicked.connect(self._on_tool_clicked)

        w = QWidget()
        lay = QVBoxLayout(w)
        hint = QLabel("Click the Enabled column to toggle a tool on or off.")
        hint.setStyleSheet("color: #888;")
        lay.addWidget(hint)
        lay.addWidget(self._tools_table)
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

    # ── Airgap tab ─────────────────────────────────────────────────────────

    def _build_airgap_tab(self) -> QWidget:
        self._airgap_label = QLabel("Airgap: (loading…)")
        self._airgap_label.setStyleSheet("font-size: 14pt;")
        self._airgap_toggle = QPushButton("Enable airgap mode")
        self._airgap_toggle.clicked.connect(self._on_airgap_toggle)

        warning = QLabel(
            "<b>Airgap mode</b> closes the public subdomain and enables the local "
            "Qt chat window for any logged-in user. "
            "Web-search tools, RAG uploads, and external API calls are blocked."
        )
        warning.setWordWrap(True)
        warning.setStyleSheet("color: #a60; padding: 8px; border: 1px solid #a60;")

        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(self._airgap_label)
        lay.addWidget(self._airgap_toggle)
        lay.addSpacing(16)
        lay.addWidget(warning)
        lay.addStretch(1)
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
            self._airgap_label.setText("Airgap: OFF")
            self._airgap_toggle.setText("Enable airgap mode")

    async def _refresh_airgap(self) -> None:
        try:
            state = await self._client.airgap_state()
            self._airgap_enabled = bool(state.get("enabled"))
        except Exception:
            self._airgap_enabled = False
        self._update_airgap_label()

    # ── VRAM tab ───────────────────────────────────────────────────────────

    def _build_vram_tab(self) -> QWidget:
        self._vram_table = QTableWidget(0, 3)
        self._vram_table.setHorizontalHeaderLabels(["Tier", "Loaded", "Estimated GB"])
        self._vram_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        w = QWidget()
        QVBoxLayout(w).addWidget(self._vram_table)
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

    # ── Router config tab ──────────────────────────────────────────────────

    def _build_router_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)

        grp = QGroupBox("Multi-agent settings")
        form = QFormLayout(grp)

        self._router_max_workers = QSpinBox()
        self._router_max_workers.setRange(1, 32)
        form.addRow("Max workers:", self._router_max_workers)

        self._router_min_workers = QSpinBox()
        self._router_min_workers.setRange(1, 32)
        form.addRow("Min workers:", self._router_min_workers)

        self._router_worker_tier = QLineEdit()
        form.addRow("Worker tier:", self._router_worker_tier)

        self._router_orchestrator_tier = QLineEdit()
        form.addRow("Orchestrator tier:", self._router_orchestrator_tier)

        self._router_interaction_rounds = QSpinBox()
        self._router_interaction_rounds.setRange(1, 20)
        form.addRow("Interaction rounds:", self._router_interaction_rounds)

        lay.addWidget(grp)
        save_row, _ = _make_save_row(self._save_router)
        lay.addLayout(save_row)
        lay.addStretch()
        return w

    def _save_router(self) -> None:
        payload = {
            "router": {
                "multi_agent": {
                    "max_workers": self._router_max_workers.value(),
                    "min_workers": self._router_min_workers.value(),
                    "worker_tier": self._router_worker_tier.text().strip(),
                    "orchestrator_tier": self._router_orchestrator_tier.text().strip(),
                    "interaction_rounds": self._router_interaction_rounds.value(),
                }
            }
        }
        async def do():
            try:
                result = await self._client.admin_patch_config(payload)
                QMessageBox.information(self, "Saved", f"Router config saved.\n{result.get('message','')}")
            except Exception as exc:
                QMessageBox.warning(self, "Save failed", str(exc))
        asyncio.ensure_future(do())

    async def _refresh_router(self, cfg: dict) -> None:
        router = cfg.get("router", {})
        ma = router.get("multi_agent", {})
        self._router_max_workers.setValue(int(ma.get("max_workers", 4)))
        self._router_min_workers.setValue(int(ma.get("min_workers", 1)))
        self._router_worker_tier.setText(str(ma.get("worker_tier", "")))
        self._router_orchestrator_tier.setText(str(ma.get("orchestrator_tier", "")))
        self._router_interaction_rounds.setValue(int(ma.get("interaction_rounds", 3)))

    # ── Auth config tab ────────────────────────────────────────────────────

    def _build_auth_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)

        grp = QGroupBox("Auth settings")
        form = QFormLayout(grp)

        self._auth_domains = QLineEdit()
        self._auth_domains.setPlaceholderText("example.com, another.org  (blank = any)")
        form.addRow("Allowed email domains:", self._auth_domains)

        self._auth_link_ttl = QSpinBox()
        self._auth_link_ttl.setRange(1, 1440)
        self._auth_link_ttl.setSuffix(" min")
        form.addRow("Magic-link TTL:", self._auth_link_ttl)

        self._auth_session_ttl = QSpinBox()
        self._auth_session_ttl.setRange(1, 43200)
        self._auth_session_ttl.setSuffix(" min")
        form.addRow("Session TTL:", self._auth_session_ttl)

        self._auth_req_per_hour = QSpinBox()
        self._auth_req_per_hour.setRange(1, 10000)
        form.addRow("Requests/hour/IP:", self._auth_req_per_hour)

        self._auth_req_per_min = QSpinBox()
        self._auth_req_per_min.setRange(1, 1000)
        form.addRow("Requests/min/user:", self._auth_req_per_min)

        lay.addWidget(grp)
        save_row, _ = _make_save_row(self._save_auth)
        lay.addLayout(save_row)
        lay.addStretch()
        return w

    def _save_auth(self) -> None:
        domains_raw = self._auth_domains.text().strip()
        domains = [d.strip() for d in domains_raw.replace(",", " ").split() if d.strip()]
        payload = {
            "auth": {
                "allowed_email_domains": domains,
                "magic_link_ttl_minutes": self._auth_link_ttl.value(),
                "session_ttl_minutes": self._auth_session_ttl.value(),
                "rate_limits": {
                    "requests_per_hour_per_ip": self._auth_req_per_hour.value(),
                    "requests_per_minute_per_user": self._auth_req_per_min.value(),
                },
            }
        }
        async def do():
            try:
                result = await self._client.admin_patch_config(payload)
                QMessageBox.information(self, "Saved", f"Auth config saved.\n{result.get('message','')}")
            except Exception as exc:
                QMessageBox.warning(self, "Save failed", str(exc))
        asyncio.ensure_future(do())

    async def _refresh_auth_config(self, cfg: dict) -> None:
        auth = cfg.get("auth", {})
        domains = auth.get("allowed_email_domains") or []
        self._auth_domains.setText(", ".join(domains))
        self._auth_link_ttl.setValue(int(auth.get("magic_link_ttl_minutes", 30)))
        self._auth_session_ttl.setValue(int(auth.get("session_ttl_minutes", 10080)))
        rl = auth.get("rate_limits", {})
        self._auth_req_per_hour.setValue(int(rl.get("requests_per_hour_per_ip", 20)))
        self._auth_req_per_min.setValue(int(rl.get("requests_per_minute_per_user", 60)))

    # ── Errors tab ─────────────────────────────────────────────────────────

    def _build_errors_tab(self) -> QWidget:
        self._errors_table = QTableWidget(0, 4)
        self._errors_table.setHorizontalHeaderLabels(["Time", "User", "Error", "Detail"])
        self._errors_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._errors_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._errors_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )

        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(lambda: asyncio.ensure_future(self._refresh_errors()))

        w = QWidget()
        lay = QVBoxLayout(w)
        bar = QHBoxLayout()
        bar.addWidget(refresh_btn)
        bar.addStretch()
        lay.addLayout(bar)
        lay.addWidget(self._errors_table)
        return w

    async def _refresh_errors(self) -> None:
        try:
            errors = await self._client.admin_errors()
        except Exception:
            errors = []
        self._errors_table.setRowCount(0)
        for row, e in enumerate(errors):
            self._errors_table.insertRow(row)
            ts = str(e.get("ts") or e.get("created_at") or "")[:19]
            self._errors_table.setItem(row, 0, QTableWidgetItem(ts))
            self._errors_table.setItem(row, 1, QTableWidgetItem(str(e.get("user_id") or "")))
            self._errors_table.setItem(row, 2, QTableWidgetItem(str(e.get("error_type") or e.get("error") or "")))
            self._errors_table.setItem(row, 3, QTableWidgetItem(str(e.get("detail") or "")[:200]))

    # ── Reload tab ─────────────────────────────────────────────────────────

    def _build_reload_tab(self) -> QWidget:
        label = QLabel(
            "Force-reload all config files from disk.\n"
            "Use this after manually editing a YAML config."
        )
        label.setWordWrap(True)

        reload_btn = QPushButton("Reload config from disk")
        reload_btn.clicked.connect(self._on_reload)

        self._reload_log = QPlainTextEdit()
        self._reload_log.setReadOnly(True)

        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(label)
        lay.addWidget(reload_btn)
        lay.addWidget(self._reload_log)
        return w

    def _on_reload(self) -> None:
        async def do():
            try:
                result = await self._client.admin_reload()
                self._reload_log.appendPlainText(f"✓ {result.get('message', 'Reloaded.')}")
            except Exception as exc:
                self._reload_log.appendPlainText(f"✗ {exc}")
        asyncio.ensure_future(do())

    # ── Refresh orchestration ──────────────────────────────────────────────

    async def _refresh_all(self) -> None:
        try:
            cfg = await self._client.admin_get_config()
        except Exception:
            cfg = {}

        await asyncio.gather(
            self._refresh_users(),
            self._refresh_models(),
            self._refresh_tools(),
            self._refresh_airgap(),
            self._refresh_vram(),
            self._refresh_router(cfg),
            self._refresh_auth_config(cfg),
            self._refresh_errors(),
        )
