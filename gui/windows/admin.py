"""Admin dashboard window — full write-parity with deleted Preact admin panel.

Tabs: Users, Models, Tools, Airgap, VRAM, Router, Auth, Errors, Reload.
Requires an admin session; callers show LoginDialog(require_admin=True) first.
"""
from __future__ import annotations

import asyncio

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QFormLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QSpinBox, QDoubleSpinBox, QTableWidget, QTableWidgetItem, QTabWidget,
    QTextEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from gui.api_client import BackendClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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

        asyncio.ensure_future(self._refresh_all())

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
