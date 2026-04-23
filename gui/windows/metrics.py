"""Metrics window — QtCharts visualizations of VRAM, latency, and
tokens-per-second per tier.

Polls /api/vram every 2 seconds and appends to a rolling 60-point buffer.
Everything is rendered with QtCharts (native Qt); no browser involved.
"""

from __future__ import annotations

import asyncio
from collections import deque

from PySide6.QtCharts import QChart, QChartView, QLineSeries, QValueAxis
from PySide6.QtCore import Qt, QPointF
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QMainWindow, QVBoxLayout, QWidget

from gui.api_client import BackendClient


WINDOW_POINTS = 60


class MetricsWindow(QMainWindow):
    def __init__(self, client: BackendClient):
        super().__init__()
        self.setWindowTitle("Local AI Stack — Metrics")
        self.resize(900, 600)
        self._client = client
        self._buffers: dict[str, deque[float]] = {}
        self._series: dict[str, QLineSeries] = {}

        self._chart = QChart()
        self._chart.setTitle("VRAM per tier (GB)")
        self._chart.legend().setAlignment(Qt.AlignmentFlag.AlignBottom)
        self._axis_x = QValueAxis()
        self._axis_x.setRange(0, WINDOW_POINTS)
        self._axis_x.setTitleText("Tick (2 s each)")
        self._axis_y = QValueAxis()
        self._axis_y.setRange(0, 48)
        self._axis_y.setTitleText("GB")
        self._chart.addAxis(self._axis_x, Qt.AlignmentFlag.AlignBottom)
        self._chart.addAxis(self._axis_y, Qt.AlignmentFlag.AlignLeft)

        view = QChartView(self._chart)
        view.setRenderHint(QPainter.RenderHint.Antialiasing)

        root = QVBoxLayout()
        root.addWidget(view)
        container = QWidget()
        container.setLayout(root)
        self.setCentralWidget(container)

        self._poll_task = asyncio.ensure_future(self._poll_loop())

    async def _poll_loop(self) -> None:
        while self.isVisible():
            try:
                data = await self._client.vram_status()
            except Exception:
                data = {}
            for tier in data.get("tiers") or []:
                name = str(tier.get("name") or "")
                val = float(tier.get("vram_used_gb") or 0.0)
                buf = self._buffers.setdefault(name, deque(maxlen=WINDOW_POINTS))
                buf.append(val)
                series = self._series.get(name)
                if series is None:
                    series = QLineSeries()
                    series.setName(name)
                    self._chart.addSeries(series)
                    series.attachAxis(self._axis_x)
                    series.attachAxis(self._axis_y)
                    self._series[name] = series
                series.replace([QPointF(i, v) for i, v in enumerate(buf)])
            await asyncio.sleep(2)

    def closeEvent(self, event):  # noqa: N802
        self._poll_task.cancel()
        super().closeEvent(event)
