"""Admin dashboard window — full write-parity with deleted Preact admin panel.

Tabs: Users, Models, Tools, Marketplaces, Airgap, VRAM, Router, Auth, Errors, Reload.
Requires an admin session; callers show LoginDialog(require_admin=True) first.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import string

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QSpinBox, QDoubleSpinBox, QSplitter, QTableWidget,
    QTableWidgetItem, QTabWidget, QTextEdit, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from gui.api_client import BackendClient


# Recipe templates for the Marketplaces tab — kept in sync with
# free_games.recipe_templates(). The names are display labels; the
# `config` is the JSON the user pre-fills the form with.
_RECIPE_TEMPLATES: list[tuple[str, dict]] = [
    (
        "WordPress (entry-title pattern)",
        {
            "name": "Example WordPress",
            "search_url": "https://example.com/?s={query}",
            "result_pattern": (
                r'<h2[^>]*class="[^"]*entry-title[^"]*"[^>]*>'
                r'\s*<a[^>]+href="([^"]+)"[^>]*>([^<]+)</a>'
            ),
            "download_pattern": (
                r'href="(https?://[^"]+\.'
                r'(?:zip|7z|rar|iso|exe|dmg|tar(?:\.[gx]z)?|tgz))"'
            ),
        },
    ),
    (
        "Generic article cards (h2/h3 → a)",
        {
            "name": "Example",
            "search_url": "https://example.com/search?q={query}",
            "result_pattern": (
                r'<(?:h2|h3)[^>]*>\s*<a[^>]+href="([^"]+)"'
                r'[^>]*>([^<]+)</a>'
            ),
            "download_pattern": "",
        },
    ),
    (
        "Game-style listing (figure/card → title)",
        {
            "name": "Example",
            "search_url": "https://example.com/?s={query}",
            "result_pattern": (
                r'<a[^>]+href="(/games?/[^"]+)"[^>]*>'
                r'\s*(?:<img[^>]*>)?\s*'
                r'<(?:span|div|h\d)[^>]*>([^<]+)</'
            ),
            "download_pattern": "",
        },
    ),
    (
        "Discourse forum (search results)",
        {
            "name": "Example Discourse",
            "search_url": "https://forum.example.com/search?q={query}",
            "result_pattern": (
                r'<a[^>]+class="[^"]*search-link[^"]*"'
                r'[^>]+href="([^"]+)"[^>]*>([^<]+)</a>'
            ),
            "download_pattern": "",
        },
    ),
    (
        "Magnet-link listing (TPB-style)",
        {
            "name": "Example tracker",
            "search_url": "https://example.com/search?q={query}",
            "result_pattern": (
                r'<a[^>]+href="(/torrent/\d+/[^"]+)"'
                r'[^>]*>([^<]+)</a>'
            ),
            "download_pattern": (
                r'(magnet:\?xt=urn:btih:[a-fA-F0-9]+[^"\']*)'
            ),
        },
    ),
]


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
        tabs.addTab(self._build_marketplaces_tab(), "Marketplaces")
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
    #
    # Tree shape: Tier → Topical group → Subgroup → Tool
    # Every non-leaf node has its own checkbox. Toggling a non-leaf flips
    # every descendant in one bulk PATCH /admin/tools call.

    def _build_tools_tab(self) -> QWidget:
        self._tools_tree = QTreeWidget()
        self._tools_tree.setHeaderLabels(["Tool", "Description"])
        self._tools_tree.setColumnCount(2)
        self._tools_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self._tools_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._tools_tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        # Suppress checkbox-driven recursion while we sync state programmatically.
        self._suppress_tool_change = False
        self._tools_tree.itemChanged.connect(self._on_tools_item_changed)

        # Search filter.
        self._tools_filter = QLineEdit()
        self._tools_filter.setPlaceholderText("Filter tools…")
        self._tools_filter.textChanged.connect(self._apply_tools_filter)

        w = QWidget()
        lay = QVBoxLayout(w)
        hint = QLabel(
            "Toggle a tool's checkbox to enable or disable it. "
            "Use the tier / group / subgroup checkboxes to flip everything beneath at once."
        )
        hint.setStyleSheet("color: #888;")
        lay.addWidget(hint)
        lay.addWidget(self._tools_filter)
        lay.addWidget(self._tools_tree)
        return w

    # Each tree node carries metadata in Qt's UserRole.
    # Leaf:  {"kind": "tool", "name": "..."}
    # Inner: {"kind": "group" | "subgroup" | "tier", "tier": ..., "group": ..., "subgroup": ..., "names": [...]}
    _ROLE_META = Qt.ItemDataRole.UserRole

    async def _refresh_tools(self) -> None:
        try:
            payload = await self._client.admin_tools()
        except Exception:
            payload = {"data": [], "groups": []}
        groups = payload.get("groups", []) or []
        data = {t["name"]: t for t in (payload.get("data") or [])}

        self._suppress_tool_change = True
        try:
            self._tools_tree.clear()
            for tier in groups:
                tier_names = [n for g in tier["groups"]
                              for s in g["subgroups"] for n in s["tools"]]
                tier_item = QTreeWidgetItem(self._tools_tree)
                tier_item.setText(0, f"{tier['title']} ({len(tier_names)})")
                tier_item.setFlags(tier_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                tier_item.setData(0, self._ROLE_META, {
                    "kind": "tier", "tier": tier["tier"], "names": tier_names,
                })

                for group in tier["groups"]:
                    group_names = [n for s in group["subgroups"] for n in s["tools"]]
                    group_item = QTreeWidgetItem(tier_item)
                    group_item.setText(0, f"{group['title']} ({len(group_names)})")
                    group_item.setFlags(group_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    group_item.setData(0, self._ROLE_META, {
                        "kind": "group", "tier": tier["tier"],
                        "group": group["group"], "names": group_names,
                    })

                    for sub in group["subgroups"]:
                        sub_item = QTreeWidgetItem(group_item)
                        sub_item.setText(0, f"{sub['title']} ({len(sub['tools'])})")
                        sub_item.setFlags(sub_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                        sub_item.setData(0, self._ROLE_META, {
                            "kind": "subgroup", "tier": tier["tier"],
                            "group": group["group"], "subgroup": sub["subgroup"],
                            "names": list(sub["tools"]),
                        })

                        for name in sub["tools"]:
                            t = data.get(name, {})
                            leaf = QTreeWidgetItem(sub_item)
                            leaf.setText(0, name)
                            leaf.setText(1, t.get("description", "")[:160])
                            leaf.setFlags(leaf.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                            leaf.setData(0, self._ROLE_META, {"kind": "tool", "name": name})
                            leaf.setCheckState(
                                0,
                                Qt.CheckState.Checked if t.get("enabled")
                                else Qt.CheckState.Unchecked,
                            )

                self._refresh_aggregate_state(tier_item)
                tier_item.setExpanded(False)
        finally:
            self._suppress_tool_change = False
        self._apply_tools_filter(self._tools_filter.text())

    def _refresh_aggregate_state(self, item: QTreeWidgetItem) -> None:
        """Walk children to set tristate (Checked / Unchecked / PartiallyChecked) on `item`."""
        meta = item.data(0, self._ROLE_META) or {}
        if meta.get("kind") == "tool":
            return
        on = total = 0
        for i in range(item.childCount()):
            child = item.child(i)
            self._refresh_aggregate_state(child)
            cm = child.data(0, self._ROLE_META) or {}
            if cm.get("kind") == "tool":
                total += 1
                if child.checkState(0) == Qt.CheckState.Checked:
                    on += 1
            else:
                cnames = cm.get("names", [])
                total += len(cnames)
                if child.checkState(0) == Qt.CheckState.Checked:
                    on += len(cnames)
                elif child.checkState(0) == Qt.CheckState.PartiallyChecked:
                    on += 1   # any non-zero — exact value doesn't matter for tristate logic
        if total == 0 or on == 0:
            state = Qt.CheckState.Unchecked
        elif on >= total:
            state = Qt.CheckState.Checked
        else:
            state = Qt.CheckState.PartiallyChecked
        item.setCheckState(0, state)

    def _on_tools_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._suppress_tool_change or column != 0:
            return
        meta = item.data(0, self._ROLE_META) or {}
        kind = meta.get("kind")
        enabled = item.checkState(0) == Qt.CheckState.Checked

        if kind == "tool":
            async def do_one():
                try:
                    await self._client.admin_set_tool_enabled(meta["name"], enabled)
                except Exception as exc:
                    QMessageBox.warning(self, "Failed", str(exc))
                    await self._refresh_tools()
                    return
                # Re-aggregate ancestors locally without a full refresh.
                self._suppress_tool_change = True
                try:
                    p = item.parent()
                    while p is not None:
                        self._refresh_aggregate_state(p)
                        p = p.parent()
                finally:
                    self._suppress_tool_change = False
            asyncio.ensure_future(do_one())
            return

        # Group / subgroup / tier — bulk PATCH.
        async def do_bulk():
            try:
                kwargs: dict = {}
                if kind == "tier":
                    kwargs["tier"] = meta["tier"]
                elif kind == "group":
                    kwargs["tier"] = meta["tier"]
                    kwargs["group"] = meta["group"]
                elif kind == "subgroup":
                    kwargs["tier"] = meta["tier"]
                    kwargs["group"] = meta["group"]
                    kwargs["subgroup"] = meta["subgroup"]
                await self._client.admin_bulk_set_tools(enabled=enabled, **kwargs)
            except Exception as exc:
                QMessageBox.warning(self, "Failed", str(exc))
            await self._refresh_tools()
        asyncio.ensure_future(do_bulk())

    def _apply_tools_filter(self, text: str) -> None:
        q = (text or "").lower().strip()

        def visit(item: QTreeWidgetItem) -> bool:
            """Return True if any descendant matches the filter."""
            meta = item.data(0, self._ROLE_META) or {}
            if meta.get("kind") == "tool":
                match = (not q) or q in meta["name"].lower()
                item.setHidden(not match)
                return match
            any_match = False
            for i in range(item.childCount()):
                if visit(item.child(i)):
                    any_match = True
            item.setHidden(not any_match)
            if q and any_match:
                item.setExpanded(True)
            return any_match

        for i in range(self._tools_tree.topLevelItemCount()):
            visit(self._tools_tree.topLevelItem(i))

    # ── Marketplaces tab ───────────────────────────────────────────────────
    #
    # Compiles the free_games marketplace workflow into a UI:
    #   1. Pick a recipe template → fields prefill
    #   2. Edit name / search_url / result_pattern / download_pattern
    #   3. "Test config" → server runs probe, dumps diagnostic into the
    #      report pane (status, blocker hints, regex match count, sample
    #      matches, download-extraction probe)
    #   4. "Save" → appends/replaces in the MARKETPLACES valve
    #   5. Saved list on the right; per-row probe/delete
    #   6. "Probe download URL" → HEAD-checks any URL before flipping
    #      WRITE_ENABLED
    #   7. DOWNLOAD_DIR + WRITE_ENABLED + REQUEST_HEADERS + USER_AGENT
    #      editable inline, saved via PATCH /admin/marketplaces/valves

    def _build_marketplaces_tab(self) -> QWidget:
        outer = QSplitter(Qt.Orientation.Horizontal)

        # ── Left: editor + probe + valves ─────────────────────────────
        left = QWidget()
        L = QVBoxLayout(left)

        # Recipe picker
        recipe_row = QHBoxLayout()
        recipe_row.addWidget(QLabel("Recipe:"))
        self._mp_recipe = QComboBox()
        self._mp_recipe.addItem("(blank)", userData=None)
        for label, cfg in _RECIPE_TEMPLATES:
            self._mp_recipe.addItem(label, userData=cfg)
        self._mp_recipe.currentIndexChanged.connect(self._on_recipe_pick)
        recipe_row.addWidget(self._mp_recipe, 1)
        L.addLayout(recipe_row)

        # Editor form
        form_box = QGroupBox("Marketplace config")
        form = QFormLayout(form_box)
        self._mp_name = QLineEdit()
        self._mp_search_url = QLineEdit()
        self._mp_search_url.setPlaceholderText("https://example.com/?s={query}")
        self._mp_result_pat = QPlainTextEdit()
        self._mp_result_pat.setPlaceholderText(
            "regex with two capture groups: 1=item URL, 2=item title"
        )
        self._mp_result_pat.setMaximumHeight(80)
        self._mp_download_pat = QPlainTextEdit()
        self._mp_download_pat.setPlaceholderText(
            "(optional) regex; one capture group = direct download URL or magnet"
        )
        self._mp_download_pat.setMaximumHeight(80)
        self._mp_test_query = QLineEdit("test")
        form.addRow("Name", self._mp_name)
        form.addRow("Search URL", self._mp_search_url)
        form.addRow("Result pattern", self._mp_result_pat)
        form.addRow("Download pattern", self._mp_download_pat)
        form.addRow("Test query", self._mp_test_query)
        L.addWidget(form_box)

        # Action buttons
        btn_row = QHBoxLayout()
        self._mp_btn_test = QPushButton("Test config")
        self._mp_btn_test.clicked.connect(self._on_mp_test)
        self._mp_btn_save = QPushButton("Save")
        self._mp_btn_save.clicked.connect(self._on_mp_save)
        self._mp_btn_clear = QPushButton("Clear")
        self._mp_btn_clear.clicked.connect(self._on_mp_clear)
        btn_row.addWidget(self._mp_btn_test)
        btn_row.addWidget(self._mp_btn_save)
        btn_row.addWidget(self._mp_btn_clear)
        btn_row.addStretch(1)
        L.addLayout(btn_row)

        # Probe-download standalone
        dl_box = QGroupBox("Probe download URL  (HEAD only — no write)")
        dl_lay = QHBoxLayout(dl_box)
        self._mp_dl_url = QLineEdit()
        self._mp_dl_url.setPlaceholderText("https://example.com/file.zip or magnet:?xt=…")
        self._mp_btn_probe_dl = QPushButton("Probe")
        self._mp_btn_probe_dl.clicked.connect(self._on_mp_probe_download)
        dl_lay.addWidget(self._mp_dl_url, 1)
        dl_lay.addWidget(self._mp_btn_probe_dl)
        L.addWidget(dl_box)

        # Valves
        valves_box = QGroupBox("Valves")
        v_form = QFormLayout(valves_box)
        self._mp_dir = QLineEdit()
        self._mp_write = QCheckBox("WRITE_ENABLED  (let download() write to disk)")
        self._mp_ua = QLineEdit()
        self._mp_headers = QPlainTextEdit()
        self._mp_headers.setMaximumHeight(70)
        self._mp_headers.setPlaceholderText('JSON object, e.g. {"Cookie":"...","Referer":"..."}')
        self._mp_btn_save_valves = QPushButton("Save valves")
        self._mp_btn_save_valves.clicked.connect(self._on_mp_save_valves)
        v_form.addRow("DOWNLOAD_DIR", self._mp_dir)
        v_form.addRow("", self._mp_write)
        v_form.addRow("User-Agent", self._mp_ua)
        v_form.addRow("REQUEST_HEADERS", self._mp_headers)
        v_form.addRow("", self._mp_btn_save_valves)
        L.addWidget(valves_box)

        L.addStretch(1)
        outer.addWidget(left)

        # ── Right: saved list + report pane ───────────────────────────
        right = QWidget()
        R = QVBoxLayout(right)

        saved_box = QGroupBox("Saved marketplaces")
        sb = QVBoxLayout(saved_box)
        self._mp_table = QTableWidget(0, 4)
        self._mp_table.setHorizontalHeaderLabels(["Name", "Search URL", "DL pattern?", ""])
        self._mp_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._mp_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._mp_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self._mp_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        sb.addWidget(self._mp_table)
        sb_btn_row = QHBoxLayout()
        self._mp_btn_refresh = QPushButton("Refresh")
        self._mp_btn_refresh.clicked.connect(
            lambda: asyncio.ensure_future(self._refresh_marketplaces())
        )
        self._mp_btn_load = QPushButton("Load selected → editor")
        self._mp_btn_load.clicked.connect(self._on_mp_load_selected)
        self._mp_btn_probe = QPushButton("Probe selected")
        self._mp_btn_probe.clicked.connect(self._on_mp_probe_selected)
        self._mp_btn_delete = QPushButton("Delete selected")
        self._mp_btn_delete.clicked.connect(self._on_mp_delete_selected)
        for b in (
            self._mp_btn_refresh,
            self._mp_btn_load,
            self._mp_btn_probe,
            self._mp_btn_delete,
        ):
            sb_btn_row.addWidget(b)
        sb_btn_row.addStretch(1)
        sb.addLayout(sb_btn_row)
        R.addWidget(saved_box)

        report_box = QGroupBox("Report")
        rb = QVBoxLayout(report_box)
        self._mp_report = QPlainTextEdit()
        self._mp_report.setReadOnly(True)
        self._mp_report.setPlaceholderText(
            "Diagnostic output goes here.  Workflow:\n"
            "  1. Pick a recipe → fields prefill\n"
            "  2. Edit the URL + regex for your target site\n"
            "  3. Test config (the server runs a probe + reports status,\n"
            "     blocker detection, regex match count, sample matches)\n"
            "  4. Save when ✅\n"
            "  5. Use 'Probe download URL' before flipping WRITE_ENABLED"
        )
        rb.addWidget(self._mp_report)
        R.addWidget(report_box, 1)

        outer.addWidget(right)
        outer.setStretchFactor(0, 1)
        outer.setStretchFactor(1, 1)

        wrap = QWidget()
        QVBoxLayout(wrap).addWidget(outer)
        return wrap

    # ── Marketplaces tab — handlers ────────────────────────────────────────

    def _on_recipe_pick(self, idx: int) -> None:
        cfg = self._mp_recipe.itemData(idx)
        if not cfg:
            return
        self._mp_name.setText(cfg.get("name", ""))
        self._mp_search_url.setText(cfg.get("search_url", ""))
        self._mp_result_pat.setPlainText(cfg.get("result_pattern", ""))
        self._mp_download_pat.setPlainText(cfg.get("download_pattern", ""))

    def _on_mp_clear(self) -> None:
        self._mp_name.clear()
        self._mp_search_url.clear()
        self._mp_result_pat.clear()
        self._mp_download_pat.clear()
        self._mp_recipe.setCurrentIndex(0)

    def _editor_config(self) -> dict | None:
        name = self._mp_name.text().strip()
        url = self._mp_search_url.text().strip()
        rp = self._mp_result_pat.toPlainText().strip()
        dp = self._mp_download_pat.toPlainText().strip()
        if not name or not url or not rp:
            QMessageBox.warning(
                self,
                "Missing fields",
                "name, search_url, and result_pattern are all required.",
            )
            return None
        cfg = {"name": name, "search_url": url, "result_pattern": rp}
        if dp:
            cfg["download_pattern"] = dp
        return cfg

    def _on_mp_test(self) -> None:
        cfg = self._editor_config()
        if not cfg:
            return
        query = self._mp_test_query.text().strip() or "test"
        self._mp_report.setPlainText(f"Testing '{cfg['name']}' with query='{query}'…\n")

        async def do():
            try:
                res = await self._client.admin_marketplace_test(cfg, query=query)
            except Exception as e:
                self._mp_report.setPlainText(f"Test failed: {e}")
                return
            self._mp_report.setPlainText(res.get("markdown", "(empty)"))
        asyncio.ensure_future(do())

    def _on_mp_save(self) -> None:
        cfg = self._editor_config()
        if not cfg:
            return

        async def do():
            try:
                res = await self._client.admin_marketplace_save(cfg)
            except Exception as e:
                QMessageBox.warning(self, "Save failed", str(e))
                return
            verb = "replaced" if res.get("replaced") else "added"
            self._mp_report.setPlainText(
                f"✅ {verb} marketplace '{cfg['name']}'.  Total saved: {res.get('count')}"
            )
            await self._refresh_marketplaces()
        asyncio.ensure_future(do())

    def _on_mp_probe_download(self) -> None:
        url = self._mp_dl_url.text().strip()
        if not url:
            QMessageBox.warning(self, "Missing URL", "Paste a download URL first.")
            return
        self._mp_report.setPlainText(f"Probing {url}…\n")

        async def do():
            try:
                res = await self._client.admin_marketplace_probe_download(url)
            except Exception as e:
                self._mp_report.setPlainText(f"Probe failed: {e}")
                return
            self._mp_report.setPlainText(res.get("markdown", "(empty)"))
        asyncio.ensure_future(do())

    def _on_mp_save_valves(self) -> None:
        body: dict = {
            "download_dir": self._mp_dir.text().strip(),
            "write_enabled": self._mp_write.isChecked(),
            "user_agent": self._mp_ua.text().strip(),
            "request_headers": self._mp_headers.toPlainText().strip() or "{}",
        }
        # Validate JSON early so the user gets a clear error here, not later.
        try:
            json.loads(body["request_headers"])
        except json.JSONDecodeError as e:
            QMessageBox.warning(self, "REQUEST_HEADERS not valid JSON", str(e))
            return

        async def do():
            try:
                await self._client.admin_marketplace_patch_valves(**body)
            except Exception as e:
                QMessageBox.warning(self, "Save valves failed", str(e))
                return
            self._mp_report.setPlainText("✅ Valves saved.")
        asyncio.ensure_future(do())

    def _selected_marketplace_name(self) -> str | None:
        row = self._mp_table.currentRow()
        if row < 0:
            return None
        item = self._mp_table.item(row, 0)
        return item.text() if item else None

    def _on_mp_load_selected(self) -> None:
        name = self._selected_marketplace_name()
        if not name:
            return

        async def do():
            try:
                payload = await self._client.admin_marketplaces()
            except Exception as e:
                self._mp_report.setPlainText(f"Refresh failed: {e}")
                return
            cfg = next(
                (m for m in payload.get("marketplaces", []) if m.get("name", "").lower() == name.lower()),
                None,
            )
            if not cfg:
                self._mp_report.setPlainText(f"No saved marketplace '{name}'.")
                return
            self._mp_name.setText(cfg.get("name", ""))
            self._mp_search_url.setText(cfg.get("search_url", ""))
            self._mp_result_pat.setPlainText(cfg.get("result_pattern", ""))
            self._mp_download_pat.setPlainText(cfg.get("download_pattern", ""))
            self._mp_report.setPlainText(f"Loaded '{name}' into the editor.")
        asyncio.ensure_future(do())

    def _on_mp_probe_selected(self) -> None:
        name = self._selected_marketplace_name()
        if not name:
            return
        query = self._mp_test_query.text().strip() or "test"
        self._mp_report.setPlainText(f"Probing '{name}' with query='{query}'…\n")

        async def do():
            try:
                res = await self._client.admin_marketplace_probe(name, query)
            except Exception as e:
                self._mp_report.setPlainText(f"Probe failed: {e}")
                return
            self._mp_report.setPlainText(res.get("markdown", "(empty)"))
        asyncio.ensure_future(do())

    def _on_mp_delete_selected(self) -> None:
        name = self._selected_marketplace_name()
        if not name:
            return
        if QMessageBox.question(
            self,
            "Delete?",
            f"Remove marketplace '{name}' from the MARKETPLACES valve?",
        ) != QMessageBox.StandardButton.Yes:
            return

        async def do():
            try:
                await self._client.admin_marketplace_delete(name)
            except Exception as e:
                QMessageBox.warning(self, "Delete failed", str(e))
                return
            await self._refresh_marketplaces()
            self._mp_report.setPlainText(f"Deleted '{name}'.")
        asyncio.ensure_future(do())

    async def _refresh_marketplaces(self) -> None:
        try:
            payload = await self._client.admin_marketplaces()
        except Exception as e:
            self._mp_report.setPlainText(f"Refresh failed: {e}")
            return
        # Valve fields
        self._mp_dir.setText(payload.get("download_dir", ""))
        self._mp_write.setChecked(bool(payload.get("write_enabled")))
        self._mp_ua.setText(payload.get("user_agent", ""))
        rh = payload.get("request_headers") or "{}"
        # Pretty-print if it parses as JSON.
        try:
            self._mp_headers.setPlainText(
                json.dumps(json.loads(rh), indent=2) if rh.strip() else "{}"
            )
        except Exception:
            self._mp_headers.setPlainText(rh)

        entries = payload.get("marketplaces", [])
        self._mp_table.setRowCount(len(entries))
        for row, e in enumerate(entries):
            self._mp_table.setItem(row, 0, QTableWidgetItem(e.get("name", "")))
            self._mp_table.setItem(row, 1, QTableWidgetItem(e.get("search_url", "")))
            self._mp_table.setItem(
                row, 2, QTableWidgetItem("yes" if e.get("download_pattern") else "no"),
            )
            self._mp_table.setItem(row, 3, QTableWidgetItem(""))

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
            self._refresh_marketplaces(),
            self._refresh_airgap(),
            self._refresh_vram(),
            self._refresh_router(cfg),
            self._refresh_auth_config(cfg),
            self._refresh_errors(),
            return_exceptions=True,
        )
