"""Hollow-ring status gauges for the chat window.

Renders two arc gauges side-by-side — context window usage and VRAM
usage — using the Okabe-Ito palette so the colours stay readable for
folks with deuteranopia/protanopia. Each ring fills clockwise from
12 o'clock and shifts hue as it crosses 50% / 75% / 90% thresholds:

    bluish-green  →  yellow  →  orange  →  vermillion

The widget is purely a view: callers push numbers in via
``set_context(used, total)`` and ``set_vram(used_gb, total_gb)``.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget


# Okabe-Ito (https://jfly.uni-koeln.de/color/) — colour-blind safe.
OKABE_ITO = {
    "black":          QColor("#000000"),
    "orange":         QColor("#E69F00"),
    "sky_blue":       QColor("#56B4E9"),
    "bluish_green":   QColor("#009E73"),
    "yellow":         QColor("#F0E442"),
    "blue":           QColor("#0072B2"),
    "vermillion":     QColor("#D55E00"),
    "reddish_purple": QColor("#CC79A7"),
}

TRACK_COLOR = QColor("#2a2a2a")
LABEL_COLOR = QColor("#cfcfcf")
SUBLABEL_COLOR = QColor("#888888")


def _fill_color(fraction: float) -> QColor:
    """Map a 0..1 fill ratio to an Okabe-Ito hue."""
    if fraction < 0.50:
        return OKABE_ITO["bluish_green"]
    if fraction < 0.75:
        return OKABE_ITO["yellow"]
    if fraction < 0.90:
        return OKABE_ITO["orange"]
    return OKABE_ITO["vermillion"]


@dataclass
class GaugeState:
    title: str
    fraction: float = 0.0     # 0..1
    primary: str = "—"        # big readout under the ring (e.g. "0 / 8K")
    secondary: str = ""       # small line under that (e.g. "tokens")


class StatusGauges(QWidget):
    """Two hollow rings: context window and VRAM."""

    RING_THICKNESS = 10
    RING_DIAMETER = 96

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._context = GaugeState(title="Context", primary="—", secondary="tokens")
        self._vram = GaugeState(title="VRAM", primary="—", secondary="GB")
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(self.RING_DIAMETER + 44)
        self.setToolTip(
            "Context window and VRAM usage. Colours: green < 50% · "
            "yellow < 75% · orange < 90% · vermillion ≥ 90% (Okabe-Ito)."
        )

    # ── Public setters ─────────────────────────────────────────────

    def set_context(self, used_tokens: int, total_tokens: int) -> None:
        if total_tokens <= 0:
            self._context.fraction = 0.0
            self._context.primary = f"{_humanize(used_tokens)} / —"
        else:
            self._context.fraction = max(0.0, min(1.0, used_tokens / total_tokens))
            self._context.primary = (
                f"{_humanize(used_tokens)} / {_humanize(total_tokens)}"
            )
        self._context.secondary = f"{int(round(self._context.fraction * 100))}% used"
        self.update()

    def set_vram(self, used_gb: float, total_gb: float) -> None:
        if total_gb <= 0:
            self._vram.fraction = 0.0
            self._vram.primary = "— / —"
            self._vram.secondary = "no GPU detected"
        else:
            self._vram.fraction = max(0.0, min(1.0, used_gb / total_gb))
            free_gb = max(0.0, total_gb - used_gb)
            self._vram.primary = f"{used_gb:.1f} / {total_gb:.1f} GB"
            self._vram.secondary = f"{free_gb:.1f} GB free"
        self.update()

    def clear(self) -> None:
        self._context = GaugeState(title="Context", primary="—", secondary="tokens")
        self._vram = GaugeState(title="VRAM", primary="—", secondary="GB")
        self.update()

    # ── Layout ─────────────────────────────────────────────────────

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(self.RING_DIAMETER * 2 + 80, self.RING_DIAMETER + 44)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        try:
            half = self.width() / 2
            self._draw_gauge(p, self._context, x_center=half / 2)
            self._draw_gauge(p, self._vram, x_center=half + half / 2)
        finally:
            p.end()

    def _draw_gauge(self, p: QPainter, state: GaugeState, x_center: float) -> None:
        d = self.RING_DIAMETER
        top = 4
        rect = QRectF(x_center - d / 2, top, d, d)

        # Track ring.
        track_pen = QPen(TRACK_COLOR)
        track_pen.setWidth(self.RING_THICKNESS)
        track_pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        p.setPen(track_pen)
        p.drawArc(rect, 0, 360 * 16)

        # Filled arc — start at 12 o'clock (90°), sweep clockwise (negative).
        if state.fraction > 0:
            fill_pen = QPen(_fill_color(state.fraction))
            fill_pen.setWidth(self.RING_THICKNESS)
            fill_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(fill_pen)
            span = -int(round(360 * 16 * state.fraction))
            p.drawArc(rect, 90 * 16, span)

        # Centre percentage.
        pct = f"{int(round(state.fraction * 100))}%"
        f_big = QFont(self.font())
        f_big.setPointSizeF(self.font().pointSizeF() + 3)
        f_big.setBold(True)
        p.setFont(f_big)
        p.setPen(LABEL_COLOR)
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, pct)

        # Title above ring.
        f_title = QFont(self.font())
        f_title.setBold(True)
        p.setFont(f_title)
        title_rect = QRectF(x_center - d / 2, top - 2, d, 14)
        p.setPen(LABEL_COLOR)
        # Drawn in the top inner band of the ring — but the percentage owns
        # the centre, so put the title underneath instead.
        below = QRectF(x_center - d, top + d + 2, d * 2, 16)
        p.drawText(below, Qt.AlignmentFlag.AlignCenter, state.title)

        # Sub-labels under the title.
        f_small = QFont(self.font())
        f_small.setPointSizeF(max(7.0, self.font().pointSizeF() - 1))
        p.setFont(f_small)
        p.setPen(SUBLABEL_COLOR)
        primary_rect = QRectF(x_center - d, top + d + 18, d * 2, 14)
        p.drawText(primary_rect, Qt.AlignmentFlag.AlignCenter, state.primary)
        secondary_rect = QRectF(x_center - d, top + d + 30, d * 2, 14)
        p.drawText(secondary_rect, Qt.AlignmentFlag.AlignCenter, state.secondary)


def _humanize(n: int) -> str:
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}K"
    return f"{n / 1_000_000:.1f}M"
