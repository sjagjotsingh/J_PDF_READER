"""
J PDF Reader - A fast, feature-rich PDF reader for Windows with Dark Mode.

Architecture (performance-focused):
    * The PDF document is opened in the main thread but ALL rendering happens
      in a dedicated background QThread (RenderWorker), keeping the UI smooth.
    * Pages are rendered LAZILY - only pages currently visible (plus a small
      pre-fetch window of neighbors) are queued for rendering.
    * Rendered pixmaps go into an LRU cache so re-visiting a page is instant.
    * Resize / zoom / mode-change events are DEBOUNCED so we never render
      hundreds of pages on every keystroke.
    * Thumbnails are produced by a separate background thread and streamed
      into the sidebar one-by-one.

Features:
    Open / close PDFs (dialog, drag-drop, command line, recent list)
    Continuous, Single-page and Two-page (book) view modes
    Lazy fast rendering, background-threaded
    Zoom in/out, fit-width, fit-page, custom %, Ctrl+wheel
    Rotate left / right
    Page thumbnails sidebar (background-generated)
    Outline / bookmarks (TOC) sidebar
    User bookmarks (add/remove/jump, persisted per-file)
    Full-text search with highlight + next/prev
    Text selection & copy
    Dark mode (true pixel inversion) + Sepia tint mode
    Password prompt for encrypted PDFs
    Document properties dialog
    Save a copy
    Extract entire document text to .txt
    Print
    Auto-scroll / reading mode
    Fullscreen / presentation mode
    Resume last page per file
    Persistent recent files, window state, dark-mode pref
"""

from __future__ import annotations

import collections
import json
import os
import queue
import re
import sys
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import fitz  # PyMuPDF
from PyQt6.QtCore import (
    QEvent,
    QObject,
    QPoint,
    QRect,
    QRectF,
    QSettings,
    QSize,
    Qt,
    QThread,
    QTimer,
    pyqtSignal,
    pyqtSlot,
)
from PyQt6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QFont,
    QGuiApplication,
    QIcon,
    QImage,
    QKeySequence,
    QPainter,
    QPixmap,
    QShortcut,
)
from PyQt6.QtPrintSupport import QPrintDialog, QPrinter
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMenuBar,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QStatusBar,
    QTabBar,
    QTabWidget,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

APP_NAME = "J PDF Reader"
ORG_NAME = "JPDFReader"


class FlowLayout(QLayout):
    """A layout that arranges child widgets left-to-right and WRAPS them onto
    new rows when they run out of horizontal space. Used for the main toolbar
    so every control is always visible (wrapping to more rows) instead of
    being hidden behind a '>>' overflow menu."""

    def __init__(self, parent=None, margin=4, hspacing=6, vspacing=6):
        super().__init__(parent)
        self._items: List = []
        self._hspace = hspacing
        self._vspace = vspacing
        self.setContentsMargins(margin, margin, margin, margin)

    def __del__(self):
        while self._items:
            self._items.pop()

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index):
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        size += QSize(m.left() + m.right(), m.top() + m.bottom())
        return size

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        m = self.contentsMargins()
        x = rect.x() + m.left()
        y = rect.y() + m.top()
        line_height = 0
        right = rect.right() - m.right()
        for item in self._items:
            w = item.sizeHint()
            next_x = x + w.width()
            if next_x - 1 > right and line_height > 0:
                # Wrap to next row.
                x = rect.x() + m.left()
                y = y + line_height + self._vspace
                next_x = x + w.width()
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), w))
            x = next_x + self._hspace
            line_height = max(line_height, w.height())
        return y + line_height - rect.y() + m.bottom()


class _FlowStrip(QWidget):
    """Container for the toolbar's FlowLayout.

    Crucially, its width size hint does NOT grow with its contents (that would
    push the toolbar/window ever wider and cause runaway horizontal growth).
    Instead it accepts whatever width it's given and reports the wrapped height
    via heightForWidth, so the FlowLayout wraps buttons onto more rows.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        # Expanding width so the QToolBar's internal box layout stretches us to
        # the full toolbar width; height follows width via heightForWidth.
        pol = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        pol.setHeightForWidth(True)
        self.setSizePolicy(pol)
        self.setMinimumWidth(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        lay = self.layout()
        if lay is not None and lay.hasHeightForWidth():
            return lay.heightForWidth(w)
        return super().heightForWidth(w)

    def sizeHint(self):
        # Report a modest width (never the full content width) so we don't
        # force the toolbar to expand; height follows from heightForWidth.
        lay = self.layout()
        h = lay.heightForWidth(self.width() or 400) if lay is not None else 36
        return QSize(200, max(36, h))

    def minimumSizeHint(self):
        return QSize(0, 36)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        # When our width changes, our wrapped height may change too.
        self.updateGeometry()


def _resource_path(name: str) -> str:
    """
    Locate a bundled resource. Works in three modes:
        - Running from source       -> looks next to this file or in build/
        - Running from PyInstaller --onefile  -> sys._MEIPASS extraction dir
        - Running from PyInstaller .app/.exe folder build -> bundle dir
    """
    candidates: List[str] = []
    if hasattr(sys, "_MEIPASS"):
        candidates.append(os.path.join(sys._MEIPASS, name))
    base = os.path.dirname(os.path.abspath(__file__))
    candidates += [
        os.path.join(base, name),
        os.path.join(base, "build", name),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return candidates[-1]  # fallback (may not exist)



MAX_RECENT = 10
START_RECENT = 5               # recent files shown on the start screen (newest)
RENDER_CACHE_MAX = 120         # cached rendered pages (bitmaps are modest in
                               # size; a larger cache avoids thrashing in
                               # two-page mode and when toggling dark/light,
                               # where visible+prefetch+dpr key variants add up)
THUMB_CACHE_MAX = 1000         # plenty of thumbs
PREFETCH_BEFORE = 1            # pages to render before viewport
PREFETCH_AFTER = 2             # pages to render after viewport
DEBOUNCE_MS = 120              # ms before re-rendering after zoom/resize


# ===========================================================================
# Modern UI stylesheets
# ===========================================================================

LIGHT_QSS = """
* { font-family: "Segoe UI", "SF Pro Text", system-ui, sans-serif; font-size: 10pt; }

QMainWindow, QWidget, QDialog { background-color: #f5f6f8; color: #1f2328; }

QWidget#toolbarStrip {
    background-color: #ffffff;
    border-bottom: 1px solid #e1e4e8;
}
QToolBar {
    background-color: #ffffff;
    border: none;
    border-bottom: 1px solid #e1e4e8;
    padding: 6px 8px;
    spacing: 6px;
}
QToolBar::separator {
    background: #e1e4e8;
    width: 1px;
    margin: 4px 6px;
}

