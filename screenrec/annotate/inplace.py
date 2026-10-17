"""Flameshot-style annotation right on the frozen screen.

The whole frozen screen is the document's base image and the selection is
its crop, so the selection can still be moved and resized after drawing
(shapes stay where they are on screen).  Every action ends the session: the
overlay emits action(name, doc), then hides and deletes itself (unless the
handler already did); the Document stays valid for whoever handles it.
"""

from PyQt6.QtCore import QPoint, QRect, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QFont, QFontMetrics, QImage, QPainter, QPainterPath, QRegion
from PyQt6.QtWidgets import QWidget

from .. import style
from .canvas import Canvas
from .document import Document
from .panels import PropsPanel, ToolPanel

GAP = 8
MARGIN = 8

_ACTION_KEYS = {  # (key, ctrl, shift) -> action
    (Qt.Key.Key_C, True, False): "copy",
    (Qt.Key.Key_S, True, False): "save",
    (Qt.Key.Key_S, True, True): "save_as",
    (Qt.Key.Key_U, True, False): "upload",
    (Qt.Key.Key_P, True, False): "pin",
    (Qt.Key.Key_T, True, False): "ocr",
    (Qt.Key.Key_E, True, False): "edit",
}


class InPlaceEditor(QWidget):
    action = pyqtSignal(str, object)    # (name, Document)
    TITLE = "screenrec — editor"

    def __init__(self, screen, background, rect, windows=None):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.scr = screen
        self.setWindowTitle(self.TITLE)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._done = False
        geo = screen.geometry()
        origin = geo.topLeft()
        if geo.width() and abs(background.width() / background.devicePixelRatio() - geo.width()) > 1:
            background = QImage(background)     # dpr not set: derive it from the screen size
            background.setDevicePixelRatio(background.width() / geo.width())
        self.doc = Document(background)
        self.doc.crop = self.doc.to_pixels(QRectF(rect.translated(-origin)))
        self.canvas = Canvas(self.doc, inplace=True, parent=self)
        self.canvas.snap_rects = [QRectF(r.translated(-origin)) for r in (windows or [])]
        self.canvas.changed.connect(self._changed)
        self.canvas.selection_changed.connect(self._sync_props)
        self.canvas.tool_changed.connect(lambda t: self.tools.set_tool(t))

        self.f_text = QFont(self.font())
        self.f_text.setPointSizeF(self.font().pointSizeF() + 0.5)
        self.f_bold = QFont(self.f_text)
        self.f_bold.setBold(True)

        self.tools = ToolPanel(self)
        self.tools.tool.connect(self.canvas.set_tool)
        self.tools.action.connect(self._action)
        self.tools.set_tool(self.canvas.tool)
        self.props = PropsPanel(self)
        self.props.changed.connect(self.canvas.set_prop)
        self._placed_for = None
        self._sync_props()

    def present(self):
        self.setScreen(self.scr)
        self.setGeometry(self.scr.geometry())
        self.showFullScreen()
        self.raise_()
        self.activateWindow()
        self.setFocus()
        self._place_panels(force=True)

    # -- actions --

    def _action(self, name):
        if name == "undo":
            self.canvas.undo()
        elif name == "redo":
            self.canvas.redo()
        else:
            self.finish(name)

    def finish(self, name):
        """End the session with action `name` (also "cancel").

        The signal goes out while the overlay is still shown and focused: on
        Wayland the clipboard only accepts data from a focused window.  The
        handler may close the overlay itself; otherwise it closes right after.
        """
        if self._done:
            return
        self._done = True
        self.canvas.detach()
        self.action.emit(name, self.doc)
        self.hide()
        self.deleteLater()

    def selection_origin(self):
        """Top-left of the selection in global logical coordinates (e.g. to pin the result there)."""
        return self.scr.geometry().topLeft() + self.doc.bounds().topLeft().toPoint()

    def selection_rect(self):
        """The selection in global logical coordinates."""
        return self.doc.bounds().toAlignedRect().translated(self.scr.geometry().topLeft())

    def closeEvent(self, ev):
        if not self._done:      # closed from outside (e.g. the compositor)
            self._done = True
            self.canvas.detach()
            self.action.emit("cancel", self.doc)
        super().closeEvent(ev)

    # -- panels --

    def _sync_props(self):
        props, values = self.canvas.active_props()
        self.props.show_props(self.canvas.active_kind(), props, values)
        self._place_panels(force=True)
        self.tools.undo.setEnabled(self.doc.can_undo())
        self.tools.redo.setEnabled(self.doc.can_redo())

    def _busy(self):
        op = self.canvas.op
        return bool(op) and op[0].startswith("crop")

    def _changed(self):
        busy = self._busy()
        self.tools.setVisible(not busy)
        self.props.setVisible(not busy and bool(self.canvas.active_props()[0]))
        if not busy:
            self._place_panels()
        self.tools.undo.setEnabled(self.doc.can_undo())
        self.tools.redo.setEnabled(self.doc.can_redo())
        # repaint around the panels only: re-styling ~40 buttons per mouse move is the slow part
        dirty = QRegion(self.rect())
        for w in (self.tools, self.props):
            if w.isVisible():
                dirty -= QRegion(w.geometry())
        self.update(dirty)

    def _place_panels(self, force=False):
        sel = self.doc.bounds().toAlignedRect()
        key = (sel, self.props.isVisibleTo(self), self.width(), self.height())
        if not force and key == self._placed_for:
            return
        self._placed_for = key
        scr = self.rect()
        maxw = max(200, scr.width() - 2 * MARGIN)
        ts = self.tools.relayout(maxw)
        show_props = bool(self.canvas.active_props()[0])
        self.props.setVisible(show_props and not self._busy())
        ps = self.props.relayout(maxw) if show_props else None
        h = ts.height() + (GAP // 2 + ps.height() if ps else 0)
        w = max(ts.width(), ps.width() if ps else 0)
        x = sel.center().x() - w // 2
        x = max(MARGIN, min(x, scr.width() - w - MARGIN))
        if sel.bottom() + GAP + h <= scr.height() - MARGIN:          # below
            y = sel.bottom() + GAP
        elif sel.top() - GAP - h >= MARGIN:                           # above
            y = sel.top() - GAP - h
        else:                                                         # inside, at the bottom
            y = max(MARGIN, min(sel.bottom() - GAP - h, scr.height() - h - MARGIN))
        tx = max(MARGIN, min(sel.center().x() - ts.width() // 2, scr.width() - ts.width() - MARGIN))
        self.tools.move(tx, y)
        if ps:
            px = max(MARGIN, min(sel.center().x() - ps.width() // 2, scr.width() - ps.width() - MARGIN))
            self.props.move(px, y + ts.height() + GAP // 2)
        self.tools.raise_()
        self.props.raise_()

    # -- input --

    def keyPressEvent(self, ev):
        if self.canvas.key(ev):
            return
        k, mods = ev.key(), ev.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        if k == Qt.Key.Key_Escape:
            self.finish("cancel")
        elif k in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.finish("confirm")
        elif (k, ctrl, shift) in _ACTION_KEYS:
            self.finish(_ACTION_KEYS[(k, ctrl, shift)])
        else:
            super().keyPressEvent(ev)

    def mousePressEvent(self, ev):
        used = self.canvas.press(ev.position(), ev.button(), ev.modifiers())
        if not used and ev.button() == Qt.MouseButton.RightButton and not self.doc.shapes:
            self.finish("cancel")       # like the picker, while nothing has been drawn

    def mouseMoveEvent(self, ev):
        pos = ev.position()
        self.canvas.move(pos, ev.modifiers())
        self.setCursor(self.canvas.cursor_at(pos))

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self.canvas.release(ev.position(), ev.modifiers())
            self.setCursor(self.canvas.cursor_at(ev.position()))

    def mouseDoubleClickEvent(self, ev):
        if not self.canvas.double_click(ev.position()):
            self.mousePressEvent(ev)

    def wheelEvent(self, ev):
        d = ev.angleDelta().y()
        if d:
            self.canvas.bump(1 if d > 0 else -1)

    # -- painting --

    def paintEvent(self, _):
        p = QPainter(self)
        self.doc.paint(p)
        sel = self.doc.bounds()
        dim = QPainterPath()
        dim.addRect(QRectF(self.rect()))
        hole = QPainterPath()
        hole.addRect(sel)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.fillPath(dim.subtracted(hole), style.DIM)
        self.canvas.paint_overlay(p)
        if not (self.canvas.op and self.canvas.op[0] == "create"):
            size = self.doc.pixel_rect().size()
            self._draw_tag(p, sel.toAlignedRect(), f"{size.width()} × {size.height()}")
        p.end()

    def _draw_tag(self, p, r, text):
        """Size tag above the selection's top-left corner (inside if there's no room)."""
        fb = QFontMetrics(self.f_bold)
        h = fb.height() + 8
        w = fb.horizontalAdvance(text) + 18
        x, y = r.left(), r.top() - h - 6
        if y < 0:
            y = r.top() + 6
            x = r.left() + 6
        x = max(2, min(x, self.width() - w - 2))
        tag = QRect(x, y, w, h)
        if self.tools.isVisible() and self.tools.geometry().intersects(tag):
            return
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(style.ACCENT)
        p.drawRect(tag)
        p.setPen(style.ACCENT_TEXT)
        p.setFont(self.f_bold)
        p.drawText(QPoint(x + 9, y + 4 + fb.ascent()), text)
