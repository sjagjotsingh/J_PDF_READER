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
)
from PyQt6.QtGui import (
    QAction,
    QColor,
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
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMenuBar,
    QMessageBox,
    QPlainTextEdit,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStatusBar,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

APP_NAME = "J PDF Reader"
ORG_NAME = "JPDFReader"


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
RENDER_CACHE_MAX = 60          # cached rendered pages
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

QToolTip { background-color: #1f2328; color: white; border: none; padding: 4px 8px; border-radius: 4px; }
"""

DARK_QSS = """
* { font-family: "Segoe UI", "SF Pro Text", system-ui, sans-serif; font-size: 10pt; }

QMainWindow, QWidget, QDialog { background-color: #181a1f; color: #e6e6e6; }

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

QTabBar::tab {
    background: #20232a;
    color: #c0c4cc;
    padding: 6px 14px;
    border: 1px solid #2c2f36;
    border-bottom: none;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
}
QTabBar::tab:selected { background: #181a1f; color: #ffffff; }
QTabBar::tab:hover { background: #2a2e36; }

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
    zoom_x1000: int          # int to make hashable & stable
    rotation: int
    color_mode: str          # "light", "dark", "sepia"

    @classmethod
    def make(cls, page_index: int, zoom: float, rotation: int, color_mode: str) -> "RenderKey":
        return cls(page_index, int(round(zoom * 1000)), rotation % 360, color_mode)


@dataclass(order=True)
class RenderTask:
    priority: int                                # lower = processed sooner
    seq: int                                     # tie-breaker (newer wins)
    key: RenderKey = field(compare=False)
    is_thumb: bool = field(default=False, compare=False)


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


class RenderWorker(QObject):
    """
    Lives on a worker QThread. Pulls render tasks off a thread-safe priority
    queue and emits the resulting QImage on the GUI thread via a signal.

    PyMuPDF is not thread-safe across threads sharing the same Document.
    Because a single worker thread handles all renders, access is serialised.
    """
    pageRendered = pyqtSignal(RenderKey, QImage, bool)  # key, image, is_thumb

    def __init__(self):
        super().__init__()
        self._queue: "queue.PriorityQueue[RenderTask]" = queue.PriorityQueue()
        self._doc: Optional[fitz.Document] = None
        self._stop = False
        self._seq = 0
        self._lock = threading.Lock()
        # Membership tracking so we don't queue a key twice.
        self._pending: set = set()

    # Document is set from the GUI thread; the worker is paused until then.
    def set_document(self, doc: Optional[fitz.Document]):
        with self._lock:
            self._doc = doc
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
            self._queue.put(RenderTask(priority, self._seq, key, is_thumb))

    def stop(self):
        self._stop = True
        # Push a dummy item to unblock the get()
        self._seq += 1
        self._queue.put(RenderTask(0, self._seq, RenderKey(-1, 0, 0, "light")))

    def run_loop(self):
        while not self._stop:
            try:
                task = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if self._stop:
                break
            with self._lock:
                doc = self._doc
                self._pending.discard((task.key, task.is_thumb))
            if doc is None or task.key.page_index < 0 or task.key.page_index >= doc.page_count:
                continue
            try:
                img = self._render(doc, task.key, task.is_thumb)
            except Exception:
                continue
            if img is not None:
                self.pageRendered.emit(task.key, img, task.is_thumb)

    def _render(self, doc: fitz.Document, key: RenderKey, is_thumb: bool) -> Optional[QImage]:
        page = doc.load_page(key.page_index)
        zoom = key.zoom_x1000 / 1000.0
        if is_thumb:
            zoom = 0.18
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
        self.zoom: float = 1.0
        self.rotation: int = 0
        # "light" | "dark" | "sepia" - controls the placeholder colour drawn
        # while the actual page bitmap is still being rendered in the bg thread.
        self.color_mode: str = "light"

        self.search_hits: List[fitz.Rect] = []
        self.current_hit_local_idx: int = -1

        # Tint overlay drawn on top of the rendered pixmap (e.g. for sepia).
        self.tint_color: Optional[QColor] = None

        self._sel_start: Optional[QPoint] = None
        self._sel_end: Optional[QPoint] = None
        self._selection_rect: Optional[QRectF] = None

        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    # ------------------------------------------------------------------
    def update_geometry(self, zoom: float, rotation: int):
        self.zoom = zoom
        self.rotation = rotation
        if rotation in (90, 270):
            w, h = self.page_h_pt, self.page_w_pt
        else:
            w, h = self.page_w_pt, self.page_h_pt
        self.setFixedSize(int(w * zoom) + 12, int(h * zoom) + 12)
        # When geometry changes, the cached pixmap (if any) might be wrong size.
        self.pixmap_image = None
        self._selection_rect = None
        self.update()

    def set_pixmap(self, pix: QPixmap):
        self.pixmap_image = pix
        self.update()

    def set_search_hits(self, hits: List[fitz.Rect], current_local_idx: int = -1):
        self.search_hits = hits
        self.current_hit_local_idx = current_local_idx
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
                p.save()
                p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Multiply)
                p.fillRect(
                    page_rect.x(), page_rect.y(),
                    self.pixmap_image.width(), self.pixmap_image.height(),
                    self.tint_color,
                )
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
        self.zoom: float = 1.0
        self.fit_mode: Optional[str] = "width"
        self.rotation: int = 0
        self.color_mode: str = "light"   # "light" | "dark" | "sepia"
        self.view_mode: str = self.VIEW_CONTINUOUS
        self.single_page_index: int = 0  # for single & two-page mode

        # Render cache & worker
        self.cache = LRUCache(RENDER_CACHE_MAX)
        self.thumb_cache = LRUCache(THUMB_CACHE_MAX)
        self.worker = RenderWorker()
        self.worker_thread = QThread()
        self.worker.moveToThread(self.worker_thread)
        self.worker_thread.started.connect(self.worker.run_loop)
        self.worker.pageRendered.connect(self._on_page_rendered)
        self.worker_thread.start()

        # Search
        self.search_hits: List[SearchHit] = []
        self.current_hit_idx: int = -1

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
        self.worker.set_document(None)
        if self.doc is not None:
            self.doc.close()
            self.doc = None
            self.doc_path = None
        for w in self.page_widgets:
            w.setParent(None)
            w.deleteLater()
        self.page_widgets = []
        self.search_hits = []
        self.current_hit_idx = -1
        self.cache.clear()
        self.thumb_cache.clear()
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

        if self.view_mode == self.VIEW_TWO:
            # Two pages per row
            i = 0
            while i < self.doc.page_count:
                row = QWidget()
                hl = QHBoxLayout(row)
                hl.setContentsMargins(0, 0, 0, 0)
                hl.setSpacing(0)
                hl.setAlignment(Qt.AlignmentFlag.AlignHCenter)
                # First "cover" page is shown alone on the right
                if i == 0:
                    spacer = QWidget()
                    spacer.setFixedSize(1, 1)
                    hl.addWidget(spacer)
                    w = self._make_page_widget(i)
                    hl.addWidget(w)
                    self.page_widgets.append(w)
                    i += 1
                else:
                    w_left = self._make_page_widget(i)
                    hl.addWidget(w_left)
                    self.page_widgets.append(w_left)
                    if i + 1 < self.doc.page_count:
                        w_right = self._make_page_widget(i + 1)
                        hl.addWidget(w_right)
                        self.page_widgets.append(w_right)
                    i += 2
                self._layout.addWidget(row)
        else:
            for i in range(self.doc.page_count):
                w = self._make_page_widget(i)
                self.page_widgets.append(w)
                self._layout.addWidget(w)
            if self.view_mode == self.VIEW_SINGLE:
                self._update_single_page_visibility()

        self._schedule_visible_render()

    def _make_page_widget(self, i: int) -> PdfPageWidget:
        rect = self.doc.load_page(i).rect
        w = PdfPageWidget(i, rect.width, rect.height, self._container)
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

    def _render_color_mode(self) -> str:
        # Sepia uses the same bitmap as light + a tint overlay at paint time.
        return "dark" if self.color_mode == "dark" else "light"

    def _ensure_rendered(self, page_index: int, priority: int = 5):
        widget = self._widget_for_page(page_index)
        if widget is None:
            return
        key = RenderKey.make(page_index, self.zoom, self.rotation, self._render_color_mode())
        cached = self.cache.get(key)
        if cached is not None:
            if widget.pixmap_image is not cached:
                widget.set_pixmap(cached)
                self._apply_search_highlight_to_widget(widget)
            return
        # Not cached yet - request render
        self.worker.submit(key, priority=priority, is_thumb=False)

    def _widget_for_page(self, page_index: int) -> Optional[PdfPageWidget]:
        for w in self.page_widgets:
            if w.page_index == page_index:
                return w
        return None

    def _on_page_rendered(self, key: RenderKey, img: QImage, is_thumb: bool):
        if is_thumb:
            return
        # Discard if config has changed in the meantime.
        if (key.zoom_x1000 != int(round(self.zoom * 1000))
                or key.rotation != self.rotation % 360
                or key.color_mode != self._render_color_mode()):
            return
        pix = QPixmap.fromImage(img)
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
        for w in self.page_widgets:
            if not w.isVisible():
                continue
            # Map widget Y in container coords
            top = w.mapTo(self._container, QPoint(0, 0)).y()
            bot = top + w.height()
            if bot >= viewport_top and top <= viewport_bot:
                result.append(w.page_index)
        return result

    def _on_scroll(self, _val):
        if self.doc:
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
            for w in self.page_widgets:
                # Try cache; if miss, force a re-render request below.
                key = RenderKey.make(w.page_index, self.zoom, self.rotation, new_render_mode)
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
        for w in self.page_widgets:
            w.set_search_hits([], -1)
        if self.doc is None or not query:
            return 0
        for i in range(self.doc.page_count):
            page = self.doc.load_page(i)
            try:
                rects = page.search_for(query, quads=False)
            except Exception:
                rects = []
            for r in rects:
                self.search_hits.append(SearchHit(page_index=i, rect=r))
        if self.search_hits:
            self.current_hit_idx = 0
            self._scroll_to_hit(self.search_hits[0])
        for w in self.page_widgets:
            self._apply_search_highlight_to_widget(w)
        return len(self.search_hits)

    def _apply_search_highlight_to_widget(self, w: PdfPageWidget):
        hits = [h.rect for h in self.search_hits if h.page_index == w.page_index]
        cur = -1
        if 0 <= self.current_hit_idx < len(self.search_hits):
            cur_hit = self.search_hits[self.current_hit_idx]
            if cur_hit.page_index == w.page_index:
                local = [h for h in self.search_hits if h.page_index == w.page_index]
                for li, h in enumerate(local):
                    if h.rect == cur_hit.rect:
                        cur = li
                        break
        w.set_search_hits(hits, cur)

    def next_hit(self):
        if not self.search_hits:
            return
        self.current_hit_idx = (self.current_hit_idx + 1) % len(self.search_hits)
        self._scroll_to_hit(self.search_hits[self.current_hit_idx])
        for w in self.page_widgets:
            self._apply_search_highlight_to_widget(w)

    def prev_hit(self):
        if not self.search_hits:
            return
        self.current_hit_idx = (self.current_hit_idx - 1) % len(self.search_hits)
        self._scroll_to_hit(self.search_hits[self.current_hit_idx])
        for w in self.page_widgets:
            self._apply_search_highlight_to_widget(w)

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
    def request_thumbnails(self):
        if self.doc is None:
            return
        mode = self._render_color_mode()
        for i in range(self.doc.page_count):
            key = RenderKey.make(i, 0.18, 0, mode)
            cached = self.thumb_cache.get(key)
            if cached is not None:
                # Push the cached one out via signal as if it just arrived
                self.thumbnailReady.emit(i, cached.toImage())
            else:
                self.worker.submit(key, priority=100 + i, is_thumb=True)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------
    def shutdown(self):
        self.worker.stop()
        self.worker_thread.quit()
        self.worker_thread.wait(1500)


# ===========================================================================
# Side panels
# ===========================================================================


class ThumbnailPanel(QListWidget):
    pageRequested = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setIconSize(QSize(140, 180))
        self.setSpacing(2)
        self.setUniformItemSizes(False)
        self.itemClicked.connect(self._on_clicked)
        self._items_by_page: Dict[int, QListWidgetItem] = {}

    def populate_placeholders(self, n: int):
        self.clear()
        self._items_by_page = {}
        for i in range(n):
            item = QListWidgetItem(f"Page {i + 1}")
            item.setData(Qt.ItemDataRole.UserRole, i)
            self.addItem(item)
            self._items_by_page[i] = item

    def set_thumbnail(self, page_index: int, image: QImage):
        item = self._items_by_page.get(page_index)
        if item is None:
            return
        item.setIcon(QIcon(QPixmap.fromImage(image)))

    def _on_clicked(self, item: QListWidgetItem):
        self.pageRequested.emit(item.data(Qt.ItemDataRole.UserRole))


class OutlinePanel(QTreeWidget):
    pageRequested = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.itemClicked.connect(self._on_clicked)

    def populate(self, doc: Optional[fitz.Document]):
        self.clear()
        if doc is None:
            return
        try:
            toc = doc.get_toc(simple=True)
        except Exception:
            toc = []
        if not toc:
            empty = QTreeWidgetItem(["(No outline / bookmarks)"])
            empty.setDisabled(True)
            self.addTopLevelItem(empty)
            return
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

        self.viewer = PdfViewer(self)
        self.setCentralWidget(self.viewer)
        self.viewer.pageChanged.connect(self._on_page_changed)
        self.viewer.zoomChanged.connect(self._on_zoom_changed)
        self.viewer.statusMessage.connect(self._set_status)
        self.viewer.documentLoaded.connect(self._on_document_loaded)
        self.viewer.documentClosed.connect(self._on_document_closed)
        self.viewer.thumbnailReady.connect(self._on_thumbnail_ready)

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

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_docks(self):
        self.thumb_panel = ThumbnailPanel()
        self.thumb_panel.pageRequested.connect(self.viewer.goto_page)
        self.thumb_dock = QDockWidget("Thumbnails", self)
        self.thumb_dock.setWidget(self.thumb_panel)
        self.thumb_dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.thumb_dock)

        self.outline_panel = OutlinePanel()
        self.outline_panel.pageRequested.connect(self.viewer.goto_page)
        self.outline_dock = QDockWidget("Outline", self)
        self.outline_dock.setWidget(self.outline_panel)
        self.outline_dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.outline_dock)

        self.bookmarks_panel = BookmarksPanel()
        self.bookmarks_panel.btn_add.clicked.connect(self._add_bookmark)
        self.bookmarks_panel.pageRequested.connect(self.viewer.goto_page)
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
        m_edit.addAction(self._mk("&Copy", self.viewer.copy_selection, QKeySequence.StandardKey.Copy))
        m_edit.addAction(self._mk("Copy Whole Page Text", self.viewer.select_all_visible_text, "Ctrl+Shift+C"))
        m_edit.addAction(self._mk("&Find...", self._focus_search, QKeySequence.StandardKey.Find))

        # View ------------------------------------------------------------
        m_view = mb.addMenu("&View")
        m_view.addAction(self._mk("Zoom &In", self.viewer.zoom_in, "Ctrl++"))
        m_view.addAction(self._mk("Zoom &Out", self.viewer.zoom_out, "Ctrl+-"))
        m_view.addAction(self._mk("Fit &Width", self.viewer.fit_width, "Ctrl+1"))
        m_view.addAction(self._mk("Fit &Page", self.viewer.fit_page, "Ctrl+2"))
        m_view.addAction(self._mk("Actual &Size (100%)", lambda: self.viewer.set_zoom(1.0, None), "Ctrl+0"))
        m_view.addSeparator()
        m_view.addAction(self._mk("Rotate &Left", lambda: self.viewer.rotate(-90), "Ctrl+L"))
        m_view.addAction(self._mk("Rotate &Right", lambda: self.viewer.rotate(90), "Ctrl+R"))
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
        self.act_continuous = self._mk("&Continuous", lambda: self.viewer.set_view_mode(PdfViewer.VIEW_CONTINUOUS), checkable=True)
        self.act_single = self._mk("&Single Page", lambda: self.viewer.set_view_mode(PdfViewer.VIEW_SINGLE), checkable=True)
        self.act_two = self._mk("&Two Page (Book)", lambda: self.viewer.set_view_mode(PdfViewer.VIEW_TWO), checkable=True)
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
        m_nav.addAction(self._mk("&First Page", lambda: self.viewer.goto_page(0), "Ctrl+Home"))
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
        tb = QToolBar("Main")
        tb.setMovable(False)
        tb.setIconSize(QSize(20, 20))
        self.addToolBar(tb)

        tb.addWidget(self._tb_btn("Open", self.action_open))
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
        tb.addWidget(self._tb_btn("−", self.viewer.zoom_out, tip="Zoom Out"))

        self.zoom_combo = QComboBox()
        self.zoom_combo.setEditable(True)
        self.zoom_combo.addItems(
            ["Fit Width", "Fit Page", "50%", "75%", "100%", "125%", "150%", "200%", "300%"]
        )
        self.zoom_combo.setFixedWidth(110)
        self.zoom_combo.activated.connect(lambda *_: self._on_zoom_combo_text())
        self.zoom_combo.lineEdit().returnPressed.connect(self._on_zoom_combo_text)
        tb.addWidget(self.zoom_combo)
        tb.addWidget(self._tb_btn("+", self.viewer.zoom_in, tip="Zoom In"))

        tb.addSeparator()
        tb.addWidget(self._tb_btn("⟲", lambda: self.viewer.rotate(-90), tip="Rotate Left"))
        tb.addWidget(self._tb_btn("⟳", lambda: self.viewer.rotate(90), tip="Rotate Right"))
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
        tb.addWidget(self._tb_btn("↑", self.viewer.prev_hit, tip="Previous match (Shift+F3)"))
        tb.addWidget(self._tb_btn("↓", self.viewer.next_hit, tip="Next match (F3)"))
        self.search_count_lbl = QLabel("")
        tb.addWidget(self.search_count_lbl)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)

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
        tb.addWidget(self.btn_ocr)

        # Color mode toggle
        self.btn_dark = QPushButton("🌙 Dark")
        self.btn_dark.setCheckable(True)
        self.btn_dark.setChecked(self.color_mode == "dark")
        self.btn_dark.setToolTip("Toggle Dark Mode (Ctrl+D)")
        self.btn_dark.clicked.connect(lambda checked: self._apply_color_mode("dark" if checked else "light"))
        tb.addWidget(self.btn_dark)

    def _tb_btn(self, text: str, slot, tip: str = "") -> QPushButton:
        b = QPushButton(text)
        if tip:
            b.setToolTip(tip)
        b.clicked.connect(slot)
        return b

    def _build_statusbar(self):
        self.status: QStatusBar = self.statusBar()
        self.page_status_lbl = QLabel(" - / - ")
        self.zoom_status_lbl = QLabel("100%")
        self.status.addPermanentWidget(self.page_status_lbl)
        self.status.addPermanentWidget(self.zoom_status_lbl)
        self.status.showMessage("Ready. Open a PDF to begin.")

    def _build_shortcuts(self):
        QShortcut(QKeySequence("F3"), self, activated=self.viewer.next_hit)
        QShortcut(QKeySequence("Shift+F3"), self, activated=self.viewer.prev_hit)
        QShortcut(QKeySequence("Esc"), self, activated=self._on_escape)
        # Arrow keys for navigation in single-page mode + scrolling
        QShortcut(QKeySequence("Right"), self, activated=self._next_page)
        QShortcut(QKeySequence("Left"), self, activated=self._prev_page)
        QShortcut(QKeySequence("Space"), self, activated=self._page_down_scroll)
        QShortcut(QKeySequence("Shift+Space"), self, activated=self._page_up_scroll)

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
        # Save state of current doc first
        self._save_session_state()
        if self.viewer.open_pdf(path):
            self.settings.setValue("last_dir", os.path.dirname(path))
            self._add_to_recent(path)

    def action_close_pdf(self):
        self._save_session_state()
        self.viewer.close_pdf()

    def action_save_copy(self):
        if self.viewer.doc is None or self.viewer.doc_path is None:
            return
        suggested = os.path.splitext(os.path.basename(self.viewer.doc_path))[0] + "_copy.pdf"
        last_dir = self.settings.value("last_dir", "")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save a Copy", os.path.join(last_dir, suggested), "PDF Files (*.pdf)"
        )
        if not path:
            return
        try:
            self.viewer.doc.save(path, garbage=4, deflate=True, clean=True)
            self._set_status(f"Saved copy: {path}")
        except Exception as e:
            QMessageBox.critical(self, "Save failed", str(e))

    def action_extract_text(self):
        if self.viewer.doc is None:
            return
        suggested = os.path.splitext(os.path.basename(self.viewer.doc_path or "document.pdf"))[0] + ".txt"
        path, _ = QFileDialog.getSaveFileName(
            self, "Extract Text", suggested, "Text Files (*.txt)"
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                for i in range(self.viewer.doc.page_count):
                    f.write(f"--- Page {i + 1} ---\n")
                    f.write(self.viewer.doc.load_page(i).get_text("text"))
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
        if self.viewer.doc is None or not self._check_tesseract():
            return
        idx = self.viewer.current_page_index()
        page = self.viewer.doc.load_page(idx)
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
        if self.viewer.doc is None or not self._check_tesseract():
            return
        # Find the selected widget (page that has a non-empty selection rect).
        target = None
        sel_rect = None
        for w in self.viewer.page_widgets:
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
        page = self.viewer.doc.load_page(target.page_index)
        rot = self.viewer.rotation
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
        if self.viewer.doc is None or not self._check_tesseract():
            return
        suggested = os.path.splitext(os.path.basename(self.viewer.doc_path or "document.pdf"))[0] + "_ocr.txt"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save OCR Text", suggested, "Text Files (*.txt)"
        )
        if not path:
            return
        n = self.viewer.doc.page_count
        progress = QProgressDialog(
            "Running OCR on entire document...", "Cancel", 0, n, self
        )
        progress.setWindowTitle("OCR in progress")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        try:
            with open(path, "w", encoding="utf-8") as f:
                for i in range(n):
                    if progress.wasCanceled():
                        break
                    progress.setValue(i)
                    progress.setLabelText(f"OCR page {i + 1} / {n}...")
                    QApplication.processEvents()
                    try:
                        page = self.viewer.doc.load_page(i)
                        txt = _ocr_page_text(page, language=self.ocr_language)
                    except Exception as e:
                        txt = f"[OCR error on page {i + 1}: {e}]"
                    f.write(f"--- Page {i + 1} ---\n")
                    f.write(txt)
                    f.write("\n\n")
            progress.setValue(n)
        except Exception as e:
            progress.close()
            QMessageBox.critical(self, "OCR failed", str(e))
            return
        if not progress.wasCanceled():
            self._set_status(f"OCR complete: {path}")
            QMessageBox.information(self, "OCR complete", f"Text saved to:\n{path}")

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

    def action_properties(self):
        if self.viewer.doc is None:
            return
        dlg = PropertiesDialog(self.viewer.doc, self.viewer.doc_path or "", self)
        dlg.exec()

    def action_print(self):
        if self.viewer.doc is None:
            QMessageBox.information(self, APP_NAME, "Open a PDF first.")
            return
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        dlg = QPrintDialog(printer, self)
        if dlg.exec() != QPrintDialog.DialogCode.Accepted:
            return
        painter = QPainter(printer)
        try:
            page_rect = printer.pageRect(QPrinter.Unit.Point).toRect()
            for i in range(self.viewer.doc.page_count):
                if i > 0:
                    printer.newPage()
                page = self.viewer.doc.load_page(i)
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
        doc = self.viewer.doc
        if doc is None:
            return
        self.setWindowTitle(f"{os.path.basename(self.viewer.doc_path or '')} — {APP_NAME}")
        self.thumb_panel.populate_placeholders(doc.page_count)
        self.outline_panel.populate(doc)
        self.page_total_lbl.setText(f" / {doc.page_count} ")
        self.page_input.setText("1")
        # Load bookmarks for this file
        self._load_bookmarks()
        # Restore last page
        last_page = self._get_per_file_setting("last_page")
        if isinstance(last_page, int) and 0 <= last_page < doc.page_count:
            QTimer.singleShot(50, lambda p=last_page: self.viewer.goto_page(p))
        # Background-render thumbnails
        QTimer.singleShot(50, self.viewer.request_thumbnails)
        self._on_page_changed(self.viewer.current_page_index())

    def _on_document_closed(self):
        self.setWindowTitle(APP_NAME)
        self.thumb_panel.clear()
        self.outline_panel.clear()
        self.bookmarks_panel.set_bookmarks([])
        self.page_input.setText("")
        self.page_total_lbl.setText(" / 0 ")
        self.search_count_lbl.setText("")

    def _on_thumbnail_ready(self, page_index: int, image: QImage):
        self.thumb_panel.set_thumbnail(page_index, image)

    # ------------------------------------------------------------------
    # Page nav
    # ------------------------------------------------------------------
    def _prev_page(self):
        if self.viewer.doc:
            self.viewer.goto_page(self.viewer.current_page_index() - 1)

    def _next_page(self):
        if self.viewer.doc:
            self.viewer.goto_page(self.viewer.current_page_index() + 1)

    def _last_page(self):
        if self.viewer.doc:
            self.viewer.goto_page(self.viewer.doc.page_count - 1)

    def _goto_from_input(self):
        try:
            self.viewer.goto_page(int(self.page_input.text()) - 1)
        except ValueError:
            pass

    def _goto_dialog(self):
        if self.viewer.doc is None:
            return
        n, ok = QInputDialog.getInt(
            self, "Go to Page", f"Page (1 - {self.viewer.doc.page_count}):",
            self.viewer.current_page_index() + 1, 1, self.viewer.doc.page_count
        )
        if ok:
            self.viewer.goto_page(n - 1)

    def _page_down_scroll(self):
        bar = self.viewer.verticalScrollBar()
        bar.setValue(bar.value() + self.viewer.viewport().height())

    def _page_up_scroll(self):
        bar = self.viewer.verticalScrollBar()
        bar.setValue(bar.value() - self.viewer.viewport().height())

    # ------------------------------------------------------------------
    # Zoom helpers
    # ------------------------------------------------------------------
    def _on_zoom_combo_text(self):
        text = self.zoom_combo.currentText().strip().lower()
        if "width" in text:
            self.viewer.fit_width()
            return
        if "page" in text:
            self.viewer.fit_page()
            return
        try:
            pct = float(text.replace("%", "").strip())
            self.viewer.set_zoom(pct / 100.0, fit_mode=None)
        except ValueError:
            pass

    def _on_zoom_changed(self, zoom: float):
        pct = f"{int(zoom * 100)}%"
        self.zoom_combo.lineEdit().setText(pct)
        self.zoom_status_lbl.setText(pct)

    def _on_view_combo(self, idx: int):
        modes = [PdfViewer.VIEW_CONTINUOUS, PdfViewer.VIEW_SINGLE, PdfViewer.VIEW_TWO]
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
        if self.viewer.doc:
            self.page_input.setText(str(idx + 1))
            self.page_status_lbl.setText(f" {idx + 1} / {self.viewer.doc.page_count} ")
            if 0 <= idx < self.thumb_panel.count():
                self.thumb_panel.blockSignals(True)
                self.thumb_panel.setCurrentRow(idx)
                self.thumb_panel.blockSignals(False)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------
    def _focus_search(self):
        self.search_input.setFocus()
        self.search_input.selectAll()

    def _do_search(self):
        q = self.search_input.text().strip()
        n = self.viewer.search(q)
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

        if refresh:
            prev_render = "dark" if self.viewer.color_mode == "dark" else "light"
            new_render = "dark" if mode == "dark" else "light"
            self.viewer.set_color_mode(mode)
            # Re-request thumbnails only when the underlying bitmap changes.
            if self.viewer.doc is not None and prev_render != new_render:
                self.viewer.thumb_cache.clear()
                self.viewer.request_thumbnails()
        else:
            self.viewer.color_mode = mode

    def _toggle_fullscreen(self):
        if self.act_full.isChecked():
            self.showFullScreen()
        else:
            self.showNormal()

    def _toggle_presentation(self):
        # Presentation = single-page + fullscreen + hide UI chrome
        if self.act_present.isChecked():
            self._pre_present_state = (
                self.viewer.view_mode,
                self.thumb_dock.isVisible(),
                self.outline_dock.isVisible(),
                self.bookmarks_dock.isVisible(),
                self.menuBar().isVisible(),
            )
            self.viewer.set_view_mode(PdfViewer.VIEW_SINGLE)
            self.thumb_dock.hide()
            self.outline_dock.hide()
            self.bookmarks_dock.hide()
            self.menuBar().hide()
            self.showFullScreen()
        else:
            mode, t, o, b, mb_v = getattr(self, "_pre_present_state", (None, True, True, True, True))
            if mode is not None:
                self.viewer.set_view_mode(mode)
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
            # Otherwise: clear any text selections + search highlights
            self.viewer.clear_all_selections()
            if self.search_input.text():
                self.search_input.clear()
            self.viewer.search("")
            self.search_count_lbl.setText("")

    # ------------------------------------------------------------------
    # Auto-scroll
    # ------------------------------------------------------------------
    def _toggle_autoscroll(self):
        active = self.viewer.toggle_autoscroll()
        self._set_status("Auto-scroll ON" if active else "Auto-scroll OFF")

    def _adj_autoscroll(self, delta: int):
        new_speed = max(1, self.viewer._autoscroll_speed + delta)
        self.viewer.set_autoscroll_speed(new_speed)
        self._set_status(f"Auto-scroll speed: {new_speed} px/tick")

    # ------------------------------------------------------------------
    # Bookmarks
    # ------------------------------------------------------------------
    def _add_bookmark(self):
        if self.viewer.doc is None:
            return
        page = self.viewer.current_page_index()
        label, ok = QInputDialog.getText(
            self, "Add Bookmark", f"Label for page {page + 1}:",
            QLineEdit.EchoMode.Normal, f"Page {page + 1}"
        )
        if not ok:
            return
        self.bookmarks_panel.add_bookmark(page, label or f"Page {page + 1}")

    def _save_bookmarks(self):
        if self.viewer.doc_path:
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

    # ------------------------------------------------------------------
    # Per-file persistence
    # ------------------------------------------------------------------
    def _per_file_key(self, suffix: str) -> Optional[str]:
        if not self.viewer.doc_path:
            return None
        return f"file/{os.path.abspath(self.viewer.doc_path)}/{suffix}"

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
        if self.viewer.doc_path is not None:
            self._set_per_file_setting("last_page", self.viewer.current_page_index())

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
            "<tr><td><b>Open / Close</b></td><td>Ctrl+O / Ctrl+W</td></tr>"
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
        self.viewer.shutdown()
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
    # High-DPI awareness on Windows is on by default in Qt6.
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
