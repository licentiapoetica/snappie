"""Live drawing, click ripples and pressed keys on top of a recording.

LiveOverlay is a transparent window that KWin keeps exactly over the recorded
area.  Region recordings capture it like anything else on screen; window
recordings only see the window's own pixels, so the recorder composites
render_layer() into the video instead.

InputWatcher reads clicks and key presses straight from /dev/input (evdev),
because Wayland doesn't let one app see another app's input.  It only reads
while started, never grabs a device (input still goes where it normally goes),
and only turns presses into short labels for the overlay.
"""

import ctypes
import ctypes.util
import itertools
import os
import select
import threading
import time

from PyQt6.QtCore import QObject, QPointF, QRect, QRectF, QSize, Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtDBus import QDBusConnection, QDBusMessage
from PyQt6.QtGui import QColor, QFont, QFontMetricsF, QImage, QPainter, QPainterPath, QPen, QPolygonF
from PyQt6.QtWidgets import QWidget

from . import settings, style

TITLE = "screenrec — live overlay"

FADE = 0.4          # s, fade-out of strokes, key bubble and hint chip
RIPPLE = 0.45       # s, click ripple
KEYS_HOLD = 1.5     # s without typing before the key bubble fades
CHIP_HOLD = 2.5     # s the hint chip stays after turning drawing on / changing the pen
FRAME_MS = 16       # ~60 fps while something animates

RIPPLE_COLORS = {1: style.ACCENT, 2: QColor("#27ae60"), 3: style.WARN}
WIDTHS = (2, 3, 4, 6, 8, 12, 16, 24)


def _fade(now, start):
    """1 before start, then a linear fade to 0 over FADE seconds."""
    return 1.0 if now <= start else max(0.0, 1.0 - (now - start) / FADE)


class _Stroke:
    """One drawn shape in area-logical coordinates.  kind: pen | arrow | rect."""

    _serials = itertools.count()

    def __init__(self, kind, color, width, p):
        self.serial = next(self._serials)     # cache key (id() gets reused after undo)
        self.kind, self.color, self.width = kind, QColor(color), width
        self.pts = [QPointF(p)]
        self.done = None            # monotonic time when the mouse was released
        self._path = None

    def add(self, p):
        if self.kind == "pen":
            last = self.pts[-1]
            if abs(p.x() - last.x()) + abs(p.y() - last.y()) < 1.5:
                return False
            self.pts.append(QPointF(p))
        else:
            self.pts[1:] = [QPointF(p)]
        self._path = None
        return True

    def tiny(self):
        if self.kind == "pen":
            return False            # a click is a dot
        if len(self.pts) < 2:
            return True
        d = self.pts[1] - self.pts[0]
        return abs(d.x()) + abs(d.y()) < 4

    def path(self):
        """Pen strokes are smoothed with quadratic curves through the midpoints."""
        if self._path is None:
            pts, path = self.pts, QPainterPath(self.pts[0])
            for a, b in zip(pts[1:-1], pts[2:]):
                path.quadTo(a, (a + b) / 2)
            if len(pts) > 1:
                path.lineTo(pts[-1])
            self._path = path
        return self._path

    def head(self):
        return max(10.0, self.width * 3.2)

    def bounds(self):
        m = self.width / 2 + 2
        if self.kind == "pen":
            r = self.path().controlPointRect()
        else:
            r = QRectF(self.pts[0], self.pts[-1]).normalized()
            if self.kind == "arrow":
                m += self.head()
        return r.adjusted(-m, -m, m, m)

    def paint(self, p):
        pen = QPen(self.color, self.width)
        if self.kind == "rect":
            pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(QRectF(self.pts[0], self.pts[-1]).normalized())
            return
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        if self.kind == "pen":
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            if len(self.pts) == 1:
                p.drawPoint(self.pts[0])
            else:
                p.drawPath(self.path())
            return
        # arrow: shaft up to the base of a filled head
        a, b = self.pts[0], self.pts[-1]
        d = b - a
        length = max(1e-6, (d.x() ** 2 + d.y() ** 2) ** 0.5)
        u = d / length
        n = QPointF(-u.y(), u.x())
        h = min(self.head(), length * 0.6)
        base = b - u * h
        p.setPen(pen)
        p.drawLine(a, b - u * (h * 0.6))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self.color)
        p.drawPolygon(QPolygonF([b, base + n * (h * 0.55), base - n * (h * 0.55)]))


