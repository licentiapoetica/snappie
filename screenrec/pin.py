"""Pinned screenshots: a frameless image window that stays on top.

Wayland apps can't keep themselves on top or choose their position, so KWin
does both (kwin.place with mode "at").  The image is shown at its logical size
so a pin lines up exactly with what was captured.
"""

import itertools

from PyQt6.QtCore import QPoint, QRect, QRectF, QSize, Qt, QVariantAnimation, pyqtSignal
from PyQt6.QtGui import QAction, QCursor, QGuiApplication, QIcon, QKeySequence, QPainter, QPen
from PyQt6.QtWidgets import QMenu, QWidget

from . import style

BORDER = 1                      # logical px of accent border around the image
ZOOM_MIN, ZOOM_MAX = 0.1, 8.0
_ids = itertools.count(1)
_open = set()                   # keep pins alive while they're shown


class PinWindow(QWidget):
    action = pyqtSignal(str, object)   # ("copy"|"save"|"edit"|"upload"|"ocr", QImage)
    closed = pyqtSignal()

    def __init__(self, image, kwin, pos=None):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)   # for the opacity setting
        self.caption = f"screenrec — pin {next(_ids)}"
        self.setWindowTitle(self.caption)
        self.image = image
        self.kwin = kwin
        self.zoom = 1.0
        self.opacity = 1.0
        self._tag = ""
        self._tag_alpha = 0.0
        self._fade = QVariantAnimation(self, startValue=1.0, endValue=0.0, duration=1400)
        self._fade.valueChanged.connect(self._set_tag_alpha)
        self._resize()

        if pos is None:
            screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
            pos = screen.availableGeometry().center() - QPoint(self.width() // 2, self.height() // 2)
        else:
            pos = (pos.toPoint() if hasattr(pos, "toPoint") else QPoint(pos)) - QPoint(BORDER, BORDER)
            # ^ the image, not the border, lands at pos
        self._placed = False
        if kwin is not None:
            try:
                kwin.place(self.caption, (pos.x(), pos.y(), self.width(), self.height()), mode="at", once=True)
                self._placed = True
            except Exception:  # noqa: BLE001 - a pin without placement is still useful
                pass
        self.move(pos)          # X11 / no KWin
        _open.add(self)
        self.show()

    # -- geometry
    def _logical(self):
        dpr = self.image.devicePixelRatio() or 1.0
        return QSize(round(self.image.width() / dpr), round(self.image.height() / dpr))

    def _resize(self):
        s = self._logical()
        w = max(8, round(s.width() * self.zoom)) + 2 * BORDER
        h = max(8, round(s.height() * self.zoom)) + 2 * BORDER
        self.setFixedSize(w, h)   # grows from the top-left corner

    def set_zoom(self, z):
        self.zoom = min(ZOOM_MAX, max(ZOOM_MIN, z))
        self._resize()
        self._show_tag(f"{round(self.zoom * 100)}%")

    def set_opacity(self, o):
        self.opacity = min(1.0, max(0.2, o))
        self._show_tag(f"Opacity {round(self.opacity * 100)}%")
        self.update()

    def _show_tag(self, text):
        self._tag = text
        self._fade.stop()
        self._fade.setKeyValueAt(0.0, 1.0)
        self._fade.setKeyValueAt(0.6, 1.0)   # stay a moment, then fade
        self._fade.setKeyValueAt(1.0, 0.0)
        self._fade.start()
        self._set_tag_alpha(1.0)

    def _set_tag_alpha(self, a):
        self._tag_alpha = float(a)
        self.update()

    # -- painting
    def paintEvent(self, _):
        p = QPainter(self)
        inner = QRectF(self.rect()).adjusted(BORDER, BORDER, -BORDER, -BORDER)
        p.setOpacity(self.opacity)
        if self.zoom != 1.0:
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, self.zoom < 2.0)
        p.drawImage(inner, self.image, QRectF(self.image.rect()))
        p.setOpacity(1.0)
        pen = QPen(style.ACCENT, BORDER)
        pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        half = BORDER / 2
        p.drawRect(QRectF(self.rect()).adjusted(half, half, -half, -half))
        if self._tag and self._tag_alpha > 0:
            self._paint_tag(p)

    def _paint_tag(self, p):
        p.setOpacity(self._tag_alpha)
        f = self.font()
        f.setBold(True)
        p.setFont(f)
        fm = p.fontMetrics()
        w, h = fm.horizontalAdvance(self._tag) + 16, fm.height() + 8
        r = QRect(BORDER + 6, self.height() - BORDER - 6 - h, w, h)
        p.fillRect(r, style.PANEL_BG)
        p.setPen(style.TEXT)
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, self._tag)

    # -- input
    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton and self.windowHandle():
            self.windowHandle().startSystemMove()

    def mouseDoubleClickEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self.close()

    def wheelEvent(self, ev):
        steps = ev.angleDelta().y() / 120
        if not steps:
            return
        if ev.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.set_opacity(round((self.opacity + 0.1 * steps) * 10) / 10)
        else:
            self.set_zoom(self.zoom * (1.1 ** steps))

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key.Key_Escape:
            self.close()
        elif ev.matches(QKeySequence.StandardKey.Copy):
            self.action.emit("copy", self.image)
        elif ev.matches(QKeySequence.StandardKey.Save):
            self.action.emit("save", self.image)
        elif ev.key() in (Qt.Key.Key_0, Qt.Key.Key_1) and ev.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.set_zoom(1.0)
        else:
            super().keyPressEvent(ev)

    def contextMenuEvent(self, ev):
        self.menu().exec(ev.globalPos())

    def menu(self):
        m = QMenu(self)

        def add(text, slot, icon=None, shortcut=None):
            a = QAction(text, m)
            if icon:
                a.setIcon(QIcon.fromTheme(icon))
            if shortcut:
                a.setShortcut(shortcut)
            a.triggered.connect(slot)
            m.addAction(a)
            return a

        emit = self.action.emit
        add("Copy", lambda: emit("copy", self.image), "edit-copy", QKeySequence.StandardKey.Copy)
        add("Save as…", lambda: emit("save", self.image), "document-save-as", QKeySequence.StandardKey.Save)
        add("Open in editor", lambda: emit("edit", self.image), "document-edit")
        add("Upload", lambda: emit("upload", self.image), "cloud-upload")
        add("Recognize text (OCR)", lambda: emit("ocr", self.image), "edit-select-text")
        m.addSeparator()
        add("Zoom 100%", lambda: self.set_zoom(1.0), "zoom-original")
        op = m.addMenu(QIcon.fromTheme("view-visible"), "Opacity")
        for pct in (100, 80, 60, 40):
            a = op.addAction(f"{pct}%")
            a.setCheckable(True)
            a.setChecked(round(self.opacity * 100) == pct)
            a.triggered.connect(lambda _=False, v=pct / 100: self.set_opacity(v))
        m.addSeparator()
        add("Close", self.close, "window-close", QKeySequence(Qt.Key.Key_Escape))
        return m

    def closeEvent(self, ev):
        if self.kwin is not None and self._placed:
            try:
                self.kwin.unplace(self.caption)
            except Exception:  # noqa: BLE001
                pass
            self._placed = False
        _open.discard(self)
        super().closeEvent(ev)
        self.closed.emit()

