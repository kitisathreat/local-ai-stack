"""QTextBrowser subclass that renders markdown-it-py output.

Deliberately uses QTextBrowser (native Qt rich-text widget) rather than
QtWebEngine — we don't want any embedded browser in the UI path. Code
blocks are rendered with a monospace font via inline CSS.

Streaming performance: assistant messages arrive one token at a time.
Re-parsing the full accumulated markdown on every token is quadratic;
instead, deltas are batched into a pending buffer and flushed on a
60 ms timer so the parser runs at most ~16 Hz.
"""

from __future__ import annotations

from markdown_it import MarkdownIt
from PySide6.QtCore import QTimer
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QTextBrowser


_STYLE = """
<style>
body { font-family: 'Segoe UI', Arial, sans-serif; font-size: 11pt; }
pre, code { font-family: 'Cascadia Code', Consolas, monospace; font-size: 10pt; }
pre { background: #1e1e1e; color: #e8e8e8; padding: 8px; border-radius: 4px; }
code { background: #2d2d2d; color: #e8e8e8; padding: 1px 4px; border-radius: 3px; }
a { color: #3794ff; }
blockquote { color: #888; border-left: 3px solid #444; padding-left: 8px; }
h1, h2, h3, h4 { margin-top: 12px; margin-bottom: 4px; }
</style>
"""

_FLUSH_INTERVAL_MS = 60


class MarkdownView(QTextBrowser):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setOpenExternalLinks(True)
        self._md = MarkdownIt("commonmark", {"html": False, "linkify": True, "breaks": True})
        self._raw = ""
        self._pending = ""
        self._flush_timer = QTimer(self)
        self._flush_timer.setSingleShot(True)
        self._flush_timer.setInterval(_FLUSH_INTERVAL_MS)
        self._flush_timer.timeout.connect(self._flush)

    def set_markdown(self, text: str) -> None:
        self._raw = text
        self._pending = ""
        self._render_current()

    def append_markdown(self, delta: str) -> None:
        self._pending += delta
        if not self._flush_timer.isActive():
            self._flush_timer.start()

    def flush_now(self) -> None:
        """Force an immediate render — call at stream completion."""
        self._flush_timer.stop()
        self._flush()

    def _flush(self) -> None:
        if not self._pending:
            return
        self._raw += self._pending
        self._pending = ""
        self._render_current()

    def _render_current(self) -> None:
        rendered = self._md.render(self._raw)
        self.setHtml(_STYLE + rendered)
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.setTextCursor(cursor)

    def raw(self) -> str:
        return self._raw + self._pending