class LiveOverlay(QWidget):
    """Transparent window over the recorded area: live pen, click ripples, key bubble.

    Drawing off: click-through, takes no focus, only shows ripples / keys / fading strokes.
    Drawing on:  left drag = pen (or rectangle, R), Shift+drag or right drag = straight arrow,
                 wheel or +/- = width, 1-8 = colour, A = arrow tool, Z = undo, C = clear,
                 Esc = drawing off.
    """

    TITLE = TITLE
    REMAP_ON_TOGGLE = False     # fallback for compositors that ignore a live input-region change

    changed = pyqtSignal()               # something visible in render_layer changed
    drawing_changed = pyqtSignal(bool)   # drawing toggled from here (Esc)
    remapped = pyqtSignal()              # window was re-created (only with REMAP_ON_TOGGLE)

    def __init__(self, size: QSize, parent=None):
        super().__init__(parent)
        self._base_flags = (Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.NoDropShadowWindowHint)
        self.setWindowFlags(self._flags(False))
        self.setWindowTitle(TITLE)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.resize(size)

        self.fade = float(settings.get("rec.draw_fade") or 0)   # 0 = strokes stay until cleared
        color = settings.get("annot.color")
        self.color = QColor(color if QColor.isValidColorName(color or "") else style.PALETTE[0])
        self.width_ = int(settings.get("annot.width") or 4)
        self.tool = "pen"                     # pen | rect | arrow
        self._drawing = False
        self.strokes = []
        self._cur = None                      # stroke being drawn
        self.ripples = []                     # [x, y, color, t0]
        self._key_items = []                  # [text, is_word, count]
        self._key_last = 0.0
        self._bubble_cache = None
        self._chip = None                     # (text, t0)
        self._layer_cache = None              # (key, QImage): finished, opaque strokes at stream size

        self._frame = QTimer(self, interval=FRAME_MS, timeout=self._tick)
        self._wake = QTimer(self, singleShot=True, timeout=self._tick)

    # -- public API --

    @property
    def drawing(self):
        return self._drawing

    def set_drawing(self, on: bool):
        on = bool(on)
        if on == self._drawing:
            return
        self._drawing = on
        if self._cur:
            self._finish_stroke()
        self._apply_flags()
        if on:
            self.setCursor(Qt.CursorShape.CrossCursor)
            self.activateWindow()             # needed for the 1-8 / R / Z / Esc keys
            self._show_chip(self._pen_text() + "   1-8 colour · R rect · A arrow · Z undo · C clear · Esc")
        else:
            self.unsetCursor()
            self._chip = None
        self.update()

    def click(self, x, y, button):
        """Ripple at area-relative logical (x, y).  Ignored while drawing (the pen shows it)."""
        if self._drawing or not (0 <= x < self.width() and 0 <= y < self.height()):
            return
        self.ripples.append([float(x), float(y), RIPPLE_COLORS.get(button, style.ACCENT), time.monotonic()])
        self._changed()

    def keys(self, text):
        """Show a key or combo in the bubble.  Single characters typed in a row merge into a word."""
        if not text or (self._drawing and self.isActiveWindow()):
            return   # keys typed into the overlay are drawing commands
        now = time.monotonic()
        items = self._key_items
        if now - self._key_last > KEYS_HOLD + FADE:
            items.clear()
        if len(text) == 1:
            if items and items[-1][1] and now - self._key_last <= KEYS_HOLD:
                items[-1][0] += text
            else:
                items.append([text, True, 1])
        elif items and not items[-1][1] and items[-1][0] == text:
            items[-1][2] += 1
        else:
            items.append([text, False, 1])
        del items[:-4]
        self._key_last = now
        self._bubble_cache = None
        self._changed()

    def undo(self):
        if self.strokes:
            self.strokes.pop()
            self._changed()

    def clear(self):
        self.strokes.clear()
        self._cur = None
        self._changed()

    def render_layer(self, w_px, h_px):
        """Current content (strokes, ripples, key bubble) at the stream resolution.

        Returns (image, x, y) — only the bounding box of what's visible, positioned at
        (x, y) in the w_px × h_px frame — or None when nothing is visible.  The image is
        QImage.Format.Format_RGBA8888, *not* premultiplied (what GdkPixbuf expects).
        The drawing border and hint chip are on-screen UI and are left out.
        """
        now = time.monotonic()
        w_px, h_px = int(w_px), int(h_px)
        box = self._content_bounds(now)
        if box is None or self.width() <= 0 or self.height() <= 0 or w_px <= 0 or h_px <= 0:
            return None
        sx, sy = w_px / self.width(), h_px / self.height()
        r = QRectF(box.x() * sx, box.y() * sy, box.width() * sx, box.height() * sy).toAlignedRect()
        r = r.intersected(QRect(0, 0, w_px, h_px))
        if r.isEmpty():
            return None
        static = self._static_layer(now, w_px, h_px, sx, sy)
        img = QImage(r.size(), QImage.Format.Format_ARGB32_Premultiplied)
        p = QPainter(img)
        if static:
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
            p.drawImage(0, 0, static[1], r.x(), r.y(), r.width(), r.height())
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        else:
            img.fill(0)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p.translate(-r.x(), -r.y())
        p.scale(sx, sy)
        self._paint_content(p, now, skip=static[0] if static else ())
        p.end()
        return img.convertToFormat(QImage.Format.Format_RGBA8888), r.x(), r.y()

    def _static_layer(self, now, w_px, h_px, sx, sy):
        """(serials, image) of the finished strokes that aren't fading, cached at stream size:
        many long strokes would otherwise be re-rasterised on every frame."""
        static = [s for s in self.strokes if s.done is not None and (self.fade <= 0 or now <= s.done + self.fade)]
        if not static:
            self._layer_cache = None
            return None
        serials = frozenset(s.serial for s in static)
        key = (w_px, h_px, self.width(), self.height(), serials)
        if self._layer_cache is None or self._layer_cache[0] != key:
            img = QImage(w_px, h_px, QImage.Format.Format_ARGB32_Premultiplied)
            img.fill(0)
            p = QPainter(img)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.scale(sx, sy)
            for s in static:
                s.paint(p)
            p.end()
            self._layer_cache = (key, img)
        return serials, self._layer_cache[1]

    # -- input transparency --

    def _flags(self, drawing):
        if drawing:
            return self._base_flags
        return (self._base_flags | Qt.WindowType.WindowTransparentForInput
                | Qt.WindowType.WindowDoesNotAcceptFocus)

    def _apply_flags(self):
        """Switch click-through without re-creating the window.

        QWidget.setWindowFlags() would hide the window and map a new one, which KWin's
        placement script wouldn't adopt again.  Instead tell QWidget the new flags
        (overrideWindowFlags) and change them on the live QWindow; QtWayland then just
        sets a new (empty or full) input region on the same surface.
        """
        flags = self._flags(self._drawing)
        self.overrideWindowFlags(flags)
        wh = self.windowHandle()
        if wh is None:
            return              # not shown yet: the flags apply when it is
        wh.setFlags(flags)
        if self.REMAP_ON_TOGGLE and self.isVisible():
            self.hide()
            self.show()
            self.remapped.emit()   # the caller must kwin.place() again

    # -- animation --

    def _changed(self):
        self.update()
        self.changed.emit()
        self._schedule(time.monotonic())

    def _tick(self):
        now = time.monotonic()
        self._prune(now)
        self.update()
        self.changed.emit()
        self._schedule(now)

    def _prune(self, now):
        if self.fade > 0:
            self.strokes = [s for s in self.strokes if s.done is None or now < s.done + self.fade + FADE]
        self.ripples = [r for r in self.ripples if now < r[3] + RIPPLE]
        if self._key_items and now >= self._key_last + KEYS_HOLD + FADE:
            self._key_items.clear()
            self._bubble_cache = None
        if self._chip and now >= self._chip[1] + CHIP_HOLD + FADE:
            self._chip = None

    def _schedule(self, now):
        """Run the 60 fps timer only while something moves; otherwise sleep until the
        next fade starts (or forever)."""
        starts = []   # times at which a fade begins
        if self.fade > 0:
            starts += [s.done + self.fade for s in self.strokes if s.done is not None]
        if self._key_items:
            starts.append(self._key_last + KEYS_HOLD)
        if self._chip:
            starts.append(self._chip[1] + CHIP_HOLD)
        animating = bool(self.ripples) or any(t <= now for t in starts)
        if animating:
            self._wake.stop()
            if not self._frame.isActive():
                self._frame.start()
            return
        self._frame.stop()
        if starts:
            self._wake.start(max(1, int((min(starts) - now) * 1000) + 1))
        else:
            self._wake.stop()

    # -- painting --

    def _content_bounds(self, now):
        box = QRectF()
        for s in self.strokes + ([self._cur] if self._cur else []):
            box = box.united(s.bounds())
        for x, y, _, t0 in self.ripples:
            m = self._ripple_radius(1.0) + 3
            box = box.united(QRectF(x - m, y - m, 2 * m, 2 * m))
        if self._key_items:
            box = box.united(self._bubble_layout()[0].adjusted(-1, -1, 1, 1))
        return None if box.isEmpty() else box

    def _paint_content(self, p, now, skip=()):
        for s in self.strokes:
            if s.serial in skip:
                continue
            a = 1.0 if self.fade <= 0 or s.done is None else _fade(now, s.done + self.fade)
            if a > 0:
                p.setOpacity(a)
                s.paint(p)
        if self._cur:
            p.setOpacity(1.0)
            self._cur.paint(p)
        for x, y, color, t0 in self.ripples:
            self._paint_ripple(p, x, y, color, min(1.0, max(0.0, (now - t0) / RIPPLE)))
        if self._key_items:
            a = _fade(now, self._key_last + KEYS_HOLD)
            if a > 0:
                p.setOpacity(a)
                self._paint_bubble(p)
        p.setOpacity(1.0)

    @staticmethod
    def _ripple_radius(t):
        return 5 + 25 * (1 - (1 - t) ** 3)      # ease-out

    def _paint_ripple(self, p, x, y, color, t):
        r, a = self._ripple_radius(t), 1.0 - t
        c = QColor(color)
        p.setOpacity(a * 0.28)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        p.drawEllipse(QPointF(x, y), r * 0.55, r * 0.55)
        p.setOpacity(a * 0.45)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(0, 0, 0), 5))       # dark halo: visible on light backgrounds too
        p.drawEllipse(QPointF(x, y), r, r)
        p.setOpacity(a)
        p.setPen(QPen(c, 3))
        p.drawEllipse(QPointF(x, y), r, r)
        p.setOpacity(1.0)

    def _bubble_font(self):
        f = QFont(self.font())
        f.setBold(True)
        f.setPixelSize(int(min(30, max(13, min(self.width(), self.height()) * 0.035))))
        return f

    def _bubble_layout(self):
        """(panel rect, [(kind, rect, text)]) in logical coords; kind: cap | plus | count."""
        key = (tuple((t, w, c) for t, w, c in self._key_items), self.width(), self.height())
        if self._bubble_cache and self._bubble_cache[0] == key:
            return self._bubble_cache[1]
        font = self._bubble_font()
        fm = QFontMetricsF(font)
        fs = font.pixelSize()
        pad, cap_px, cap_h, gap = fs * 0.5, fs * 0.55, fs * 1.8, fs * 0.6
        max_w = self.width() * 0.92 - 2 * pad

        def item_parts(text, is_word, count):
            if is_word:
                text = text if len(text) <= 40 else "…" + text[-39:]
                caps = [text if text.strip() else "Space"]
            else:
                caps = text.split(" + ")
            parts = []
            for i, c in enumerate(caps):
                if i:
                    parts.append(("plus", fm.horizontalAdvance("+") + fs * 0.5, "+"))
                parts.append(("cap", fm.horizontalAdvance(c) + 2 * cap_px, c))
            if count > 1:
                parts.append(("count", fm.horizontalAdvance(f"×{count}") + fs * 0.4, f"×{count}"))
            return parts

        items = [item_parts(*it) for it in self._key_items]
        width = lambda its: sum(w for parts in its for _, w, _ in parts) + gap * (len(its) - 1)
        while len(items) > 1 and width(items) > max_w:
            items.pop(0)                    # oldest first
        if width(items) > max_w:            # one long word: keep its end
            kind, w, text = items[0][0]
            text = fm.elidedText(text, Qt.TextElideMode.ElideLeft, max_w - 2 * cap_px)
            items = [[(kind, fm.horizontalAdvance(text) + 2 * cap_px, text)] + items[0][1:]]
        total = width(items)
        panel = QRectF(0, 0, total + 2 * pad, cap_h + 2 * pad)
        panel.moveCenter(QPointF(self.width() / 2, 0))
        panel.moveBottom(self.height() - max(12.0, self.height() * 0.06))
        out, x = [], panel.x() + pad
        for i, parts in enumerate(items):
            if i:
                x += gap
            for kind, w, text in parts:
                out.append((kind, QRectF(x, panel.y() + pad, w, cap_h), text))
                x += w
        result = (panel, out)
        self._bubble_cache = (key, result)
        return result

    def _paint_bubble(self, p):
        panel, parts = self._bubble_layout()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(style.PANEL_BG)
        p.drawRect(panel)
        p.setFont(self._bubble_font())
        edge = max(2.0, panel.height() * 0.05)
        for kind, r, text in parts:
            if kind == "cap":
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(255, 255, 255, 34))
                p.drawRect(r)
                p.setBrush(QColor(255, 255, 255, 22))      # key-cap bottom edge
                p.drawRect(QRectF(r.x(), r.bottom() - edge, r.width(), edge))
                p.setPen(style.TEXT)
                p.drawText(r.adjusted(0, 0, 0, -edge / 2), Qt.AlignmentFlag.AlignCenter, text)
            else:
                p.setPen(style.MUTED)
                p.drawText(r, Qt.AlignmentFlag.AlignCenter, text)

    def _pen_text(self):
        return {"pen": "Pen", "rect": "Rectangle", "arrow": "Arrow"}[self.tool] + f" · {self.width_} px"

    def _show_chip(self, text):
        self._chip = (text, time.monotonic())
        self.update()
        self._schedule(time.monotonic())

    def _paint_ui(self, p, now):
        """On-screen only: accent border while drawing, and the pen/hint chip."""
        if self._drawing:
            p.setPen(QPen(style.ACCENT, 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(QRectF(self.rect()).adjusted(1, 1, -1, -1))
        if self._chip:
            a = _fade(now, self._chip[1] + CHIP_HOLD)
            if a <= 0:
                return
            p.setOpacity(a)
            f = QFont(self.font())
            f.setPixelSize(12)
            p.setFont(f)
            fm = QFontMetricsF(f)
            text = fm.elidedText(self._chip[0], Qt.TextElideMode.ElideRight, max(40, self.width() - 52))
            r = QRectF(8, 8, fm.horizontalAdvance(text) + 36, 26)
            p.fillRect(r, style.PANEL_BG)
            p.fillRect(QRectF(r.x() + 8, r.y() + 7, 12, 12), self.color)
            p.setPen(style.TEXT)
            p.drawText(r.adjusted(28, 0, -8, 0), Qt.AlignmentFlag.AlignVCenter, text)
            p.setOpacity(1.0)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        p.fillRect(self.rect(), Qt.GlobalColor.transparent)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        now = time.monotonic()
        self._paint_content(p, now)
        self._paint_ui(p, now)

    # -- drawing input --

    def mousePressEvent(self, ev):
        if not self._drawing:
            return ev.ignore()
        b = ev.button()
        if b not in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton):
            return
        if self._cur:
            self._finish_stroke()
        shift = ev.modifiers() & Qt.KeyboardModifier.ShiftModifier
        kind = "arrow" if (b == Qt.MouseButton.RightButton or shift) else self.tool
        self._cur = _Stroke(kind, self.color, self.width_, ev.position())
        if kind == "arrow" or kind == "rect":
            self._cur.add(ev.position())
        self._changed()

    def mouseMoveEvent(self, ev):
        if self._cur and self._cur.add(ev.position()):
            self._changed()

    def mouseReleaseEvent(self, ev):
        if self._cur:
            self._cur.add(ev.position())
            self._finish_stroke()

    def _finish_stroke(self):
        s, self._cur = self._cur, None
        if not s.tiny():
            s.done = time.monotonic()
            self.strokes.append(s)
        self._changed()

    def wheelEvent(self, ev):
        if not self._drawing:
            return ev.ignore()
        step = 1 if ev.angleDelta().y() > 0 else -1 if ev.angleDelta().y() < 0 else 0
        if step:
            self._step_width(step)

    def _step_width(self, step):
        i = min(range(len(WIDTHS)), key=lambda k: abs(WIDTHS[k] - self.width_))
        self.width_ = WIDTHS[max(0, min(len(WIDTHS) - 1, i + step))]
        self._show_chip(self._pen_text())

    def keyPressEvent(self, ev):
        if not self._drawing:
            return ev.ignore()
        k, text = ev.key(), ev.text().lower()
        if k == Qt.Key.Key_Escape:
            self.set_drawing(False)
            self.drawing_changed.emit(False)
        elif text and text in "12345678":
            self.color = QColor(style.PALETTE[int(text) - 1])
            self._show_chip(self._pen_text())
        elif k == Qt.Key.Key_R:
            self.tool = "pen" if self.tool == "rect" else "rect"
            self._show_chip(self._pen_text())
        elif k == Qt.Key.Key_A:
            self.tool = "pen" if self.tool == "arrow" else "arrow"
            self._show_chip(self._pen_text())
        elif k == Qt.Key.Key_Z:
            self.undo()
        elif k == Qt.Key.Key_C:
            self.clear()
        elif k in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self._step_width(1)
        elif k in (Qt.Key.Key_Minus, Qt.Key.Key_Underscore):
            self._step_width(-1)
        else:
            ev.ignore()

    def showEvent(self, ev):
        super().showEvent(ev)
        self._apply_flags()


# ---------------------------------------------------------------- input devices

# Readable names for keys that don't type a character (xkb keysym names → label)
_SYM_NAMES = {
    "Return": "Enter", "KP_Enter": "Enter", "BackSpace": "Backspace", "Escape": "Esc",
    "Tab": "Tab", "ISO_Left_Tab": "Tab", "Delete": "Delete", "KP_Delete": "Delete",
    "Insert": "Insert", "KP_Insert": "Insert", "Home": "Home", "KP_Home": "Home",
    "End": "End", "KP_End": "End", "Prior": "PgUp", "KP_Prior": "PgUp", "Next": "PgDn",
    "KP_Next": "PgDn", "Left": "←", "Right": "→", "Up": "↑", "Down": "↓", "KP_Left": "←",
    "KP_Right": "→", "KP_Up": "↑", "KP_Down": "↓", "space": "Space", "Print": "Print",
    "Menu": "Menu", "Pause": "Pause", "Caps_Lock": "CapsLock", "Num_Lock": "NumLock",
    "Scroll_Lock": "ScrollLock",
}

_CTRL = {29, 97}          # KEY_LEFTCTRL, KEY_RIGHTCTRL
_SHIFT = {42, 54}         # KEY_LEFTSHIFT, KEY_RIGHTSHIFT
_ALT = {56}               # KEY_LEFTALT
_RALT = 100               # KEY_RIGHTALT: AltGr on most non-US layouts
_SUPER = {125, 126}       # KEY_LEFTMETA, KEY_RIGHTMETA
_MODIFIERS = _CTRL | _SHIFT | _ALT | _SUPER | {_RALT, 58}   # + KEY_CAPSLOCK


class _RuleNames(ctypes.Structure):
    _fields_ = [(n, ctypes.c_char_p) for n in ("rules", "model", "layout", "variant", "options")]


class _Xkb:
    """Just enough libxkbcommon (via ctypes) to turn evdev key codes into what the
    user's keyboard layout types.  Layouts come from KDE's kxkbrc; the active one is
    set from org.kde.KeyboardLayouts."""

    def __init__(self):
        lib = ctypes.CDLL(ctypes.util.find_library("xkbcommon") or "libxkbcommon.so.0")
        P, U, I = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int
        for name, res, args in (
                ("xkb_context_new", P, [I]),
                ("xkb_keymap_new_from_names", P, [P, ctypes.POINTER(_RuleNames), I]),
                ("xkb_state_new", P, [P]),
                ("xkb_state_update_key", I, [P, U, I]),
                ("xkb_state_update_mask", I, [P, U, U, U, U, U, U]),
                ("xkb_state_serialize_mods", U, [P, I]),
                ("xkb_state_key_get_utf8", I, [P, U, ctypes.c_char_p, ctypes.c_size_t]),
                ("xkb_state_key_get_layout", U, [P, U]),
                ("xkb_keymap_key_get_syms_by_level", I, [P, U, U, U, ctypes.POINTER(ctypes.POINTER(U))]),
                ("xkb_keymap_mod_get_index", U, [P, ctypes.c_char_p]),
                ("xkb_keysym_get_name", I, [U, ctypes.c_char_p, ctypes.c_size_t]),
                ("xkb_keysym_to_utf32", U, [U]),
                ("xkb_keymap_unref", None, [P]), ("xkb_state_unref", None, [P]),
                ("xkb_context_unref", None, [P])):
            fn = getattr(lib, name)
            fn.restype, fn.argtypes = res, args
        self.lib = lib
        self.ctx = lib.xkb_context_new(0)
        names = _RuleNames(*[v.encode() if v else None for v in _kde_layout_names()])
        self.keymap = lib.xkb_keymap_new_from_names(self.ctx, ctypes.byref(names), 0)
        if not self.keymap:
            self.keymap = lib.xkb_keymap_new_from_names(self.ctx, None, 0)
        if not self.keymap:
            raise OSError("no xkb keymap")
        self.state = lib.xkb_state_new(self.keymap)
        self.layout = 0
        self._buf = ctypes.create_string_buffer(64)

    def close(self):
        self.lib.xkb_state_unref(self.state)
        self.lib.xkb_keymap_unref(self.keymap)
        self.lib.xkb_context_unref(self.ctx)

    def update(self, kc, down):
        self.lib.xkb_state_update_key(self.state, kc, 1 if down else 0)

    def set_locks(self, layout=None, caps=None, num=None):
        lib, st = self.lib, self.state
        dep, lat, lock = (lib.xkb_state_serialize_mods(st, c) for c in (1, 2, 4))
        for name, on in ((b"Lock", caps), (b"Mod2", num)):
            idx = lib.xkb_keymap_mod_get_index(self.keymap, name)
            if on is not None and idx < 32:
                lock = (lock | 1 << idx) if on else (lock & ~(1 << idx))
        if layout is not None:
            self.layout = layout
        lib.xkb_state_update_mask(st, dep, lat, lock, 0, 0, self.layout)

    def text(self, kc):
        n = self.lib.xkb_state_key_get_utf8(self.state, kc, self._buf, 64)
        return self._buf.value[:n].decode(errors="ignore") if n > 0 else ""

    def base_sym(self, kc):
        """Unshifted keysym of the key in the active layout (combos show this)."""
        syms = ctypes.POINTER(ctypes.c_uint32)()
        layout = self.lib.xkb_state_key_get_layout(self.state, kc)
        n = self.lib.xkb_keymap_key_get_syms_by_level(self.keymap, kc, layout, 0, ctypes.byref(syms))
        return syms[0] if n > 0 else 0

    def sym_name(self, sym):
        n = self.lib.xkb_keysym_get_name(sym, self._buf, 64)
        return self._buf.value.decode() if n > 0 else ""

    def sym_char(self, sym):
        u = self.lib.xkb_keysym_to_utf32(sym)
        return chr(u) if u else ""


def _kde_layout_names():
    """(rules, model, layout, variant, options) from ~/.config/kxkbrc, Nones when unset."""
    import configparser
    cfg = configparser.RawConfigParser(strict=False, interpolation=None)
    try:
        cfg.read(os.path.join(os.path.dirname(settings.CONFIG_DIR), "kxkbrc"))
        sec = cfg["Layout"]
    except (configparser.Error, KeyError, OSError):
        return (None,) * 5
    if sec.get("Use", "true").lower() != "true" or not sec.get("LayoutList"):
        return (None,) * 5
    return ("evdev", sec.get("Model") or None, sec.get("LayoutList"),
            sec.get("VariantList") or None, sec.get("Options") or None)


class InputWatcher(QObject):
    """Clicks and key presses from all keyboards and pointing devices.

    Privacy: this reads raw input devices (/dev/input/event*, needs the `input` group)
    while started — every key typed anywhere, passwords included.  Nothing is stored or
    logged; presses become labels for the overlay and are dropped.  Devices are opened
    read-only and never grabbed.

    One background thread select()s over the devices and rescans /dev/input every few
    seconds (hotplug).  Signals are emitted from that thread; Qt queues them to the
    receivers on the GUI thread.

    Key labels: plain typing gives single characters as the active KDE keyboard layout
    types them (via libxkbcommon; Shift, AltGr and Caps Lock respected); with Ctrl, Alt
    or Super held a combo like "Ctrl + Shift + T"; non-text keys get names ("Enter",
    "Backspace", "←", "F5" …).  Modifiers alone, key repeats and dead keys emit nothing.
    """

    clicked = pyqtSignal(int)     # 1 left, 2 middle, 3 right
    keys = pyqtSignal(str)

    RESCAN = 3.0                  # s between /dev/input rescans
    _BUTTONS = {0x110: 1, 0x112: 2, 0x111: 3}   # BTN_LEFT, BTN_MIDDLE, BTN_RIGHT

    def __init__(self, clicks=True, keys=True, parent=None):
        super().__init__(parent)
        self._want_clicks, self._want_keys = clicks, keys
        self._thread = None
        self._stop = threading.Event()
        self._wake = None
        self._xkb = None
        self._layout = None          # set from D-Bus (GUI thread), applied by the reader
        self._held = set()
        self._altgr = True

    def start(self):
        if self._thread:
            return
        import evdev  # noqa: F401 - fail early, in the caller's thread
        if self._want_keys:
            try:
                self._xkb = _Xkb()
                self._altgr = self._xkb.sym_name(self._xkb.base_sym(_RALT + 8)) == "ISO_Level3_Shift"
            except OSError:
                self._xkb = None     # fall back to US key positions
            self._watch_layout(True)
        self._stop.clear()
        self._wake = os.pipe()
        self._thread = threading.Thread(target=self._run, name="screenrec-input", daemon=True)
        self._thread.start()

    def stop(self):
        if not self._thread:
            return
        self._stop.set()
        os.write(self._wake[1], b"x")
        self._thread.join(2.0)
        self._thread = None
        for fd in self._wake:
            os.close(fd)
        self._wake = None
        if self._want_keys:
            self._watch_layout(False)
        if self._xkb:
            self._xkb.close()
            self._xkb = None
        self._held.clear()

    # -- KDE keyboard layout (GUI thread) --

    def _watch_layout(self, on):
        bus = QDBusConnection.sessionBus()
        args = ("org.kde.keyboard", "/Layouts", "org.kde.KeyboardLayouts", "layoutChanged", self._on_layout)
        if not on:
            bus.disconnect(*args)
            return
        bus.connect(*args)
        reply = bus.call(QDBusMessage.createMethodCall(*args[:3], "getLayout"), timeout=500)
        if reply.type() == QDBusMessage.MessageType.ReplyMessage and reply.arguments():
            self._layout = int(reply.arguments()[0])

    @pyqtSlot(QDBusMessage)
    def _on_layout(self, msg):
        if msg.arguments():
            self._layout = int(msg.arguments()[0])

    # -- reader thread --

    def _run(self):
        import evdev
        devs = {}            # fd -> (InputDevice, keyboard, pointer)
        seen = {}            # path -> device identity (st_rdev, st_ctime) already looked at
        next_scan = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if now >= next_scan:
                self._scan(evdev, devs, seen)
                next_scan = now + self.RESCAN
            try:
                ready, _, _ = select.select([self._wake[0], *devs], [], [], max(0.05, next_scan - now))
            except (OSError, ValueError):
                ready = [fd for fd in devs if not _alive(devs[fd][0])]
            for fd in ready:
                if fd == self._wake[0] or fd not in devs:
                    continue
                dev, kbd, ptr = devs[fd]
                try:
                    for ev in dev.read():
                        if ev.type == 1:            # EV_KEY
                            self._key(ev.code, ev.value, kbd, ptr)
                except BlockingIOError:
                    pass
                except OSError:                     # unplugged
                    del devs[fd]
                    seen.pop(dev.path, None)
                    _close(dev)
        for dev, _, _ in devs.values():
            _close(dev)

    def _scan(self, evdev, devs, seen):
        open_paths = {d.path for d, _, _ in devs.values()}
        for path in evdev.list_devices():
            try:
                st = os.stat(path)
            except OSError:
                continue
            ident = (st.st_rdev, st.st_ctime_ns)
            if path in open_paths or seen.get(path) == ident:
                continue
            seen[path] = ident
            try:
                dev = evdev.InputDevice(path)
                codes = set(dev.capabilities().get(1, []))
            except OSError:
                continue
            kbd = self._want_keys and {30, 44, 57, 28} <= codes     # KEY_A, KEY_Z, SPACE, ENTER
            ptr = self._want_clicks and 0x110 in codes              # BTN_LEFT
            if not (kbd or ptr):
                _close(dev)
                continue
            if kbd and self._xkb:
                try:
                    leds = set(dev.leds())
                    self._xkb.set_locks(caps=1 in leds, num=0 in leds)   # LED_CAPSL, LED_NUML
                except OSError:
                    pass
            devs[dev.fd] = (dev, kbd, ptr)

    def _key(self, code, value, kbd, ptr):
        if ptr and code in self._BUTTONS:
            if value == 1:
                self.clicked.emit(self._BUTTONS[code])
            return
        if not kbd or value == 2 or code >= 0x100:
            return
        down = value == 1
        xkb = self._xkb
        if xkb and self._layout is not None and self._layout != xkb.layout:
            xkb.set_locks(layout=self._layout)
        if down and code not in _MODIFIERS:
            label = self._label(code)
            if label:
                self.keys.emit(label)
        if xkb:
            xkb.update(code + 8, down)
        if code in _MODIFIERS:
            (self._held.add if down else self._held.discard)(code)

    def _label(self, code):
        held = self._held
        ctrl, shift, sup = bool(held & _CTRL), bool(held & _SHIFT), bool(held & _SUPER)
        alt = bool(held & _ALT) or (_RALT in held and not self._altgr)
        if ctrl or alt or sup:
            mods = [n for n, on in (("Ctrl", ctrl), ("Alt", alt), ("Shift", shift), ("Super", sup)) if on]
            base = self._base_name(code)
            return " + ".join(mods + [base]) if base else None
        text = self._xkb.text(code + 8) if self._xkb else _us_text(code, shift)
        if text and text.isprintable():
            return text
        base = self._base_name(code)
        if not base:
            return None
        return f"Shift + {base}" if shift else base

    def _base_name(self, code):
        if not self._xkb:
            return _us_name(code)
        sym = self._xkb.base_sym(code + 8)
        name = self._xkb.sym_name(sym)
        if not name or name.startswith("dead_"):
            return None
        if name in _SYM_NAMES:
            return _SYM_NAMES[name]
        ch = self._xkb.sym_char(sym)
        if ch and ch.isprintable() and not ch.isspace():
            return ch.upper()
        return name.removeprefix("XF86").replace("_", " ")


def _alive(dev):
    try:
        os.fstat(dev.fd)
        return True
    except OSError:
        return False


def _close(dev):
    try:
        dev.close()
    except OSError:
        pass


# Fallback without libxkbcommon: US key positions, Shift only changes letter case.
def _us_text(code, shift):
    import evdev
    name = evdev.ecodes.KEY.get(code)
    name = name[0] if isinstance(name, list) else name or ""
    key = name.removeprefix("KEY_")
    if len(key) == 1 and key.isalpha():
        return key if shift else key.lower()
    if shift:
        return ""
    if len(key) == 1:
        return key
    return {"SPACE": " ", "MINUS": "-", "EQUAL": "=", "COMMA": ",", "DOT": ".", "SLASH": "/",
            "SEMICOLON": ";", "APOSTROPHE": "'", "GRAVE": "`", "BACKSLASH": "\\",
            "LEFTBRACE": "[", "RIGHTBRACE": "]"}.get(key, "")


def _us_name(code):
    import evdev
    name = evdev.ecodes.KEY.get(code)
    name = name[0] if isinstance(name, list) else name or ""
    key = name.removeprefix("KEY_")
    named = {"ENTER": "Enter", "KPENTER": "Enter", "BACKSPACE": "Backspace", "ESC": "Esc",
             "TAB": "Tab", "DELETE": "Delete", "INSERT": "Insert", "HOME": "Home", "END": "End",
             "PAGEUP": "PgUp", "PAGEDOWN": "PgDn", "LEFT": "←", "RIGHT": "→", "UP": "↑",
             "DOWN": "↓", "SPACE": "Space", "SYSRQ": "Print", "COMPOSE": "Menu"}
    if key in named:
        return named[key]
    t = _us_text(code, False)
    return t.upper() if t else key.title() if key else None