QPushButton {
    background-color: #ffffff;
    color: #1f2328;
    border: 1px solid #d0d7de;
    border-radius: 6px;
    padding: 6px 12px;
    min-width: 24px;
}
QPushButton:hover { background-color: #f3f4f6; border-color: #b6bfca; }
QPushButton:pressed { background-color: #e8eaed; }
QPushButton:checked { background-color: #2563eb; color: white; border-color: #2563eb; }
QPushButton:disabled { color: #9aa0a6; background-color: #fafafa; }

QLineEdit, QComboBox, QSpinBox {
    background-color: #ffffff;
    color: #1f2328;
    border: 1px solid #d0d7de;
    border-radius: 6px;
    padding: 5px 10px;
    selection-background-color: #2563eb;
    selection-color: white;
    min-height: 18px;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border-color: #2563eb; }
QComboBox::drop-down { border: none; width: 20px; }
QComboBox::down-arrow { image: none; border-left: 4px solid transparent; border-right: 4px solid transparent; border-top: 5px solid #57606a; margin-right: 6px; }

QMenuBar {
    background-color: #ffffff;
    color: #1f2328;
    border-bottom: 1px solid #e1e4e8;
    padding: 2px 4px;
}
QMenuBar::item { padding: 6px 12px; border-radius: 4px; background: transparent; }
QMenuBar::item:selected { background-color: #eef0f3; }
QMenu {
    background-color: #ffffff;
    color: #1f2328;
    border: 1px solid #d0d7de;
    border-radius: 8px;
    padding: 4px;
}
QMenu::item { padding: 6px 24px 6px 16px; border-radius: 4px; }
QMenu::item:selected { background-color: #2563eb; color: white; }
QMenu::separator { height: 1px; background: #e1e4e8; margin: 4px 8px; }

QStatusBar {
    background-color: #ffffff;
    border-top: 1px solid #e1e4e8;
    color: #57606a;
}
QStatusBar QLabel { color: #57606a; padding: 0 8px; }

QDockWidget { color: #1f2328; }
QDockWidget::title {
    background-color: #eef0f3;
    color: #1f2328;
    padding: 6px 10px;
    border-bottom: 1px solid #e1e4e8;
    font-weight: 600;
}

QListWidget, QTreeWidget {
    background-color: #ffffff;
    color: #1f2328;
    border: none;
    outline: 0;
    padding: 4px;
}
QListWidget::item, QTreeWidget::item { padding: 4px; border-radius: 4px; }
QListWidget::item:hover, QTreeWidget::item:hover { background-color: #f3f4f6; }
QListWidget::item:selected, QTreeWidget::item:selected { background-color: #2563eb; color: white; }

QScrollBar:vertical { background: transparent; width: 12px; margin: 0; }
QScrollBar::handle:vertical { background: #c6cdd5; border-radius: 5px; min-height: 30px; margin: 2px; }
QScrollBar::handle:vertical:hover { background: #8c96a0; }
QScrollBar:horizontal { background: transparent; height: 12px; margin: 0; }
QScrollBar::handle:horizontal { background: #c6cdd5; border-radius: 5px; min-width: 30px; margin: 2px; }
QScrollBar::handle:horizontal:hover { background: #8c96a0; }
QScrollBar::add-line, QScrollBar::sub-line { background: none; height: 0; width: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: none; }

/* Only style the *document* tab bar (the QTabWidget at the top of the viewer).
   Dock tab bars are bare QTabBar widgets - they keep Qt defaults. */
QTabWidget#docTabs::pane { border: none; }
QTabWidget#docTabs > QTabBar { background: transparent; }
QTabWidget#docTabs > QTabBar::tab {
    background: #eef0f3;
    color: #57606a;
    padding: 6px 14px;
    border: 1px solid #e1e4e8;
    border-bottom: none;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    min-width: 120px;
    max-width: 220px;
}
QTabWidget#docTabs > QTabBar::tab:selected { background: #ffffff; color: #1f2328; border-bottom: 1px solid #ffffff; }
QTabWidget#docTabs > QTabBar::tab:hover:!selected { background: #f3f4f6; }
QTabWidget#docTabs > QTabBar::tab:first { margin-left: 4px; }
QTabWidget#docTabs > QTabBar::close-button { subcontrol-position: right; }
QPushButton#tabNewBtn {
    background: transparent; color: #57606a; border: none;
    font-size: 16pt; font-weight: 500; padding: 0 0 2px 0;
    margin-right: 6px; border-radius: 4px;
}
QPushButton#tabNewBtn:hover  { background: #eef0f3; color: #1f2328; }
QPushButton#tabNewBtn:pressed{ background: #e1e4e8; }

/* ----- Start Screen ----- */
QWidget#startScreen { background-color: #f5f6f8; }
QLabel#startTitle    { font-size: 28pt; font-weight: 700; color: #2563eb; }
QLabel#startSubtitle { font-size: 11pt; color: #57606a; }
QLabel#startSection  { font-size: 11pt; font-weight: 600; color: #57606a; }
QPushButton#startOpenBtn {
    background: #2563eb; color: white; border: none; border-radius: 8px;
    font-size: 11pt; font-weight: 600; text-align: center;
}
QPushButton#startOpenBtn:hover  { background: #1d4ed8; }
QPushButton#startOpenBtn:pressed{ background: #1e40af; }
QPushButton#startClearBtn {
    background: transparent; border: none; color: #6b7280;
    font-size: 9pt; padding: 2px 6px;
}
QPushButton#startClearBtn:hover { color: #2563eb; text-decoration: underline; }

QPushButton#recentRow {
    background: #ffffff; border: 1px solid #e1e4e8; border-radius: 8px;
    text-align: left; padding: 0;
}
QPushButton#recentRow:hover   { background: #f8fafc; border-color: #2563eb; }
QPushButton#recentRow:pressed { background: #eef2f7; }
QPushButton#recentRow[missing="true"] { background: #fafafa; border-style: dashed; }
QLabel#recentRowIcon   { font-size: 16pt; background: transparent; border: none; }
QLabel#recentRowName   { font-size: 10pt; font-weight: 600; color: #1f2328; background: transparent; border: none; }
QLabel#recentRowFolder { font-size: 8pt;  color: #8c96a0; background: transparent; border: none; }
QLabel#recentRowMeta   { font-size: 8pt;  color: #8c96a0; background: transparent; border: none; }
QPushButton#recentRowRemove {
    background: transparent; border: none; border-radius: 12px;
    color: #8c96a0; font-size: 11pt; font-weight: 600; padding: 0; min-width: 0;
}
QPushButton#recentRowRemove:hover   { background: #e5484d; color: #ffffff; }
QPushButton#recentRowRemove:pressed { background: #c93c40; color: #ffffff; }
QPushButton#recentRow[missing="true"] QLabel#recentRowName   { color: #9aa0a6; }
QPushButton#recentRow[missing="true"] QLabel#recentRowIcon   { color: #c6cdd5; }

QToolTip { background-color: #1f2328; color: white; border: none; padding: 4px 8px; border-radius: 4px; }
"""

DARK_QSS = """
* { font-family: "Segoe UI", "SF Pro Text", system-ui, sans-serif; font-size: 10pt; }

QMainWindow, QWidget, QDialog { background-color: #181a1f; color: #e6e6e6; }

QWidget#toolbarStrip {
    background-color: #20232a;
    border-bottom: 1px solid #2c2f36;
}
QToolBar {
    background-color: #20232a;
    border: none;
    border-bottom: 1px solid #2c2f36;
    padding: 6px 8px;
    spacing: 6px;
}
QToolBar::separator { background: #2c2f36; width: 1px; margin: 4px 6px; }

QPushButton {
    background-color: #262a31;
    color: #e6e6e6;
    border: 1px solid #353a43;
    border-radius: 6px;
    padding: 6px 12px;
    min-width: 24px;
}
QPushButton:hover { background-color: #2f343c; border-color: #4a505a; }
QPushButton:pressed { background-color: #1a1d22; }
QPushButton:checked { background-color: #3b82f6; color: white; border-color: #3b82f6; }
QPushButton:disabled { color: #6a6f78; background-color: #1d2026; }

QLineEdit, QComboBox, QSpinBox {
    background-color: #1d2026;
    color: #e6e6e6;
    border: 1px solid #353a43;
    border-radius: 6px;
    padding: 5px 10px;
    selection-background-color: #3b82f6;
    selection-color: white;
    min-height: 18px;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border-color: #3b82f6; }
QComboBox::drop-down { border: none; width: 20px; }
QComboBox::down-arrow { image: none; border-left: 4px solid transparent; border-right: 4px solid transparent; border-top: 5px solid #9aa0a6; margin-right: 6px; }
QComboBox QAbstractItemView { background-color: #20232a; color: #e6e6e6; border: 1px solid #353a43; selection-background-color: #3b82f6; }

QMenuBar {
    background-color: #20232a;
    color: #e6e6e6;
    border-bottom: 1px solid #2c2f36;
    padding: 2px 4px;
}
QMenuBar::item { padding: 6px 12px; border-radius: 4px; background: transparent; }
QMenuBar::item:selected { background-color: #2f343c; }
QMenu {
    background-color: #20232a;
    color: #e6e6e6;
    border: 1px solid #353a43;
    border-radius: 8px;
    padding: 4px;
}
QMenu::item { padding: 6px 24px 6px 16px; border-radius: 4px; }
QMenu::item:selected { background-color: #3b82f6; color: white; }
QMenu::separator { height: 1px; background: #2c2f36; margin: 4px 8px; }

QStatusBar { background-color: #20232a; border-top: 1px solid #2c2f36; color: #9aa0a6; }
QStatusBar QLabel { color: #9aa0a6; padding: 0 8px; }

QDockWidget { color: #e6e6e6; }
QDockWidget::title {
    background-color: #1d2026;
    color: #e6e6e6;
    padding: 6px 10px;
    border-bottom: 1px solid #2c2f36;
    font-weight: 600;
}

QListWidget, QTreeWidget {
    background-color: #181a1f;
    color: #e6e6e6;
    border: none;
    outline: 0;
    padding: 4px;
}
QListWidget::item, QTreeWidget::item { padding: 4px; border-radius: 4px; }
QListWidget::item:hover, QTreeWidget::item:hover { background-color: #262a31; }
QListWidget::item:selected, QTreeWidget::item:selected { background-color: #3b82f6; color: white; }

QScrollBar:vertical { background: transparent; width: 12px; margin: 0; }
QScrollBar::handle:vertical { background: #3a404a; border-radius: 5px; min-height: 30px; margin: 2px; }
QScrollBar::handle:vertical:hover { background: #555c68; }
QScrollBar:horizontal { background: transparent; height: 12px; margin: 0; }
QScrollBar::handle:horizontal { background: #3a404a; border-radius: 5px; min-width: 30px; margin: 2px; }
QScrollBar::handle:horizontal:hover { background: #555c68; }
QScrollBar::add-line, QScrollBar::sub-line { background: none; height: 0; width: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: none; }

/* Only style the *document* tab bar (the QTabWidget at the top of the viewer).
   Dock tab bars are bare QTabBar widgets - they keep the default dark look. */
QTabWidget#docTabs::pane { border: none; }
QTabWidget#docTabs > QTabBar { background: transparent; }
QTabWidget#docTabs > QTabBar::tab {
    background: #20232a;
    color: #9aa0a6;
    padding: 6px 14px;
    border: 1px solid #2c2f36;
    border-bottom: none;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    min-width: 120px;
    max-width: 220px;
}
QTabWidget#docTabs > QTabBar::tab:selected { background: #181a1f; color: #e6e6e6; border-bottom: 1px solid #181a1f; }
QTabWidget#docTabs > QTabBar::tab:hover:!selected { background: #2a2e36; }
QTabWidget#docTabs > QTabBar::tab:first { margin-left: 4px; }
QTabWidget#docTabs > QTabBar::close-button { subcontrol-position: right; }
QPushButton#tabNewBtn {
    background: transparent; color: #9aa0a6; border: none;
    font-size: 16pt; font-weight: 500; padding: 0 0 2px 0;
    margin-right: 6px; border-radius: 4px;
}
QPushButton#tabNewBtn:hover  { background: #2a2e36; color: #e6e6e6; }
QPushButton#tabNewBtn:pressed{ background: #1a1d22; }

/* ----- Start Screen ----- */
QWidget#startScreen { background-color: #181a1f; }
QLabel#startTitle    { font-size: 28pt; font-weight: 700; color: #60a5fa; }
QLabel#startSubtitle { font-size: 11pt; color: #9aa0a6; }
QLabel#startSection  { font-size: 11pt; font-weight: 600; color: #c0c4cc; }
QPushButton#startOpenBtn {
    background: #3b82f6; color: white; border: none; border-radius: 8px;
    font-size: 11pt; font-weight: 600; text-align: center;
}
QPushButton#startOpenBtn:hover  { background: #2563eb; }
QPushButton#startOpenBtn:pressed{ background: #1d4ed8; }
QPushButton#startClearBtn {
    background: transparent; border: none; color: #8b949e;
    font-size: 9pt; padding: 2px 6px;
}
QPushButton#startClearBtn:hover { color: #60a5fa; text-decoration: underline; }

QPushButton#recentRow {
    background: #20232a; border: 1px solid #2c2f36; border-radius: 8px;
    text-align: left; padding: 0;
}
QPushButton#recentRow:hover   { background: #262a31; border-color: #3b82f6; }
QPushButton#recentRow:pressed { background: #1a1d22; }
QPushButton#recentRow[missing="true"] { background: #1d2026; border-style: dashed; }
QLabel#recentRowIcon   { font-size: 16pt; background: transparent; border: none; }
QLabel#recentRowName   { font-size: 10pt; font-weight: 600; color: #e6e6e6; background: transparent; border: none; }
QLabel#recentRowFolder { font-size: 8pt;  color: #8b949e; background: transparent; border: none; }
QLabel#recentRowMeta   { font-size: 8pt;  color: #8b949e; background: transparent; border: none; }
QPushButton#recentRowRemove {
    background: transparent; border: none; border-radius: 12px;
    color: #8b949e; font-size: 11pt; font-weight: 600; padding: 0; min-width: 0;
}
QPushButton#recentRowRemove:hover   { background: #e5484d; color: #ffffff; }
QPushButton#recentRowRemove:pressed { background: #c93c40; color: #ffffff; }
QPushButton#recentRow[missing="true"] QLabel#recentRowName   { color: #6a6f78; }
QPushButton#recentRow[missing="true"] QLabel#recentRowIcon   { color: #4a505a; }

QToolTip { background-color: #e6e6e6; color: #181a1f; border: none; padding: 4px 8px; border-radius: 4px; }
"""


# ===========================================================================
# Data classes
# ===========================================================================


@dataclass
class SearchHit:
    page_index: int
    rect: fitz.Rect


@dataclass(frozen=True)
class RenderKey:
    """Cache key for a rendered page bitmap."""
    page_index: int
    zoom_x1000: int          # int to make hashable & stable (LOGICAL zoom)
    rotation: int
    color_mode: str          # "light", "dark", "sepia"
    dpr_x1000: int = 1000    # device pixel ratio (screen scale), x1000

    @property
    def dpr(self) -> float:
        return self.dpr_x1000 / 1000.0

    @classmethod
    def make(cls, page_index: int, zoom: float, rotation: int, color_mode: str,
             dpr: float = 1.0) -> "RenderKey":
        return cls(page_index, int(round(zoom * 1000)), rotation % 360, color_mode,
                   int(round(dpr * 1000)))


@dataclass(order=True)
class RenderTask:
    priority: int                                # lower = processed sooner
    seq: int                                     # tie-breaker (newer wins)
    key: RenderKey = field(compare=False)
    is_thumb: bool = field(default=False, compare=False)
    doc_gen: int = field(default=0, compare=False)   # document generation stamp


# ===========================================================================
# LRU cache
# ===========================================================================


class LRUCache:
    def __init__(self, capacity: int):
        self.capacity = capacity
        self._od: "collections.OrderedDict[RenderKey, QPixmap]" = collections.OrderedDict()
        self._lock = threading.Lock()

    def get(self, key) -> Optional[QPixmap]:
        with self._lock:
            v = self._od.get(key)
            if v is not None:
                self._od.move_to_end(key)
            return v

    def put(self, key, value: QPixmap):
        with self._lock:
            self._od[key] = value
            self._od.move_to_end(key)
            while len(self._od) > self.capacity:
                self._od.popitem(last=False)

    def clear(self):
        with self._lock:
            self._od.clear()

    def __contains__(self, key):
        with self._lock:
            return key in self._od


# ===========================================================================
# Background render worker
# ===========================================================================


def _render_pool_size() -> int:
    """Number of parallel render threads. Uses most CPU cores but leaves one
    for the GUI, capped so we don't open too many fitz.Document handles."""
    try:
        cores = os.cpu_count() or 4
    except Exception:
        cores = 4
    return max(2, min(6, cores - 1))


def _cpu_pool_size() -> int:
    """Worker count for CPU-heavy per-page batch jobs (search, extract, OCR).
    Uses most cores but leaves one free for the UI. Cross-platform (relies
    only on os.cpu_count and threads, so identical on macOS and Windows)."""
    try:
        cores = os.cpu_count() or 4
    except Exception:
        cores = 4
    return max(2, min(8, cores - 1))


def parallel_pages(doc_path: str, page_indices, fn, max_workers: int = None,
                   cancel=None, on_progress=None):
    """Run ``fn(page, page_index)`` for many pages IN PARALLEL and return a
    dict ``{page_index: result}``.

    Cross-platform parallelism for CPU-heavy PDF work. PyMuPDF is not safe to
    share one Document across threads, so each worker thread opens its OWN
    ``fitz.open(doc_path)`` handle (thread-local) and loads pages from it.

    Args:
        doc_path:     path to the PDF file (each thread opens its own handle).
        page_indices: iterable of 0-based page indices to process.
        fn:           callable(page: fitz.Page, page_index: int) -> result.
        max_workers:  thread count (defaults to _cpu_pool_size()).
        cancel:       optional threading.Event; processing stops if it is set.
        on_progress:  optional callable(done:int, total:int) called from
                      worker threads as pages finish (must be thread-safe /
                      marshalled by the caller if it touches the GUI).

    Results are collected out of order but keyed by page index, so the caller
    can reassemble them in page order.
    """
    from concurrent.futures import ThreadPoolExecutor

    page_indices = list(page_indices)
    total = len(page_indices)
    results: Dict[int, object] = {}
    if total == 0:
        return results
    if max_workers is None:
        max_workers = _cpu_pool_size()
    max_workers = max(1, min(max_workers, total))

    # Thread-local storage so each worker reuses one fitz.Document handle.
    tls = threading.local()
    lock = threading.Lock()
    open_docs: List[fitz.Document] = []   # every handle we open, for cleanup
    done = 0

    def _get_doc():
        d = getattr(tls, "doc", None)
        if d is None:
            d = fitz.open(doc_path)
            tls.doc = d
            with lock:
                open_docs.append(d)
        return d

    def _worker(idx: int):
        nonlocal done
        if cancel is not None and cancel.is_set():
            return idx, None
        try:
            d = _get_doc()
            page = d.load_page(idx)
            res = fn(page, idx)
        except Exception:
            res = None
        with lock:
            done += 1
            cur = done
        if on_progress is not None:
            try:
                on_progress(cur, total)
            except Exception:
                pass
        return idx, res

    try:
        with ThreadPoolExecutor(max_workers=max_workers,
                                thread_name_prefix="pdfpar") as ex:
            for idx, res in ex.map(_worker, page_indices):
                if res is not None:
                    results[idx] = res
    finally:
        # Deterministically close every per-thread document handle. Safe here
        # because the ThreadPoolExecutor has fully shut down (its 'with' block
        # exited) so no worker is still using a handle.
        for d in open_docs:
            try:
                d.close()
            except Exception:
                pass
    return results


class RenderWorker(QObject):
    """
    Owns a POOL of render threads. Each thread pulls tasks off a shared
    thread-safe priority queue and emits the resulting QImage on the GUI
    thread via a signal.

    PyMuPDF is NOT thread-safe across threads sharing the same Document, so
    each pool thread opens its OWN fitz.Document handle from the file path.
    Rendering different pages in parallel then gives a big speed-up on large
    documents / high zoom without corrupting PyMuPDF state.
    """
    pageRendered = pyqtSignal(RenderKey, QImage, bool)  # key, image, is_thumb

    def __init__(self):
        super().__init__()
        self._queue: "queue.PriorityQueue[RenderTask]" = queue.PriorityQueue()
        self._doc_path: Optional[str] = None
        self._doc_generation = 0     # bumped on every set_document; stale tasks dropped
        self._stop = False
        self._seq = 0
        self._lock = threading.Lock()
        # Membership tracking so we don't queue a key twice.
        self._pending: set = set()
        self._threads: List[threading.Thread] = []
        self._n = _render_pool_size()

    def start(self):
        """Spin up the worker thread pool. Safe to call once."""
        if self._threads:
            return
        self._stop = False
        for i in range(self._n):
            t = threading.Thread(target=self._run_loop, name=f"render-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    # Document is set from the GUI thread; workers open their own handle by path.
    def set_document(self, doc: Optional[fitz.Document]):
        with self._lock:
            # We only need the PATH - each worker opens its own handle.
            try:
                self._doc_path = doc.name if doc is not None else None
            except Exception:
                self._doc_path = None
            self._doc_generation += 1
            # Drain queue when switching docs.
            try:
                while True:
                    self._queue.get_nowait()
            except queue.Empty:
                pass
            self._pending.clear()

    def submit(self, key: RenderKey, priority: int = 10, is_thumb: bool = False):
        with self._lock:
            mem_key = (key, is_thumb)
            if mem_key in self._pending:
                return
            self._pending.add(mem_key)
            self._seq += 1
            gen = self._doc_generation
            self._queue.put(RenderTask(priority, self._seq, key, is_thumb, gen))

    def stop(self):
        self._stop = True
        # Push one sentinel per thread to unblock their get() calls.
        with self._lock:
            for _ in range(max(1, len(self._threads))):
                self._seq += 1
                self._queue.put(RenderTask(0, self._seq, RenderKey(-1, 0, 0, "light"), -1))

    def join(self, timeout: float = 1.5):
        """Wait for all pool threads to finish (best-effort)."""
        for t in self._threads:
            try:
                t.join(timeout=timeout)
            except Exception:
                pass
        self._threads = []

    def _run_loop(self):
        """Body of each pool thread. Owns its own fitz.Document, reopened
        whenever the active document (path/generation) changes."""
        local_doc: Optional[fitz.Document] = None
        local_path: Optional[str] = None
        local_gen = -1
        try:
            while not self._stop:
                try:
                    task = self._queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                if self._stop:
                    break
                with self._lock:
                    path = self._doc_path
                    gen = self._doc_generation
                    self._pending.discard((task.key, task.is_thumb))
                # Drop tasks queued against an older document.
                if task.doc_gen != gen or path is None or task.key.page_index < 0:
                    continue
                # (Re)open this thread's private document handle if the active
                # document changed since we last rendered.
                if local_doc is None or local_path != path or local_gen != gen:
                    if local_doc is not None:
                        try:
                            local_doc.close()
                        except Exception:
                            pass
                        local_doc = None
                    try:
                        local_doc = fitz.open(path)
                    except Exception:
                        local_doc = None
                    local_path = path
                    local_gen = gen
                if local_doc is None or task.key.page_index >= local_doc.page_count:
                    continue
                try:
                    img = self._render(local_doc, task.key, task.is_thumb)
                except Exception:
                    continue
                if img is not None:
                    self.pageRendered.emit(task.key, img, task.is_thumb)
        finally:
            if local_doc is not None:
                try:
                    local_doc.close()
                except Exception:
                    pass

    def _render(self, doc: fitz.Document, key: RenderKey, is_thumb: bool) -> Optional[QImage]:
        page = doc.load_page(key.page_index)
        zoom = key.zoom_x1000 / 1000.0
        if is_thumb:
            zoom = 0.18
        else:
            # Render at the screen's device pixel ratio so the bitmap has 1:1
            # physical pixels on HiDPI displays (no upscaling => no blur).
            zoom *= key.dpr
        mat = fitz.Matrix(zoom, zoom).prerotate(key.rotation)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        img = QImage(
            pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888
        ).copy()
        # Dark mode: fast C-implemented per-pixel inversion.
        if key.color_mode == "dark":
            img.invertPixels(QImage.InvertMode.InvertRgb)
        # Sepia is rendered identically to "light" - the warm tint is applied
        # cheaply at paint time as a translucent overlay (see PdfPageWidget).
        return img


# ===========================================================================
# PDF page widget
# ===========================================================================


class PdfPageWidget(QWidget):
    """A single page placeholder. Paints either a 'loading' box or its pixmap."""

    selectionMade = pyqtSignal(int)  # page_index

    def __init__(self, page_index: int, page_w_pt: float, page_h_pt: float, parent=None):
        super().__init__(parent)
        self.page_index = page_index
        self.page_w_pt = page_w_pt
        self.page_h_pt = page_h_pt

        self.pixmap_image: Optional[QPixmap] = None
        # Previous render kept while a new one is pending, drawn scaled so
        # zooming shows a preview instead of a blank page.
        self._stale_pixmap: Optional[QPixmap] = None
        self.zoom: float = 1.0
        self.rotation: int = 0
        # "light" | "dark" | "sepia" - controls the placeholder colour drawn
        # while the actual page bitmap is still being rendered in the bg thread.
        self.color_mode: str = "light"

        self.search_hits: List[fitz.Rect] = []
        self.current_hit_local_idx: int = -1

        # Sentence-level highlight rects driven by the read-aloud feature.
        # Listed in PDF-point coords (same as search_hits).
        self.tts_hits: List[fitz.Rect] = []

        # Tint overlay drawn on top of the rendered pixmap (e.g. for sepia).
        self.tint_color: Optional[QColor] = None

        self._sel_start: Optional[QPoint] = None
        self._sel_end: Optional[QPoint] = None
        self._selection_rect: Optional[QRectF] = None

        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    # ------------------------------------------------------------------
    def update_geometry(self, zoom: float, rotation: int):
        old_rotation = self.rotation
        self.zoom = zoom
        self.rotation = rotation
        if rotation in (90, 270):
            w, h = self.page_h_pt, self.page_w_pt
        else:
            w, h = self.page_w_pt, self.page_h_pt
        self.setFixedSize(int(w * zoom) + 12, int(h * zoom) + 12)
        # The cached pixmap is now the wrong size, but instead of blanking the
        # page (which flashes an empty placeholder while zooming) we keep it as a
        # "stale" bitmap that paintEvent scales to fill the page until the fresh
        # render arrives. Rotation changes make scaling meaningless, so drop it.
        if self.pixmap_image is not None and old_rotation == rotation:
            self._stale_pixmap = self.pixmap_image
        else:
            self._stale_pixmap = None
        self.pixmap_image = None
        self._selection_rect = None
        self.update()

    def set_pixmap(self, pix: QPixmap):
        self.pixmap_image = pix
        self._stale_pixmap = None
        self.update()

    def set_search_hits(self, hits: List[fitz.Rect], current_local_idx: int = -1):
        self.search_hits = hits
        self.current_hit_local_idx = current_local_idx
        self.update()

    def set_tts_hits(self, hits: List[fitz.Rect]):
        """Set the read-aloud highlight rects for this page (in PDF points)."""
        self.tts_hits = hits or []
        self.update()

    def clear_selection(self):
        self._sel_start = None
        self._sel_end = None
        self._selection_rect = None
        self.update()

    def get_selection_rect_in_pdf(self) -> Optional[fitz.Rect]:
        if self._selection_rect is None or self.zoom <= 0:
            return None
        r = self._selection_rect
        # Selection coords are in widget space; subtract 6px page margin.
        x0 = (r.x() - 6) / self.zoom
        y0 = (r.y() - 6) / self.zoom
        x1 = (r.x() + r.width() - 6) / self.zoom
        y1 = (r.y() + r.height() - 6) / self.zoom
        return fitz.Rect(x0, y0, x1, y1)

    # ------------------------------------------------------------------
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._sel_start = e.position().toPoint()
            self._sel_end = self._sel_start
            self._selection_rect = None  # don't draw 0-size rect
            # Tell the viewer to clear selections on OTHER pages.
            self.selectionMade.emit(-1 - self.page_index)  # negative = "starting"
            self.update()

    def mouseMoveEvent(self, e):
        if self._sel_start is not None:
            self._sel_end = e.position().toPoint()
            dx = abs(self._sel_end.x() - self._sel_start.x())
            dy = abs(self._sel_end.y() - self._sel_start.y())
            if dx + dy < 3:
                # Treat as click; no selection rect.
                self._selection_rect = None
            else:
                self._selection_rect = QRectF(
                    min(self._sel_start.x(), self._sel_end.x()),
                    min(self._sel_start.y(), self._sel_end.y()),
                    dx, dy,
                )
            self.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self._sel_start is not None:
            self._sel_end = e.position().toPoint()
            # If the user just clicked (no drag), clear any existing selection.
            if self._selection_rect is None:
                self._sel_start = None
                self._sel_end = None
                self.update()
                return
            self.selectionMade.emit(self.page_index)

    # ------------------------------------------------------------------
    def paintEvent(self, _ev):
        p = QPainter(self)
        rect = self.rect()
        # Page area inside the 6px margin
        page_rect = rect.adjusted(6, 6, -6, -6)

        if self.pixmap_image is not None:
            p.drawPixmap(page_rect.topLeft(), self.pixmap_image)
            if self.tint_color is not None:
                # Sepia / warm tint overlay; multiplied compositing tints the page.
                # Use the pixmap's LOGICAL (device-independent) size so the tint
                # lines up on HiDPI displays where the pixmap holds dpr*zoom px.
                dpr = self.pixmap_image.devicePixelRatio() or 1.0
                log_w = int(self.pixmap_image.width() / dpr)
                log_h = int(self.pixmap_image.height() / dpr)
                p.save()
                p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Multiply)
                p.fillRect(
                    page_rect.x(), page_rect.y(),
                    log_w, log_h,
                    self.tint_color,
                )
                p.restore()
        elif self._stale_pixmap is not None:
            # A re-render is pending (e.g. mid-zoom). Draw the previous bitmap
            # scaled to the new page size so the user sees a smooth preview
            # instead of a blank flash. Slightly soft until the crisp render
            # lands, then set_pixmap() replaces it.
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            p.drawPixmap(page_rect, self._stale_pixmap, self._stale_pixmap.rect())
            if self.tint_color is not None:
                p.save()
                p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Multiply)
                p.fillRect(page_rect, self.tint_color)
                p.restore()
        else:
            # Loading placeholder - colours match the active color mode so the
            # page doesn't flash white when re-rendering in dark/sepia mode.
            if self.color_mode == "dark":
                bg, border, text = QColor(0, 0, 0), QColor(60, 60, 60), QColor(140, 140, 140)
            elif self.color_mode == "sepia":
                bg, border, text = QColor(244, 230, 200), QColor(190, 170, 130), QColor(120, 100, 60)
            else:
                bg, border, text = QColor(255, 255, 255), QColor(200, 200, 200), QColor(140, 140, 140)
            p.fillRect(page_rect, bg)
            p.setPen(border)
            p.drawRect(page_rect)
            p.setPen(text)
            p.drawText(page_rect, Qt.AlignmentFlag.AlignCenter, f"Page {self.page_index + 1}")

        # Search highlights
        if self.search_hits:
            for i, r in enumerate(self.search_hits):
                if i == self.current_hit_local_idx:
                    color = QColor(255, 60, 60, 160)
                else:
                    color = QColor(255, 165, 0, 110)
                p.fillRect(
                    int(r.x0 * self.zoom) + 6,
                    int(r.y0 * self.zoom) + 6,
                    max(1, int((r.x1 - r.x0) * self.zoom)),
                    max(1, int((r.y1 - r.y0) * self.zoom)),
                    color,
                )

        # Read-aloud sentence highlight (one rect per visual line covered).
        if self.tts_hits:
            color = QColor(255, 230, 0, 90)  # translucent yellow
            for r in self.tts_hits:
                p.fillRect(
                    int(r.x0 * self.zoom) + 6,
                    int(r.y0 * self.zoom) + 6,
                    max(1, int((r.x1 - r.x0) * self.zoom)),
                    max(1, int((r.y1 - r.y0) * self.zoom)),
                    color,
                )

        # Selection rectangle
        if self._selection_rect is not None:
            p.fillRect(self._selection_rect, QColor(80, 140, 240, 90))

        p.end()


# ===========================================================================
# PDF Viewer
# ===========================================================================


class PdfViewer(QScrollArea):
    pageChanged = pyqtSignal(int)
    zoomChanged = pyqtSignal(float)
    statusMessage = pyqtSignal(str)
    documentLoaded = pyqtSignal()
    documentClosed = pyqtSignal()
    thumbnailReady = pyqtSignal(int, QImage)

    VIEW_CONTINUOUS = "continuous"
    VIEW_SINGLE = "single"
    VIEW_TWO = "two"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.setAcceptDrops(True)

        self._container = QWidget()
        self._layout = QVBoxLayout(self._container)
        self._layout.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self.setWidget(self._container)

        # Document state
        self.doc: Optional[fitz.Document] = None
        self.doc_path: Optional[str] = None
        self.page_widgets: List[PdfPageWidget] = []
        # Timer that builds page placeholder widgets in the background for
        # large documents (see _build_placeholders / _build_placeholder_chunk).
        self._build_timer: Optional[QTimer] = None
        # Last page the user actually scrolled to in this viewer.
        # current_page_index() relies on the widget being visible, so when this
        # viewer is hidden (e.g. another tab is active) it can't compute the
        # real page. We update _last_known_page from pageChanged.emit calls so
        # save_session_state can persist the correct page for inactive tabs.
        self._last_known_page: int = 0
        # Page-index stash kept across hide/show so switching tabs
        # doesn't reset us to page 1 (Qt collapses hidden scroll areas).
        self._stashed_page_idx: Optional[int] = None
        self.zoom: float = 1.0
        self.fit_mode: Optional[str] = "width"
        self.rotation: int = 0
        self.color_mode: str = "light"   # "light" | "dark" | "sepia"
        self.view_mode: str = self.VIEW_CONTINUOUS
        self.single_page_index: int = 0  # for single & two-page mode

        # Render cache & worker
        self.cache = LRUCache(RENDER_CACHE_MAX)
        self.thumb_cache = LRUCache(THUMB_CACHE_MAX)
        # OCR-words cache populated by AutoOcrController for pages that have
        # no native text layer. Read by ReadAloudController and search.
        self._ocr_words_cache: Dict[int, List[OcrWord]] = {}
        # RenderWorker now manages its OWN pool of daemon threads (each with a
        # private fitz.Document), so it no longer needs a wrapping QThread.
        self.worker = RenderWorker()
        self.worker.pageRendered.connect(self._on_page_rendered)
        self.worker.start()

        # Search
        self.search_hits: List[SearchHit] = []
        self.current_hit_idx: int = -1
        self._hits_by_page: Dict[int, List[SearchHit]] = {}

        # Debounce timers
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(self._do_refresh_visible)

        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.timeout.connect(self._apply_fit_mode)

        # Auto-scroll
        self._autoscroll_timer = QTimer(self)
        self._autoscroll_timer.timeout.connect(self._autoscroll_step)
        self._autoscroll_speed = 1   # pixels per tick
        self._autoscroll_active = False

        self.verticalScrollBar().valueChanged.connect(self._on_scroll)
        # Track the last page the user navigated to, so save_session_state can
        # persist the right value even when this viewer is in an inactive tab.
        self.pageChanged.connect(self._remember_page)

        # Forward thumbnail-rendered events through our class-level signal.
        self.worker.pageRendered.connect(self._forward_thumb)

    def _forward_thumb(self, key: RenderKey, img: QImage, is_thumb: bool):
        if is_thumb:
            self.thumb_cache.put(key, QPixmap.fromImage(img))
            self.thumbnailReady.emit(key.page_index, img)

    # ------------------------------------------------------------------
    # Open/close
    # ------------------------------------------------------------------
    def open_pdf(self, path: str, password: Optional[str] = None) -> bool:
        try:
            doc = fitz.open(path)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to open PDF:\n{e}")
            return False

        if doc.needs_pass:
            pwd = password
            if pwd is None:
                pwd, ok = QInputDialog.getText(
                    self, "Password Required",
                    "This PDF is encrypted. Enter password:",
                    QLineEdit.EchoMode.Password,
                )
                if not ok:
                    doc.close()
                    return False
            if not doc.authenticate(pwd):
                QMessageBox.warning(self, "Wrong Password", "The password was incorrect.")
                doc.close()
                return False

        self.close_pdf(emit=False)
        self.doc = doc
        self.doc_path = path
        self.worker.set_document(doc)
        self._build_placeholders()
        self._apply_fit_mode()
        self.statusMessage.emit(
            f"Opened: {os.path.basename(path)}  •  {doc.page_count} pages"
        )
        self.documentLoaded.emit()
        return True

    def close_pdf(self, emit: bool = True):
        # Stop any in-flight chunked placeholder build.
        if self._build_timer is not None:
            self._build_timer.stop()
            self._build_timer = None
        self.worker.set_document(None)
        if self.doc is not None:
            self.doc.close()
            self.doc = None
            self.doc_path = None
        for w in self.page_widgets:
            w.setParent(None)
            w.deleteLater()
        self.page_widgets = []
        self._widget_map = {}
        self.search_hits = []
        self.current_hit_idx = -1
        self._hits_by_page = {}
        self.cache.clear()
        self.thumb_cache.clear()
        # Drop per-document OCR word boxes so they don't leak / go stale when a
        # different PDF is opened in this same viewer.
        self._ocr_words_cache.clear()
        # Drop the read-aloud sentence cache for the old document too.
        if hasattr(self, "_tts") and self._tts is not None:
            try:
                self._tts.clear_cache()
            except Exception:
                pass
        self.single_page_index = 0
        if emit:
            self.documentClosed.emit()

    # ------------------------------------------------------------------
    # Layout building
    # ------------------------------------------------------------------
    def _build_placeholders(self):
        if self.doc is None:
            return
        # Clear existing items in layout
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.page_widgets = []
        self._widget_map = {}

        # PERFORMANCE: PyMuPDF's load_page(i).rect is expensive for large/complex
        # PDFs (it parses the full page object). Calling it for every page on the
        # UI thread caused multi-minute hangs on large documents. Instead we use
        # page 0's size as the default for every placeholder, then refine each
        # one's true rect lazily in the background. Most PDFs have uniform pages
        # so the lazy refinement is rarely visible.
        try:
            ref_rect = self.doc.load_page(0).rect
            default_w, default_h = ref_rect.width, ref_rect.height
        except Exception:
            default_w, default_h = 612.0, 792.0  # US Letter fallback

        # Track which page rects we've actually loaded so we don't redo them.
        self._page_size_loaded: set = {0}
        self._default_page_size = (default_w, default_h)

        # PERFORMANCE: creating one QWidget per page up front blocks the GUI
        # thread. For a 1760-page book that's a multi-second freeze before the
        # document even appears. We therefore build the placeholders in
        # CHUNKS: the first chunk synchronously (so the first pages show
        # instantly), and the remainder in the background via a timer so the
        # window is interactive immediately.
        self._build_page_count = self.doc.page_count
        self._build_next_index = 0
        # Cancel any in-flight chunked build from a previous document.
        if hasattr(self, "_build_timer") and self._build_timer is not None:
            self._build_timer.stop()

        # Build enough right away to fill a couple of screens.
        self._build_placeholder_chunk(first_chunk=True)

        if self._build_next_index < self._build_page_count:
            self._build_timer = QTimer(self)
            self._build_timer.setSingleShot(False)
            self._build_timer.timeout.connect(self._build_placeholder_chunk)
            self._build_timer.start(0)   # run between event-loop iterations
        else:
            if self.view_mode == self.VIEW_SINGLE:
                self._update_single_page_visibility()

        self._schedule_visible_render()
        # Page-size refinement is now done lazily ONLY for pages that scroll
        # into view (see _do_refresh_visible). Refining every page upfront
        # called load_page() thousands of times on the UI thread and made
        # large PDFs unusable.

    def _build_placeholder_chunk(self, first_chunk: bool = False):
        """Create a batch of page placeholder widgets. Called synchronously
        for the first batch, then repeatedly from a timer for the rest so the
        UI stays responsive on very large documents."""
        if self.doc is None:
            return
        default_w, default_h = getattr(self, "_default_page_size", (612.0, 792.0))
        # Bigger first batch (fills the view immediately); smaller follow-ups
        # to keep each timer tick short.
        batch = 40 if first_chunk else 60
        count = self._build_page_count
        end = min(self._build_next_index + batch, count)

        if self.view_mode == self.VIEW_TWO:
            i = self._build_next_index
            while i < end:
                row = QWidget()
                hl = QHBoxLayout(row)
                hl.setContentsMargins(0, 0, 0, 0)
                hl.setSpacing(0)
                hl.setAlignment(Qt.AlignmentFlag.AlignHCenter)
                if i == 0:
                    spacer = QWidget()
                    spacer.setFixedSize(1, 1)
                    hl.addWidget(spacer)
                    w = self._make_page_widget(i, default_w, default_h, exact=(i == 0))
                    hl.addWidget(w)
                    self.page_widgets.append(w)
                    i += 1
                else:
                    w_left = self._make_page_widget(i, default_w, default_h)
                    hl.addWidget(w_left)
                    self.page_widgets.append(w_left)
                    if i + 1 < count:
                        w_right = self._make_page_widget(i + 1, default_w, default_h)
                        hl.addWidget(w_right)
                        self.page_widgets.append(w_right)
                    i += 2
                self._layout.addWidget(row)
            self._build_next_index = i
        else:
            for i in range(self._build_next_index, end):
                w = self._make_page_widget(i, default_w, default_h, exact=(i == 0))
                self.page_widgets.append(w)
                self._layout.addWidget(w)
            self._build_next_index = end

        done = self._build_next_index >= count
        if done:
            if hasattr(self, "_build_timer") and self._build_timer is not None:
                self._build_timer.stop()
            if self.view_mode == self.VIEW_SINGLE:
                self._update_single_page_visibility()
            self._schedule_visible_render()

    def _make_page_widget(self, i: int, w_pt: float, h_pt: float,
                          exact: bool = False) -> PdfPageWidget:
        """Build a placeholder widget. If ``exact`` is False, w_pt/h_pt is just
        a guess - the real page rect will be loaded later by
        _refine_page_sizes_chunk."""
        w = PdfPageWidget(i, w_pt, h_pt, self._container)
        w.color_mode = self.color_mode
        w.tint_color = QColor(244, 220, 170) if self.color_mode == "sepia" else None
        w.update_geometry(self.zoom, self.rotation)
        w.selectionMade.connect(self._on_selection_made)
        return w

    # ------------------------------------------------------------------
    # Rendering pipeline
    # ------------------------------------------------------------------
    def _schedule_visible_render(self):
        # Debounce frequent calls
        self._refresh_timer.start(DEBOUNCE_MS)

    def _do_refresh_visible(self):
        if self.doc is None or not self.page_widgets:
            return
        visible = self._visible_indices()
        if not visible:
            return
        first, last = visible[0], visible[-1]
        # Lazily refine page rects ONLY for the small set of currently-visible
        # pages. This keeps non-uniform PDFs visually correct without ever
        # touching pages the user hasn't scrolled to.
        self._refine_visible_page_sizes(visible)
        # Build prioritised list: visible first, then prefetch pages.
        prio_list: List[Tuple[int, int]] = []
        for idx in visible:
            prio_list.append((idx, 0))
        for d in range(1, max(PREFETCH_BEFORE, PREFETCH_AFTER) + 1):
            if d <= PREFETCH_AFTER and last + d < self.doc.page_count:
                prio_list.append((last + d, d))
            if d <= PREFETCH_BEFORE and first - d >= 0:
                prio_list.append((first - d, d + 10))

        for idx, prio in prio_list:
            self._ensure_rendered(idx, priority=prio)

    def _refine_visible_page_sizes(self, visible: List[int]):
        """Load the real rect of each visible page and resize its placeholder
        if it differs from the default. Only touches pages already in view -
        never iterates over every page in the document."""
        if not hasattr(self, "_page_size_loaded"):
            return
        for page_idx in visible:
            if page_idx in self._page_size_loaded:
                continue
            self._page_size_loaded.add(page_idx)
            try:
                rect = self.doc.load_page(page_idx).rect
            except Exception:
                continue
            w = self._widget_for_page(page_idx)
            if w is None:
                continue
            real_w, real_h = rect.width, rect.height
            if (abs(real_w - w.page_w_pt) > 0.5 or
                    abs(real_h - w.page_h_pt) > 0.5):
                w.page_w_pt = real_w
                w.page_h_pt = real_h
                w.update_geometry(self.zoom, self.rotation)

    def _render_color_mode(self) -> str:
        # Sepia uses the same bitmap as light + a tint overlay at paint time.
        return "dark" if self.color_mode == "dark" else "light"

    def _device_pixel_ratio(self) -> float:
        """Current screen device pixel ratio (Windows display scale). Falls
        back to 1.0 if unavailable."""
        try:
            dpr = self.devicePixelRatioF()
            return dpr if dpr and dpr > 0 else 1.0
        except Exception:
            return 1.0

    def _ensure_rendered(self, page_index: int, priority: int = 5):
        widget = self._widget_for_page(page_index)
        if widget is None:
            return
        key = RenderKey.make(page_index, self.zoom, self.rotation,
                             self._render_color_mode(), self._device_pixel_ratio())
        cached = self.cache.get(key)
        if cached is not None:
            if widget.pixmap_image is not cached:
                widget.set_pixmap(cached)
                self._apply_search_highlight_to_widget(widget)
            return
        # Not cached yet - request render
        self.worker.submit(key, priority=priority, is_thumb=False)

    def _rebuild_widget_map(self):
        """(Re)build the page_index -> widget lookup. Call after page_widgets is
        replaced (open/close/chunked build)."""
        self._widget_map = {w.page_index: w for w in self.page_widgets}

    def _widget_for_page(self, page_index: int) -> Optional[PdfPageWidget]:
        # O(1) lookup via a page_index -> widget map, replacing an O(n) linear
        # scan that was called in per-page loops (making refreshes O(n^2)).
        cache = getattr(self, "_widget_map", None)
        if cache is None or len(cache) != len(self.page_widgets):
            self._rebuild_widget_map()
            cache = self._widget_map
        return cache.get(page_index)

    def _on_page_rendered(self, key: RenderKey, img: QImage, is_thumb: bool):
        if is_thumb:
            return
        # Discard if config has changed in the meantime.
        if (key.zoom_x1000 != int(round(self.zoom * 1000))
                or key.rotation != self.rotation % 360
                or key.color_mode != self._render_color_mode()
                or key.dpr_x1000 != int(round(self._device_pixel_ratio() * 1000))):
            return
        pix = QPixmap.fromImage(img)
        # Tell Qt this pixmap is high-DPI: it holds (zoom*dpr) device pixels but
        # should occupy (zoom) logical pixels, so drawPixmap maps it 1:1 on
        # screen and it renders crisply instead of being upscaled/blurred.
        if key.dpr_x1000 != 1000:
            pix.setDevicePixelRatio(key.dpr)
        self.cache.put(key, pix)
        widget = self._widget_for_page(key.page_index)
        if widget is not None:
            widget.set_pixmap(pix)
            self._apply_search_highlight_to_widget(widget)

    # ------------------------------------------------------------------
    # Visibility / scroll
    # ------------------------------------------------------------------
    def _visible_indices(self) -> List[int]:
        if not self.page_widgets:
            return []
        viewport_top = self.verticalScrollBar().value()
        viewport_bot = viewport_top + self.viewport().height()
        result: List[int] = []
        # page_widgets are laid out top-to-bottom, so once a widget starts below
        # the viewport bottom we can stop scanning (avoids an O(n) walk of every
        # page on a large document for each scroll tick). mapTo() is the costly
        # call, so short-circuiting it matters.
        for w in self.page_widgets:
            if not w.isVisible():
                continue
            top = w.mapTo(self._container, QPoint(0, 0)).y()
            bot = top + w.height()
            if bot < viewport_top:
                continue                      # entirely above viewport
            if top > viewport_bot:
                break                         # this and all following are below
            result.append(w.page_index)
        return result

    def _on_scroll(self, _val):
        if not self.doc:
            return
        cur = self.current_page_index()
        self.pageChanged.emit(cur)
        self._schedule_visible_render()

    def current_page_index(self) -> int:
        if not self.page_widgets:
            return 0
        visible = self._visible_indices()
        if visible:
            return visible[0]
        return self.page_widgets[0].page_index

    def _remember_page(self, idx: int):
        """Cache the most recent page index emitted via pageChanged.

        Only update when there's an actual visible page widget. When the viewer
        is hidden (another tab is active), _visible_indices() is [] and
        current_page_index() falls back to 0 - we must NOT cache that, or it
        would clobber the real last-known page when the user just switches tabs.
        """
        if self._visible_indices():
            # The page came from a real visible widget - trust it.
            self._last_known_page = idx
        elif idx > 0:
            # No visible widgets but a positive index was emitted (e.g. by
            # goto_page from session restore) - still trust it.
            self._last_known_page = idx
        # else: idx == 0 with no visible widgets => phantom event, ignore.

    def last_known_page(self) -> int:
        """Best-effort current page that works even when the viewer is hidden."""
        # When the viewer is the active tab and has visible pages,
        # current_page_index() is authoritative. Otherwise return the cached
        # value from the last real navigation.
        if self._visible_indices():
            return self.current_page_index()
        return self._last_known_page

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------
    def goto_page(self, page_index: int):
        if self.doc is None or not self.page_widgets:
            return
        self.clear_all_selections()
        page_index = max(0, min(self.doc.page_count - 1, page_index))
        if self.view_mode == self.VIEW_SINGLE:
            self.single_page_index = page_index
            self._update_single_page_visibility()
            self.verticalScrollBar().setValue(0)
            self.pageChanged.emit(page_index)
            self._schedule_visible_render()
            return
        if self.view_mode == self.VIEW_TWO:
            # Snap to even-indexed (after first cover).
            self.single_page_index = page_index
        widget = self._widget_for_page(page_index)
        if widget is not None:
            top = widget.mapTo(self._container, QPoint(0, 0)).y()
            # +1 ensures we land just *inside* the new page so the previous one
            # is no longer counted as "visible" by current_page_index().
            self.verticalScrollBar().setValue(max(0, top + 1))
            self.pageChanged.emit(page_index)

    def _update_single_page_visibility(self):
        if self.view_mode != self.VIEW_SINGLE:
            for w in self.page_widgets:
                w.setVisible(True)
            return
        for w in self.page_widgets:
            w.setVisible(w.page_index == self.single_page_index)

    # ------------------------------------------------------------------
    # Zoom / fit
    # ------------------------------------------------------------------
    def set_zoom(self, zoom: float, fit_mode: Optional[str] = None):
        zoom = max(0.1, min(10.0, zoom))
        if abs(zoom - self.zoom) < 1e-3 and fit_mode == self.fit_mode:
            return
        self.clear_all_selections()
        self.zoom = zoom
        self.fit_mode = fit_mode
        for w in self.page_widgets:
            w.update_geometry(self.zoom, self.rotation)
        self._schedule_visible_render()
        self.zoomChanged.emit(zoom)

    def zoom_in(self):
        self.set_zoom(self.zoom * 1.2, fit_mode=None)

    def zoom_out(self):
        self.set_zoom(self.zoom / 1.2, fit_mode=None)

    def fit_width(self):
        self.fit_mode = "width"
        self._apply_fit_mode()

    def fit_page(self):
        self.fit_mode = "page"
        self._apply_fit_mode()

    def _apply_fit_mode(self):
        if self.doc is None or not self.fit_mode:
            return
        page = self.doc.load_page(0)
        rect = page.rect
        if self.rotation in (90, 270):
            page_w, page_h = rect.height, rect.width
        else:
            page_w, page_h = rect.width, rect.height

        viewport_w = max(1, self.viewport().width() - 40)
        viewport_h = max(1, self.viewport().height() - 40)

        # In two-page mode, fit width assumes two pages side by side.
        if self.view_mode == self.VIEW_TWO and self.fit_mode == "width":
            page_w *= 2

        if self.fit_mode == "width":
            zoom = viewport_w / page_w
        elif self.fit_mode == "page":
            zoom = min(viewport_w / page_w, viewport_h / page_h)
        else:
            return
        zoom = max(0.1, min(10.0, zoom))
        if abs(zoom - self.zoom) > 1e-3:
            self.zoom = zoom
            for w in self.page_widgets:
                w.update_geometry(self.zoom, self.rotation)
            self.zoomChanged.emit(zoom)
        self._schedule_visible_render()

    # ------------------------------------------------------------------
    # Rotation
    # ------------------------------------------------------------------
    def rotate(self, delta: int):
        self.rotation = (self.rotation + delta) % 360
        for w in self.page_widgets:
            w.update_geometry(self.zoom, self.rotation)
        if self.fit_mode:
            self._apply_fit_mode()
        self._schedule_visible_render()

    # ------------------------------------------------------------------
    # Color modes
    # ------------------------------------------------------------------
    def set_color_mode(self, mode: str):
        if mode not in ("light", "dark", "sepia"):
            return
        if mode == self.color_mode:
            return
        prev_render_mode = self._render_color_mode()
        self.color_mode = mode
        new_render_mode = self._render_color_mode()
        # Background color for the empty area around pages.
        if mode == "dark":
            bg = "#1e1e1e"
        elif mode == "sepia":
            bg = "#e9d8b8"
        else:
            bg = "#3a3a3a"
        self._container.setStyleSheet(f"background-color: {bg};")
        self.viewport().setStyleSheet(f"background-color: {bg};")

        # Set / clear tint overlay and update placeholder color on each page.
        tint = QColor(244, 220, 170) if mode == "sepia" else None
        for w in self.page_widgets:
            w.tint_color = tint
            w.color_mode = mode
            w.update()

        # If the underlying bitmap has changed (light <-> dark), rebuild visible.
        if prev_render_mode != new_render_mode:
            dpr = self._device_pixel_ratio()
            for w in self.page_widgets:
                # Try cache; if miss, force a re-render request below. Must use
                # the real device pixel ratio or the lookup always misses on
                # HiDPI displays (where every other path bakes dpr into the key).
                key = RenderKey.make(w.page_index, self.zoom, self.rotation,
                                     new_render_mode, dpr)
                cached = self.cache.get(key)
                if cached is not None:
                    w.set_pixmap(cached)
                else:
                    w.pixmap_image = None
                    w.update()
            self._schedule_visible_render()

    # ------------------------------------------------------------------
    # View mode
    # ------------------------------------------------------------------
    def set_view_mode(self, mode: str):
        if mode not in (self.VIEW_CONTINUOUS, self.VIEW_SINGLE, self.VIEW_TWO):
            return
        if mode == self.view_mode:
            return
        cur = self.current_page_index()
        self.view_mode = mode
        if self.doc is not None:
            self._build_placeholders()
            self._apply_fit_mode()
            self.goto_page(cur)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------
    def search(self, query: str) -> int:
        self.search_hits = []
        self.current_hit_idx = -1
        self._hits_by_page = {}
        for w in self.page_widgets:
            w.set_search_hits([], -1)
        if self.doc is None or not query:
            return 0

        n = self.doc.page_count

        # Small documents: a plain sequential scan is fastest (no thread setup
        # overhead). Large ones: fan out across CPU cores. Each parallel worker
        # opens its own fitz handle (PyMuPDF isn't safe sharing one Document).
        if n <= 8 or not self.doc_path:
            for i in range(n):
                try:
                    rects = self.doc.load_page(i).search_for(query, quads=False)
                except Exception:
                    rects = []
                for r in rects:
                    self.search_hits.append(SearchHit(page_index=i, rect=r))
        else:
            def _search_page(page, idx):
                try:
                    return page.search_for(query, quads=False)
                except Exception:
                    return []
            per_page = parallel_pages(self.doc_path, range(n), _search_page)
            # Reassemble in page order for stable next/prev navigation.
            for i in range(n):
                rects = per_page.get(i)
                if not rects:
                    continue
                for r in rects:
                    self.search_hits.append(SearchHit(page_index=i, rect=r))

        # Index hits by page once, so highlight application is O(hits-on-page)
        # instead of re-scanning the whole hit list for every widget (which made
        # next/prev O(pages * hits)).
        by_page: Dict[int, List[SearchHit]] = {}
        for h in self.search_hits:
            by_page.setdefault(h.page_index, []).append(h)
        self._hits_by_page = by_page

        if self.search_hits:
            self.current_hit_idx = 0
            self._scroll_to_hit(self.search_hits[0])
        # Only highlight the widgets that are currently on screen; the rest get
        # highlighted lazily as they render/scroll into view. Avoids an O(pages)
        # loop on every search.
        visible = set(self._visible_indices())
        for w in self.page_widgets:
            if w.page_index in visible:
                self._apply_search_highlight_to_widget(w)
        return len(self.search_hits)

    def _apply_search_highlight_to_widget(self, w: PdfPageWidget):
        local = getattr(self, "_hits_by_page", {}).get(w.page_index, [])
        hits = [h.rect for h in local]
        cur = -1
        if 0 <= self.current_hit_idx < len(self.search_hits):
            cur_hit = self.search_hits[self.current_hit_idx]
            if cur_hit.page_index == w.page_index:
                for li, h in enumerate(local):
                    if h.rect == cur_hit.rect:
                        cur = li
                        break
        w.set_search_hits(hits, cur)

    def _move_hit(self, delta: int):
        if not self.search_hits:
            return
        old_page = -1
        if 0 <= self.current_hit_idx < len(self.search_hits):
            old_page = self.search_hits[self.current_hit_idx].page_index
        self.current_hit_idx = (self.current_hit_idx + delta) % len(self.search_hits)
        new_hit = self.search_hits[self.current_hit_idx]
        self._scroll_to_hit(new_hit)
        # Only the page losing the "current" marker and the page gaining it need
        # re-highlighting — not every widget (which was O(pages * hits)).
        for pidx in {old_page, new_hit.page_index}:
            if pidx < 0:
                continue
            w = self._widget_for_page(pidx)
            if w is not None:
                self._apply_search_highlight_to_widget(w)

    def next_hit(self):
        self._move_hit(+1)

    def prev_hit(self):
        self._move_hit(-1)

    def _scroll_to_hit(self, hit: SearchHit):
        widget = self._widget_for_page(hit.page_index)
        if widget is None:
            return
        if self.view_mode == self.VIEW_SINGLE:
            self.single_page_index = hit.page_index
            self._update_single_page_visibility()
            self.verticalScrollBar().setValue(max(0, int(hit.rect.y0 * self.zoom) - 60))
        else:
            top = widget.mapTo(self._container, QPoint(0, 0)).y()
            self.verticalScrollBar().setValue(max(0, top + int(hit.rect.y0 * self.zoom) - 60))
        self._schedule_visible_render()

    # ------------------------------------------------------------------
    # Selection / copy
    # ------------------------------------------------------------------
    def _on_selection_made(self, page_idx: int):
        # Negative value (encoded as -1 - idx) means "a new selection is starting
        # on this page; clear selections on every OTHER page".
        if page_idx < 0:
            origin = -1 - page_idx
            for w in self.page_widgets:
                if w.page_index != origin:
                    w.clear_selection()
            return
        text = self.get_selected_text(page_idx)
        if text:
            self.statusMessage.emit(f"Selected {len(text)} chars  •  Ctrl+C to copy")

    def clear_all_selections(self):
        for w in self.page_widgets:
            w.clear_selection()

    def get_selected_text(self, page_idx: Optional[int] = None) -> str:
        if self.doc is None:
            return ""
        widgets = (
            [self._widget_for_page(page_idx)] if page_idx is not None
            else self.page_widgets
        )
        chunks: List[str] = []
        for w in widgets:
            if w is None:
                continue
            r = w.get_selection_rect_in_pdf()
            if r is None or r.is_empty:
                continue
            page = self.doc.load_page(w.page_index)
            try:
                if self.rotation:
                    pr = page.rect
                    if self.rotation == 90:
                        r = fitz.Rect(r.y0, pr.width - r.x1, r.y1, pr.width - r.x0)
                    elif self.rotation == 180:
                        r = fitz.Rect(pr.width - r.x1, pr.height - r.y1,
                                      pr.width - r.x0, pr.height - r.y0)
                    elif self.rotation == 270:
                        r = fitz.Rect(pr.height - r.y1, r.x0, pr.height - r.y0, r.x1)
                txt = page.get_text("text", clip=r)
                if txt:
                    chunks.append(txt)
            except Exception:
                continue
        return "\n".join(chunks).strip()

    def copy_selection(self):
        text = self.get_selected_text()
        if text:
            QGuiApplication.clipboard().setText(text)
            self.statusMessage.emit(f"Copied {len(text)} characters")
        else:
            self.statusMessage.emit("Nothing selected")

    def select_all_visible_text(self):
        # Quick "copy whole page text" helper
        idx = self.current_page_index()
        if self.doc is None:
            return
        text = self.doc.load_page(idx).get_text("text")
        QGuiApplication.clipboard().setText(text)
        self.statusMessage.emit(f"Copied page {idx + 1} text ({len(text)} chars)")

    # ------------------------------------------------------------------
    # Auto-scroll
    # ------------------------------------------------------------------
    def toggle_autoscroll(self) -> bool:
        if self._autoscroll_active:
            self._autoscroll_timer.stop()
            self._autoscroll_active = False
            return False
        self._autoscroll_timer.start(40)
        self._autoscroll_active = True
        return True

    def set_autoscroll_speed(self, pixels_per_tick: int):
        self._autoscroll_speed = max(1, pixels_per_tick)

    def _autoscroll_step(self):
        bar = self.verticalScrollBar()
        if bar.value() >= bar.maximum():
            self._autoscroll_timer.stop()
            self._autoscroll_active = False
            return
        bar.setValue(bar.value() + self._autoscroll_speed)

    # ------------------------------------------------------------------
    # Resize / wheel
    # ------------------------------------------------------------------
    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit_timer.start(120)

    def hideEvent(self, e):
        # Cache the page index before Qt collapses our hidden viewport.
        # Without this, switching to another tab and back resets the scroll
        # to the top (page 1) because the layout of a hidden QScrollArea
        # is rebuilt on next show.
        if self.doc is not None:
            # Only stash if we have a real visible page right now -
            # otherwise the previously-stashed value is more accurate than
            # whatever current_page_index() reports during the hide transition.
            if self._visible_indices():
                self._stashed_page_idx = self.current_page_index()
            elif self._last_known_page > 0:
                self._stashed_page_idx = self._last_known_page
        super().hideEvent(e)

    def showEvent(self, e):
        super().showEvent(e)
        # Restore the page after Qt finishes laying out the viewport AND any
        # pending fit-mode recalculation. resizeEvent triggers fit_timer with a
        # 120 ms delay, and that recomputes page geometries; if we restored
        # earlier the scroll position would be invalidated by the reflow.
        if getattr(self, "_stashed_page_idx", None) is not None:
            stashed_page = self._stashed_page_idx
            self._stashed_page_idx = None
            QTimer.singleShot(150, lambda p=stashed_page: self._restore_after_show(p))

    def _restore_after_show(self, page_idx: int):
        if self.doc is None or not self.page_widgets:
            return
        # Use goto_page so it works correctly across all view modes and
        # accounts for the post-reflow layout. goto_page clamps to valid range.
        self.goto_page(page_idx)

    def wheelEvent(self, e):
        if e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            if e.angleDelta().y() > 0:
                self.zoom_in()
            else:
                self.zoom_out()
            e.accept()
            return
        super().wheelEvent(e)

    # ------------------------------------------------------------------
    # Drag & drop
    # ------------------------------------------------------------------
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            for u in e.mimeData().urls():
                if u.toLocalFile().lower().endswith(".pdf"):
                    e.acceptProposedAction()
                    return
        e.ignore()

    def dropEvent(self, e):
        for u in e.mimeData().urls():
            p = u.toLocalFile()
            if p.lower().endswith(".pdf"):
                self.open_pdf(p)
                break

    # ------------------------------------------------------------------
    # Thumbnails request
    # ------------------------------------------------------------------
    def request_thumbnails_for(self, page_indices: List[int]):
        """Render thumbnails ON DEMAND for just the given pages.

        Called by MainWindow as the thumbnail panel scrolls. Avoids the old
        approach of queuing N tasks upfront, which choked the worker for
        large PDFs (5000 pages -> 5000 lock-protected queue inserts every
        time a tab was switched).
        """
        if self.doc is None or not page_indices:
            return
        mode = self._render_color_mode()
        n = self.doc.page_count
        for rank, i in enumerate(page_indices):
            if i < 0 or i >= n:
                continue
            key = RenderKey.make(i, 0.18, 0, mode)
            cached = self.thumb_cache.get(key)
            if cached is not None:
                self.thumbnailReady.emit(i, cached.toImage())
            else:
                # Lower than visible-page renders (5-10) but higher than
                # default 200 used for backlog work. Ranked by request order.
                self.worker.submit(key, priority=150 + rank, is_thumb=True)

    # Kept for backwards compatibility; now a no-op so callers don't accidentally
    # queue thousands of renders. Use request_thumbnails_for(visible_pages).
    def request_thumbnails(self):
        pass

    # ------------------------------------------------------------------
    # Read aloud
    # ------------------------------------------------------------------
    def get_page_words(self, page_index: int) -> List["OcrWord"]:
        """Best-effort word extraction: native PDF text first, OCR cache
        second. Returns [] only for pages that have neither.

        MUST be called on the GUI thread — it touches self.doc directly. For
        background threads use get_page_words_threadsafe()."""
        if self.doc is None:
            return []
        try:
            page = self.doc.load_page(page_index)
        except Exception:
            return []
        words = _extract_page_words(page)
        if words:
            return words
        cached = self._ocr_words_cache.get(page_index)
        if cached:
            return cached
        return []

    def get_page_words_threadsafe(self, page_index: int) -> List["OcrWord"]:
        """Thread-safe word extraction for background workers.

        PyMuPDF is NOT safe for concurrent access to a single Document, so this
        opens its OWN short-lived fitz handle by path (never touching self.doc)
        exactly like parallel_pages does. Falls back to the OCR-words cache
        (a plain dict read, safe under CPython) for image-only pages."""
        path = self.doc_path
        if path:
            local_doc = None
            try:
                local_doc = fitz.open(path)
                page = local_doc.load_page(page_index)
                words = _extract_page_words(page)
                if words:
                    return words
            except Exception:
                pass
            finally:
                if local_doc is not None:
                    try:
                        local_doc.close()
                    except Exception:
                        pass
        cached = self._ocr_words_cache.get(page_index)
        if cached:
            return list(cached)
        return []

    def get_auto_ocr(self) -> "AutoOcrController":
        """Lazy-create the AutoOcrController for this viewer."""
        if not hasattr(self, "_auto_ocr") or self._auto_ocr is None:
            self._auto_ocr = AutoOcrController(self, parent=self)
        return self._auto_ocr

    def get_tts(self) -> "ReadAloudController":
        """Lazy-create the ReadAloudController for this viewer."""
        if not hasattr(self, "_tts") or self._tts is None:
            self._tts = ReadAloudController(self, self)
            self._tts.sentenceHighlight.connect(self._on_tts_highlight)
            self._tts.highlightCleared.connect(self._on_tts_highlight_cleared)
            self._tts.statusMessage.connect(self.statusMessage)
        return self._tts

    def _on_tts_highlight(self, page_index: int, _sentence_idx: int, rects: list):
        # Clear any old highlight on a different page
        for w in self.page_widgets:
            if w.page_index != page_index and w.tts_hits:
                w.set_tts_hits([])
        widget = self._widget_for_page(page_index)
        if widget is not None:
            widget.set_tts_hits(rects)

    def _on_tts_highlight_cleared(self):
        for w in self.page_widgets:
            if w.tts_hits:
                w.set_tts_hits([])

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------
    def shutdown(self):
        if hasattr(self, "_auto_ocr") and self._auto_ocr is not None:
            try:
                self._auto_ocr.shutdown()
            except Exception:
                pass
        if hasattr(self, "_tts") and self._tts is not None:
            try:
                self._tts.shutdown()
            except Exception:
                pass
        self.worker.stop()
        self.worker.join(timeout=1.5)


# ===========================================================================
# Side panels
# ===========================================================================


class ThumbnailPanel(QListWidget):
    pageRequested = pyqtSignal(int)
    # Emitted (debounced) with the list of page indices currently visible in
    # the thumbnail panel. MainWindow uses this to render thumbnails on demand
    # instead of queuing thousands of render jobs upfront.
    visiblePagesChanged = pyqtSignal(list)

    # Size of the preview icon and the fixed row/cell each thumbnail occupies.
    THUMB_ICON = QSize(140, 180)
    # Cell = icon + padding + a line for the "Page N" caption underneath.
    CELL_SIZE = QSize(160, 220)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setIconSize(self.THUMB_ICON)
        # Icon-above-text layout, centered - like a classic thumbnail strip.
        self.setViewMode(QListWidget.ViewMode.IconMode)
        self.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.setMovement(QListWidget.Movement.Static)
        self.setFlow(QListWidget.Flow.TopToBottom)
        self.setWrapping(False)
        self.setSpacing(6)
        self.setUniformItemSizes(True)
        self.setWordWrap(True)
        self.itemClicked.connect(self._on_clicked)
        self._items_by_page: Dict[int, QListWidgetItem] = {}
        self._populate_pending: int = 0

        # Debounce visible-page emissions during scroll so we don't flood
        # the render worker with re-prioritised tasks.
        self._vis_timer = QTimer(self)
        self._vis_timer.setSingleShot(True)
        self._vis_timer.setInterval(60)
        self._vis_timer.timeout.connect(self._emit_visible_pages)
        self.verticalScrollBar().valueChanged.connect(
            lambda _v: self._vis_timer.start()
        )
        # Builds items in chunks for very large documents.
        self._populate_timer = QTimer(self)
        self._populate_timer.setSingleShot(False)
        self._populate_timer.timeout.connect(self._populate_chunk)

    def _new_item(self, i: int) -> QListWidgetItem:
        item = QListWidgetItem(f"Page {i + 1}")
        item.setData(Qt.ItemDataRole.UserRole, i)
        item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom)
        # Reserve the full cell so the preview image has room to display even
        # before it's rendered (otherwise rows collapse to text height and the
        # thumbnail never becomes visible).
        item.setSizeHint(self.CELL_SIZE)
        return item

    def populate_placeholders(self, n: int):
        self.clear()
        self._items_by_page = {}
        self._populate_timer.stop()
        self._populate_pending = n
        self._populate_next = 0
        # Build the first batch now so the strip appears immediately, then the
        # rest in the background - creating thousands of items at once would
        # block the GUI thread on large books.
        self._populate_chunk(first=True)
        if self._populate_next < n:
            self._populate_timer.start(0)
        # Trigger an initial visibility emit once Qt has laid out the rows.
        QTimer.singleShot(50, self._emit_visible_pages)

    def _populate_chunk(self, first: bool = False):
        n = self._populate_pending
        batch = 60 if first else 120
        end = min(self._populate_next + batch, n)
        for i in range(self._populate_next, end):
            item = self._new_item(i)
            self.addItem(item)
            self._items_by_page[i] = item
        self._populate_next = end
        if end >= n:
            self._populate_timer.stop()
            self._vis_timer.start()

    def set_thumbnail(self, page_index: int, image: QImage):
        item = self._items_by_page.get(page_index)
        if item is None:
            return
        item.setIcon(QIcon(QPixmap.fromImage(image)))

    def _on_clicked(self, item: QListWidgetItem):
        self.pageRequested.emit(item.data(Qt.ItemDataRole.UserRole))

    def _emit_visible_pages(self):
        """Find the index range of items visible in the viewport and emit it."""
        if self.count() == 0:
            return
        vp = self.viewport().rect()
        # Items are centered in IconMode, so probe the horizontal CENTER of the
        # viewport (the left edge can fall in the empty margin and return None).
        cx = vp.center().x()
        first_item = self.itemAt(cx, vp.top() + 1)
        last_item = self.itemAt(cx, vp.bottom() - 1)
        # Fall back to scanning a few probe points if the exact edges missed.
        if first_item is None:
            for dy in range(0, vp.height(), 20):
                first_item = self.itemAt(cx, vp.top() + dy)
                if first_item is not None:
                    break
        if last_item is None:
            for dy in range(0, vp.height(), 20):
                last_item = self.itemAt(cx, vp.bottom() - 1 - dy)
                if last_item is not None:
                    break
        if first_item is None and last_item is None:
            # Nothing resolved yet (e.g. layout not ready) - just render the
            # first screenful so something shows.
            first, last = 0, min(self.count() - 1, 12)
        else:
            first = self.row(first_item) if first_item else 0
            last = self.row(last_item) if last_item else self.count() - 1
        # Pad a bit so neighbouring thumbs are pre-rendered for smooth scroll.
        PAD = 4
        first = max(0, first - PAD)
        last = min(self.count() - 1, last + PAD)
        self.visiblePagesChanged.emit(list(range(first, last + 1)))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._vis_timer.start()

    def showEvent(self, e):
        super().showEvent(e)
        self._vis_timer.start()


class OutlinePanel(QTreeWidget):
    pageRequested = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.itemClicked.connect(self._on_clicked)

    def populate(self, doc: Optional[fitz.Document]) -> bool:
        """Populate the outline tree. Returns True if the document has a
        non-empty outline / table of contents."""
        self.clear()
        if doc is None:
            return False
        try:
            toc = doc.get_toc(simple=True)
        except Exception:
            toc = []
        if not toc:
            empty = QTreeWidgetItem(["(No outline / bookmarks)"])
            empty.setDisabled(True)
            self.addTopLevelItem(empty)
            return False
        stack: List[Tuple[int, QTreeWidgetItem]] = []
        for entry in toc:
            level, title, page = entry[0], entry[1], entry[2]
            it = QTreeWidgetItem([title])
            it.setData(0, Qt.ItemDataRole.UserRole, page - 1)
            while stack and stack[-1][0] >= level:
                stack.pop()
            (stack[-1][1].addChild if stack else self.addTopLevelItem)(it)
            stack.append((level, it))
        self.expandToDepth(1)
        return True

    def _on_clicked(self, item: QTreeWidgetItem, _col: int):
        page = item.data(0, Qt.ItemDataRole.UserRole)
        if page is not None:
            self.pageRequested.emit(int(page))


class BookmarksPanel(QWidget):
    """User bookmarks (per-document, persisted in QSettings)."""
    pageRequested = pyqtSignal(int)
    bookmarksChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        btn_row = QHBoxLayout()
        self.btn_add = QPushButton("+ Bookmark current page")
        self.btn_remove = QPushButton("Remove")
        btn_row.addWidget(self.btn_add)
        btn_row.addWidget(self.btn_remove)
        layout.addLayout(btn_row)

        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(self._open)
        layout.addWidget(self.list)

        self.btn_remove.clicked.connect(self._remove_selected)

        self._bookmarks: List[Dict] = []  # {"page": int, "label": str}

    def set_bookmarks(self, items: List[Dict]):
        self._bookmarks = list(items)
        self._refresh()

    def get_bookmarks(self) -> List[Dict]:
        return list(self._bookmarks)

    def add_bookmark(self, page: int, label: Optional[str] = None):
        label = label or f"Page {page + 1}"
        for b in self._bookmarks:
            if b["page"] == page:
                return
        self._bookmarks.append({"page": page, "label": label})
        self._bookmarks.sort(key=lambda b: b["page"])
        self._refresh()
        self.bookmarksChanged.emit()

    def _remove_selected(self):
        row = self.list.currentRow()
        if 0 <= row < len(self._bookmarks):
            self._bookmarks.pop(row)
            self._refresh()
            self.bookmarksChanged.emit()

    def _refresh(self):
        self.list.clear()
        for b in self._bookmarks:
            item = QListWidgetItem(f"  Page {b['page'] + 1}  —  {b['label']}")
            item.setData(Qt.ItemDataRole.UserRole, b["page"])
            self.list.addItem(item)

    def _open(self, item: QListWidgetItem):
        page = item.data(Qt.ItemDataRole.UserRole)
        if page is not None:
            self.pageRequested.emit(int(page))


# ===========================================================================
# Document properties dialog
# ===========================================================================


class PropertiesDialog(QDialog):
    def __init__(self, doc: fitz.Document, path: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Document Properties")
        self.resize(460, 380)

        meta = doc.metadata or {}
        form = QFormLayout(self)

        def add(label, value):
            v = QLineEdit(str(value or ""))
            v.setReadOnly(True)
            form.addRow(label, v)

        add("File", path)
        try:
            size = os.path.getsize(path)
            add("Size", f"{size / 1024:,.1f} KB")
        except OSError:
            pass
        add("Pages", doc.page_count)
        add("Title", meta.get("title"))
        add("Author", meta.get("author"))
        add("Subject", meta.get("subject"))
        add("Keywords", meta.get("keywords"))
        add("Creator", meta.get("creator"))
        add("Producer", meta.get("producer"))
        add("Created", meta.get("creationDate"))
        add("Modified", meta.get("modDate"))
        add("PDF version", meta.get("format"))
        add("Encrypted", "Yes" if doc.is_encrypted else "No")

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        bb.accepted.connect(self.accept)
        form.addRow(bb)


# ===========================================================================
# OCR support  (uses Tesseract via PyMuPDF)
# ===========================================================================


def _tesseract_available() -> Tuple[bool, str]:
    """
    Return (available, path). Looks for the `tesseract` executable on PATH first;
    falls back to standard install locations on Windows / macOS / Linux.
    Also sets TESSDATA_PREFIX so PyMuPDF can find the language data.
    """
    import shutil
    exe = shutil.which("tesseract")
    candidates: List[str] = []

    if exe:
        candidates.append(exe)

    if sys.platform.startswith("win"):
        candidates += [
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        ]
    elif sys.platform == "darwin":
        # Homebrew (Apple Silicon + Intel), MacPorts, manual.
        candidates += [
            "/opt/homebrew/bin/tesseract",        # Apple Silicon Homebrew
            "/usr/local/bin/tesseract",           # Intel Homebrew
            "/opt/local/bin/tesseract",           # MacPorts
            "/usr/bin/tesseract",
        ]
    else:
        # Linux / other Unix
        candidates += [
            "/usr/bin/tesseract",
            "/usr/local/bin/tesseract",
        ]

    for path in candidates:
        if path and os.path.isfile(path):
            # Make sure PATH contains it so the PyMuPDF subprocess call works.
            bin_dir = os.path.dirname(path)
            if bin_dir and bin_dir not in os.environ.get("PATH", "").split(os.pathsep):
                os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")

            # TESSDATA_PREFIX hunting (per-platform conventions)
            data_candidates = [
                os.path.join(bin_dir, "tessdata"),                            # bundled next to bin
                os.path.normpath(os.path.join(bin_dir, "..", "share", "tessdata")),  # /usr/local/share/tessdata
                os.path.normpath(os.path.join(bin_dir, "..", "share", "tesseract-ocr", "5", "tessdata")),
                os.path.normpath(os.path.join(bin_dir, "..", "share", "tesseract-ocr", "4.00", "tessdata")),
                "/opt/homebrew/share/tessdata",
                "/usr/local/share/tessdata",
                "/usr/share/tesseract-ocr/4.00/tessdata",
                "/usr/share/tesseract-ocr/5/tessdata",
                "/usr/share/tessdata",
            ]
            for d in data_candidates:
                if os.path.isdir(d):
                    os.environ.setdefault("TESSDATA_PREFIX", d)
                    break
            return True, path
    return False, ""


def _missing_tesseract_message() -> str:
    if sys.platform.startswith("win"):
        install = (
            "Install Tesseract for Windows from:\n"
            "    https://github.com/UB-Mannheim/tesseract/wiki"
        )
    elif sys.platform == "darwin":
        install = (
            "Install Tesseract on macOS via Homebrew:\n"
            "    brew install tesseract\n\n"
            "(or with extra languages: brew install tesseract-lang)"
        )
    else:
        install = (
            "Install Tesseract on Linux, e.g. on Debian/Ubuntu:\n"
            "    sudo apt install tesseract-ocr"
        )
    return (
        "Tesseract OCR engine was not found on this system.\n\n"
        "OCR (Optical Character Recognition) lets J PDF Reader read text from "
        "scanned / image-based PDFs.\n\n"
        f"{install}\n\n"
        "After installation, restart the app."
    )


def _ocr_page_text(page: fitz.Page, language: str = "eng", dpi: int = 300,
                   clip: Optional[fitz.Rect] = None) -> str:
    """Run OCR on a single page (or a clip rect) and return extracted text."""
    tp = page.get_textpage_ocr(language=language, dpi=dpi, full=True)
    if clip is not None:
        return page.get_text("text", clip=clip, textpage=tp)
    return page.get_text("text", textpage=tp)


# ---------------------------------------------------------------------------
# Read-aloud helpers (OCR with word-level bounding boxes + sentence grouping)
# ---------------------------------------------------------------------------

@dataclass
class OcrWord:
    """A single OCRed word with its bounding box in PDF-point coordinates."""
    text: str
    rect: fitz.Rect          # in PDF points (matches page.rect coords)


@dataclass
class OcrSentence:
    """A run of OCR'd words that form one TTS-readable sentence."""
    text: str
    rects: List[fitz.Rect]   # one rect per visual line covered by this sentence


def _extract_page_words(page: fitz.Page) -> List[OcrWord]:
    """Return word-level boxes from a page using PyMuPDF's native text layer.

    No external dependencies, no OCR install required, and effectively
    instant - we just read the text already embedded in the PDF. Pages
    with no embedded text (pure-image scans) return an empty list.

    PyMuPDF's get_text("words") returns tuples of:
        (x0, y0, x1, y1, word, block_no, line_no, word_no)
    in PDF-point coordinates - exactly what we need.
    """
    try:
        raw = page.get_text("words")
    except Exception:
        return []
    words: List[OcrWord] = []
    for tup in raw:
        if len(tup) < 5:
            continue
        x0, y0, x1, y1, txt = tup[0], tup[1], tup[2], tup[3], tup[4]
        txt = (txt or "").strip()
        if not txt:
            continue
        words.append(OcrWord(text=txt, rect=fitz.Rect(x0, y0, x1, y1)))
    return words


def _ocr_page_words_via_tesseract(page: fitz.Page, language: str = "eng",
                                  dpi: int = 220) -> List[OcrWord]:
    """Run Tesseract on the rendered page and return word-level boxes in PDF
    points. Returns [] if pytesseract / Tesseract / PIL are unavailable, or
    if Tesseract fails for any reason. Used by the auto-OCR background pass
    for pages whose native text layer is empty."""
    try:
        import pytesseract  # type: ignore
    except ImportError:
        return []
    try:
        from PIL import Image  # type: ignore
    except ImportError:
        return []

    zoom = dpi / 72.0
    try:
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        data = pytesseract.image_to_data(
            img, lang=language, output_type=pytesseract.Output.DICT
        )
    except Exception:
        return []

    words: List[OcrWord] = []
    n = len(data.get("text", []))
    for i in range(n):
        txt = (data["text"][i] or "").strip()
        if not txt:
            continue
        try:
            conf = float(data["conf"][i])
        except (ValueError, KeyError):
            conf = -1.0
        if conf != -1.0 and conf < 30:    # drop very low-confidence noise
            continue
        x = data["left"][i]
        y = data["top"][i]
        w = data["width"][i]
        h = data["height"][i]
        rect = fitz.Rect(x / zoom, y / zoom, (x + w) / zoom, (y + h) / zoom)
        words.append(OcrWord(text=txt, rect=rect))
    return words


_SENTENCE_END_RE = re.compile(r'[.!?](?:["\'\u201d\u2019)\]]+)?\s*$')


def _group_words_into_sentences(words: List[OcrWord]) -> List[OcrSentence]:
    """Group OCRed words into sentence-sized chunks for TTS playback.

    Splits on . ! ? terminators. Also forces a split at a large vertical gap
    so a paragraph break in a scan doesn't produce a single 800-word run-on
    that the highlight box can't sensibly cover.
    """
    if not words:
        return []

    sentences: List[OcrSentence] = []
    cur_words: List[OcrWord] = []

    def flush():
        if not cur_words:
            return
        text = " ".join(w.text for w in cur_words).strip()
        if not text:
            cur_words.clear()
            return
        # Group word rects per visual line (rects whose y-range overlaps).
        lines: List[List[fitz.Rect]] = []
        for w in cur_words:
            placed = False
            for line in lines:
                # Compare against the line's avg y-center.
                ly = sum((r.y0 + r.y1) for r in line) / (2 * len(line))
                wy = (w.rect.y0 + w.rect.y1) / 2
                if abs(ly - wy) < (w.rect.y1 - w.rect.y0) * 0.6:
                    line.append(w.rect)
                    placed = True
                    break
            if not placed:
                lines.append([w.rect])
        # Union the rects of each line into one bbox per line.
        line_rects: List[fitz.Rect] = []
        for line in lines:
            x0 = min(r.x0 for r in line)
            y0 = min(r.y0 for r in line)
            x1 = max(r.x1 for r in line)
            y1 = max(r.y1 for r in line)
            line_rects.append(fitz.Rect(x0, y0, x1, y1))
        sentences.append(OcrSentence(text=text, rects=line_rects))
        cur_words.clear()

    prev_word: Optional[OcrWord] = None
    for w in words:
        # Force a flush on a large vertical gap (paragraph break).
        if prev_word is not None and cur_words:
            line_h = max(1.0, prev_word.rect.y1 - prev_word.rect.y0)
            vertical_gap = w.rect.y0 - prev_word.rect.y1
            if vertical_gap > line_h * 1.6:
                flush()
        cur_words.append(w)
        if _SENTENCE_END_RE.search(w.text):
            flush()
        prev_word = w
    flush()
    return sentences


# ===========================================================================
# Read Aloud  (Text-to-speech with sentence-level highlighting)
# ===========================================================================

# Cache the voices list at module level - querying SAPI is cheap but creates
# a transient COM-affinity issue if done from a worker thread. Doing it once
# from the GUI thread (and caching) sidesteps the problem entirely.
_TTS_VOICES_CACHE: Optional[List[Tuple[str, str]]] = None


def _list_tts_voices() -> List[Tuple[str, str]]:
    """Return [(voice_id, friendly_name), ...] for installed SAPI voices.
    Cached after the first successful call."""
    global _TTS_VOICES_CACHE
    if _TTS_VOICES_CACHE is not None:
        return _TTS_VOICES_CACHE
    try:
        import pyttsx3
        engine = pyttsx3.init()
        voices = engine.getProperty("voices") or []
        result = [(v.id, v.name) for v in voices]
        # Release the engine; the actual TTS playback will create its own.
        try:
            engine.stop()
        except Exception:
            pass
        _TTS_VOICES_CACHE = result
        return result
    except Exception:
        _TTS_VOICES_CACHE = []
        return []


class TtsWorker(QObject):
    """Owns the pyttsx3 engine on its own QThread.

    pyttsx3 is synchronous (engine.say + engine.runAndWait). We feed one
    utterance at a time so the controller can pause/skip between sentences
    without trying to interrupt SAPI mid-utterance (which is unreliable).
    """

    # Tells the controller this utterance just *started* speaking.
    utteranceStarted = pyqtSignal(int)   # sentence index
    # Fired after a sentence has finished playing.
    utteranceFinished = pyqtSignal(int)  # sentence index
    # Fired once after a stop() request - so the controller can clean up state.
    stopped = pyqtSignal()
    # Engine could not be initialised (no SAPI voices, etc.)
    initFailed = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._engine = None
        self._lock = threading.Lock()
        self._cur_text: Optional[str] = None
        self._cur_index: int = -1
        self._stop_requested = False

    @pyqtSlot()
    def init_engine(self):
        try:
            import pyttsx3
            self._engine = pyttsx3.init()
        except Exception as e:
            self.initFailed.emit(str(e))
            return

    @pyqtSlot(int)
    def set_rate(self, rate: int):
        if self._engine is None:
            return
        try:
            self._engine.setProperty("rate", rate)
        except Exception:
            pass

    @pyqtSlot(str)
    def set_voice(self, voice_id: str):
        if self._engine is None or not voice_id:
            return
        try:
            self._engine.setProperty("voice", voice_id)
        except Exception:
            pass

    def list_voices(self) -> List[Tuple[str, str]]:
        """Return [(voice_id, friendly_name), ...]."""
        if self._engine is None:
            return []
        try:
            return [(v.id, v.name) for v in self._engine.getProperty("voices")]
        except Exception:
            return []

    @pyqtSlot(str, int)
    def speak(self, text: str, sentence_index: int):
        """Speak one utterance synchronously. Returns to event loop afterwards."""
        if self._engine is None:
            self.utteranceFinished.emit(sentence_index)
            return
        with self._lock:
            self._stop_requested = False
            self._cur_text = text
            self._cur_index = sentence_index
        self.utteranceStarted.emit(sentence_index)
        try:
            self._engine.say(text)
            self._engine.runAndWait()
        except Exception:
            pass
        with self._lock:
            self._cur_text = None
            self._cur_index = -1
        if self._stop_requested:
            self.stopped.emit()
        else:
            self.utteranceFinished.emit(sentence_index)

    @pyqtSlot()
    def request_stop(self):
        """Ask the engine to stop the current utterance ASAP."""
        with self._lock:
            self._stop_requested = True
        if self._engine is not None:
            try:
                self._engine.stop()
            except Exception:
                pass


class AutoOcrController(QObject):
    """Background OCR pass that runs after a PDF opens.

    Walks every page, skips ones with native selectable text, and OCRs the
    rest with Tesseract. Results are cached per-page on the viewer so
    read-aloud / search can use them without re-running OCR.

    Silently does nothing if Tesseract isn't available.
    """

    progress = pyqtSignal(int, int)        # done, total (only OCRable pages)
    finished = pyqtSignal(int)              # number of pages actually OCR'd
    skipped  = pyqtSignal(str)              # reason (e.g. "Tesseract not found")

    def __init__(self, viewer: "PdfViewer", language: str = "eng", parent=None):
        super().__init__(parent)
        self.viewer = viewer
        self.language = language
        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._doc_path: Optional[str] = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        """Begin scanning. Safe to call repeatedly - in-flight runs are aborted."""
        if self.is_running():
            self.cancel()
            self._thread.join(timeout=1.0)
        if self.viewer.doc is None or self.viewer.doc_path is None:
            return
        # Quick "is Tesseract installed?" check using the existing helper.
        ok, _ = _tesseract_available()
        if not ok:
            self.skipped.emit("Tesseract not installed")
            return
        self._doc_path = self.viewer.doc_path
        self._cancel.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def cancel(self):
        self._cancel.set()

    def shutdown(self):
        self.cancel()
        if self._thread is not None:
            self._thread.join(timeout=1.5)

    # ------------------------------------------------------------------
    def _run(self):
        """Worker entry point. Opens its own fitz.Document handle so we never
        share a Document instance between threads (PyMuPDF is not safe for
        concurrent access to the same Document)."""
        path = self._doc_path
        if path is None:
            return
        try:
            doc = fitz.open(path)
        except Exception:
            return

        try:
            # First pass: list pages that need OCR (no native text).
            #
            # PERFORMANCE: get_text() fully parses each page's content stream.
            # On very large PDFs (thousands of pages) walking every page here
            # is CPU-bound and, because PyMuPDF holds the GIL for stretches,
            # it starves the render-worker thread so page/thumbnail images
            # never appear promptly. We therefore:
            #   * short-circuit as soon as we've confirmed the document clearly
            #     has a native text layer (most digital PDFs), and
            #   * yield the GIL periodically so the UI/render threads breathe.
            import time
            todo: List[int] = []
            pages_with_text = 0
            total_pages = doc.page_count
            for i in range(total_pages):
                if self._cancel.is_set():
                    return
                try:
                    page = doc.load_page(i)
                    raw = page.get_text("words")
                except Exception:
                    raw = []
                if not raw:
                    todo.append(i)
                else:
                    pages_with_text += 1
                # Breathe every few pages so the GUI + render worker aren't
                # starved of the GIL on huge documents.
                if (i & 0x1F) == 0:
                    time.sleep(0)
                # Early exit: if the first stretch of pages all have real text,
                # this is a normal digital PDF - no OCR pre-scan is worthwhile.
                # Bail out so we don't parse all N pages up front.
                if i >= 24 and not todo and pages_with_text > 24:
                    QTimer.singleShot(0, lambda: self.finished.emit(0))
                    return

            total = len(todo)
            if total == 0:
                # Hand back to GUI thread on the main event loop.
                QTimer.singleShot(0, lambda: self.finished.emit(0))
                return

            QTimer.singleShot(0, lambda t=total: self.progress.emit(0, t))

            done = 0
            for page_idx in todo:
                if self._cancel.is_set():
                    break
                # Skip if some other code already populated the cache (e.g.
                # the user manually OCR'd a page from the menu).
                if page_idx in self.viewer._ocr_words_cache:
                    done += 1
                    continue
                try:
                    page = doc.load_page(page_idx)
                    words = _ocr_page_words_via_tesseract(
                        page, language=self.language
                    )
                except Exception:
                    words = []
                # Stash the result on the viewer (dict assignment is atomic
                # enough in CPython that we don't need a lock for this case).
                self.viewer._ocr_words_cache[page_idx] = words
                done += 1
                QTimer.singleShot(
                    0,
                    lambda d=done, t=total: self.progress.emit(d, t),
                )
            QTimer.singleShot(0, lambda d=done: self.finished.emit(d))
        finally:
            try:
                doc.close()
            except Exception:
                pass


class ReadAloudController(QObject):
    """Coordinates page extraction (OCR) and TTS playback for one PdfViewer.

    State machine: IDLE -> EXTRACTING -> PLAYING <-> PAUSED -> IDLE.
    Emits highlight updates so PdfPageWidget can paint the active sentence.
    """

    STATE_IDLE = "idle"
    STATE_EXTRACTING = "extracting"
    STATE_PLAYING = "playing"
    STATE_PAUSED = "paused"

    # (page_index, sentence_index, sentence_rects_in_pdf_pts)
    sentenceHighlight = pyqtSignal(int, int, list)
    # Called when nothing is highlighted (idle, end-of-page, between sentences).
    highlightCleared = pyqtSignal()
    stateChanged = pyqtSignal(str)
    statusMessage = pyqtSignal(str)

    def __init__(self, viewer: "PdfViewer", parent=None):
        super().__init__(parent)
        self.viewer = viewer
        self.language = "eng"
        self.rate = 180

        self._state = self.STATE_IDLE
        self._page_index = -1
        self._sentences: List[OcrSentence] = []
        self._cursor: int = 0    # next sentence to speak
        self._page_cache: Dict[int, List[OcrSentence]] = {}

        # TTS thread
        self._thread = QThread()
        self._worker = TtsWorker()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.init_engine)
        self._worker.utteranceStarted.connect(self._on_utterance_started)
        self._worker.utteranceFinished.connect(self._on_utterance_finished)
        self._worker.stopped.connect(self._on_engine_stopped)
        self._worker.initFailed.connect(self._on_init_failed)
        self._thread.start()

    # ------------------------------------------------------------------
    @property
    def state(self) -> str:
        return self._state

    def is_playing(self) -> bool:
        return self._state == self.STATE_PLAYING

    def is_paused(self) -> bool:
        return self._state == self.STATE_PAUSED

    # ------------------------------------------------------------------
    def play_current_page(self):
        """Begin reading from the user's current page (or resume if paused)."""
        if self._state == self.STATE_PAUSED:
            self._set_state(self.STATE_PLAYING)
            self._speak_next()
            return
        if self._state in (self.STATE_PLAYING, self.STATE_EXTRACTING):
            return
        if self.viewer.doc is None:
            return
        page_idx = self.viewer.last_known_page()
        self._start_page(page_idx, sentence_offset=0)

    def pause(self):
        if self._state != self.STATE_PLAYING:
            return
        self._set_state(self.STATE_PAUSED)
        self._worker.request_stop()  # stops mid-utterance; we resume from same sentence

    def stop(self):
        if self._state == self.STATE_IDLE:
            return
        self._set_state(self.STATE_IDLE)
        self._sentences = []
        self._cursor = 0
        self._page_index = -1
        self.highlightCleared.emit()
        self._worker.request_stop()

    def set_rate(self, rate: int):
        self.rate = max(80, min(400, rate))
        QTimer.singleShot(0, lambda: self._worker.set_rate(self.rate))

    def set_voice(self, voice_id: str):
        QTimer.singleShot(0, lambda: self._worker.set_voice(voice_id))

    def list_voices(self) -> List[Tuple[str, str]]:
        # Use the module-level cached query: safer than asking the worker
        # thread (SAPI/COM has thread-affinity quirks).
        return _list_tts_voices()

    def clear_cache(self):
        """Drop cached extracted sentences (called when the document changes)."""
        self.stop()
        self._page_cache.clear()
        self._sentences = []
        self._page_index = -1
        self._cursor = 0

    def shutdown(self):
        self.stop()
        # speak() blocks the worker's event loop inside engine.runAndWait();
        # request_stop() interrupts that so quit() can actually return. Then
        # wait long enough (with a fallback terminate) that we never delete the
        # QThread while the worker is still executing inside it.
        try:
            self._worker.request_stop()
        except Exception:
            pass
        self._thread.quit()
        if not self._thread.wait(3000):
            # Worker still stuck (e.g. driver hung mid-utterance): force it down
            # rather than tearing down a live QThread underneath it.
            self._thread.terminate()
            self._thread.wait(1000)

    # ------------------------------------------------------------------
    def _set_state(self, new_state: str):
        if new_state == self._state:
            return
        self._state = new_state
        self.stateChanged.emit(new_state)

    def _start_page(self, page_idx: int, sentence_offset: int = 0):
        if self.viewer.doc is None:
            return
        self._page_index = page_idx
        self._cursor = sentence_offset
        cached = self._page_cache.get(page_idx)
        if cached is not None:
            self._sentences = cached
            self._set_state(self.STATE_PLAYING)
            self._worker.set_rate(self.rate)
            self._speak_next()
            return
        # Extract sentences in a background thread so the UI doesn't freeze
        # while OCR runs (can be a few seconds per page).
        self._set_state(self.STATE_EXTRACTING)
        self.statusMessage.emit(f"Read aloud: extracting page {page_idx + 1}…")
        QTimer.singleShot(0, lambda p=page_idx: self._extract_async(p))

    def _extract_async(self, page_idx: int):
        # Pull words from the PDF (native text first, OCR cache second) on a
        # worker thread so the GUI stays responsive.
        def do_extract():
            try:
                # Thread-safe: opens its own fitz handle by path rather than
                # touching the viewer's shared self.doc from a worker thread.
                words = self.viewer.get_page_words_threadsafe(page_idx)
                sentences = _group_words_into_sentences(words)
            except Exception:
                sentences = []
            # Hand back to the GUI thread.
            QTimer.singleShot(
                0, lambda s=sentences, p=page_idx: self._on_extracted(p, s)
            )
        threading.Thread(target=do_extract, daemon=True).start()

    def _on_extracted(self, page_idx: int, sentences: List[OcrSentence]):
        # User may have stopped while extraction was running.
        if self._state == self.STATE_IDLE or self._page_index != page_idx:
            return
        self._page_cache[page_idx] = sentences
        self._sentences = sentences
        if not sentences:
            # Page has no embedded text (likely an image-only scan). Skip ahead.
            self.statusMessage.emit(
                f"Page {page_idx + 1} has no extractable text - skipping"
            )
            self._advance_page()
            return
        self._set_state(self.STATE_PLAYING)
        self._worker.set_rate(self.rate)
        self._speak_next()

    def _speak_next(self):
        if self._state != self.STATE_PLAYING:
            return
        if self._cursor >= len(self._sentences):
            self._advance_page()
            return
        sent = self._sentences[self._cursor]
        # Emit highlight before queuing speech.
        self.sentenceHighlight.emit(self._page_index, self._cursor, sent.rects)
        # Auto-scroll viewer to the page being read.
        if self.viewer.doc is not None:
            cur_page = self.viewer.last_known_page()
            if cur_page != self._page_index:
                self.viewer.goto_page(self._page_index)
        # Queue the utterance on the worker thread.
        QTimer.singleShot(
            0,
            lambda t=sent.text, i=self._cursor: self._worker.speak(t, i),
        )

    def _advance_page(self):
        if self.viewer.doc is None:
            self.stop()
            return
        next_page = self._page_index + 1
        if next_page >= self.viewer.doc.page_count:
            self.statusMessage.emit("Read aloud: end of document")
            self.stop()
            return
        self._start_page(next_page, sentence_offset=0)

    # ------------------------------------------------------------------
    # Slots from TtsWorker
    # ------------------------------------------------------------------
    @pyqtSlot(int)
    def _on_utterance_started(self, sentence_index: int):
        # Already highlighted in _speak_next; nothing extra to do here.
        pass

    @pyqtSlot(int)
    def _on_utterance_finished(self, sentence_index: int):
        if self._state != self.STATE_PLAYING:
            return
        self._cursor = sentence_index + 1
        self._speak_next()

    @pyqtSlot()
    def _on_engine_stopped(self):
        # Worker confirmed it stopped a request_stop(). If we're paused, the
        # cursor stays put so play() resumes the same sentence; if stopped,
        # state is already IDLE.
        if self._state == self.STATE_PAUSED:
            self.highlightCleared.emit()

    @pyqtSlot(str)
    def _on_init_failed(self, msg: str):
        self.statusMessage.emit(f"Read aloud unavailable: {msg}")
        self._set_state(self.STATE_IDLE)


class OcrResultDialog(QDialog):
    """Shows OCR'd text with copy / save buttons."""

    def __init__(self, title: str, text: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(720, 540)
        layout = QVBoxLayout(self)

        self.editor = QPlainTextEdit()
        self.editor.setPlainText(text)
        self.editor.setReadOnly(False)
        layout.addWidget(self.editor)

        row = QHBoxLayout()
        btn_copy = QPushButton("Copy to Clipboard")
        btn_copy.clicked.connect(self._copy)
        btn_save = QPushButton("Save as .txt...")
        btn_save.clicked.connect(self._save)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.accept)
        row.addWidget(btn_copy)
        row.addWidget(btn_save)
        row.addStretch(1)
        row.addWidget(btn_close)
        layout.addLayout(row)

    def _copy(self):
        QGuiApplication.clipboard().setText(self.editor.toPlainText())

    def _save(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save OCR Text", "ocr_output.txt", "Text Files (*.txt)")
        if path:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(self.editor.toPlainText())
            except Exception as e:
                QMessageBox.critical(self, "Save failed", str(e))


# ===========================================================================
# Audiobook  (natural-sounding TTS export of selected pages via edge-tts)
# ===========================================================================

# edge-tts voice list is fetched once and cached. These are a curated subset
# of Microsoft's neural voices - full list is fetched live when online.
_EDGE_TTS_VOICES_CACHE: Optional[List[Tuple[str, str]]] = None

# A small, sensible default set shown immediately so the UI isn't empty while
# (or if) the live voice list can't be fetched. (short_name, friendly_label)
_EDGE_TTS_DEFAULT_VOICES: List[Tuple[str, str]] = [
    ("en-US-AriaNeural",     "Aria — US English (Female)"),
    ("en-US-GuyNeural",      "Guy — US English (Male)"),
    ("en-US-JennyNeural",    "Jenny — US English (Female)"),
    ("en-US-MichelleNeural", "Michelle — US English (Female)"),
    ("en-US-RogerNeural",    "Roger — US English (Male)"),
    ("en-GB-SoniaNeural",    "Sonia — UK English (Female)"),
    ("en-GB-RyanNeural",     "Ryan — UK English (Male)"),
    ("en-AU-NatashaNeural",  "Natasha — Australian English (Female)"),
    ("en-IN-NeerjaNeural",   "Neerja — Indian English (Female)"),
]


def edge_tts_available() -> bool:
    """True if the edge-tts package is importable."""
    try:
        import edge_tts  # type: ignore  # noqa: F401
        return True
    except Exception:
        return False


def _fetch_edge_tts_voices() -> List[Tuple[str, str]]:
    """Fetch the live list of edge-tts neural voices (needs internet).

    Returns [(short_name, friendly_label), ...]. Falls back to the built-in
    default list on any failure (offline, package missing, etc.). Cached.
    """
    global _EDGE_TTS_VOICES_CACHE
    if _EDGE_TTS_VOICES_CACHE is not None:
        return _EDGE_TTS_VOICES_CACHE
    voices: List[Tuple[str, str]] = []
    try:
        import asyncio
        import edge_tts  # type: ignore

        async def _list():
            return await edge_tts.list_voices()

        loop = asyncio.new_event_loop()
        try:
            raw = loop.run_until_complete(_list())
        finally:
            loop.close()
        for v in raw:
            short = v.get("ShortName", "")
            if not short:
                continue
            gender = v.get("Gender", "")
            locale = v.get("Locale", "")
            friendly = short.split("-")[-1].replace("Neural", "")
            label = f"{friendly} — {locale} ({gender})"
            voices.append((short, label))
        voices.sort(key=lambda t: t[1])
    except Exception:
        voices = []
    if not voices:
        voices = list(_EDGE_TTS_DEFAULT_VOICES)
    _EDGE_TTS_VOICES_CACHE = voices
    return voices


def _safe_filename(name: str, maxlen: int = 60) -> str:
    """Turn an arbitrary chapter title into a safe file name (no path chars)."""
    name = (name or "").strip()
    # Remove characters illegal on Windows/macOS filesystems.
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if len(name) > maxlen:
        name = name[:maxlen].rstrip()
    return name


class AudiobookWorker(QObject):
    """Generates an audiobook file from text using edge-tts on a background
    thread with its own asyncio event loop.

    edge-tts streams synthesized neural-voice audio (MP3) from Microsoft's
    cloud, so this REQUIRES an internet connection. Failures (offline,
    package missing) are reported via the `failed` signal.
    """

    progress = pyqtSignal(int, int, str)   # done, total, human status
    log = pyqtSignal(str)                  # detailed log line for the activity view
    finished = pyqtSignal(str)             # output file path (or folder)
    failed = pyqtSignal(str)               # error message

    def __init__(self, chunks: List[str], voice: str, rate_pct: int,
                 out_path: str, chapters: Optional[List[Tuple[str, str]]] = None,
                 out_dir: Optional[str] = None, parent=None):
        """
        Two modes:
          * Single file  -> pass `chunks` (one string per page) + `out_path`.
          * By chapters  -> pass `chapters` as [(chapter_title, text), ...] and
                            `out_dir`; one audio file is written per chapter.
        """
        super().__init__(parent)
        self._chunks = chunks           # one string per selected page
        self._voice = voice
        self._rate_pct = rate_pct       # -50..+100, edge-tts "rate" percent
        self._out_path = out_path
        self._chapters = chapters
        self._out_dir = out_dir
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    def _emit_log(self, msg: str):
        """Emit a timestamped log line to the activity view."""
        import time
        self.log.emit(f"[{time.strftime('%H:%M:%S')}] {msg}")

    @staticmethod
    def _fmt_size(n: float) -> str:
        if n < 1024:
            return f"{n:.0f} B"
        if n < 1024 * 1024:
            return f"{n / 1024:.1f} KB"
        return f"{n / (1024 * 1024):.1f} MB"

    @pyqtSlot()
    def run(self):
        try:
            import edge_tts  # type: ignore  # noqa: F401
        except Exception:
            self.failed.emit(
                "The 'edge-tts' package is not installed.\n\n"
                "Install it with:\n    pip install edge-tts"
            )
            return
        self._emit_log(f"Voice: {self._voice}   Speed: {self._rate_str()}")
        if self._chapters is not None:
            self._emit_log(f"Mode: whole book by chapters ({len(self._chapters)} chapter(s))")
            self._emit_log(f"Output folder: {self._out_dir}")
            self._run_chapters()
        else:
            self._emit_log(f"Mode: single file ({len(self._chunks)} page(s))")
            self._emit_log(f"Output file: {self._out_path}")
            self._run_single()

    # ------------------------------------------------------------------
    def _rate_str(self) -> str:
        return f"{'+' if self._rate_pct >= 0 else ''}{self._rate_pct}%"

    async def _synth_text_to_file(self, text: str, mp3_path: str, label: str = ""):
        """Synthesize one blob of text into a single MP3 file."""
        import time
        import edge_tts  # type: ignore
        with open(mp3_path, "wb") as out:
            text = (text or "").strip()
            if not text:
                return
            communicate = edge_tts.Communicate(text, self._voice, rate=self._rate_str())
            written = 0
            last_report = 0.0
            async for chunk in communicate.stream():
                if self._cancel.is_set():
                    raise RuntimeError("cancelled")
                if chunk.get("type") == "audio":
                    data = chunk["data"]
                    out.write(data)
                    written += len(data)
                    # Throttle byte-progress logs to ~2/sec so the view stays readable.
                    now = time.monotonic()
                    if now - last_report >= 0.5:
                        last_report = now
                        pre = f"{label}: " if label else ""
                        self._emit_log(f"  {pre}received {self._fmt_size(written)}…")
            pre = f"{label} " if label else ""
            self._emit_log(f"  {pre}audio complete ({self._fmt_size(written)})")

    # ------------------------------------------------------------------
    def _run_single(self):
        import asyncio
        import tempfile

        rate_str = self._rate_str()
        total = len(self._chunks)

        async def _synth_all(mp3_path: str):
            import edge_tts  # type: ignore
            with open(mp3_path, "wb") as out:
                for i, text in enumerate(self._chunks):
                    if self._cancel.is_set():
                        raise RuntimeError("cancelled")
                    self.progress.emit(i, total, f"Synthesizing page {i + 1} of {total}…")
                    text = (text or "").strip()
                    if not text:
                        self._emit_log(f"Page {i + 1}/{total}: (no text, skipped)")
                        continue
                    self._emit_log(
                        f"Page {i + 1}/{total}: synthesizing {len(text):,} characters…"
                    )
                    communicate = edge_tts.Communicate(text, self._voice, rate=rate_str)
                    written = 0
                    async for chunk in communicate.stream():
                        if self._cancel.is_set():
                            raise RuntimeError("cancelled")
                        if chunk.get("type") == "audio":
                            data = chunk["data"]
                            out.write(data)
                            written += len(data)
                    self._emit_log(
                        f"  page {i + 1} done ({self._fmt_size(written)})"
                    )

        loop = asyncio.new_event_loop()
        tmp_mp3 = None
        try:
            asyncio.set_event_loop(loop)
            want_wav = self._out_path.lower().endswith(".wav")
            if want_wav:
                fd, tmp_mp3 = tempfile.mkstemp(suffix=".mp3")
                os.close(fd)
                mp3_target = tmp_mp3
            else:
                mp3_target = self._out_path

            loop.run_until_complete(_synth_all(mp3_target))

            if self._cancel.is_set():
                self.failed.emit("Cancelled.")
                return

            if want_wav:
                self.progress.emit(total, total, "Converting to WAV…")
                self._emit_log("Converting MP3 to WAV via ffmpeg…")
                if not self._convert_mp3_to_wav(tmp_mp3, self._out_path):
                    self.failed.emit(
                        "Generated MP3 but could not convert to WAV.\n"
                        "WAV output needs ffmpeg on PATH. The MP3 is available "
                        "if you choose MP3 output instead."
                    )
                    return
                self._emit_log("WAV conversion complete.")

            try:
                final_sz = os.path.getsize(self._out_path)
                self._emit_log(f"Saved: {self._out_path} ({self._fmt_size(final_sz)})")
            except Exception:
                self._emit_log(f"Saved: {self._out_path}")
            self.progress.emit(total, total, "Done.")
            self._emit_log("All done.")
            self.finished.emit(self._out_path)
        except RuntimeError as e:
            if str(e) == "cancelled":
                self.failed.emit("Cancelled.")
            else:
                self.failed.emit(str(e))
        except Exception as e:
            self.failed.emit(self._friendly_error(e))
        finally:
            try:
                loop.close()
            except Exception:
                pass
            if tmp_mp3 and os.path.isfile(tmp_mp3):
                try:
                    os.remove(tmp_mp3)
                except Exception:
                    pass

    # ------------------------------------------------------------------
    def _run_chapters(self):
        """Synthesize one audio file per chapter into self._out_dir."""
        import asyncio
        import tempfile

        want_wav = self._out_path.lower().endswith(".wav")
        ext = "wav" if want_wav else "mp3"
        chapters = self._chapters or []
        total = len(chapters)

        loop = asyncio.new_event_loop()
        written: List[str] = []
        try:
            asyncio.set_event_loop(loop)
            for i, (title, text) in enumerate(chapters):
                if self._cancel.is_set():
                    self.failed.emit("Cancelled.")
                    return
                safe = _safe_filename(title) or f"Chapter {i + 1}"
                base = f"{i + 1:02d} - {safe}"
                self.progress.emit(i, total, f"Chapter {i + 1} of {total}: {title[:40]}")
                self._emit_log(
                    f"Chapter {i + 1}/{total}: \"{title}\" "
                    f"({len((text or '').strip()):,} characters)"
                )
                if not (text or "").strip():
                    self._emit_log("  (no text, skipped)")
                    continue

                if want_wav:
                    fd, tmp_mp3 = tempfile.mkstemp(suffix=".mp3")
                    os.close(fd)
                    try:
                        loop.run_until_complete(
                            self._synth_text_to_file(text, tmp_mp3, label=f"ch {i + 1}")
                        )
                        wav_path = os.path.join(self._out_dir, base + ".wav")
                        self._emit_log(f"  converting to WAV: {base}.wav")
                        if not self._convert_mp3_to_wav(tmp_mp3, wav_path):
                            self.failed.emit(
                                "WAV output needs ffmpeg on PATH. Try MP3 format instead."
                            )
                            return
                        written.append(wav_path)
                        self._emit_log(f"  saved: {base}.wav")
                    finally:
                        if os.path.isfile(tmp_mp3):
                            try:
                                os.remove(tmp_mp3)
                            except Exception:
                                pass
                else:
                    mp3_path = os.path.join(self._out_dir, base + ".mp3")
                    loop.run_until_complete(
                        self._synth_text_to_file(text, mp3_path, label=f"ch {i + 1}")
                    )
                    written.append(mp3_path)
                    try:
                        self._emit_log(
                            f"  saved: {base}.mp3 ({self._fmt_size(os.path.getsize(mp3_path))})"
                        )
                    except Exception:
                        self._emit_log(f"  saved: {base}.mp3")

            if self._cancel.is_set():
                self.failed.emit("Cancelled.")
                return
            self.progress.emit(total, total, "Done.")
            self._emit_log(f"All done — {len(written)} file(s) written to:")
            self._emit_log(f"  {self._out_dir}")
            self.finished.emit(self._out_dir)
        except RuntimeError as e:
            if str(e) == "cancelled":
                self.failed.emit("Cancelled.")
            else:
                self.failed.emit(str(e))
        except Exception as e:
            self.failed.emit(self._friendly_error(e))
        finally:
            try:
                loop.close()
            except Exception:
                pass

    @staticmethod
    def _friendly_error(e: Exception) -> str:
        msg = str(e)
        if any(s in msg.lower() for s in ("getaddrinfo", "connect", "network", "resolve", "temporary failure")):
            return ("Could not reach the Microsoft neural-voice service.\n\n"
                    "edge-tts requires an internet connection. Please check "
                    "your connection and try again.\n\nDetails: " + msg)
        return msg

    @staticmethod
    def _convert_mp3_to_wav(mp3_path: str, wav_path: str) -> bool:
        """Convert MP3 -> WAV using ffmpeg if available. Returns success."""
        import shutil
        import subprocess
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return False
        try:
            creationflags = 0
            if sys.platform.startswith("win"):
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.run(
                [ffmpeg, "-y", "-i", mp3_path, wav_path],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
            return os.path.isfile(wav_path)
        except Exception:
            return False


class _PageTile(QWidget):
    """A selectable page-preview tile: checkbox + label + thumbnail image."""

    toggled = pyqtSignal()

    def __init__(self, page_index: int, parent=None):
        super().__init__(parent)
        self.page_index = page_index
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)

        self.check = QCheckBox(f"Page {page_index + 1}")
        self.check.toggled.connect(lambda _c: self.toggled.emit())
        lay.addWidget(self.check, alignment=Qt.AlignmentFlag.AlignHCenter)

        self.img = QLabel()
        self.img.setFixedSize(QSize(150, 200))
        self.img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.img.setStyleSheet("border: 1px solid rgba(128,128,128,0.5);")
        self.img.setText("…")
        lay.addWidget(self.img, alignment=Qt.AlignmentFlag.AlignHCenter)

    def set_pixmap(self, pix: QPixmap):
        self.img.setPixmap(pix.scaled(
            self.img.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))

    def is_checked(self) -> bool:
        return self.check.isChecked()

    def set_checked(self, on: bool):
        self.check.setChecked(on)


class ProgressLogDialog(QDialog):
    """A progress dialog that also shows a live, scrolling activity log so the
    user can see exactly what's happening (per-chapter synthesis, bytes
    received, files saved, …) instead of a bar sitting at 0%."""

    canceled = pyqtSignal()

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        # Non-modal so the user can keep reading/using the app while audio
        # generates in the background. Give the window real min/max/close
        # buttons so it can be minimised to the taskbar.
        self.setModal(False)
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowCloseButtonHint
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.CustomizeWindowHint
        )
        self.setMinimumWidth(520)
        self._cancelled = False

        root = QVBoxLayout(self)

        self.status_lbl = QLabel("Starting…")
        self.status_lbl.setWordWrap(True)
        self.status_lbl.setStyleSheet("font-weight: 600;")
        root.addWidget(self.status_lbl)

        row = QHBoxLayout()
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setTextVisible(True)
        row.addWidget(self.bar, 1)
        root.addLayout(row)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(220)
        self.log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        # Monospace so timestamps and byte counts line up.
        f = QFont("Consolas")
        f.setStyleHint(QFont.StyleHint.Monospace)
        f.setPointSize(9)
        self.log_view.setFont(f)
        root.addWidget(self.log_view, 1)

        hint = QLabel(
            "Generation runs in the background — you can keep using the app. "
            "Minimise or click “Run in Background” to hide this window."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #9aa0a6;")
        root.addWidget(hint)

        btns = QHBoxLayout()
        self.copy_btn = QPushButton("Copy Log")
        self.copy_btn.clicked.connect(self._copy_log)
        btns.addWidget(self.copy_btn)
        btns.addStretch(1)
        self.bg_btn = QPushButton("Run in Background")
        self.bg_btn.setToolTip("Hide this window and keep generating in the background.")
        self.bg_btn.clicked.connect(self.showMinimized)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self._on_cancel)
        btns.addWidget(self.bg_btn)
        btns.addWidget(self.cancel_btn)
        root.addLayout(btns)

    def append_log(self, line: str):
        self.log_view.appendPlainText(line)
        # Auto-scroll to the newest line.
        sb = self.log_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def set_status(self, text: str):
        self.status_lbl.setText(text)

    def set_progress(self, done: int, total: int):
        if total <= 0:
            self.bar.setRange(0, 0)          # indeterminate (busy) bar
            return
        self.bar.setRange(0, total)
        self.bar.setValue(min(done, total))

    def finish(self, ok: bool, message: str):
        """Switch the dialog into its 'done' state: full bar, Close button."""
        if ok:
            self.set_progress(1, 1)
            self.bar.setValue(self.bar.maximum())
        self.set_status(message)
        # No longer running: drop the background button, turn Cancel into Close.
        self.bg_btn.hide()
        self.cancel_btn.setEnabled(True)
        self.cancel_btn.setText("Close")
        try:
            self.cancel_btn.clicked.disconnect()
        except TypeError:
            pass
        self.cancel_btn.clicked.connect(self.accept)
        # Bring the window back to the foreground so the result is visible even
        # if the user had minimised it.
        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()

    def _copy_log(self):
        QApplication.clipboard().setText(self.log_view.toPlainText())

    def _on_cancel(self):
        if self._cancelled:
            return
        self._cancelled = True
        self.set_status("Cancelling…")
        self.cancel_btn.setEnabled(False)
        self.canceled.emit()

    def closeEvent(self, e):
        # Closing the window mid-run behaves like Cancel.
        if not self._cancelled and self.cancel_btn.text() == "Cancel":
            self._on_cancel()
        super().closeEvent(e)


class AudiobookDialog(QDialog):
    """Dialog to select PDF pages (with previews) and export them as a
    natural-sounding audiobook (MP3/WAV) using edge-tts neural voices."""

    THUMB_ZOOM = 0.35   # render scale for the preview thumbnails

    def __init__(self, viewer: "PdfViewer", settings: QSettings, parent=None):
        super().__init__(parent)
        self.viewer = viewer
        self.doc = viewer.doc
        self.settings = settings
        self._tiles: List[_PageTile] = []
        self._chapter_items: List[QTreeWidgetItem] = []
        self._worker: Optional[AudiobookWorker] = None
        self._thread: Optional[QThread] = None

        self.setWindowTitle("Create Audiobook from Pages")
        self.resize(860, 760)

        root = QVBoxLayout(self)

        # --- top: selection controls --------------------------------------
        top = QHBoxLayout()
        self.btn_all = QPushButton("Select All")
        self.btn_none = QPushButton("Clear")
        self.btn_all.clicked.connect(lambda: self._set_all(True))
        self.btn_none.clicked.connect(lambda: self._set_all(False))
        top.addWidget(self.btn_all)
        top.addWidget(self.btn_none)
        top.addSpacing(12)
        top.addWidget(QLabel("Range:"))
        self.range_edit = QLineEdit()
        self.range_edit.setPlaceholderText("e.g. 1-5, 8, 12")
        self.range_edit.setToolTip("Type a page range then click Apply, e.g. 1-5, 8, 12")
        self.range_edit.returnPressed.connect(self._apply_range)
        top.addWidget(self.range_edit, 1)
        self.btn_range = QPushButton("Apply")
        self.btn_range.clicked.connect(self._apply_range)
        top.addWidget(self.btn_range)
        root.addLayout(top)

        # --- middle: scrollable thumbnail grid ----------------------------
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        grid_host = QWidget()
        self.grid = QGridLayout(grid_host)
        self.grid.setSpacing(10)
        self.scroll.setWidget(grid_host)
        root.addWidget(self.scroll, 1)

        # --- chapter split controls ---------------------------------------
        chapter_head = QHBoxLayout()
        chapter_head.setSpacing(6)
        self.chapter_lbl = QLabel("Split whole book by chapter")
        self.chapter_lbl.setStyleSheet("font-weight: 600;")
        chapter_head.addWidget(self.chapter_lbl)
        self.chapter_count_lbl = QLabel("")
        self.chapter_count_lbl.setStyleSheet("color: #9aa0a6;")
        chapter_head.addWidget(self.chapter_count_lbl)
        chapter_head.addStretch(1)
        self.btn_ch_num = QPushButton("Numbered")
        self.btn_ch_num.setToolTip("Select only the numbered chapters (e.g. 1, 2, 3 …)")
        self.btn_ch_all = QPushButton("All")
        self.btn_ch_all.setToolTip("Select every chapter, including front/back matter")
        self.btn_ch_none = QPushButton("None")
        self.btn_ch_none.setToolTip("Clear the chapter selection")
        for b in (self.btn_ch_num, self.btn_ch_all, self.btn_ch_none):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_ch_num.clicked.connect(self._select_numbered_chapters)
        self.btn_ch_all.clicked.connect(lambda: self._set_all_chapters(True))
        self.btn_ch_none.clicked.connect(lambda: self._set_all_chapters(False))
        chapter_head.addWidget(self.btn_ch_num)
        chapter_head.addWidget(self.btn_ch_all)
        chapter_head.addWidget(self.btn_ch_none)
        root.addLayout(chapter_head)
        self.chapter_tree = QTreeWidget()
        self.chapter_tree.setHeaderLabels(["", "Pages", "Chapter"])
        self.chapter_tree.setRootIsDecorated(False)
        self.chapter_tree.setAlternatingRowColors(True)
        self.chapter_tree.setUniformRowHeights(True)
        self.chapter_tree.setMinimumHeight(160)
        self.chapter_tree.setMaximumHeight(230)
        self.chapter_tree.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.chapter_tree.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # Clicking anywhere on a row toggles its checkbox (bigger hit target).
        self.chapter_tree.itemClicked.connect(self._on_chapter_row_clicked)
        # Comfortable row height + subtle grid so ranges are easy to scan.
        self.chapter_tree.setStyleSheet(
            """
            QTreeWidget { border: 1px solid palette(mid); border-radius: 6px; }
            QTreeWidget::item { padding: 7px 4px; }
            QHeaderView::section {
                padding: 6px 8px; font-weight: 600; border: none;
                border-bottom: 1px solid palette(mid);
            }
            """
        )
        hdr = self.chapter_tree.header()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        hdr.resizeSection(0, 40)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        hdr.setStretchLastSection(True)
        self.chapter_tree.itemChanged.connect(lambda *_: self._update_chapter_count())
        root.addWidget(self.chapter_tree)

        # --- bottom: voice / format / actions -----------------------------
        opts = QHBoxLayout()
        opts.addWidget(QLabel("Voice:"))
        self.voice_combo = QComboBox()
        self.voice_combo.setMinimumWidth(260)
        opts.addWidget(self.voice_combo, 1)

        opts.addWidget(QLabel("Speed:"))
        self.speed_combo = QComboBox()
        # Playback-speed multipliers. edge-tts uses a percentage relative to the
        # normal rate, so rate% = (multiplier - 1) * 100  (e.g. 1.5x -> +50%).
        default_idx = 0
        for i, mult in enumerate((0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0)):
            pct = round((mult - 1.0) * 100)
            # Show "1x" rather than "1.0x", "1.25x" rather than "1.25x".
            label = f"{mult:g}x"
            if mult == 1.0:
                label = "1x (Normal)"
                default_idx = i
            self.speed_combo.addItem(label, pct)
        self.speed_combo.setCurrentIndex(default_idx)
        opts.addWidget(self.speed_combo)

        opts.addWidget(QLabel("Format:"))
        self.format_combo = QComboBox()
        self.format_combo.addItem("MP3", "mp3")
        self.format_combo.addItem("WAV (needs ffmpeg)", "wav")
        opts.addWidget(self.format_combo)
        root.addLayout(opts)

        self.status_lbl = QLabel("")
        root.addWidget(self.status_lbl)

        # Progress+log dialog is created on demand when generation starts.
        self.progress = None

        actions = QHBoxLayout()
        self.sel_count_lbl = QLabel("0 pages selected")
        actions.addWidget(self.sel_count_lbl)
        actions.addStretch(1)
        # Whole-book, split into one file per top-level chapter (uses the TOC).
        self.btn_chapters = QPushButton("Whole Book by Chapters…")
        self.btn_chapters.setToolTip(
            "Generate the entire book as one audio file per chapter, using the "
            "document's table of contents."
        )
        self.btn_chapters.clicked.connect(self._generate_chapters)
        self.btn_generate = QPushButton("Generate from Selection…")
        self.btn_generate.clicked.connect(self._generate)
        self.btn_close = QPushButton("Close")
        self.btn_close.clicked.connect(self.reject)
        actions.addWidget(self.btn_chapters)
        actions.addWidget(self.btn_generate)
        actions.addWidget(self.btn_close)
        root.addLayout(actions)

        self._populate_voices()
        self._build_tiles()
        self._build_chapter_list()
        # Preselect the current page for convenience.
        cur = viewer.current_page_index()
        if 0 <= cur < len(self._tiles):
            self._tiles[cur].set_checked(True)
        self._update_selection_count()
        self._update_chapter_count()
        if not self._chapter_items:
            self.btn_chapters.setToolTip("This PDF has no table of contents to split by.")
        # Render previews lazily after the dialog is shown.
        QTimer.singleShot(0, self._render_previews)

    # ------------------------------------------------------------------
    # Preferred default voice when the user hasn't chosen one yet.
    DEFAULT_VOICE = "en-US-AriaNeural"

    def _populate_voices(self):
        voices = _fetch_edge_tts_voices()
        idx_default = -1
        idx_us = -1
        for i, (short, label) in enumerate(voices):
            self.voice_combo.addItem(label, short)
            if short == self.DEFAULT_VOICE:
                idx_default = i
            if idx_us == -1 and short.startswith("en-US"):
                idx_us = i
        # Always default to US Aria. Fall back to any US English voice, then
        # the first available voice if Aria isn't listed.
        if idx_default != -1:
            idx_to_select = idx_default
        elif idx_us != -1:
            idx_to_select = idx_us
        else:
            idx_to_select = 0
        self.voice_combo.setCurrentIndex(idx_to_select)

    def _build_tiles(self):
        cols = 4
        for i in range(self.doc.page_count):
            tile = _PageTile(i)
            tile.toggled.connect(self._update_selection_count)
            self._tiles.append(tile)
            self.grid.addWidget(tile, i // cols, i % cols)

    def _build_chapter_list(self):
        """Show TOC-derived chapter ranges and let the user choose which
        chapter files to create. Numbered chapters are selected by default;
        front/back matter such as Welcome, Copyright/Preface, Glossary stays
        visible but unchecked so books like this show 17 selected out of 20.
        """
        self.chapter_tree.clear()
        self._chapter_items = []
        chapters = self._toc_chapters()
        if not chapters:
            item = QTreeWidgetItem(["", "", "(No table of contents found)"])
            item.setDisabled(True)
            self.chapter_tree.addTopLevelItem(item)
            self.chapter_tree.setEnabled(False)
            return
        self.chapter_tree.setEnabled(True)
        for title, start, end in chapters:
            n_pages = end - start + 1
            if start != end:
                pages = f"{start + 1}\u2013{end + 1}"          # en-dash range
            else:
                pages = f"{start + 1}"
            item = QTreeWidgetItem(["", pages, title])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            checked = self._is_default_chapter(title)
            item.setCheckState(0, Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            item.setData(0, Qt.ItemDataRole.UserRole, (title, start, end))

            # Center the checkbox column; right-align the page range.
            item.setTextAlignment(0, Qt.AlignmentFlag.AlignCenter)
            item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            item.setToolTip(
                1, f"{n_pages} page{'s' if n_pages != 1 else ''}"
            )
            item.setToolTip(2, title)

            # Emphasise real (numbered) chapters; de-emphasise front/back matter
            # (Welcome, Copyright, Glossary, Index, …) that stays unchecked.
            font = item.font(2)
            if checked:
                font.setBold(True)
            else:
                item.setForeground(1, QBrush(QColor("#9aa0a6")))
                item.setForeground(2, QBrush(QColor("#9aa0a6")))
            item.setFont(2, font)

            self.chapter_tree.addTopLevelItem(item)
            self._chapter_items.append(item)
        self.chapter_tree.resizeColumnToContents(1)

    def _on_chapter_row_clicked(self, item: QTreeWidgetItem, column: int):
        """Toggle a row's checkbox when the user clicks anywhere on the row
        (not just the tiny checkbox), giving a much larger hit target."""
        if item is None or not (item.flags() & Qt.ItemFlag.ItemIsUserCheckable):
            return
        # Clicking directly on the checkbox column already toggles it; only
        # mirror the toggle for clicks on the Pages/Chapter columns.
        if column == 0:
            return
        new_state = (
            Qt.CheckState.Unchecked
            if item.checkState(0) == Qt.CheckState.Checked
            else Qt.CheckState.Checked
        )
        item.setCheckState(0, new_state)

    @staticmethod
    def _is_default_chapter(title: str) -> bool:
        """Default checked chapters are numbered chapter headings, e.g.
        '1: Introduction...' through '17: Leadership...'."""
        return bool(re.match(r"^\s*\d+\s*[:.\-]", title or ""))

    def _selected_chapters(self) -> List[Tuple[str, int, int]]:
        selected: List[Tuple[str, int, int]] = []
        for item in self._chapter_items:
            if item.checkState(0) != Qt.CheckState.Checked:
                continue
            data = item.data(0, Qt.ItemDataRole.UserRole)
            if data:
                selected.append(data)
        return selected

    def _set_all_chapters(self, checked: bool):
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        self.chapter_tree.blockSignals(True)
        try:
            for item in self._chapter_items:
                item.setCheckState(0, state)
        finally:
            self.chapter_tree.blockSignals(False)
        self._update_chapter_count()

    def _select_numbered_chapters(self):
        self.chapter_tree.blockSignals(True)
        try:
            for item in self._chapter_items:
                data = item.data(0, Qt.ItemDataRole.UserRole)
                title = data[0] if data else ""
                item.setCheckState(
                    0,
                    Qt.CheckState.Checked if self._is_default_chapter(title)
                    else Qt.CheckState.Unchecked,
                )
        finally:
            self.chapter_tree.blockSignals(False)
        self._update_chapter_count()

    def _update_chapter_count(self):
        total = len(self._chapter_items)
        selected = len(self._selected_chapters())
        if total == 0:
            self.chapter_count_lbl.setText("(no table of contents found)")
            self.btn_chapters.setEnabled(False)
            self.btn_chapters.setText("Whole Book by Chapters…")
            for b in (self.btn_ch_num, self.btn_ch_all, self.btn_ch_none):
                b.setEnabled(False)
            return
        for b in (self.btn_ch_num, self.btn_ch_all, self.btn_ch_none):
            b.setEnabled(True)
        self.chapter_count_lbl.setText(f"— {selected} of {total} selected")
        self.btn_chapters.setEnabled(selected > 0 and self._thread is None)
        self.btn_chapters.setText(f"Whole Book by Chapters ({selected})…")

    def _render_previews(self):
        """Render each page to a small pixmap for its tile. Runs on the GUI
        thread but pages are small; done in slices to stay responsive."""
        if self.doc is None:
            return
        pending = list(range(len(self._tiles)))

        def render_slice():
            done = 0
            while pending and done < 3:
                i = pending.pop(0)
                try:
                    page = self.doc.load_page(i)
                    mat = fitz.Matrix(self.THUMB_ZOOM, self.THUMB_ZOOM)
                    pix = page.get_pixmap(matrix=mat, alpha=False)
                    img = QImage(
                        pix.samples, pix.width, pix.height, pix.stride,
                        QImage.Format.Format_RGB888,
                    ).copy()
                    self._tiles[i].set_pixmap(QPixmap.fromImage(img))
                except Exception:
                    self._tiles[i].img.setText("(preview\nunavailable)")
                done += 1
            if pending:
                QTimer.singleShot(0, render_slice)

        render_slice()

    # ------------------------------------------------------------------
    def _set_all(self, on: bool):
        for t in self._tiles:
            t.set_checked(on)
        self._update_selection_count()

    def _apply_range(self):
        spec = self.range_edit.text().strip()
        if not spec:
            return
        wanted = self._parse_range(spec, self.doc.page_count)
        if wanted is None:
            QMessageBox.warning(self, "Invalid range",
                                "Could not parse that range. Use e.g. 1-5, 8, 12.")
            return
        for i, t in enumerate(self._tiles):
            t.set_checked((i + 1) in wanted)
        self._update_selection_count()

    @staticmethod
    def _parse_range(spec: str, page_count: int) -> Optional[set]:
        result: set = set()
        try:
            for part in spec.split(","):
                part = part.strip()
                if not part:
                    continue
                if "-" in part:
                    a, b = part.split("-", 1)
                    a, b = int(a), int(b)
                    if a > b:
                        a, b = b, a
                    for n in range(a, b + 1):
                        if 1 <= n <= page_count:
                            result.add(n)
                else:
                    n = int(part)
                    if 1 <= n <= page_count:
                        result.add(n)
        except ValueError:
            return None
        return result

    def _selected_indices(self) -> List[int]:
        return [t.page_index for t in self._tiles if t.is_checked()]

    def _update_selection_count(self):
        n = len(self._selected_indices())
        self.sel_count_lbl.setText(f"{n} page{'s' if n != 1 else ''} selected")

    # ------------------------------------------------------------------
    def _page_text(self, page_index: int) -> str:
        """Best text for a page: viewer's word cache (native + OCR) joined,
        falling back to raw get_text."""
        words = self.viewer.get_page_words(page_index)
        if words:
            return " ".join(w.text for w in words)
        try:
            return self.doc.load_page(page_index).get_text("text")
        except Exception:
            return ""

    # ------------------------------------------------------------------
    def _toc_chapters(self) -> List[Tuple[str, int, int]]:
        """Return the book's chapters from level-1 TOC entries as
        [(title, start_page_idx, end_page_idx_inclusive), ...] (0-based)."""
        try:
            toc = self.doc.get_toc(simple=True)
        except Exception:
            toc = []
        # Keep only top-level entries.
        tops = [(t[1], t[2] - 1) for t in toc if t[0] == 1 and t[2] >= 1]
        if not tops:
            return []
        n = self.doc.page_count
        chapters: List[Tuple[str, int, int]] = []
        for i, (title, start) in enumerate(tops):
            start = max(0, min(start, n - 1))
            # End is the page just before the next chapter starts.
            if i + 1 < len(tops):
                end = max(start, tops[i + 1][1] - 1)
            else:
                end = n - 1
            end = min(end, n - 1)
            chapters.append((title, start, end))
        return chapters

    def has_toc(self) -> bool:
        return len(self._toc_chapters()) > 0

    def _generate(self):
        if not edge_tts_available():
            QMessageBox.critical(
                self, "edge-tts not installed",
                "This feature uses Microsoft neural voices via the 'edge-tts' "
                "package, which isn't installed.\n\nInstall it with:\n"
                "    pip install edge-tts\n\nThen reopen this dialog."
            )
            return
        pages = self._selected_indices()
        if not pages:
            QMessageBox.information(self, APP_NAME, "Select at least one page.")
            return

        # Gather text; warn if selection has no extractable text at all.
        # For many pages, extract native text in parallel across CPU cores;
        # fall back to the viewer's per-page word/OCR cache for anything the
        # parallel native-text pass couldn't read (e.g. scanned pages).
        chunks: List[str] = []
        any_text = False
        par_text: Dict[int, str] = {}
        if len(pages) > 8 and self.viewer.doc_path:
            def _native(page, idx):
                try:
                    return page.get_text("text")
                except Exception:
                    return ""
            par_text = parallel_pages(self.viewer.doc_path, pages, _native)
        for i in pages:
            txt = (par_text.get(i) or "").strip()
            if not txt:
                txt = self._page_text(i).strip()
            if txt:
                any_text = True
            chunks.append(txt)
        if not any_text:
            QMessageBox.warning(
                self, "No text found",
                "The selected pages don't have any extractable text.\n\n"
                "If these are scanned pages, enable 'Auto-OCR Scanned Pages' "
                "in the Tools menu, let it finish, then try again."
            )
            return

        fmt = self.format_combo.currentData()
        base = "audiobook"
        if self.viewer.doc_path:
            base = os.path.splitext(os.path.basename(self.viewer.doc_path))[0]
        default_name = f"{base}.{fmt}"
        filt = "MP3 Audio (*.mp3)" if fmt == "mp3" else "WAV Audio (*.wav)"
        out_path, _ = QFileDialog.getSaveFileName(
            self, "Save Audiobook As", default_name, filt
        )
        if not out_path:
            return
        if not out_path.lower().endswith("." + fmt):
            out_path += "." + fmt

        voice = self.voice_combo.currentData()
        self.settings.setValue("audiobook_voice", voice)
        rate_pct = self.speed_combo.currentData()

        # Spin up the worker on a background thread.
        self._thread = QThread(self)
        self._worker = AudiobookWorker(chunks, voice, rate_pct, out_path)
        self._launch_worker(len(chunks), "Generating audiobook…")

    def _generate_chapters(self):
        if not edge_tts_available():
            QMessageBox.critical(
                self, "edge-tts not installed",
                "This feature uses Microsoft neural voices via the 'edge-tts' "
                "package, which isn't installed.\n\nInstall it with:\n"
                "    pip install edge-tts\n\nThen reopen this dialog."
            )
            return
        chapters = self._selected_chapters()
        if not chapters:
            QMessageBox.information(
                self, APP_NAME,
                "Select at least one chapter to create."
            )
            return

        # Gather text for the whole book in parallel, then slice per chapter.
        n = self.doc.page_count
        page_text: Dict[int, str] = {}
        if self.viewer.doc_path:
            def _native(page, idx):
                try:
                    return page.get_text("text")
                except Exception:
                    return ""
            page_text = parallel_pages(self.viewer.doc_path, range(n), _native)

        def text_for_page(i: int) -> str:
            t = (page_text.get(i) or "").strip()
            if not t:
                t = self._page_text(i).strip()
            return t

        chapter_data: List[Tuple[str, str]] = []
        any_text = False
        for title, start, end in chapters:
            parts = [text_for_page(p) for p in range(start, end + 1)]
            body = "\n".join(p for p in parts if p).strip()
            if body:
                any_text = True
            # Read the chapter title aloud first, then its content.
            full = f"{title}.\n\n{body}" if body else title
            chapter_data.append((title, full))

        if not any_text:
            QMessageBox.warning(
                self, "No text found",
                "The book's pages don't have extractable text.\n\n"
                "If this is a scanned PDF, enable 'Auto-OCR Scanned Pages' in "
                "the Tools menu, let it finish, then try again."
            )
            return

        fmt = self.format_combo.currentData()
        out_dir = QFileDialog.getExistingDirectory(
            self, "Choose a folder for the chapter audio files"
        )
        if not out_dir:
            return
        # Put the files in a dedicated sub-folder named after the book.
        book = "audiobook"
        if self.viewer.doc_path:
            book = os.path.splitext(os.path.basename(self.viewer.doc_path))[0]
        target_dir = os.path.join(out_dir, _safe_filename(book) + " - Audiobook")
        try:
            os.makedirs(target_dir, exist_ok=True)
        except Exception as e:
            QMessageBox.critical(self, "Cannot create folder", str(e))
            return

        voice = self.voice_combo.currentData()
        self.settings.setValue("audiobook_voice", voice)
        rate_pct = self.speed_combo.currentData()

        self._thread = QThread(self)
        # out_path only carries the extension hint (.mp3/.wav) for the worker.
        self._worker = AudiobookWorker(
            [], voice, rate_pct, out_path=f"x.{fmt}",
            chapters=chapter_data, out_dir=target_dir,
        )
        self._launch_worker(
            len(chapter_data),
            f"Generating {len(chapter_data)} chapter file(s)…",
        )

    def _launch_worker(self, total: int, status: str):
        """Common wiring: move worker to its thread, build the log dialog,
        connect signals, and start."""
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.log.connect(self._on_log)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)

        self.progress = ProgressLogDialog("Generating Audiobook", self)
        self.progress.canceled.connect(self._cancel_generation)
        self.progress.set_status(status)
        self.progress.set_progress(0, total)
        self.progress.append_log("Starting…")
        self.progress.show()

        self._set_busy(True)
        self._thread.start()

    def _set_busy(self, busy: bool):
        self.btn_generate.setEnabled(not busy)
        self.btn_chapters.setEnabled(not busy and len(self._selected_chapters()) > 0)
        self.chapter_tree.setEnabled(not busy and bool(self._chapter_items))
        for b in (self.btn_ch_num, self.btn_ch_all, self.btn_ch_none):
            b.setEnabled(not busy and bool(self._chapter_items))
        self.btn_all.setEnabled(not busy)
        self.btn_none.setEnabled(not busy)
        self.btn_range.setEnabled(not busy)

    @pyqtSlot(int, int, str)
    def _on_progress(self, done: int, total: int, msg: str):
        if self.progress is not None:
            self.progress.set_progress(done, total)
            self.progress.set_status(msg)
        self.status_lbl.setText(msg)

    @pyqtSlot(str)
    def _on_log(self, line: str):
        if self.progress is not None:
            self.progress.append_log(line)

    @pyqtSlot(str)
    def _on_finished(self, path: str):
        self._teardown_thread()
        self._set_busy(False)
        self.status_lbl.setText(f"Saved: {path}")
        is_dir = os.path.isdir(path)
        if self.progress is not None:
            self.progress.finish(True, f"Done — saved to:\n{path}")
        what = "chapter files were saved to folder" if is_dir else "audiobook was saved to"
        ret = QMessageBox.information(
            self, "Audiobook created",
            f"Your {what}:\n{path}",
            QMessageBox.StandardButton.Open | QMessageBox.StandardButton.Ok,
        )
        if ret == QMessageBox.StandardButton.Open:
            self._open_in_system(path)

    @pyqtSlot(str)
    def _on_failed(self, msg: str):
        self._teardown_thread()
        self._set_busy(False)
        cancelled = msg.lower() == "cancelled."
        self.status_lbl.setText("Cancelled." if cancelled else "Failed.")
        if self.progress is not None:
            self.progress.append_log("Cancelled." if cancelled else f"ERROR: {msg}")
            self.progress.finish(False, "Cancelled." if cancelled else "Failed.")
        if not cancelled:
            QMessageBox.critical(self, "Audiobook generation failed", msg)

    def _cancel_generation(self):
        if self._worker is not None:
            self._worker.cancel()
        self.status_lbl.setText("Cancelling…")

    def _teardown_thread(self):
        if self._thread is not None:
            # run() blocks inside asyncio's loop.run_until_complete(); quit()
            # can't interrupt that, only the worker's _cancel event (checked in
            # the audio stream) can. Signal cancel first so the thread actually
            # returns before we wait, and terminate as a last resort so we never
            # delete a QThread that's still executing run().
            if self._worker is not None:
                try:
                    self._worker.cancel()
                except Exception:
                    pass
            self._thread.quit()
            if not self._thread.wait(5000):
                self._thread.terminate()
                self._thread.wait(1000)
            self._thread = None
        self._worker = None

    @staticmethod
    def _open_in_system(path: str):
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                import subprocess
                subprocess.Popen(["open", path])
            else:
                import subprocess
                subprocess.Popen(["xdg-open", path])
        except Exception:
            pass

    def reject(self):
        # If generation is still running, confirm before cancelling — otherwise
        # closing this window would silently abort the background job.
        if self._thread is not None and self._thread.isRunning():
            ret = QMessageBox.question(
                self, "Generation in progress",
                "Audio is still being generated.\n\n"
                "Close and cancel it? (Choose “No” to keep it running — you can "
                "minimise this window and keep using the app.)",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ret != QMessageBox.StandardButton.Yes:
                return                      # keep the dialog (and job) open
            if self._worker is not None:
                self._worker.cancel()
            self._teardown_thread()
            if self.progress is not None:
                self.progress.close()
        super().reject()


# ===========================================================================
# Start Screen  (shown when no PDF tabs are open)
# ===========================================================================

class StartScreen(QWidget):
    """Welcome screen shown on cold start.  Displays up to 10 recent files
    as a vertical list of full-width rows (full filename always visible)."""

    openRequested = pyqtSignal(str)         # emitted when user clicks a recent file
    openDialogRequested = pyqtSignal()      # emitted when user clicks "Open PDF…"
    clearRecentRequested = pyqtSignal()     # emitted when user clicks "Clear list"
    removeRecentRequested = pyqtSignal(str) # emitted when user clicks a row's ✕

    _ROW_H = 56
    _CONTENT_MAX_W = 720    # max width of the centered content column
    _TOP_MARGIN    = 60     # space from top of window to title

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("startScreen")
        self._recent: List[str] = []
        self._build_ui()

    # ------------------------------------------------------------------
    def _build_ui(self):
        # Outer:  top-margin | centered-content | bottom-stretch
        outer = QVBoxLayout(self)
        outer.setContentsMargins(40, self._TOP_MARGIN, 40, 40)
        outer.setSpacing(0)

        # Horizontal centering row: stretch | content | stretch
        center_row = QHBoxLayout()
        center_row.setSpacing(0)
        center_row.addStretch(1)

        content = QWidget()
        content.setMaximumWidth(self._CONTENT_MAX_W)
        content.setMinimumWidth(420)
        col = QVBoxLayout(content)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)

        # --- Hero ---------------------------------------------------------
        title = QLabel(APP_NAME)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setObjectName("startTitle")
        col.addWidget(title)

        sub = QLabel("A fast, modern PDF reader")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub.setObjectName("startSubtitle")
        col.addWidget(sub)

        col.addSpacing(24)

        # --- Open button --------------------------------------------------
        btn_open = QPushButton("Open PDF…")
        btn_open.setObjectName("startOpenBtn")
        btn_open.setFixedSize(180, 42)
        btn_open.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_open.clicked.connect(self.openDialogRequested)

        btn_row = QHBoxLayout()
        btn_row.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        btn_row.addWidget(btn_open)
        col.addLayout(btn_row)

        col.addSpacing(36)

        # --- Recent files header (with "Clear" link on the right) --------
        self._recent_header = QWidget()
        hdr = QHBoxLayout(self._recent_header)
        hdr.setContentsMargins(0, 0, 0, 6)
        hdr.setSpacing(12)

        self._recent_label = QLabel("Recent Files")
        self._recent_label.setObjectName("startSection")
        hdr.addWidget(self._recent_label)
        hdr.addStretch(1)

        self._btn_clear = QPushButton("Clear list")
        self._btn_clear.setObjectName("startClearBtn")
        self._btn_clear.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_clear.setFlat(True)
        self._btn_clear.clicked.connect(self.clearRecentRequested)
        hdr.addWidget(self._btn_clear)

        col.addWidget(self._recent_header)

        # --- Vertical list of rows ---------------------------------------
        self._list_widget = QWidget()
        self._list = QVBoxLayout(self._list_widget)
        self._list.setSpacing(6)
        self._list.setContentsMargins(0, 0, 0, 0)
        col.addWidget(self._list_widget)

        center_row.addWidget(content)
        center_row.addStretch(1)

        outer.addLayout(center_row)
        outer.addStretch(1)

    # ------------------------------------------------------------------
    def refresh(self, recent_files: List[str]):
        """Rebuild the recent-files list from the supplied list (newest 5)."""
        self._recent = recent_files[:START_RECENT]

        # Clear old rows
        while self._list.count():
            item = self._list.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not self._recent:
            self._recent_header.hide()
            self._list_widget.hide()
            return

        self._recent_header.show()
        self._list_widget.show()

        for path in self._recent:
            self._list.addWidget(self._make_row(path))

    # ------------------------------------------------------------------
    @staticmethod
    def _human_size(num_bytes: float) -> str:
        for unit in ("B", "KB", "MB", "GB"):
            if num_bytes < 1024 or unit == "GB":
                return f"{int(num_bytes)} {unit}" if unit == "B" else f"{num_bytes:.1f} {unit}"
            num_bytes /= 1024
        return f"{num_bytes:.1f} GB"

    @staticmethod
    def _relative_time(ts: float) -> str:
        import time
        delta = time.time() - ts
        if delta < 60:               return "just now"
        if delta < 3600:             return f"{int(delta // 60)} min ago"
        if delta < 86400:            return f"{int(delta // 3600)} hr ago"
        if delta < 86400 * 7:        return f"{int(delta // 86400)} days ago"
        if delta < 86400 * 30:       return f"{int(delta // (86400 * 7))} wk ago"
        if delta < 86400 * 365:      return f"{int(delta // (86400 * 30))} mo ago"
        return f"{int(delta // (86400 * 365))} yr ago"

    # ------------------------------------------------------------------
    def _make_row(self, path: str) -> QPushButton:
        """Build a full-width row showing the complete filename + folder/meta."""
        name = os.path.basename(path) or path
        folder = os.path.dirname(path) or ""
        # Show parent + grandparent for context (e.g. "Documents\Books")
        if folder and len(folder) > 50:
            folder = "…" + folder[-49:]

        exists = os.path.isfile(path)
        if exists:
            try:
                st = os.stat(path)
                meta = f"{self._human_size(st.st_size)}  ·  {self._relative_time(st.st_mtime)}"
            except OSError:
                meta = ""
        else:
            meta = "(file not found)"

        btn = QPushButton()
        btn.setObjectName("recentRow")
        btn.setProperty("missing", "true" if not exists else "false")
        btn.style().unpolish(btn)
        btn.style().polish(btn)
        btn.setMinimumHeight(self._ROW_H)
        btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setToolTip(path)
        btn.clicked.connect(lambda _=False, p=path: self.openRequested.emit(p))

        # Row content: [icon] | filename (full)         folder · meta
        h = QHBoxLayout(btn)
        h.setContentsMargins(14, 8, 14, 8)
        h.setSpacing(14)

        icon = QLabel("\U0001F4C4")   # 📄
        icon.setObjectName("recentRowIcon")
        icon.setFixedWidth(24)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        h.addWidget(icon)

        # Left column: filename (full, no elide) + folder
        left = QVBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(2)

        lbl_name = QLabel(name)
        lbl_name.setObjectName("recentRowName")
        left.addWidget(lbl_name)

        if folder:
            lbl_folder = QLabel(folder)
            lbl_folder.setObjectName("recentRowFolder")
            left.addWidget(lbl_folder)

        h.addLayout(left, 1)

        # Right column: file size · relative time
        lbl_meta = QLabel(meta)
        lbl_meta.setObjectName("recentRowMeta")
        lbl_meta.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        h.addWidget(lbl_meta, 0)

        # Remove-from-recents "X" button on the far right.
        btn_remove = QPushButton("\u2715")   # ✕
        btn_remove.setObjectName("recentRowRemove")
        btn_remove.setFixedSize(24, 24)
        btn_remove.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_remove.setToolTip("Remove from Recent Files")
        # Don't let the click fall through to the row's open handler.
        btn_remove.clicked.connect(
            lambda _=False, p=path: self.removeRecentRequested.emit(p)
        )
        h.addWidget(btn_remove, 0)

        return btn


# ===========================================================================
# Tab Manager  (QTabWidget wrapping multiple PdfViewer instances)
# ===========================================================================

class TabManager(QTabWidget):
    """A QTabWidget where each tab is one PdfViewer.

    Signals forwarded to MainWindow come from the *active* viewer only.
    """

    # Re-emit signals from whichever tab is currently active
    pageChanged    = pyqtSignal(int)
    zoomChanged    = pyqtSignal(float)
    statusMessage  = pyqtSignal(str)
    documentLoaded = pyqtSignal()
    documentClosed = pyqtSignal()
    thumbnailReady = pyqtSignal(int, QImage)
    # Notify when active viewer switches (MainWindow must resync docks etc.)
    activeViewerChanged = pyqtSignal()
    # User clicked the "+" tab corner button -> show start menu
    newTabRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("docTabs")  # used by QSS to scope tab styles to *document* tabs only
        self.setTabsClosable(True)
        self.setMovable(True)
        self.setDocumentMode(True)
        self.tabCloseRequested.connect(self._close_tab)
        self.currentChanged.connect(self._on_current_changed)

        # "+" button on the right side of the tab bar.
        # Clicking it returns to the start menu so the user can pick a recent file.
        self._btn_new = QPushButton("+")
        self._btn_new.setObjectName("tabNewBtn")
        self._btn_new.setFixedSize(28, 24)
        self._btn_new.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_new.setToolTip("New tab — open the start menu (Ctrl+T)")
        self._btn_new.setFlat(True)
        self._btn_new.clicked.connect(self.newTabRequested)
        self.setCornerWidget(self._btn_new, Qt.Corner.TopRightCorner)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    @property
    def viewer(self) -> Optional[PdfViewer]:
        """The currently active PdfViewer, or None."""
        w = self.currentWidget()
        return w if isinstance(w, PdfViewer) else None

    def open_new_tab(self) -> PdfViewer:
        """Create a blank PdfViewer tab, make it active, and return it."""
        v = PdfViewer(self)
        self._connect_viewer(v)
        idx = self.addTab(v, "New Tab")
        self.setCurrentIndex(idx)
        return v

    def find_tab_for_path(self, path: str) -> int:
        """Return tab index whose viewer has *path* open, or -1."""
        norm = os.path.normcase(os.path.abspath(path))
        for i in range(self.count()):
            w = self.widget(i)
            if isinstance(w, PdfViewer) and w.doc_path:
                if os.path.normcase(os.path.abspath(w.doc_path)) == norm:
                    return i
        return -1

    def set_tab_title(self, viewer: PdfViewer, title: str):
        idx = self.indexOf(viewer)
        if idx >= 0:
            # Truncate long names for the tab label
            short = title if len(title) <= 30 else title[:28] + "…"
            self.setTabText(idx, short)
            self.setTabToolTip(idx, title)

    def shutdown_all(self):
        for i in range(self.count()):
            w = self.widget(i)
            if isinstance(w, PdfViewer):
                w.shutdown()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    def _connect_viewer(self, v: PdfViewer):
        v.pageChanged.connect(   lambda val, _v=v: self._fwd(self.pageChanged,    val, _v))
        v.zoomChanged.connect(   lambda val, _v=v: self._fwd(self.zoomChanged,    val, _v))
        v.statusMessage.connect( lambda val, _v=v: self._fwd(self.statusMessage,  val, _v))
        v.documentLoaded.connect(lambda      _v=v: self._fwd_noarg(self.documentLoaded, _v))
        v.documentClosed.connect(lambda      _v=v: self._fwd_noarg(self.documentClosed, _v))
        v.thumbnailReady.connect(lambda pi, img, _v=v: self._fwd2(self.thumbnailReady, pi, img, _v))

    def _fwd(self, signal, val, sender: PdfViewer):
        if self.viewer is sender:
            signal.emit(val)

    def _fwd_noarg(self, signal, sender: PdfViewer):
        if self.viewer is sender:
            signal.emit()

    def _fwd2(self, signal, a, b, sender: PdfViewer):
        if self.viewer is sender:
            signal.emit(a, b)

    def _close_tab(self, index: int):
        w = self.widget(index)
        if isinstance(w, PdfViewer):
            w.shutdown()
        self.removeTab(index)
        self.activeViewerChanged.emit()

    def _on_current_changed(self, _index: int):
        self.activeViewerChanged.emit()


# ===========================================================================
# Main Window
# ===========================================================================


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings(ORG_NAME, APP_NAME)
        self.color_mode = self.settings.value("color_mode", "light", type=str)
        self.ocr_language = self.settings.value("ocr_language", "eng", type=str)

        self.setWindowTitle(APP_NAME)
        self.resize(1320, 880)
        self.setAcceptDrops(True)

        # --- Central area: QStackedWidget holds start screen (index 0)
        #     and tab manager (index 1) ---
        self._stack = QStackedWidget(self)
        self.setCentralWidget(self._stack)

        # Start screen
        self._start_screen = StartScreen(self)
        self._start_screen.openRequested.connect(self._open_path)
        self._start_screen.openDialogRequested.connect(self.action_open)
        self._start_screen.clearRecentRequested.connect(self._clear_recent_from_start)
        self._start_screen.removeRecentRequested.connect(self._remove_from_recent)
        self._stack.addWidget(self._start_screen)   # index 0

        # Tab manager
        self.tabs = TabManager(self)
        self._stack.addWidget(self.tabs)             # index 1

        # Wire TabManager signals (same names as old single-viewer wiring)
        self.tabs.pageChanged.connect(self._on_page_changed)
        self.tabs.zoomChanged.connect(self._on_zoom_changed)
        self.tabs.statusMessage.connect(self._set_status)
        self.tabs.documentLoaded.connect(self._on_document_loaded)
        self.tabs.documentClosed.connect(self._on_document_closed)
        self.tabs.thumbnailReady.connect(self._on_thumbnail_ready)
        self.tabs.activeViewerChanged.connect(self._on_active_viewer_changed)
        self.tabs.newTabRequested.connect(self._show_start_screen)

        self._build_docks()
        self._build_menus()
        self._build_toolbar()
        self._build_statusbar()
        self._build_shortcuts()

        # Apply persisted color mode (without triggering signal storms)
        self._apply_color_mode(self.color_mode, refresh=False)
        self._update_recent_menu()

        geo = self.settings.value("geometry")
        if geo:
            self.restoreGeometry(geo)
        state = self.settings.value("windowState")
        if state:
            self.restoreState(state)

        # Show start screen on cold start (no CLI arg yet)
        self._show_start_screen()
        self._toolbar_refit_done = False

    def showEvent(self, e):
        super().showEvent(e)
        # Re-measure toolbar buttons now that the window is visible and the
        # global stylesheet + real display DPI are in effect. Doing it here
        # (rather than during __init__, before the QSS is applied) guarantees
        # the labels are sized correctly and never clip. Run once, deferred so
        # layout has settled.
        if not getattr(self, "_toolbar_refit_done", False):
            self._toolbar_refit_done = True
            QTimer.singleShot(0, self._refit_toolbar_buttons)

    # ------------------------------------------------------------------
    # Stack helpers
    # ------------------------------------------------------------------
    def _show_start_screen(self):
        self._start_screen.refresh(self._recent_files())
        self._stack.setCurrentIndex(0)
        # Hide side panels + toolbar on the welcome screen
        self._set_chrome_visible(False)

    def _show_tabs(self):
        self._stack.setCurrentIndex(1)
        self._set_chrome_visible(True)

    def _set_chrome_visible(self, visible: bool):
        """Show / hide the docks and toolbar.  Used to keep the start screen clean."""
        for dock in (
            getattr(self, "thumb_dock", None),
            getattr(self, "outline_dock", None),
            getattr(self, "bookmarks_dock", None),
        ):
            if dock is not None:
                dock.setVisible(visible)
        # Toolbar (QToolBar host) built in _build_toolbar - hide the whole bar
        # so its background strip doesn't linger on the welcome screen.
        tbar = getattr(self, "_main_toolbar", None)
        if tbar is not None:
            tbar.setVisible(visible)
        strip = getattr(self, "_toolbar_strip", None)
        if strip is not None:
            strip.setVisible(visible)
        # NOTE: the top menu bar (File / Edit / View / ...) stays visible on the
        # start screen so its actions remain reachable.

    @property
    def viewer(self) -> Optional[PdfViewer]:
        """Convenience: active PdfViewer or None."""
        return self.tabs.viewer

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_docks(self):
        self.thumb_panel = ThumbnailPanel()
        self.thumb_panel.pageRequested.connect(lambda p: self.viewer and self.viewer.goto_page(p))
        # Render thumbnails on-demand for currently-visible rows only.
        self.thumb_panel.visiblePagesChanged.connect(
            lambda pages: self.viewer and self.viewer.request_thumbnails_for(pages)
        )
        self.thumb_dock = QDockWidget("Thumbnails", self)
        self.thumb_dock.setWidget(self.thumb_panel)
        self.thumb_dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.thumb_dock)

        self.outline_panel = OutlinePanel()
        self.outline_panel.pageRequested.connect(lambda p: self.viewer and self.viewer.goto_page(p))
        self.outline_dock = QDockWidget("Outline", self)
        self.outline_dock.setWidget(self.outline_panel)
        self.outline_dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.outline_dock)

        self.bookmarks_panel = BookmarksPanel()
        self.bookmarks_panel.btn_add.clicked.connect(self._add_bookmark)
        self.bookmarks_panel.pageRequested.connect(lambda p: self.viewer and self.viewer.goto_page(p))
        self.bookmarks_panel.bookmarksChanged.connect(self._save_bookmarks)
        self.bookmarks_dock = QDockWidget("Bookmarks", self)
        self.bookmarks_dock.setWidget(self.bookmarks_panel)
        self.bookmarks_dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.bookmarks_dock)

        self.tabifyDockWidget(self.thumb_dock, self.outline_dock)
        self.tabifyDockWidget(self.outline_dock, self.bookmarks_dock)
        self.thumb_dock.raise_()

    def _build_menus(self):
        mb: QMenuBar = self.menuBar()

        # File ------------------------------------------------------------
        m_file = mb.addMenu("&File")
        m_file.addAction(self._mk("Open...", self.action_open, QKeySequence.StandardKey.Open))
        self.recent_menu = m_file.addMenu("Open &Recent")
        m_file.addAction(self._mk("&Close", self.action_close_pdf, "Ctrl+W"))
        m_file.addSeparator()
        m_file.addAction(self._mk("Save a Copy...", self.action_save_copy, "Ctrl+S"))
        m_file.addAction(self._mk("Extract Text to File...", self.action_extract_text))
        m_file.addAction(self._mk("&Properties...", self.action_properties))
        m_file.addSeparator()
        m_file.addAction(self._mk("&Print...", self.action_print, QKeySequence.StandardKey.Print))
        m_file.addSeparator()
        m_file.addAction(self._mk("E&xit", self.close, "Ctrl+Q"))

        # Edit ------------------------------------------------------------
        m_edit = mb.addMenu("&Edit")
        m_edit.addAction(self._mk("&Copy", lambda: self.viewer and self.viewer.copy_selection(), QKeySequence.StandardKey.Copy))
        m_edit.addAction(self._mk("Copy Whole Page Text", lambda: self.viewer and self.viewer.select_all_visible_text(), "Ctrl+Shift+C"))
        m_edit.addAction(self._mk("&Find...", self._focus_search, QKeySequence.StandardKey.Find))

        # View ------------------------------------------------------------
        m_view = mb.addMenu("&View")
        m_view.addAction(self._mk("Zoom &In",  lambda: self.viewer and self.viewer.zoom_in(),  "Ctrl++"))
        m_view.addAction(self._mk("Zoom &Out", lambda: self.viewer and self.viewer.zoom_out(), "Ctrl+-"))
        m_view.addAction(self._mk("Fit &Width", lambda: self.viewer and self.viewer.fit_width(), "Ctrl+1"))
        m_view.addAction(self._mk("Fit &Page",  lambda: self.viewer and self.viewer.fit_page(),  "Ctrl+2"))
        m_view.addAction(self._mk("Actual &Size (100%)", lambda: self.viewer and self.viewer.set_zoom(1.0, None), "Ctrl+0"))
        m_view.addSeparator()
        m_view.addAction(self._mk("Rotate &Left",  lambda: self.viewer and self.viewer.rotate(-90), "Ctrl+L"))
        m_view.addAction(self._mk("Rotate &Right", lambda: self.viewer and self.viewer.rotate(90),  "Ctrl+R"))
        m_view.addSeparator()

        # Color modes
        m_color = m_view.addMenu("&Color Mode")
        self.act_light = self._mk("Light", lambda: self._apply_color_mode("light"), checkable=True)
        self.act_dark = self._mk("&Dark", lambda: self._apply_color_mode("dark"), "Ctrl+D", checkable=True)
        self.act_sepia = self._mk("&Sepia", lambda: self._apply_color_mode("sepia"), "Ctrl+E", checkable=True)
        m_color.addAction(self.act_light)
        m_color.addAction(self.act_dark)
        m_color.addAction(self.act_sepia)

        # View modes
        m_layout = m_view.addMenu("Page &Layout")
        self.act_continuous = self._mk("&Continuous",    lambda: self.viewer and self.viewer.set_view_mode(PdfViewer.VIEW_CONTINUOUS), checkable=True)
        self.act_single     = self._mk("&Single Page",   lambda: self.viewer and self.viewer.set_view_mode(PdfViewer.VIEW_SINGLE),     checkable=True)
        self.act_two        = self._mk("&Two Page (Book)", lambda: self.viewer and self.viewer.set_view_mode(PdfViewer.VIEW_TWO),      checkable=True)
        self.act_continuous.setChecked(True)
        m_layout.addAction(self.act_continuous)
        m_layout.addAction(self.act_single)
        m_layout.addAction(self.act_two)

        m_view.addSeparator()
        self.act_full = self._mk("&Fullscreen", self._toggle_fullscreen, "F11", checkable=True)
        self.act_present = self._mk("&Presentation Mode", self._toggle_presentation, "F5", checkable=True)
        m_view.addAction(self.act_full)
        m_view.addAction(self.act_present)
        m_view.addSeparator()
        m_view.addAction(self.thumb_dock.toggleViewAction())
        m_view.addAction(self.outline_dock.toggleViewAction())
        m_view.addAction(self.bookmarks_dock.toggleViewAction())

        # Navigate --------------------------------------------------------
        m_nav = mb.addMenu("&Navigate")
        m_nav.addAction(self._mk("&First Page", lambda: self.viewer and self.viewer.goto_page(0), "Ctrl+Home"))
        m_nav.addAction(self._mk("&Previous Page", self._prev_page, "PgUp"))
        m_nav.addAction(self._mk("&Next Page", self._next_page, "PgDown"))
        m_nav.addAction(self._mk("&Last Page", self._last_page, "Ctrl+End"))
        m_nav.addAction(self._mk("&Go to Page...", self._goto_dialog, "Ctrl+G"))
        m_nav.addSeparator()
        m_nav.addAction(self._mk("Add &Bookmark", self._add_bookmark, "Ctrl+B"))
        m_nav.addSeparator()
        m_nav.addAction(self._mk("Toggle &Auto-Scroll", self._toggle_autoscroll, "Ctrl+Shift+A"))
        m_nav.addAction(self._mk("Auto-Scroll Faster", lambda: self._adj_autoscroll(+1), "Ctrl+Alt+="))
        m_nav.addAction(self._mk("Auto-Scroll Slower", lambda: self._adj_autoscroll(-1), "Ctrl+Alt+-"))

        # Tools (OCR) -----------------------------------------------------
        m_tools = mb.addMenu("&Tools")
        m_tools.addAction(self._mk("OCR &Current Page", self.action_ocr_current_page))
        m_tools.addAction(self._mk("OCR &Selection", self.action_ocr_selection))
        m_tools.addAction(self._mk("OCR &Whole Document...", self.action_ocr_document))
        m_tools.addSeparator()
        m_tools.addAction(self._mk("OCR &Language...", self.action_set_ocr_language))
        # Auto-OCR toggle
        self.act_auto_ocr = self._mk(
            "Auto-OCR &Scanned Pages on Open",
            self._toggle_auto_ocr,
            checkable=True,
        )
        # Set the initial checked state WITHOUT firing the toggled handler:
        # _toggle_auto_ocr() touches self.status, which _build_statusbar()
        # has not created yet at this point in construction.
        self.act_auto_ocr.blockSignals(True)
        self.act_auto_ocr.setChecked(
            self.settings.value("auto_ocr_enabled", True, type=bool)
        )
        self.act_auto_ocr.blockSignals(False)
        m_tools.addAction(self.act_auto_ocr)
        m_tools.addSeparator()
        m_tools.addAction(self._mk("&Read Aloud / Pause", self.action_tts_play_pause, "Ctrl+Space"))
        m_tools.addAction(self._mk("Stop Reading", self.action_tts_stop))
        m_tools.addSeparator()
        m_tools.addAction(self._mk("Create &Audiobook from Pages...", self.action_create_audiobook, "Ctrl+Shift+B"))

        # Help ------------------------------------------------------------
        m_help = mb.addMenu("&Help")
        m_help.addAction(self._mk("&Keyboard Shortcuts", self._show_shortcuts))
        m_help.addAction(self._mk("&About", self._show_about))

    def _mk(self, text: str, slot, shortcut=None, checkable: bool = False) -> QAction:
        a = QAction(text, self)
        if checkable:
            a.setCheckable(True)
            a.toggled.connect(lambda _checked: slot())
        else:
            a.triggered.connect(slot)
        if shortcut:
            a.setShortcut(shortcut if not isinstance(shortcut, str) else QKeySequence(shortcut))
        return a

    def _build_toolbar(self):
        # Top-docked QToolBar (so it spans the FULL window width, ABOVE the
        # dock panels / thumbnail preview bar) that hosts a single container
        # using a wrapping FlowLayout. Every control is always visible - when
        # the window is too narrow the buttons wrap onto more rows instead of
        # hiding behind a ">>" overflow menu.
        qtb = QToolBar("Main")
        qtb.setObjectName("mainToolBar")
        qtb.setMovable(False)
        qtb.setFloatable(False)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, qtb)
        self._main_toolbar = qtb

        strip = _FlowStrip()
        strip.setObjectName("toolbarStrip")
        flow = FlowLayout(strip, margin=6, hspacing=6, vspacing=6)
        strip.setLayout(flow)
        qtb.addWidget(strip)
        self._toolbar_strip = strip

        # Every push-button we create on the toolbar, so we can re-fit their
        # widths after the stylesheet/DPI are known (see _refit_toolbar_buttons).
        self._toolbar_buttons: List[QPushButton] = []
        self._toolbar_flow = flow

        # Shim so the existing "tb.addWidget / tb.addSeparator" calls below add
        # into the FlowLayout.
        class _TB:
            def __init__(self, flow):
                self._flow = flow

            def addWidget(self, w):
                self._flow.addWidget(w)

            def addSeparator(self):
                sep = QWidget()
                sep.setFixedSize(1, 26)
                sep.setStyleSheet("background: rgba(128,128,128,0.35);")
                self._flow.addWidget(sep)

        tb = _TB(flow)

        tb.addWidget(self._tb_btn("Open", self.action_open))
        tb.addWidget(self._tb_btn("+ Tab", self._new_tab, tip="Open a new tab (Ctrl+T)"))
        tb.addSeparator()
        tb.addWidget(self._tb_btn("◀", self._prev_page, tip="Previous page (PgUp)"))

        self.page_input = QLineEdit()
        self.page_input.setFixedWidth(60)
        self.page_input.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.page_input.returnPressed.connect(self._goto_from_input)
        tb.addWidget(self.page_input)
        self.page_total_lbl = QLabel(" / 0 ")
        tb.addWidget(self.page_total_lbl)
        tb.addWidget(self._tb_btn("▶", self._next_page, tip="Next page (PgDown)"))
        tb.addSeparator()
        tb.addWidget(self._tb_btn("−", lambda: self.viewer and self.viewer.zoom_out(), tip="Zoom Out"))

        self.zoom_combo = QComboBox()
        self.zoom_combo.setEditable(True)
        self.zoom_combo.addItems(
            ["Fit Width", "Fit Page", "50%", "75%", "100%", "125%", "150%", "200%", "300%"]
        )
        self.zoom_combo.setFixedWidth(110)
        self.zoom_combo.activated.connect(lambda *_: self._on_zoom_combo_text())
        self.zoom_combo.lineEdit().returnPressed.connect(self._on_zoom_combo_text)
        tb.addWidget(self.zoom_combo)
        tb.addWidget(self._tb_btn("+", lambda: self.viewer and self.viewer.zoom_in(), tip="Zoom In"))

        tb.addSeparator()
        tb.addWidget(self._tb_btn("⟲", lambda: self.viewer and self.viewer.rotate(-90), tip="Rotate Left"))
        tb.addWidget(self._tb_btn("⟳", lambda: self.viewer and self.viewer.rotate(90), tip="Rotate Right"))
        tb.addSeparator()

        # View mode quick switch
        self.view_combo = QComboBox()
        self.view_combo.addItems(["Continuous", "Single Page", "Two Page"])
        self.view_combo.activated.connect(self._on_view_combo)
        tb.addWidget(self.view_combo)

        tb.addSeparator()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search... (Ctrl+F)")
        self.search_input.setFixedWidth(220)
        self.search_input.returnPressed.connect(self._do_search)
        tb.addWidget(self.search_input)
        tb.addWidget(self._tb_btn("↑", lambda: self.viewer and self.viewer.prev_hit(), tip="Previous match (Shift+F3)"))
        tb.addWidget(self._tb_btn("↓", lambda: self.viewer and self.viewer.next_hit(), tip="Next match (F3)"))
        self.search_count_lbl = QLabel("")
        self.search_count_lbl.setStyleSheet("background: transparent; border: none;")
        tb.addWidget(self.search_count_lbl)

        tb.addSeparator()

        # Read aloud (TTS) controls
        self.btn_tts_play = self._tb_btn(
            "▶ Read", self.action_tts_play_pause,
            tip="Read aloud / Pause (Ctrl+Space)",
        )
        tb.addWidget(self.btn_tts_play)
        self.btn_tts_stop = self._tb_btn(
            "■", self.action_tts_stop, tip="Stop reading (Esc)",
        )
        tb.addWidget(self.btn_tts_stop)
        # Voice / rate menu
        self.btn_tts_settings = QPushButton("⚙")
        self.btn_tts_settings.setToolTip("Read-aloud settings: voice & speed")
        self._tts_menu = QMenu(self)
        self.btn_tts_settings.setMenu(self._tts_menu)
        # Build the menu lazily on first click so we don't init pyttsx3 unless needed.
        self._tts_menu.aboutToShow.connect(self._build_tts_menu)
        self._fit_button_width(self.btn_tts_settings)
        self._toolbar_buttons.append(self.btn_tts_settings)
        tb.addWidget(self.btn_tts_settings)
        # Audiobook export (natural neural voice via edge-tts)
        self.btn_audiobook = self._tb_btn(
            "🎧 Audiobook", self.action_create_audiobook,
            tip="Create an audiobook from selected pages (Ctrl+Shift+B)",
        )
        tb.addWidget(self.btn_audiobook)
        tb.addSeparator()

        # OCR button
        self.btn_ocr = QPushButton("OCR")
        self.btn_ocr.setToolTip("OCR menu (scanned PDFs)")
        ocr_menu = QMenu(self)
        ocr_menu.addAction("OCR Current Page", self.action_ocr_current_page)
        ocr_menu.addAction("OCR Selection", self.action_ocr_selection)
        ocr_menu.addAction("OCR Whole Document...", self.action_ocr_document)
        ocr_menu.addSeparator()
        ocr_menu.addAction("Set OCR Language...", self.action_set_ocr_language)
        self.btn_ocr.setMenu(ocr_menu)
        self._fit_button_width(self.btn_ocr)
        self._toolbar_buttons.append(self.btn_ocr)
        tb.addWidget(self.btn_ocr)

        # Color mode toggle
        self.btn_dark = QPushButton("🌙 Dark")
        self.btn_dark.setCheckable(True)
        self.btn_dark.setChecked(self.color_mode == "dark")
        self.btn_dark.setToolTip("Toggle Dark Mode (Ctrl+D)")
        self.btn_dark.clicked.connect(lambda checked: self._apply_color_mode("dark" if checked else "light"))
        self._fit_button_width(self.btn_dark)
        self._toolbar_buttons.append(self.btn_dark)
        tb.addWidget(self.btn_dark)

    @staticmethod
    def _fit_button_width(b: QPushButton, extra: int = 0):
        """Ensure a toolbar button never clips its label.

        We make the button's MINIMUM size equal to its natural size hint
        (which the Qt style computes from the text + QSS padding at the real
        display DPI) plus a safety margin, and lock its size policy so the
        toolbar can't squeeze it. ``ensurePolished`` forces the stylesheet /
        font to be applied first so the metrics are accurate on HiDPI screens.
        """
        b.ensurePolished()
        # Natural width Qt wants for this button (text + QSS padding + arrow).
        hint = b.sizeHint().width()
        # Also compute a font-metric floor as a backstop.
        fm = b.fontMetrics()
        text_w = fm.horizontalAdvance(b.text())
        floor = text_w + 28 + extra + (22 if b.menu() is not None else 0)
        need = max(hint, floor) + 8   # small safety pad so glyphs never touch edges
        b.setMinimumWidth(need)
        b.setMinimumHeight(max(b.sizeHint().height(), 28))
        # Fixed horizontal policy: the button keeps exactly `need` px and the
        # toolbar overflow menu (">>") absorbs anything that doesn't fit,
        # instead of clipping the text.
        pol = b.sizePolicy()
        pol.setHorizontalPolicy(QSizePolicy.Policy.Fixed)
        b.setSizePolicy(pol)

    def _tb_btn(self, text: str, slot, tip: str = "") -> QPushButton:
        b = QPushButton(text)
        if tip:
            b.setToolTip(tip)
        b.clicked.connect(slot)
        self._fit_button_width(b)
        self._toolbar_buttons.append(b)
        return b

    def _refit_toolbar_buttons(self):
        """Re-measure every toolbar button once the stylesheet and real screen
        DPI are known. Called after the window is shown, so font metrics /
        QSS padding are accurate and labels never clip."""
        for b in getattr(self, "_toolbar_buttons", []):
            try:
                b.setMinimumWidth(0)      # reset so sizeHint reflects content
                b.style().unpolish(b)
                b.style().polish(b)
                self._fit_button_width(b)
            except Exception:
                pass


    def _build_statusbar(self):
        self.status: QStatusBar = self.statusBar()
        self.page_status_lbl = QLabel(" - / - ")
        self.zoom_status_lbl = QLabel("100%")

        # Auto-OCR progress widgets - hidden until a scan starts.
        self.ocr_progress_lbl = QLabel("")
        self.ocr_progress_lbl.setStyleSheet("color: #6b7280;")
        self.ocr_cancel_btn = QPushButton("Cancel")
        self.ocr_cancel_btn.setFlat(True)
        self.ocr_cancel_btn.setStyleSheet(
            "QPushButton { color: #2563eb; background: transparent; border: none; padding: 0 6px; }"
            "QPushButton:hover { text-decoration: underline; }"
        )
        self.ocr_cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.ocr_cancel_btn.clicked.connect(self._cancel_auto_ocr)
        self.ocr_progress_lbl.setVisible(False)
        self.ocr_cancel_btn.setVisible(False)

        self.status.addPermanentWidget(self.ocr_progress_lbl)
        self.status.addPermanentWidget(self.ocr_cancel_btn)
        self.status.addPermanentWidget(self.page_status_lbl)
        self.status.addPermanentWidget(self.zoom_status_lbl)
        self.status.showMessage("Ready. Open a PDF to begin.")

    def _build_shortcuts(self):
        QShortcut(QKeySequence("F3"),         self, activated=lambda: self.viewer and self.viewer.next_hit())
        QShortcut(QKeySequence("Shift+F3"),   self, activated=lambda: self.viewer and self.viewer.prev_hit())
        QShortcut(QKeySequence("Esc"),        self, activated=self._on_escape)
        QShortcut(QKeySequence("Ctrl+T"),     self, activated=self._new_tab)
        QShortcut(QKeySequence("Ctrl+Space"), self, activated=self.action_tts_play_pause)
        # Arrow keys for navigation in single-page mode + scrolling
        QShortcut(QKeySequence("Right"),      self, activated=self._next_page)
        QShortcut(QKeySequence("Left"),       self, activated=self._prev_page)
        QShortcut(QKeySequence("Space"),      self, activated=self._page_down_scroll)
        QShortcut(QKeySequence("Shift+Space"),self, activated=self._page_up_scroll)

    # ------------------------------------------------------------------
    # Tab helpers
    # ------------------------------------------------------------------
    def _new_tab(self):
        """Open a file-picker dialog and load the chosen PDF into a new tab."""
        last_dir = self.settings.value("last_dir", "")
        path, _ = QFileDialog.getOpenFileName(
            self, "Open PDF in New Tab", last_dir, "PDF Files (*.pdf);;All Files (*)"
        )
        if path:
            self._open_path(path)

    def _on_active_viewer_changed(self):
        """Called when the user switches tabs or a tab is closed/opened."""
        v = self.viewer
        if v is None:
            # No tabs left — go back to start screen
            if self.tabs.count() == 0:
                self._show_start_screen()
            self._sync_toolbar_to_viewer(None)
            self._on_tts_state_changed(ReadAloudController.STATE_IDLE)
            return
        self._show_tabs()
        # Re-sync toolbar / docks to the newly active viewer
        if v.doc:
            self._on_document_loaded()
        else:
            self._on_document_closed()
        self._sync_toolbar_to_viewer(v)
        # Reflect the active tab's TTS state in the toolbar.
        if hasattr(v, "_tts") and v._tts is not None:
            self._on_tts_state_changed(v._tts.state)
        else:
            self._on_tts_state_changed(ReadAloudController.STATE_IDLE)

    def _sync_toolbar_to_viewer(self, v: Optional[PdfViewer]):
        """Update zoom combo and page counter to reflect the active viewer."""
        if v is None or v.doc is None:
            self.zoom_combo.lineEdit().setText("100%")
            self.zoom_status_lbl.setText("100%")
            self.page_input.setText("")
            self.page_total_lbl.setText(" / 0 ")
            self.search_count_lbl.setText("")
            return
        # Zoom
        pct = f"{int(v.zoom * 100)}%"
        self.zoom_combo.lineEdit().setText(pct)
        self.zoom_status_lbl.setText(pct)
        # Page
        idx = v.current_page_index()
        self.page_input.setText(str(idx + 1))
        self.page_total_lbl.setText(f" / {v.doc.page_count} ")
        self.page_status_lbl.setText(f" {idx + 1} / {v.doc.page_count} ")

    # ------------------------------------------------------------------
    # File actions
    # ------------------------------------------------------------------
    def action_open(self):
        last_dir = self.settings.value("last_dir", "")
        path, _ = QFileDialog.getOpenFileName(
            self, "Open PDF", last_dir, "PDF Files (*.pdf);;All Files (*)"
        )
        if path:
            self._open_path(path)

    def _open_path(self, path: str):
        if not os.path.isfile(path):
            QMessageBox.warning(self, APP_NAME, f"File not found:\n{path}")
            return

        # If this file is already open in a tab, just switch to it
        existing = self.tabs.find_tab_for_path(path)
        if existing >= 0:
            self.tabs.setCurrentIndex(existing)
            self._show_tabs()
            return

        # Save state of current tab's doc before opening a new one
        self._save_session_state()

        # Always open in a fresh tab.
        # Inherit the app's current color mode so the first render uses dark/sepia
        # immediately - otherwise the new viewer defaults to "light" until the
        # user toggles the mode.
        v = self.tabs.open_new_tab()
        v.set_color_mode(self.color_mode)
        if v.open_pdf(path):
            self.settings.setValue("last_dir", os.path.dirname(path))
            self._add_to_recent(path)
            self._show_tabs()
        else:
            # open failed — remove the blank tab we just created
            idx = self.tabs.indexOf(v)
            if idx >= 0:
                self.tabs._close_tab(idx)
            if self.tabs.count() == 0:
                self._show_start_screen()

    def action_close_pdf(self):
        """Close the active tab (or its PDF if it has one)."""
        v = self.viewer
        if v is None:
            return
        self._save_session_state()
        idx = self.tabs.indexOf(v)
        if idx >= 0:
            self.tabs._close_tab(idx)
        if self.tabs.count() == 0:
            self._show_start_screen()

    def action_save_copy(self):
        v = self.viewer
        if v is None or v.doc is None or v.doc_path is None:
            return
        suggested = os.path.splitext(os.path.basename(v.doc_path))[0] + "_copy.pdf"
        last_dir = self.settings.value("last_dir", "")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save a Copy", os.path.join(last_dir, suggested), "PDF Files (*.pdf)"
        )
        if not path:
            return
        try:
            v.doc.save(path, garbage=4, deflate=True, clean=True)
            self._set_status(f"Saved copy: {path}")
        except Exception as e:
            QMessageBox.critical(self, "Save failed", str(e))

    def action_extract_text(self):
        v = self.viewer
        if v is None or v.doc is None:
            return
        suggested = os.path.splitext(os.path.basename(v.doc_path or "document.pdf"))[0] + ".txt"
        path, _ = QFileDialog.getSaveFileName(
            self, "Extract Text", suggested, "Text Files (*.txt)"
        )
        if not path:
            return
        n = v.doc.page_count
        try:
            if n <= 8 or not v.doc_path:
                # Small doc: sequential is fine and avoids thread overhead.
                pages_text = {i: v.doc.load_page(i).get_text("text") for i in range(n)}
            else:
                # Large doc: extract text across CPU cores in parallel.
                def _extract(page, idx):
                    return page.get_text("text")
                pages_text = parallel_pages(v.doc_path, range(n), _extract)
            with open(path, "w", encoding="utf-8") as f:
                for i in range(n):
                    f.write(f"--- Page {i + 1} ---\n")
                    f.write(pages_text.get(i, ""))
                    f.write("\n")
            self._set_status(f"Text extracted to {path}")
        except Exception as e:
            QMessageBox.critical(self, "Extract failed", str(e))

    # ------------------------------------------------------------------
    # OCR actions
    # ------------------------------------------------------------------
    def _check_tesseract(self) -> bool:
        ok, _ = _tesseract_available()
        if not ok:
            QMessageBox.warning(self, "Tesseract not found", _missing_tesseract_message())
        return ok

    def action_ocr_current_page(self):
        v = self.viewer
        if v is None or v.doc is None or not self._check_tesseract():
            return
        idx = v.current_page_index()
        page = v.doc.load_page(idx)
        # If page already has selectable text, ask whether to use OCR anyway.
        existing = page.get_text("text").strip()
        if existing and len(existing) > 20:
            ans = QMessageBox.question(
                self, "Page already has text",
                "This page already contains selectable text.\n"
                "Run OCR anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ans != QMessageBox.StandardButton.Yes:
                # Just show the existing text instead.
                dlg = OcrResultDialog(f"Page {idx + 1} text", existing, self)
                dlg.exec()
                return
        self.statusBar().showMessage(f"Running OCR on page {idx + 1}...")
        QApplication.processEvents()
        try:
            text = _ocr_page_text(page, language=self.ocr_language)
        except Exception as e:
            QMessageBox.critical(self, "OCR failed", str(e))
            return
        self.statusBar().showMessage(f"OCR complete - {len(text)} characters")
        dlg = OcrResultDialog(f"OCR — Page {idx + 1}", text, self)
        dlg.exec()

    def action_ocr_selection(self):
        """Run OCR on the rectangle the user has selected on the current page."""
        v = self.viewer
        if v is None or v.doc is None or not self._check_tesseract():
            return
        # Find the selected widget (page that has a non-empty selection rect).
        target = None
        sel_rect = None
        for w in v.page_widgets:
            r = w.get_selection_rect_in_pdf()
            if r is not None and not r.is_empty:
                target = w
                sel_rect = r
                break
        if target is None or sel_rect is None:
            QMessageBox.information(
                self, "No selection",
                "Drag a rectangle on the page first, then run OCR Selection."
            )
            return
        # Map widget-zoom rect back to page coordinates accounting for rotation.
        page = v.doc.load_page(target.page_index)
        rot = v.rotation
        if rot:
            pr = page.rect
            r = sel_rect
            if rot == 90:
                sel_rect = fitz.Rect(r.y0, pr.width - r.x1, r.y1, pr.width - r.x0)
            elif rot == 180:
                sel_rect = fitz.Rect(pr.width - r.x1, pr.height - r.y1,
                                     pr.width - r.x0, pr.height - r.y0)
            elif rot == 270:
                sel_rect = fitz.Rect(pr.height - r.y1, r.x0, pr.height - r.y0, r.x1)

        self.statusBar().showMessage("Running OCR on selection...")
        QApplication.processEvents()
        try:
            text = _ocr_page_text(page, language=self.ocr_language, clip=sel_rect)
        except Exception as e:
            QMessageBox.critical(self, "OCR failed", str(e))
            return
        text = text.strip()
        if text:
            QGuiApplication.clipboard().setText(text)
            self.statusBar().showMessage(f"OCR complete — {len(text)} characters copied to clipboard")
        dlg = OcrResultDialog(f"OCR — Page {target.page_index + 1} (selection)", text, self)
        dlg.exec()

    def action_ocr_document(self):
        v = self.viewer
        if v is None or v.doc is None or not self._check_tesseract():
            return
        if not v.doc_path:
            QMessageBox.information(self, APP_NAME, "Save the PDF to disk first.")
            return
        suggested = os.path.splitext(os.path.basename(v.doc_path or "document.pdf"))[0] + "_ocr.txt"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save OCR Text", suggested, "Text Files (*.txt)"
        )
        if not path:
            return
        n = v.doc.page_count
        lang = self.ocr_language
        doc_path = v.doc_path

        progress = QProgressDialog(
            "Running OCR on entire document (parallel)...", "Cancel", 0, n, self
        )
        progress.setWindowTitle("OCR in progress")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)

        cancel = threading.Event()
        progress.canceled.connect(cancel.set)

        # Shared, thread-safe progress state polled by a GUI-thread timer.
        state = {"done": 0, "finished": False, "error": None, "results": None}
        state_lock = threading.Lock()

        def _ocr_one(page, idx):
            try:
                return _ocr_page_text(page, language=lang)
            except Exception as e:
                return f"[OCR error on page {idx + 1}: {e}]"

        def _bump(done, total):
            with state_lock:
                state["done"] = done

        def _run():
            try:
                res = parallel_pages(
                    doc_path, range(n), _ocr_one,
                    cancel=cancel, on_progress=_bump,
                )
                with state_lock:
                    state["results"] = res
            except Exception as e:
                with state_lock:
                    state["error"] = str(e)
            finally:
                with state_lock:
                    state["finished"] = True

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()

        # Poll progress on the GUI thread; write the file once done.
        poll = QTimer(self)

        def _tick():
            with state_lock:
                done = state["done"]
                finished = state["finished"]
                error = state["error"]
                results = state["results"]
            if not finished:
                progress.setValue(min(done, n))
                progress.setLabelText(f"OCR page {done} / {n}...")
                return
            # Finished (or cancelled).
            poll.stop()
            progress.setValue(n)
            progress.close()
            if error:
                QMessageBox.critical(self, "OCR failed", error)
                return
            if cancel.is_set():
                self._set_status("OCR cancelled")
                return
            try:
                with open(path, "w", encoding="utf-8") as f:
                    for i in range(n):
                        txt = (results or {}).get(i, "")
                        f.write(f"--- Page {i + 1} ---\n")
                        f.write(txt)
                        f.write("\n\n")
            except Exception as e:
                QMessageBox.critical(self, "OCR failed", str(e))
                return
            self._set_status(f"OCR complete: {path}")
            QMessageBox.information(self, "OCR complete", f"Text saved to:\n{path}")

        poll.timeout.connect(_tick)
        poll.start(120)

    def action_set_ocr_language(self):
        text, ok = QInputDialog.getText(
            self, "OCR Language",
            "Tesseract language code (e.g. 'eng', 'fra', 'deu', 'eng+fra'):",
            QLineEdit.EchoMode.Normal, self.ocr_language,
        )
        if ok and text.strip():
            self.ocr_language = text.strip()
            self.settings.setValue("ocr_language", self.ocr_language)
            self._set_status(f"OCR language: {self.ocr_language}")

    # ------------------------------------------------------------------
    # Read aloud (TTS) actions
    # ------------------------------------------------------------------
    def action_tts_play_pause(self):
        v = self.viewer
        if v is None or v.doc is None:
            return
        # No Tesseract dependency - we extract from the PDF's text layer.
        tts = v.get_tts()
        # Pick up persisted settings on first activation
        rate = int(self.settings.value("tts_rate", 180))
        voice_id = self.settings.value("tts_voice", "", type=str)
        tts.set_rate(rate)
        if voice_id:
            tts.set_voice(voice_id)
        # Connect once per controller (cannot use UniqueConnection - it raises
        # TypeError on duplicate connect attempts).
        if not getattr(tts, "_state_signal_wired", False):
            tts.stateChanged.connect(self._on_tts_state_changed)
            tts._state_signal_wired = True
        if tts.is_playing():
            tts.pause()
        else:
            tts.play_current_page()

    def action_tts_stop(self):
        v = self.viewer
        if v is None:
            return
        if hasattr(v, "_tts") and v._tts is not None:
            v._tts.stop()

    def action_create_audiobook(self):
        v = self.viewer
        if v is None or v.doc is None:
            QMessageBox.information(self, APP_NAME, "Open a PDF first.")
            return
        if not edge_tts_available():
            QMessageBox.warning(
                self, "edge-tts required",
                "Creating a natural-sounding audiobook uses Microsoft neural "
                "voices via the 'edge-tts' package (requires internet), which "
                "isn't installed.\n\nInstall it with:\n    pip install edge-tts\n\n"
                "Then reopen this dialog."
            )
            return
        # Non-modal so the user can keep using the app while an audiobook is
        # being generated in the background. Keep a reference so Python doesn't
        # garbage-collect the dialog (and its worker thread) while it's open.
        dlg = AudiobookDialog(v, self.settings, self)
        dlg.setModal(False)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._audiobook_dlg = dlg
        dlg.destroyed.connect(lambda *_: setattr(self, "_audiobook_dlg", None))
        dlg.show()

    def _on_tts_state_changed(self, state: str):
        if state == ReadAloudController.STATE_PLAYING:
            self.btn_tts_play.setText("⏸ Pause")
            self._set_status("Read aloud: playing")
        elif state == ReadAloudController.STATE_PAUSED:
            self.btn_tts_play.setText("▶ Resume")
            self._set_status("Read aloud: paused")
        elif state == ReadAloudController.STATE_EXTRACTING:
            self.btn_tts_play.setText("… OCR")
        else:  # IDLE
            self.btn_tts_play.setText("▶ Read")

    def _build_tts_menu(self):
        v = self.viewer
        self._tts_menu.clear()
        if v is None or v.doc is None:
            self._tts_menu.addAction("(open a PDF first)").setEnabled(False)
            return
        tts = v.get_tts()
        # Voice submenu
        voice_menu = self._tts_menu.addMenu("Voice")
        voices = tts.list_voices()
        cur_voice = self.settings.value("tts_voice", "", type=str)
        if not voices:
            voice_menu.addAction("(no voices found)").setEnabled(False)
        else:
            for vid, vname in voices:
                act = QAction(vname, self, checkable=True)
                act.setChecked(vid == cur_voice)
                act.triggered.connect(lambda _checked=False, _id=vid: self._set_tts_voice(_id))
                voice_menu.addAction(act)
        # Speed submenu
        speed_menu = self._tts_menu.addMenu("Speed")
        cur_rate = int(self.settings.value("tts_rate", 180))
        for label, rate in (
            ("Slowest (100)", 100),
            ("Slow (140)",    140),
            ("Normal (180)",  180),
            ("Fast (220)",    220),
            ("Faster (260)",  260),
            ("Fastest (300)", 300),
        ):
            act = QAction(label, self, checkable=True)
            act.setChecked(rate == cur_rate)
            act.triggered.connect(lambda _checked=False, _r=rate: self._set_tts_rate(_r))
            speed_menu.addAction(act)

    def _set_tts_voice(self, voice_id: str):
        self.settings.setValue("tts_voice", voice_id)
        v = self.viewer
        if v is not None:
            v.get_tts().set_voice(voice_id)
        self._set_status("TTS voice updated")

    def _set_tts_rate(self, rate: int):
        self.settings.setValue("tts_rate", rate)
        v = self.viewer
        if v is not None:
            v.get_tts().set_rate(rate)
        self._set_status(f"TTS speed: {rate} wpm")

    # ------------------------------------------------------------------
    # Auto-OCR toggle
    # ------------------------------------------------------------------
    def _toggle_auto_ocr(self):
        enabled = self.act_auto_ocr.isChecked()
        self.settings.setValue("auto_ocr_enabled", enabled)
        if enabled:
            self._set_status("Auto-OCR enabled - will scan PDFs on open")
            # Toggling ON is an explicit request, so force a scan even for
            # large documents.
            self._start_auto_ocr_for_current_viewer(force=True)
        else:
            self._set_status("Auto-OCR disabled")
            self._cancel_auto_ocr()

    def action_properties(self):
        v = self.viewer
        if v is None or v.doc is None:
            return
        dlg = PropertiesDialog(v.doc, v.doc_path or "", self)
        dlg.exec()

    def action_print(self):
        v = self.viewer
        if v is None or v.doc is None:
            QMessageBox.information(self, APP_NAME, "Open a PDF first.")
            return
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        dlg = QPrintDialog(printer, self)
        if dlg.exec() != QPrintDialog.DialogCode.Accepted:
            return
        painter = QPainter(printer)
        try:
            page_rect = printer.pageRect(QPrinter.Unit.Point).toRect()
            for i in range(v.doc.page_count):
                if i > 0:
                    printer.newPage()
                page = v.doc.load_page(i)
                z = printer.resolution() / 72.0
                pix = page.get_pixmap(matrix=fitz.Matrix(z, z), alpha=False)
                img = QImage(
                    pix.samples, pix.width, pix.height, pix.stride,
                    QImage.Format.Format_RGB888,
                ).copy()
                scaled = img.scaled(
                    page_rect.width(), page_rect.height(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                x = page_rect.x() + (page_rect.width() - scaled.width()) // 2
                y = page_rect.y() + (page_rect.height() - scaled.height()) // 2
                painter.drawImage(x, y, scaled)
        finally:
            painter.end()
        self._set_status("Print job sent.")

    # ------------------------------------------------------------------
    # Document open/close handlers
    # ------------------------------------------------------------------
    def _on_document_loaded(self):
        v = self.viewer
        if v is None or v.doc is None:
            return
        doc = v.doc
        fname = os.path.basename(v.doc_path or "")
        self.setWindowTitle(f"{fname} — {APP_NAME}")
        # Update the tab label
        self.tabs.set_tab_title(v, fname)
        self.thumb_panel.populate_placeholders(doc.page_count)
        has_outline = self.outline_panel.populate(doc)
        # Default the sidebar to the Outline tab when the PDF has a table of
        # contents; otherwise show the Thumbnails (page preview) tab.
        if has_outline:
            self.outline_dock.raise_()
        else:
            self.thumb_dock.raise_()
        self.page_total_lbl.setText(f" / {doc.page_count} ")
        self.page_input.setText("1")
        # Load bookmarks for this file
        self._load_bookmarks()
        # Restore last page
        last_page = self._get_per_file_setting("last_page")
        if isinstance(last_page, int) and 0 <= last_page < doc.page_count:
            QTimer.singleShot(50, lambda p=last_page, _v=v: _v.goto_page(p))
        # Trigger an on-demand thumbnail request for whatever is visible in
        # the panel right now (after the panel has had a chance to scroll to
        # the current page). This avoids the previous approach of queuing
        # render tasks for every page in the document.
        QTimer.singleShot(250, self.thumb_panel._emit_visible_pages)
        self._on_page_changed(v.current_page_index())
        # Kick off auto-OCR (background, deferred so the UI settles first).
        QTimer.singleShot(800, self._start_auto_ocr_for_current_viewer)

    # ------------------------------------------------------------------
    # Auto-OCR
    # ------------------------------------------------------------------
    # Documents larger than this are NOT auto-OCR-prescanned on open, because
    # walking every page's text layer up front is slow and starves the render
    # worker (thumbnails/pages appear blank). Such pages are still OCR'd
    # on-demand when Read-Aloud / search needs them, or via the Tools > OCR
    # menu, and the user can force a full scan by toggling Auto-OCR off/on.
    AUTO_OCR_MAX_PAGES = 400

    def _start_auto_ocr_for_current_viewer(self, force: bool = False):
        v = self.viewer
        if v is None or v.doc is None:
            return
        # Respect the user-toggleable setting (default ON).
        enabled = self.settings.value("auto_ocr_enabled", True, type=bool)
        if not enabled:
            return
        # Skip the up-front full-document scan for very large PDFs unless the
        # user explicitly asked for it (force=True from the menu toggle).
        if not force and v.doc.page_count > self.AUTO_OCR_MAX_PAGES:
            self._set_status(
                f"Large document ({v.doc.page_count} pages): auto-OCR pre-scan "
                "skipped for speed. Pages are OCR'd on demand."
            )
            return
        ctrl = v.get_auto_ocr()
        ctrl.language = self.ocr_language
        # Wire signals once per controller.
        if not getattr(ctrl, "_signals_wired", False):
            ctrl.progress.connect(self._on_auto_ocr_progress)
            ctrl.finished.connect(self._on_auto_ocr_finished)
            ctrl.skipped.connect(self._on_auto_ocr_skipped)
            ctrl._signals_wired = True
        ctrl.start()

    def _cancel_auto_ocr(self):
        v = self.viewer
        if v is not None and hasattr(v, "_auto_ocr") and v._auto_ocr is not None:
            v._auto_ocr.cancel()
        self.ocr_progress_lbl.setVisible(False)
        self.ocr_cancel_btn.setVisible(False)

    @pyqtSlot(int, int)
    def _on_auto_ocr_progress(self, done: int, total: int):
        self.ocr_progress_lbl.setText(f"OCR: page {done}/{total}")
        self.ocr_progress_lbl.setVisible(True)
        self.ocr_cancel_btn.setVisible(True)

    @pyqtSlot(int)
    def _on_auto_ocr_finished(self, ocred: int):
        self.ocr_progress_lbl.setVisible(False)
        self.ocr_cancel_btn.setVisible(False)
        if ocred > 0:
            self._set_status(f"Auto-OCR done: {ocred} page(s) recognised")

    @pyqtSlot(str)
    def _on_auto_ocr_skipped(self, reason: str):
        # Silent skip per spec - log a status message but no popup.
        self._set_status(f"Auto-OCR skipped: {reason}")
        self.ocr_progress_lbl.setVisible(False)
        self.ocr_cancel_btn.setVisible(False)

    def _on_document_closed(self):
        self.setWindowTitle(APP_NAME)
        self.thumb_panel.clear()
        self.outline_panel.clear()
        self.bookmarks_panel.set_bookmarks([])
        self.page_input.setText("")
        self.page_total_lbl.setText(" / 0 ")
        self.search_count_lbl.setText("")
        if self.tabs.count() == 0:
            self._show_start_screen()

    def _on_thumbnail_ready(self, page_index: int, image: QImage):
        self.thumb_panel.set_thumbnail(page_index, image)
        # Re-scroll once the thumbnail of the *current* page actually arrives.
        # The list row resizes when an icon is set, which can push the previously-
        # centered current row off-screen.
        v = self.viewer
        if v is not None and v.doc is not None and page_index == v.last_known_page():
            self._schedule_thumb_scroll()

    # ------------------------------------------------------------------
    # Page nav
    # ------------------------------------------------------------------
    def _prev_page(self):
        v = self.viewer
        if v and v.doc:
            v.goto_page(v.current_page_index() - 1)

    def _next_page(self):
        v = self.viewer
        if v and v.doc:
            v.goto_page(v.current_page_index() + 1)

    def _last_page(self):
        v = self.viewer
        if v and v.doc:
            v.goto_page(v.doc.page_count - 1)

    def _goto_from_input(self):
        v = self.viewer
        if v is None:
            return
        try:
            v.goto_page(int(self.page_input.text()) - 1)
        except ValueError:
            pass

    def _goto_dialog(self):
        v = self.viewer
        if v is None or v.doc is None:
            return
        n, ok = QInputDialog.getInt(
            self, "Go to Page", f"Page (1 - {v.doc.page_count}):",
            v.current_page_index() + 1, 1, v.doc.page_count
        )
        if ok:
            v.goto_page(n - 1)

    def _page_down_scroll(self):
        v = self.viewer
        if v is None:
            return
        bar = v.verticalScrollBar()
        bar.setValue(bar.value() + v.viewport().height())

    def _page_up_scroll(self):
        v = self.viewer
        if v is None:
            return
        bar = v.verticalScrollBar()
        bar.setValue(bar.value() - v.viewport().height())

    # ------------------------------------------------------------------
    # Zoom helpers
    # ------------------------------------------------------------------
    def _on_zoom_combo_text(self):
        v = self.viewer
        if v is None:
            return
        text = self.zoom_combo.currentText().strip().lower()
        if "width" in text:
            v.fit_width()
            return
        if "page" in text:
            v.fit_page()
            return
        try:
            pct = float(text.replace("%", "").strip())
            v.set_zoom(pct / 100.0, fit_mode=None)
        except ValueError:
            pass

    def _on_zoom_changed(self, zoom: float):
        pct = f"{int(zoom * 100)}%"
        self.zoom_combo.lineEdit().setText(pct)
        self.zoom_status_lbl.setText(pct)

    def _on_view_combo(self, idx: int):
        modes = [PdfViewer.VIEW_CONTINUOUS, PdfViewer.VIEW_SINGLE, PdfViewer.VIEW_TWO]
        if self.viewer is not None:
            self.viewer.set_view_mode(modes[idx])
        self.act_continuous.blockSignals(True)
        self.act_single.blockSignals(True)
        self.act_two.blockSignals(True)
        self.act_continuous.setChecked(idx == 0)
        self.act_single.setChecked(idx == 1)
        self.act_two.setChecked(idx == 2)
        self.act_continuous.blockSignals(False)
        self.act_single.blockSignals(False)
        self.act_two.blockSignals(False)

    def _on_page_changed(self, idx: int):
        v = self.viewer
        if v and v.doc:
            self.page_input.setText(str(idx + 1))
            self.page_status_lbl.setText(f" {idx + 1} / {v.doc.page_count} ")
            if 0 <= idx < self.thumb_panel.count():
                self.thumb_panel.blockSignals(True)
                self.thumb_panel.setCurrentRow(idx)
                self.thumb_panel.blockSignals(False)
            # Debounced scroll - avoids races with row geometry changing as
            # thumbnails finish rendering in the background.
            self._schedule_thumb_scroll()

    def _schedule_thumb_scroll(self):
        """Coalesce many calls into a single deferred scrollToItem."""
        if not hasattr(self, "_thumb_scroll_timer"):
            self._thumb_scroll_timer = QTimer(self)
            self._thumb_scroll_timer.setSingleShot(True)
            self._thumb_scroll_timer.timeout.connect(self._do_thumb_scroll)
        # 80 ms is enough for the QListWidget viewport to settle after
        # row-height changes (icon arrival) and for the active-viewer-change
        # restore to finish.
        self._thumb_scroll_timer.start(80)

    def _do_thumb_scroll(self):
        v = self.viewer
        if v is None or v.doc is None:
            return
        idx = v.last_known_page()
        if 0 <= idx < self.thumb_panel.count():
            item = self.thumb_panel.item(idx)
            if item is not None:
                # Always pin the current page to the top of the thumbnail list.
                self.thumb_panel.scrollToItem(
                    item, QListWidget.ScrollHint.PositionAtTop
                )

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------
    def _focus_search(self):
        self.search_input.setFocus()
        self.search_input.selectAll()

    def _do_search(self):
        v = self.viewer
        if v is None:
            self.search_count_lbl.setText("")
            return
        q = self.search_input.text().strip()
        n = v.search(q)
        self.search_count_lbl.setText(f" {n} matches " if n else (" no matches " if q else ""))

    # ------------------------------------------------------------------
    # Color modes / fullscreen / presentation
    # ------------------------------------------------------------------
    def _apply_color_mode(self, mode: str, refresh: bool = True):
        self.color_mode = mode
        self.settings.setValue("color_mode", mode)
        # Sync menu actions
        for a, m in (
            (getattr(self, "act_light", None), "light"),
            (getattr(self, "act_dark", None), "dark"),
            (getattr(self, "act_sepia", None), "sepia"),
        ):
            if a is not None:
                a.blockSignals(True)
                a.setChecked(mode == m)
                a.blockSignals(False)
        if hasattr(self, "btn_dark"):
            self.btn_dark.blockSignals(True)
            self.btn_dark.setChecked(mode == "dark")
            self.btn_dark.setText("☀ Light" if mode == "dark" else "🌙 Dark")
            self.btn_dark.blockSignals(False)

        # Apply the appropriate modern stylesheet to the WHOLE application
        # so menus, dock widgets and dialogs inherit the same look.
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(DARK_QSS if mode == "dark" else LIGHT_QSS)

        # Apply to all open viewers so every tab re-renders with the new mode.
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if not isinstance(w, PdfViewer):
                continue
            if refresh:
                prev_render = "dark" if w.color_mode == "dark" else "light"
                new_render = "dark" if mode == "dark" else "light"
                w.set_color_mode(mode)
                # Re-request thumbnails only when the underlying bitmap changes.
                if w.doc is not None and prev_render != new_render:
                    w.thumb_cache.clear()
                    if w is self.viewer:
                        w.request_thumbnails()
            else:
                w.color_mode = mode

    def _toggle_fullscreen(self):
        if self.act_full.isChecked():
            self.showFullScreen()
        else:
            self.showNormal()

    def _toggle_presentation(self):
        # Presentation = single-page + fullscreen + hide UI chrome
        v = self.viewer
        if self.act_present.isChecked():
            self._pre_present_state = (
                v.view_mode if v else None,
                self.thumb_dock.isVisible(),
                self.outline_dock.isVisible(),
                self.bookmarks_dock.isVisible(),
                self.menuBar().isVisible(),
            )
            if v is not None:
                v.set_view_mode(PdfViewer.VIEW_SINGLE)
            self.thumb_dock.hide()
            self.outline_dock.hide()
            self.bookmarks_dock.hide()
            self.menuBar().hide()
            self.showFullScreen()
        else:
            mode, t, o, b, mb_v = getattr(self, "_pre_present_state", (None, True, True, True, True))
            if mode is not None and v is not None:
                v.set_view_mode(mode)
            self.thumb_dock.setVisible(t)
            self.outline_dock.setVisible(o)
            self.bookmarks_dock.setVisible(b)
            self.menuBar().setVisible(mb_v)
            self.showNormal()

    def _on_escape(self):
        if self.act_present.isChecked():
            self.act_present.setChecked(False)
        elif self.isFullScreen():
            self.act_full.setChecked(False)
        else:
            # If read-aloud is active, Esc stops it first.
            v = self.viewer
            if v is not None and hasattr(v, "_tts") and v._tts is not None:
                if v._tts.is_playing() or v._tts.is_paused():
                    v._tts.stop()
                    return
            # Otherwise: clear any text selections + search highlights
            if v:
                v.clear_all_selections()
            if self.search_input.text():
                self.search_input.clear()
            if v:
                v.search("")
            self.search_count_lbl.setText("")

    # ------------------------------------------------------------------
    # Auto-scroll
    # ------------------------------------------------------------------
    def _toggle_autoscroll(self):
        if self.viewer:
            active = self.viewer.toggle_autoscroll()
            self._set_status("Auto-scroll ON" if active else "Auto-scroll OFF")

    def _adj_autoscroll(self, delta: int):
        if self.viewer is None:
            return
        new_speed = max(1, self.viewer._autoscroll_speed + delta)
        self.viewer.set_autoscroll_speed(new_speed)
        self._set_status(f"Auto-scroll speed: {new_speed} px/tick")

    # ------------------------------------------------------------------
    # Bookmarks
    # ------------------------------------------------------------------
    def _add_bookmark(self):
        v = self.viewer
        if v is None or v.doc is None:
            return
        page = v.current_page_index()
        label, ok = QInputDialog.getText(
            self, "Add Bookmark", f"Label for page {page + 1}:",
            QLineEdit.EchoMode.Normal, f"Page {page + 1}"
        )
        if not ok:
            return
        self.bookmarks_panel.add_bookmark(page, label or f"Page {page + 1}")

    def _save_bookmarks(self):
        v = self.viewer
        if v is not None and v.doc_path:
            self._set_per_file_setting("bookmarks", self.bookmarks_panel.get_bookmarks())

    def _load_bookmarks(self):
        items = self._get_per_file_setting("bookmarks") or []
        if not isinstance(items, list):
            items = []
        self.bookmarks_panel.set_bookmarks(items)

    # ------------------------------------------------------------------
    # Recent files
    # ------------------------------------------------------------------
    def _recent_files(self) -> List[str]:
        raw = self.settings.value("recent_files", "[]")
        try:
            if isinstance(raw, str):
                return json.loads(raw)
            return list(raw or [])
        except Exception:
            return []

    def _add_to_recent(self, path: str):
        items = [p for p in self._recent_files() if p != path]
        items.insert(0, path)
        items = items[:MAX_RECENT]
        self.settings.setValue("recent_files", json.dumps(items))
        self._update_recent_menu()
        # Keep start screen in sync if it's visible
        if hasattr(self, "_start_screen"):
            self._start_screen.refresh(items)

    def _update_recent_menu(self):
        self.recent_menu.clear()
        items = self._recent_files()
        if not items:
            a = self.recent_menu.addAction("(none)")
            a.setEnabled(False)
            return
        for p in items:
            a = self.recent_menu.addAction(p)
            a.triggered.connect(lambda _checked=False, path=p: self._open_path(path))
        self.recent_menu.addSeparator()
        clear = self.recent_menu.addAction("Clear Recent")
        clear.triggered.connect(self._clear_recent)

    def _clear_recent(self):
        self.settings.setValue("recent_files", "[]")
        self._update_recent_menu()

    def _clear_recent_from_start(self):
        """Same as _clear_recent but also refresh the visible start screen."""
        self._clear_recent()
        self._start_screen.refresh([])

    def _remove_from_recent(self, path: str):
        """Remove a single file from the recent-files list (via a row's ✕)."""
        items = [p for p in self._recent_files() if p != path]
        self.settings.setValue("recent_files", json.dumps(items))
        self._update_recent_menu()
        self._start_screen.refresh(items)

    # ------------------------------------------------------------------
    # Per-file persistence
    # ------------------------------------------------------------------
    def _per_file_key(self, suffix: str) -> Optional[str]:
        v = self.viewer
        if v is None or not v.doc_path:
            return None
        return f"file/{os.path.abspath(v.doc_path)}/{suffix}"

    def _get_per_file_setting(self, suffix: str):
        k = self._per_file_key(suffix)
        if k is None:
            return None
        v = self.settings.value(k)
        if isinstance(v, str):
            try:
                return json.loads(v)
            except Exception:
                return v
        return v

    def _set_per_file_setting(self, suffix: str, value):
        k = self._per_file_key(suffix)
        if k is None:
            return
        self.settings.setValue(k, json.dumps(value))

    def _save_session_state(self):
        """Persist the last-page-index for *every* open tab, not just the active one.

        Uses last_known_page() (not current_page_index()) so inactive/hidden
        viewers still report the correct page - current_page_index() relies on
        widget visibility and would otherwise return 0 for any hidden tab.
        """
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, PdfViewer) and w.doc_path is not None and w.doc is not None:
                key = f"file/{os.path.abspath(w.doc_path)}/last_page"
                self.settings.setValue(key, json.dumps(w.last_known_page()))

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    def _set_status(self, msg: str):
        self.status.showMessage(msg, 5000)

    def _show_about(self):
        QMessageBox.about(
            self, f"About {APP_NAME}",
            f"<h3>{APP_NAME}</h3>"
            "<p>A fast PDF reader for Windows with dark mode, sepia mode, "
            "background rendering, and per-file resume.</p>"
            "<p>Built with <b>Python + PyQt6 + PyMuPDF</b>.</p>"
        )

    def _show_shortcuts(self):
        text = (
            "<table cellpadding='4'>"
            "<tr><td><b>Open / New Tab / Close</b></td><td>Ctrl+O / Ctrl+T / Ctrl+W</td></tr>"
            "<tr><td><b>Save copy / Print</b></td><td>Ctrl+S / Ctrl+P</td></tr>"
            "<tr><td><b>Find / Next / Prev</b></td><td>Ctrl+F / F3 / Shift+F3</td></tr>"
            "<tr><td><b>Copy / Page text</b></td><td>Ctrl+C / Ctrl+Shift+C</td></tr>"
            "<tr><td><b>Zoom in / out / 100%</b></td><td>Ctrl++ / Ctrl+- / Ctrl+0</td></tr>"
            "<tr><td><b>Fit width / page</b></td><td>Ctrl+1 / Ctrl+2</td></tr>"
            "<tr><td><b>Rotate L / R</b></td><td>Ctrl+L / Ctrl+R</td></tr>"
            "<tr><td><b>Dark / Sepia</b></td><td>Ctrl+D / Ctrl+E</td></tr>"
            "<tr><td><b>Fullscreen / Presentation</b></td><td>F11 / F5</td></tr>"
            "<tr><td><b>Page nav</b></td><td>PgUp / PgDown / Arrows</td></tr>"
            "<tr><td><b>First / Last / Goto</b></td><td>Ctrl+Home / Ctrl+End / Ctrl+G</td></tr>"
            "<tr><td><b>Add bookmark</b></td><td>Ctrl+B</td></tr>"
            "<tr><td><b>Auto-scroll</b></td><td>Ctrl+Shift+A</td></tr>"
            "<tr><td><b>Zoom by mouse</b></td><td>Ctrl + Mouse Wheel</td></tr>"
            "</table>"
        )
        QMessageBox.information(self, "Keyboard Shortcuts", text)

    # ------------------------------------------------------------------
    # Window events
    # ------------------------------------------------------------------
    def closeEvent(self, e):
        self._save_session_state()
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("windowState", self.saveState())
        self.tabs.shutdown_all()
        super().closeEvent(e)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            for u in e.mimeData().urls():
                if u.toLocalFile().lower().endswith(".pdf"):
                    e.acceptProposedAction()
                    return
        e.ignore()

    def dropEvent(self, e):
        for u in e.mimeData().urls():
            p = u.toLocalFile()
            if p.lower().endswith(".pdf"):
                self._open_path(p)
                break


# ===========================================================================
# Entry point
# ===========================================================================


def main():
    # ---- High-DPI setup (MUST run before QApplication is created) --------
    # Qt6 enables high-DPI scaling by default, but the DEFAULT rounding policy
    # rounds fractional scale factors (e.g. Windows 125% / 150%) which can make
    # the toolbar/controls look misaligned. PassThrough keeps the exact
    # fractional factor so the UI scales cleanly at any Windows display scale.
    try:
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
    except Exception:
        pass
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORG_NAME)

    # Platform-appropriate modern font baseline. Qt falls back automatically
    # if the requested family isn't installed, so this is safe everywhere.
    from PyQt6.QtGui import QFont
    if sys.platform == "darwin":
        app.setFont(QFont("SF Pro Text", 13))
    elif sys.platform.startswith("win"):
        app.setFont(QFont("Segoe UI", 10))
    else:
        app.setFont(QFont("Inter", 10))

    # Application/window icon (used in taskbar, dock, alt-tab, dialogs).
    icon_path = _resource_path("App_icon.png")
    if os.path.isfile(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    win = MainWindow()
    if os.path.isfile(icon_path):
        win.setWindowIcon(QIcon(icon_path))
    # Apply the stylesheet now that QApplication exists.
    app.setStyleSheet(DARK_QSS if win.color_mode == "dark" else LIGHT_QSS)
    win.show()
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        win._open_path(sys.argv[1])
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
