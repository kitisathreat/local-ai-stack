"""
Health-check diagnostics window.

Spawned by tests/local_health.py after the suite finishes:
    pythonw diagnostics.py --log <path-to-health-YYYYMMDD-HHmmss.log>

Shows a QTreeWidget with one row per test result (PASS/WARN/FAIL/SKIP).
Selecting a FAIL/WARN row shows detail + fix_hint in a panel below.
Buttons: Auto-fix selected, Re-run suite, Save log.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QStatusBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)


# ---------------------------------------------------------------------------
# Status colours
# ---------------------------------------------------------------------------

_COLOURS = {
    "PASS": QColor("#2ecc71"),
    "WARN": QColor("#f39c12"),
    "FAIL": QColor("#e74c3c"),
    "SKIP": QColor("#95a5a6"),
}
_ICONS = {"PASS": "✓", "WARN": "!", "FAIL": "✗", "SKIP": "–"}


# ---------------------------------------------------------------------------
# Re-run worker thread
# ---------------------------------------------------------------------------

class _RunnerThread(QThread):
    finished = Signal(list)  # list[dict]

    def __init__(self, areas: list[str] | None = None, fix: bool = False):
        super().__init__()
        self._areas = areas
        self._fix = fix

    def run(self) -> None:
        try:
            repo = pathlib.Path(__file__).resolve().parents[2]
            sys.path.insert(0, str(repo))
            from tests.local_health import run_suite, write_log, _log_path
            results = run_suite(areas=self._areas, fix=self._fix)
            log_path = _log_path()
            write_log(results, log_path)
            self.finished.emit(results)
        except Exception as e:
            self.finished.emit([
                {"area": "?", "test": "runner_error", "status": "FAIL",
                 "detail": str(e), "fix_hint": "Check console output"}
            ])


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class DiagnosticsWindow(QMainWindow):
    def __init__(self, results: list[dict], log_path: pathlib.Path | None = None):
        super().__init__()
        self._results = results
        self._log_path = log_path
        self._runner: _RunnerThread | None = None

        self.setWindowTitle("Local AI Stack — Health Check")
        self.resize(900, 600)
        self._build_ui()
        self._populate(results)

    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)

        splitter = QSplitter(Qt.Vertical)
        root.addWidget(splitter)

        # ── Tree ──────────────────────────────────────────────────────
        self._tree = QTreeWidget()
        self._tree.setColumnCount(4)
        self._tree.setHeaderLabels(["Status", "Area", "Test", "Detail"])
        self._tree.setColumnWidth(0, 60)
        self._tree.setColumnWidth(1, 50)
        self._tree.setColumnWidth(2, 260)
        self._tree.setColumnWidth(3, 400)
        self._tree.setAlternatingRowColors(True)
        self._tree.currentItemChanged.connect(self._on_selection)
        splitter.addWidget(self._tree)

        # ── Detail panel ──────────────────────────────────────────────
        detail_group = QGroupBox("Details")
        detail_layout = QVBoxLayout(detail_group)

        self._detail_label = QLabel("Select a row above to see details.")
        self._detail_label.setWordWrap(True)
        detail_layout.addWidget(self._detail_label)

        self._fix_hint = QLabel("")
        self._fix_hint.setWordWrap(True)
        self._fix_hint.setStyleSheet("color: #e67e22; font-weight: bold;")
        detail_layout.addWidget(self._fix_hint)

        splitter.addWidget(detail_group)
        splitter.setSizes([400, 150])

        # ── Buttons ───────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        root.addLayout(btn_row)

        self._fix_btn = QPushButton("Auto-fix selected")
        self._fix_btn.setEnabled(False)
        self._fix_btn.clicked.connect(self._auto_fix)
        btn_row.addWidget(self._fix_btn)

        rerun_btn = QPushButton("Re-run all")
        rerun_btn.clicked.connect(self._rerun)
        btn_row.addWidget(rerun_btn)

        save_btn = QPushButton("Save log…")
        save_btn.clicked.connect(self._save_log)
        btn_row.addWidget(save_btn)

        btn_row.addStretch()

        # Status bar
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)

    # ------------------------------------------------------------------
    def _populate(self, results: list[dict]) -> None:
        self._tree.clear()
        counts = {"PASS": 0, "WARN": 0, "FAIL": 0, "SKIP": 0}
        for r in results:
            s = r.get("status", "FAIL")
            counts[s] = counts.get(s, 0) + 1
            icon = _ICONS.get(s, "?")
            item = QTreeWidgetItem([
                f"{icon} {s}",
                r.get("area", ""),
                r.get("test", ""),
                r.get("detail", "")[:120],
            ])
            colour = _COLOURS.get(s, QColor("white"))
            for col in range(4):
                item.setForeground(col, colour)
            item.setData(0, Qt.UserRole, r)  # store full result for detail panel
            self._tree.addTopLevelItem(item)

        self._status_bar.showMessage(
            f"  {counts['PASS']} PASS   {counts['WARN']} WARN   "
            f"{counts['FAIL']} FAIL   {counts['SKIP']} SKIP"
        )

    # ------------------------------------------------------------------
    def _on_selection(self, current: QTreeWidgetItem | None, _prev) -> None:
        if current is None:
            self._detail_label.setText("Select a row above to see details.")
            self._fix_hint.setText("")
            self._fix_btn.setEnabled(False)
            return
        r: dict = current.data(0, Qt.UserRole)
        self._detail_label.setText(r.get("detail", ""))
        hint = r.get("fix_hint", "")
        self._fix_hint.setText(f"Fix: {hint}" if hint else "")
        self._fix_btn.setEnabled(r.get("status") in ("FAIL", "WARN") and bool(hint))

    # ------------------------------------------------------------------
    def _auto_fix(self) -> None:
        item = self._tree.currentItem()
        if item is None:
            return
        r: dict = item.data(0, Qt.UserRole)
        hint = r.get("fix_hint", "")
        if not hint:
            return
        reply = QMessageBox.question(
            self, "Auto-fix",
            f"Attempt fix for '{r['test']}'?\n\n{hint}",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        # Only run safe, known commands (those in _FIX_ACTIONS)
        try:
            repo = pathlib.Path(__file__).resolve().parents[2]
            sys.path.insert(0, str(repo))
            from tests.local_health import _FIX_ACTIONS, _attempt_fix
            result = _attempt_fix(r)
            if result:
                self._status_bar.showMessage(f"Auto-fix: {result}", 5000)
            else:
                QMessageBox.information(
                    self, "Manual action required",
                    f"No automated fix available.\n\n{hint}"
                )
        except Exception as e:
            QMessageBox.warning(self, "Fix error", str(e))

    # ------------------------------------------------------------------
    def _rerun(self) -> None:
        if self._runner and self._runner.isRunning():
            return
        self._status_bar.showMessage("Re-running health checks…")
        self._runner = _RunnerThread()
        self._runner.finished.connect(self._on_rerun_done)
        self._runner.start()

    def _on_rerun_done(self, results: list[dict]) -> None:
        self._results = results
        self._populate(results)

    # ------------------------------------------------------------------
    def _save_log(self) -> None:
        default = str(self._log_path) if self._log_path else "health.log"
        dest, _ = QFileDialog.getSaveFileName(
            self, "Save log", default, "Log files (*.log);;All files (*)"
        )
        if not dest:
            return
        try:
            import shutil
            if self._log_path and self._log_path.exists():
                shutil.copy2(self._log_path, dest)
            else:
                # Rewrite from in-memory results
                with open(dest, "w", encoding="utf-8") as f:
                    import datetime
                    for r in self._results:
                        row = {"ts": datetime.datetime.now().isoformat(timespec="seconds"), **r}
                        f.write(json.dumps(row, ensure_ascii=False) + "\n")
            self._status_bar.showMessage(f"Saved: {dest}", 4000)
        except Exception as e:
            QMessageBox.warning(self, "Save error", str(e))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", help="Path to a health-<ts>.log file to display")
    args = parser.parse_args()

    results: list[dict] = []
    log_path: pathlib.Path | None = None

    if args.log:
        log_path = pathlib.Path(args.log)
        if log_path.exists():
            for line in log_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    try:
                        results.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass

    app = QApplication(sys.argv)
    app.setApplicationName("Local AI Stack Diagnostics")
    win = DiagnosticsWindow(results, log_path=log_path)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
