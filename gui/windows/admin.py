"""Admin dashboard window.

First-pass: read-only view of tier status (from /api/resolved-models),
the tool registry, and airgap flag. Writes (rename a user, toggle
airgap, edit rate limits) wire onto /admin/* endpoints and are added
incrementally — parity with the Preact dashboard is explicitly a
follow-up.
"""

from __future__ import annotations

import asyncio

from PySide6.QtWidgets import (
    QHeaderView, QMainWindow, QTableWidget, QTableWidgetItem,
    QTabWidget, QVBoxLayout, QWidget,
)

from gui.api_client import BackendClient


class AdminWindow(QMainWindow):
    def __init__(self, client: BackendClient):
        super().__init__()
        self.setWindowTitle("Local AI Stack — Admin")
        self.resize(900, 600)
        self._client = client

        tabs = QTabWidget()
        tabs.addTab(self._build_models_tab(), "Models")
        tabs.addTab(self._build_tools_tab(), "Tools")
        tabs.addTab(self._build_vram_tab(), "VRAM")

        root = QVBoxLayout()
        root.addWidget(tabs)
        container = QWidget()
        container.setLayout(root)
        self.setCentralWidget(container)

        asyncio.ensure_future(self._refresh())

    # ── Tabs ──────────────────────────────────────────────────────

    def _build_models_tab(self) -> QWidget:
        self._models_table = QTableWidget(0, 5)
        self._models_table.setHorizontalHeaderLabels(
            ["Tier", "Source", "Identifier", "Origin", "Update?"]
        )
        self._models_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        w = QWidget()
        lay = QVBoxLayout()
        lay.addWidget(self._models_table)
        w.setLayout(lay)
        return w

    def _build_tools_tab(self) -> QWidget:
        self._tools_table = QTableWidget(0, 2)
        self._tools_table.setHorizontalHeaderLabels(["Name", "Enabled"])
        w = QWidget()
        lay = QVBoxLayout()
        lay.addWidget(self._tools_table)
        w.setLayout(lay)
        return w

    def _build_vram_tab(self) -> QWidget:
        self._vram_table = QTableWidget(0, 3)
        self._vram_table.setHorizontalHeaderLabels(["Tier", "Loaded", "Estimated GB"])
        w = QWidget()
        lay = QVBoxLayout()
        lay.addWidget(self._vram_table)
        w.setLayout(lay)
        return w

    # ── Data refresh ──────────────────────────────────────────────

    async def _refresh(self) -> None:
        await asyncio.gather(
            self._refresh_models(),
            self._refresh_vram(),
        )

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
