"""The video editor: play a video, cut parts out anywhere, crop it (by hand, to an aspect ratio,
or by removing black bars), then export through the convert window.

Playback is GStreamer's playbin (a dependency anyway; QtMultimedia isn't) with frames scaled
down to a preview size in an appsink, so 4K files stay smooth.  Frames arrive on a streaming
thread and reach the GUI as a queued signal.  Paused seeks are frame-exact (the preroll frame
is shown); while dragging on the timeline they snap to keyframes to keep up.

The edit itself lives in videocut.VideoCut (pieces kept/removed + crop, undoable); playing
skips removed pieces, so what you see is what the export makes.
"""

import os

from PyQt6.QtCore import QObject, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QBrush, QColor, QIcon, QImage, QKeySequence, QPainter, QPen, QPolygonF
from PyQt6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QLayout, QMainWindow, QPushButton, QSizePolicy,
                             QSlider, QToolButton, QVBoxLayout, QWidget)

from . import convert, settings, style
from .app_util import log
from .videocut import MIN_PIECE, VideoCut

PREVIEW_MAX = 1280         # decode the preview at most this big (long side)
ASPECTS = [("Free", None), ("16:9", 16 / 9), ("9:16", 9 / 16), ("1:1", 1.0), ("4:3", 4 / 3), ("4:5", 4 / 5),
           ("21:9", 21 / 9)]


def fmt_time(t, precise=True):
    t = max(0.0, t)
    m, s = divmod(t, 60)
    h, m = divmod(int(m), 60)
    sec = f"{s:04.1f}" if precise else f"{int(s):02d}"
    return f"{h}:{m:02d}:{sec}" if h else f"{m}:{sec}"


def preview_size(w, h):
    k = min(1.0, PREVIEW_MAX / max(w, h))
    return max(2, int(w * k) // 2 * 2), max(2, int(h * k) // 2 * 2)


class Player(QObject):
    """playbin → RGBA QImages.  position()/duration in seconds."""
    frame = pyqtSignal(QImage)
    ended = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, path, size, parent=None):
        super().__init__(parent)
        from .cast import Gst
        self.Gst = Gst
        self.playing = False
        self.pipe = Gst.ElementFactory.make("playbin", None)
        if self.pipe is None:
            raise RuntimeError("GStreamer's playbin is missing (gst-plugins-base).")
        self.pipe.set_property("uri", Gst.filename_to_uri(path))
        w, h = size
        sink = Gst.parse_bin_from_description(
            f"videoconvert ! videoscale ! video/x-raw,format=RGBA,width={w},height={h},pixel-aspect-ratio=1/1"
            " ! appsink name=sink emit-signals=true max-buffers=2 drop=true", True)
        app = sink.get_by_name("sink")
        app.connect("new-sample", lambda s: self._pull(s.emit("pull-sample")))
        app.connect("new-preroll", lambda s: self._pull(s.emit("pull-preroll")))
        self.pipe.set_property("video-sink", sink)
        # Mute and the slider work on our own volume element.  playbin's volume and mute would set
        # the system stream's (even with its soft-volume flag), and WirePlumber remembers those per
        # app name: a preview muted once would come back muted.  The stream gets its own name for
        # the same reason; a plain "python3" shares that memory with every Python program.
        self.pipe.set_property("flags", int(self.pipe.get_property("flags")) & ~0x4 | 0x10)  # no subs, soft vol
        has_pulse = Gst.ElementFactory.find("pulsesink") is not None    # PipeWire serves the Pulse API too
        audio = Gst.parse_bin_from_description(
            "volume name=vol ! audioconvert ! audioresample ! "
            + ("pulsesink name=out" if has_pulse else "autoaudiosink"), True)
        self.vol = audio.get_by_name("vol")
        if has_pulse:
            audio.get_by_name("out").set_property("stream-properties", Gst.Structure.new_from_string(
                'props, application.name="Snappie video editor", application.icon_name=snappie'))
        self.pipe.set_property("audio-sink", audio)
        bus = self.pipe.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._message)
        self.pipe.set_state(Gst.State.PAUSED)        # prerolls: the first frame shows up

    def _pull(self, sample):      # streaming thread
        Gst = self.Gst
        if sample is None:
            return Gst.FlowReturn.OK
        s = sample.get_caps().get_structure(0)
        w, h = s.get_value("width"), s.get_value("height")
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if ok:
            try:
                img = QImage(bytes(info.data), w, h, w * 4, QImage.Format.Format_RGBA8888).copy()
            finally:
                buf.unmap(info)
            self.frame.emit(img)
        return Gst.FlowReturn.OK

    def _message(self, bus, msg):
        Gst = self.Gst
        if msg.type == Gst.MessageType.EOS:
            self.pause()
            self.ended.emit()
        elif msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            log("video editor playback:", err.message, dbg or "")
            self.error.emit(err.message)

    def position(self):
        ok, ns = self.pipe.query_position(self.Gst.Format.TIME)
        return ns / 1e9 if ok else None

    def play(self):
        self.playing = True
        self.pipe.set_state(self.Gst.State.PLAYING)

    def pause(self):
        self.playing = False
        self.pipe.set_state(self.Gst.State.PAUSED)

    def seek(self, t, exact=True, stop=None):
        """With a stop, playing ends there (EOS) — frame-exact, unlike watching the position."""
        Gst = self.Gst
        flags = Gst.SeekFlags.FLUSH | (Gst.SeekFlags.ACCURATE if exact else Gst.SeekFlags.KEY_UNIT)
        self.pipe.seek(1.0, Gst.Format.TIME, flags, Gst.SeekType.SET, max(0, int(t * 1e9)),
                       Gst.SeekType.SET, -1 if stop is None else int(stop * 1e9))

    def set_muted(self, on):
        self.vol.set_property("mute", on)

    def set_volume(self, percent):
        # cubic, like desktop volume sliders: halfway sounds about half as loud
        self.vol.set_property("volume", (max(0, min(100, percent)) / 100) ** 3)

    def close(self):
        if self.pipe is not None:
            self.pipe.get_bus().remove_signal_watch()
            self.pipe.set_state(self.Gst.State.NULL)
            self.pipe = None


class VideoView(QWidget):
    """The current frame, fitted.  In crop mode the whole frame shows with the crop box over it;
    otherwise only the cropped part shows, as it will look in the export."""
    crop_changed = pyqtSignal(object)      # (x, y, w, h) in video pixels, on release

    HANDLE = 7

    def __init__(self, cut):
        super().__init__()
        self.cut = cut
        self.image = None
        self.cropping = False
        self.aspect = None
        self._drag = None         # (mode, press point in video px, box at press)
        self._box = None          # the box while dragging
        self.setMinimumSize(320, 180)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)

    def sizeHint(self):
        return QSize(960, 540)

    def set_frame(self, img):
        self.image = img
        self.update()

    def _shown(self):
        """The part of the video on screen, in video pixels."""
        if self.cut.crop and not self.cropping:
            return self.cut.crop
        return (0, 0, self.cut.width, self.cut.height)

    def _fit(self):
        _, _, W, H = self._shown()
        k = min((self.width() - 16) / W, (self.height() - 16) / H)
        w, h = W * k, H * k
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h), k

    def _to_video(self, pos):
        r, k = self._fit()
        return (pos.x() - r.x()) / k, (pos.y() - r.y()) / k

    def _to_view(self, box):
        r, k = self._fit()
        x, y, w, h = box
        return QRectF(r.x() + x * k, r.y() + y * k, w * k, h * k)

    def box(self):
        return self._box or self.cut.crop

    def paintEvent(self, ev):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#16181b"))
        r, _ = self._fit()
        if self.image is not None:
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            x, y, w, h = self._shown()
            k = self.image.width() / self.cut.width        # frames are scaled to the preview size
            p.drawImage(r, self.image, QRectF(x * k, y * k, w * k, h * k))
        box = self.box()
        if box and self.cropping:
            b = self._to_view(box)
            shade = QColor(0, 0, 0, 150)
            for part in (QRectF(r.left(), r.top(), r.width(), b.top() - r.top()),
                         QRectF(r.left(), b.bottom(), r.width(), r.bottom() - b.bottom()),
                         QRectF(r.left(), b.top(), b.left() - r.left(), b.height()),
                         QRectF(b.right(), b.top(), r.right() - b.right(), b.height())):
                p.fillRect(part, shade)
        if self.cropping:
            b = self._to_view(box or (0, 0, self.cut.width, self.cut.height))
            p.setPen(QPen(style.ACCENT, 2))
            p.drawRect(b)
            p.setPen(QPen(QColor(255, 255, 255, 90), 1, Qt.PenStyle.DashLine))
            for i in (1, 2):    # rule of thirds
                x, y = b.left() + b.width() * i / 3, b.top() + b.height() * i / 3
                p.drawLine(QPointF(x, b.top()), QPointF(x, b.bottom()))
                p.drawLine(QPointF(b.left(), y), QPointF(b.right(), y))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("white"))
            for pt in self._handles(b).values():
                p.drawRect(QRectF(pt.x() - self.HANDLE / 2, pt.y() - self.HANDLE / 2, self.HANDLE, self.HANDLE))
            bx = box or (0, 0, self.cut.width, self.cut.height)
            text = f"{bx[2]} × {bx[3]}"
            p.setPen(QColor("white"))
            fm = p.fontMetrics()
            tr = QRectF(b.left() + 6, b.top() + 6, fm.horizontalAdvance(text) + 12, fm.height() + 6)
            p.fillRect(tr, QColor(0, 0, 0, 170))
            p.drawText(tr, Qt.AlignmentFlag.AlignCenter, text)
        p.end()

    @staticmethod
    def _handles(b):
        cx, cy = b.center().x(), b.center().y()
        return {"tl": b.topLeft(), "t": QPointF(cx, b.top()), "tr": b.topRight(),
                "r": QPointF(b.right(), cy), "br": b.bottomRight(), "b": QPointF(cx, b.bottom()),
                "bl": b.bottomLeft(), "l": QPointF(b.left(), cy)}

    def _hit(self, pos):
        box = self.box() or (0, 0, self.cut.width, self.cut.height)
        b = self._to_view(box)
        for name, pt in self._handles(b).items():
            if abs(pt.x() - pos.x()) <= self.HANDLE + 3 and abs(pt.y() - pos.y()) <= self.HANDLE + 3:
                return name
        return "move" if b.contains(pos) and self.box() else "new"

    CURSORS = {"tl": Qt.CursorShape.SizeFDiagCursor, "br": Qt.CursorShape.SizeFDiagCursor,
               "tr": Qt.CursorShape.SizeBDiagCursor, "bl": Qt.CursorShape.SizeBDiagCursor,
               "t": Qt.CursorShape.SizeVerCursor, "b": Qt.CursorShape.SizeVerCursor,
               "l": Qt.CursorShape.SizeHorCursor, "r": Qt.CursorShape.SizeHorCursor,
               "move": Qt.CursorShape.SizeAllCursor, "new": Qt.CursorShape.CrossCursor}

    def mousePressEvent(self, ev):
        if not self.cropping or ev.button() != Qt.MouseButton.LeftButton:
            return
        pos = ev.position()
        mode = self._hit(pos)
        self._drag = (mode, self._to_video(pos), self.box() or (0, 0, self.cut.width, self.cut.height))

    def mouseMoveEvent(self, ev):
        pos = ev.position()
        if not self._drag:
            if self.cropping:
                self.setCursor(self.CURSORS[self._hit(pos)])
            return
        mode, (px, py), (x, y, w, h) = self._drag
        vx, vy = self._to_video(pos)
        dx, dy = vx - px, vy - py
        if mode == "move":
            box = (x + dx, y + dy, w, h)
        elif mode == "new":
            x0, y0 = min(px, vx), min(py, vy)
            bw, bh = abs(vx - px), abs(vy - py)
            if self.aspect:
                bh = bw / self.aspect
                if vy < py:
                    y0 = py - bh
            box = (x0, y0, bw, bh)
        else:
            l, t, r, b = x, y, x + w, y + h
            if "l" in mode:
                l = min(l + dx, r - 16)
            if "r" in mode:
                r = max(r + dx, l + 16)
            if "t" in mode:
                t = min(t + dy, b - 16)
            if "b" in mode:
                b = max(b + dy, t + 16)
            if self.aspect:
                if mode in ("t", "b"):
                    r = l + (b - t) * self.aspect
                elif "t" in mode:
                    t = b - (r - l) / self.aspect
                else:
                    b = t + (r - l) / self.aspect
            box = (l, t, r - l, b - t)
        self._box = self.cut.clamp_crop(box)
        self.update()

    def mouseReleaseEvent(self, ev):
        if self._drag:
            box, self._box, self._drag = self._box, None, None
            if box:
                self.crop_changed.emit(box)
            self.update()


class Timeline(QWidget):
    """The pieces (kept / removed), split points, the selected range and the playhead.  Drag in
    the bar to select a range (its edges can be dragged after); click to seek and pick a piece;
    drag the time ruler above it to scrub; double-click a piece to remove or restore it."""
    seek = pyqtSignal(float, bool)      # seconds, exact
    toggle = pyqtSignal(int)
    range_changed = pyqtSignal()

    DRAG_PX = 4         # moves shorter than this are clicks
    EDGE_PX = 6         # grab a range edge within this

    def __init__(self, cut):
        super().__init__()
        self.cut = cut
        self.pos = 0.0
        self.selected = 0
        self.range = None         # (start, end) seconds, or None
        self._drag = None         # ("scrub" | "click" | "range", anchor seconds, press x)
        self.setMinimumHeight(64)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)

    def _bar(self):
        return QRectF(12, 22, self.width() - 24, self.height() - 30)

    def _x(self, t):
        b = self._bar()
        return b.left() + b.width() * t / max(self.cut.duration, 1e-6)

    def _t(self, x):
        b = self._bar()
        return min(self.cut.duration, max(0.0, (x - b.left()) / b.width() * self.cut.duration))

    def set_pos(self, t):
        self.pos = t
        self.update()

    @property
    def dragging(self):
        return self._drag is not None

    def set_range(self, r):
        if r != self.range:
            self.range = r
            self.update()
            self.range_changed.emit()

    def _edge(self, x):
        """The range edge near x: the other edge (the drag's anchor), or None."""
        if self.range:
            a, b = self.range
            for near, other in ((b, a), (a, b)):
                if abs(self._x(near) - x) <= self.EDGE_PX:
                    return other
        return None

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        b = self._bar()
        # time ticks
        p.setPen(pal.color(pal.ColorRole.PlaceholderText))
        dur = self.cut.duration
        step = next((s for s in (0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800)
                     if b.width() / max(dur / s, 1) >= 70), 3600)
        t = 0.0
        while t <= dur + 1e-6:
            x = self._x(t)
            p.drawLine(QPointF(x, b.top() - 5), QPointF(x, b.top() - 1))
            p.drawText(QPointF(x + 3, b.top() - 6), fmt_time(t, precise=step < 1))
            t += step
        # pieces
        for i, (s, e, keep) in enumerate(self.cut.pieces):
            r = QRectF(self._x(s), b.top(), self._x(e) - self._x(s), b.height())
            if keep:
                p.fillRect(r, style.ACCENT.darker(130))
            else:
                p.fillRect(r, QColor("#2a2d31"))
                p.fillRect(r, QBrush(QColor("#c0392b"), Qt.BrushStyle.BDiagPattern))
            if i == self.selected and not self.range:
                p.setPen(QPen(QColor("white"), 2))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRect(r.adjusted(1, 1, -1, -1))
        p.setPen(QPen(QColor("#f5f6f7"), 2))
        for s, _, _ in self.cut.pieces[1:]:
            p.drawLine(QPointF(self._x(s), b.top()), QPointF(self._x(s), b.bottom()))
        # selected range
        if self.range:
            x0, x1 = self._x(self.range[0]), self._x(self.range[1])
            p.fillRect(QRectF(x0, b.top(), x1 - x0, b.height()), QColor(255, 255, 255, 70))
            p.setPen(QPen(QColor("white"), 2))
            p.drawLine(QPointF(x0, b.top()), QPointF(x1, b.top()))
            p.drawLine(QPointF(x0, b.bottom()), QPointF(x1, b.bottom()))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("white"))
            for x in (x0, x1):      # grips
                p.drawRoundedRect(QRectF(x - 2.5, b.top() - 1, 5, b.height() + 2), 2, 2)
        # playhead
        x = self._x(self.pos)
        p.setPen(QPen(QColor("#ffcc00"), 2))
        p.drawLine(QPointF(x, b.top() - 4), QPointF(x, b.bottom() + 2))
        p.setBrush(QColor("#ffcc00"))
        p.setPen(Qt.PenStyle.NoPen)
        top = b.top()
        p.drawPolygon(QPolygonF([QPointF(x - 5, top - 8), QPointF(x + 5, top - 8), QPointF(x, top - 2)]))
        p.end()

    def _cursor(self, pos):
        if pos.y() < self._bar().top() - 2:
            return Qt.CursorShape.PointingHandCursor
        if self._edge(pos.x()) is not None:
            return Qt.CursorShape.SizeHorCursor
        return Qt.CursorShape.IBeamCursor

    def mousePressEvent(self, ev):
        if ev.button() != Qt.MouseButton.LeftButton:
            return
        pos = ev.position()
        t = self._t(pos.x())
        if pos.y() < self._bar().top() - 2:
            self._drag = ("scrub", t, pos.x())
        elif (anchor := self._edge(pos.x())) is not None:
            self._drag = ("range", anchor, pos.x())
            return              # the playhead jumps once the edge moves
        else:
            self._drag = ("click", t, pos.x())
        self.seek.emit(t, False)
        self.set_pos(t)

    def mouseMoveEvent(self, ev):
        pos = ev.position()
        if not self._drag:
            self.setCursor(self._cursor(pos))
            return
        mode, anchor, x0 = self._drag
        if mode == "click" and abs(pos.x() - x0) >= self.DRAG_PX:
            mode = "range"
            self._drag = (mode, anchor, x0)
        t = self._t(pos.x())
        if mode == "range":
            t = self.cut.snap(t)
            self.set_range((min(anchor, t), max(anchor, t)))
        self.seek.emit(t, False)
        self.set_pos(t)

    def mouseReleaseEvent(self, ev):
        if not self._drag:
            return
        mode, _, x0 = self._drag
        self._drag = None
        x = ev.position().x()
        t = self._t(x)
        if mode == "range":
            t = self.cut.snap(t)
            if self.range and self.range[1] - self.range[0] < MIN_PIECE:
                self.set_range(None)
            if abs(x - x0) < self.DRAG_PX:      # an edge grabbed but not moved
                return
        elif mode == "click":
            self.selected = self.cut.piece_at(t)
            self.set_range(None)
        self.seek.emit(t, True)
        self.set_pos(t)

    def mouseDoubleClickEvent(self, ev):
        if ev.position().y() < self._bar().top() - 2:
            return
        i = self.cut.piece_at(self._t(ev.position().x()))
        self.selected = i
        self.set_range(None)
        self.toggle.emit(i)


class FlowLayout(QLayout):
    """Lays its items out left to right and wraps to a new row when the width runs out."""

    def __init__(self, parent=None, spacing=6):
        super().__init__(parent)
        self._items = []
        self._spacing = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._place(QRect(0, 0, width, 0), move=False)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._place(rect, move=True)

    def sizeHint(self):
        w = sum(i.sizeHint().width() for i in self._items) + self._spacing * max(0, len(self._items) - 1)
        return QSize(w, max((i.sizeHint().height() for i in self._items), default=0))

    def minimumSize(self):
        size = QSize()
        for i in self._items:
            size = size.expandedTo(i.minimumSize())
        return size

    def _place(self, rect, move):
        """Returns the height used.  Items in a row share its height, centered."""
        rows, row, x = [], [], rect.x()
        for item in self._items:
            w = item.sizeHint().width()
            if row and x + w > rect.right() + 1:
                rows.append(row)
                row, x = [], rect.x()
            row.append(item)
            x += w + self._spacing
        if row:
            rows.append(row)
        y = rect.y()
        for row in rows:
            h = max(i.sizeHint().height() for i in row)
            x = rect.x()
            for item in row:
                hint = item.sizeHint()
                if move:
                    item.setGeometry(QRect(QPoint(x, y + (h - hint.height()) // 2), hint))
                x += hint.width() + self._spacing
            y += h + self._spacing
        return y - rect.y() - (self._spacing if rows else 0)


class ToolRows(QWidget):
    """A toolbar that wraps into more rows instead of hiding buttons in an overflow menu.
    Buttons come in groups that wrap as a whole; lines separate groups sharing a row."""

    def __init__(self):
        super().__init__()
        self._groups = []
        FlowLayout(self, spacing=10)
        self.layout().setContentsMargins(4, 2, 4, 2)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)

    def add_group(self, *items):
        group = QWidget()
        lay = QHBoxLayout(group)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        for item in items:
            if isinstance(item, QAction):
                button = QToolButton()
                button.setDefaultAction(item)
                button.setAutoRaise(True)
                button.setFocusPolicy(Qt.FocusPolicy.NoFocus)      # like toolbar buttons: keys stay shortcuts
                button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
                item = button
            lay.addWidget(item)
        self.layout().addWidget(group)
        self._groups.append(group)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setPen(QPen(self.palette().color(self.palette().ColorRole.Mid), 1))
        for prev, g in zip(self._groups, self._groups[1:]):
            a, b = prev.geometry(), g.geometry()
            if a.top() <= b.bottom() and b.top() <= a.bottom():         # same row
                x = (a.right() + b.left()) // 2 + 0.5
                p.drawLine(QPointF(x, min(a.top(), b.top()) + 4), QPointF(x, max(a.bottom(), b.bottom()) - 4))
        p.end()


class VideoEditor(QMainWindow):
    """export(segments, crop) asks for the convert window with this edit."""
    export = pyqtSignal(object, object)
    closed = pyqtSignal()
    _done_bars = pyqtSignal(object)      # from the black-bar thread

    def __init__(self, info):
        super().__init__()
        self.info = info
        self.cut = VideoCut(info.duration, info.width, info.height)
        self.setWindowTitle(f"Snappie — Cut and crop: {os.path.basename(info.path)}")
        self.setWindowIcon(QIcon.fromTheme("snappie"))
        self._seeking_to = None

        self.view = VideoView(self.cut)
        self.timeline = Timeline(self.cut)
        self.tools = ToolRows()
        central = QWidget()
        lay = QVBoxLayout(central)
        lay.setContentsMargins(6, 4, 6, 6)
        lay.addWidget(self.tools)
        lay.addWidget(self.view, 1)
        lay.addWidget(self.timeline)
        self.setCentralWidget(central)
        self.status = QLabel()
        self.statusBar().addWidget(self.status, 1)
        self.time = QLabel()
        self.time.setMinimumWidth(150)
        self._build_actions()

        self.player = Player(info.path, preview_size(info.width, info.height), self)
        self.player.frame.connect(self.view.set_frame)
        self.player.ended.connect(self._ended)
        self.player.error.connect(lambda m: self.statusBar().showMessage(f"Playback: {m}", 8000))
        self.timeline.seek.connect(self._seek)
        self.timeline.toggle.connect(self._toggle)
        self.timeline.range_changed.connect(self._range_changed)
        self._set_volume(self.volume.value())
        self.view.crop_changed.connect(self._crop_drawn)
        self._done_bars.connect(self._bars_found, Qt.ConnectionType.QueuedConnection)
        self.tick = QTimer(self, interval=40, timeout=self._tick)
        self.tick.start()
        self._changed()
        self.resize(1100, 760)

    # -- building

    def _act(self, text, icon, slot, keys=(), tip=None, checkable=False):
        a = QAction(QIcon.fromTheme(icon), text, self)
        if keys:
            a.setShortcuts([QKeySequence(k) for k in keys])
        a.setToolTip(f"{tip or text} ({QKeySequence(keys[0]).toString(QKeySequence.SequenceFormat.NativeText)})"
                     if keys else tip or text)
        a.setCheckable(checkable)
        (a.toggled if checkable else a.triggered).connect(slot)
        self.addAction(a)
        return a

    def _build_actions(self):
        self.play_act = self._act("Play", "media-playback-start", self.toggle_play, ["Space", "K"])
        self.tools.add_group(self.play_act, self.time)
        self.remove_act = self._act("Remove", "edit-delete", self.toggle_selected, ["Delete", "Backspace"],
                                    "Remove or restore the selected part or dragged range (or double-click a part)")
        self.tools.add_group(
            self._act("Split", "edit-cut", self.split, ["S"], "Split at the playhead"),
            self.remove_act,
            self._act("Start here", "go-first", lambda: self._apply(self.cut.trim_start(self._pos())),
                      ["I"], "Remove everything before the playhead"),
            self._act("End here", "go-last", lambda: self._apply(self.cut.trim_end(self._pos())),
                      ["O"], "Remove everything after the playhead"))
        self.crop_act = self._act("Crop", "transform-crop", self._crop_mode, ["C"], "Draw or adjust the crop box",
                                  checkable=True)
        self.aspect = QComboBox()
        for label, ratio in ASPECTS:
            self.aspect.addItem(label, ratio)
        self.aspect.setToolTip("Crop box shape")
        self.aspect.currentIndexChanged.connect(self._aspect)
        self.bars_act = self._act("Remove black bars", "zoom-fit-best", self.detect_bars,
                                  tip="Crop away black bars")
        self.tools.add_group(self.crop_act, self.aspect, self.bars_act,
                             self._act("No crop", "edit-clear", lambda: self._apply(self.cut.set_crop(None))))
        self.tools.add_group(
            self._act("Undo", "edit-undo", lambda: self._apply(self.cut.undo()), ["Ctrl+Z"]),
            self._act("Redo", "edit-redo", lambda: self._apply(self.cut.redo()), ["Ctrl+Shift+Z", "Ctrl+Y"]))
        self.mute_act = self._act("Mute", "audio-volume-muted", self._mute, ["M"], checkable=True)
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(int(settings.get("videoedit.volume")))
        self.volume.setFixedWidth(110)
        self.volume.setFocusPolicy(Qt.FocusPolicy.NoFocus)      # keys stay with the editor
        self.volume.valueChanged.connect(self._set_volume)
        self.tools.add_group(self.mute_act, self.volume)
        # Export sits in the status bar, next to the result it saves
        exp = self._act("Export…", "document-save-as", self._export, ["Ctrl+E", "Ctrl+S"],
                        "Choose a format and save the result")
        self.export_button = button = QPushButton(exp.icon(), exp.text())
        button.setToolTip(f"{exp.toolTip()}.  With a range selected on the timeline, only that part.")
        button.setDefault(True)
        button.clicked.connect(exp.trigger)
        self.statusBar().addPermanentWidget(button)
        for keys, dt in ((["Left"], -1), (["Right"], 1), (["Shift+Left", "J"], -50), (["Shift+Right", "L"], 50)):
            self._act("", "", lambda _=False, d=dt: self.step(d), keys)
        self._act("", "", lambda: self._seek(0, True), ["Home"])
        self._act("", "", lambda: self._seek(self.cut.duration, True), ["End"])
        self._act("", "", self.close, ["Ctrl+W"])
        self._act("", "", lambda: self.timeline.set_range(None), ["Escape"])

    # -- playback

    def _pos(self):
        return self.timeline.pos

    def _tick(self):
        if not self.player.playing:
            return
        t = self.player.position()
        if t is None:
            return
        if self._seeking_to is not None:       # wait for a skip seek to land
            if abs(t - self._seeking_to) > 0.3:
                return
            self._seeking_to = None
        loop = self._loop()
        if loop:
            if self.timeline.dragging:
                return
            if not loop[0] - 0.05 <= t < loop[1] - 1e-3:      # scrubbed away, or past the stop
                self._loop_back()
                return
            self.timeline.set_pos(t)      # removed parts play too: you're looking at this part
            self._show_time()
            return
        jump = self.cut.skip_to(t)
        if jump is not None:
            if jump >= self.cut.duration - 1e-3:
                self.player.pause()
                self._seek(self.cut.kept()[-1][1] - 0.001, True)
                self._sync_play()
                return
            self._seeking_to = jump
            self.player.seek(jump, True)
            t = jump
        self.timeline.set_pos(t)
        self._show_time()

    def _ended(self):
        if self._loop() and not self.timeline.dragging:
            self._loop_back()
            self.player.play()
        self._sync_play()

    def _loop(self):
        """The selected range: playing loops in it."""
        r = self.timeline.range
        return r if r and r[1] - r[0] >= MIN_PIECE else None

    def _loop_back(self):
        start = self._loop()[0]
        self._seeking_to = start
        self._seek(start, True)

    def _range_changed(self):
        self._changed()
        if not self.timeline.dragging:
            self._seek(self._pos(), True)     # sets or drops the loop's stop

    def toggle_play(self):
        if self.player.playing:
            self.player.pause()
        else:
            t = self._pos()
            kept = self.cut.kept()
            loop = self._loop()
            if loop:
                self._seek(t if loop[0] <= t < loop[1] - 0.05 else loop[0], True)
            elif t >= kept[-1][1] - 0.05:
                self._seek(kept[0][0], True)      # at the end: from the start
            elif self.cut.skip_to(t) is not None:
                self._seek(self.cut.skip_to(t), True)
            self.player.play()
        self._sync_play()

    def _sync_play(self):
        on = self.player.playing
        self.play_act.setText("Pause" if on else "Play")
        self.play_act.setIcon(QIcon.fromTheme("media-playback-pause" if on else "media-playback-start"))

    def _seek(self, t, exact):
        t = min(max(0.0, t), self.cut.duration)
        loop = self._loop()
        self.player.seek(t, exact, loop[1] if loop and loop[0] - 1e-3 <= t < loop[1] else None)
        self.timeline.set_pos(t)
        self._show_time()

    def step(self, frames):
        if self.player.playing:
            self.player.pause()
            self._sync_play()
        fps = self.info.fps or 30
        self._seek(self._pos() + frames / fps, True)

    def _mute(self, on):
        self.player.set_muted(on)
        self._volume_icon()

    def _set_volume(self, v):
        self.volume.setToolTip(f"Preview volume: {v}%")
        self.player.set_volume(v)
        if self.mute_act.isChecked() and v:
            self.mute_act.setChecked(False)       # moving the slider unmutes
        self._volume_icon()

    def _volume_icon(self):
        v = self.volume.value()
        name = ("muted" if self.mute_act.isChecked() or not v else
                "low" if v < 34 else "medium" if v < 67 else "high")
        self.mute_act.setIcon(QIcon.fromTheme(f"audio-volume-{name}"))

    def _show_time(self):
        self.time.setText(f"  {fmt_time(self._pos())} / {fmt_time(self.cut.duration)}  ")

    # -- editing

    def _apply(self, changed):
        if changed:
            self._changed()

    def _changed(self):
        self.timeline.selected = min(self.timeline.selected, len(self.cut.pieces) - 1)
        self.timeline.update()
        self.view.update()
        r = self.timeline.range
        segments = self._segments()
        w, h = (self.cut.crop[2], self.cut.crop[3]) if self.cut.crop else (self.info.width, self.info.height)
        if segments == []:
            text = "Result: nothing (everything selected is removed)"
        else:
            o = convert.Options("mp4", segments=segments, crop=self.cut.crop)
            text = f"Result{' of the selection' if r else ''}: {fmt_time(convert.out_duration(self.info, o))}"
            text += f" · {w}×{h}"
            parts = len(segments or [None])
            if parts > 1:
                text += f" · {parts} parts joined"
        if r:
            text += (f"   Selected {fmt_time(r[0])}–{fmt_time(r[1])} ({r[1] - r[0]:.1f} s): plays in a loop,"
                     f" Delete to {'remove' if self.cut.range_kept(*r) else 'restore'}, Export saves only it,"
                     f" Esc to clear")
            removing = self.cut.range_kept(*r)
        else:
            removing = self.cut.pieces[self.timeline.selected][2]
        self.status.setText(text)
        self.remove_act.setText("Remove" if removing else "Restore")
        self.export_button.setText("Export selection…" if r else "Export…")
        self._show_time()

    def split(self):
        if self.cut.split(self._pos()):
            self.timeline.selected = self.cut.piece_at(self._pos())
            self._changed()

    def toggle_selected(self):
        r = self.timeline.range
        if not r:
            self._toggle(self.timeline.selected)
            return
        keep = not self.cut.range_kept(*r)
        if self.cut.set_range(*r, keep):
            self.timeline.selected = self.cut.piece_at(self.cut.snap(r[0]) + MIN_PIECE / 2)
            self.timeline.set_range(None)       # also refreshes everything (range_changed)
        elif not keep:
            self.statusBar().showMessage("At least one part has to stay.", 3000)

    def _toggle(self, i):
        if not self.cut.toggle(i):
            self.statusBar().showMessage("At least one part has to stay.", 3000)
        self._changed()

    def _crop_mode(self, on):
        self.view.cropping = on
        self.view.setCursor(Qt.CursorShape.CrossCursor if on else Qt.CursorShape.ArrowCursor)
        self.view.update()

    def _aspect(self):
        ratio = self.aspect.currentData()
        self.view.aspect = ratio
        if ratio:
            self.crop_act.setChecked(True)
            self._apply(self.cut.set_crop(self.cut.aspect_crop(ratio)))

    def _crop_drawn(self, box):
        self._apply(self.cut.set_crop(box))

    def detect_bars(self):
        import threading
        self.bars_act.setEnabled(False)
        self.statusBar().showMessage("Looking for black bars…")
        result = {}

        def work():
            try:
                result["box"] = convert.detect_bars(self.info)
            except (RuntimeError, OSError) as e:
                result["error"] = str(e)
            self._done_bars.emit(result)
        threading.Thread(target=work, daemon=True).start()

    def _bars_found(self, result):
        self.bars_act.setEnabled(True)
        box = result.get("box")
        if box:
            self._apply(self.cut.set_crop(box))
            self.crop_act.setChecked(False)      # show the result, not the box
            self.statusBar().showMessage(f"Black bars removed: {box[2]}×{box[3]} left.", 5000)
        else:
            self.statusBar().showMessage(result.get("error") or "No black bars found.", 5000)

    def _segments(self):
        """What Export saves: the edit, or only its part inside the selected range."""
        r = self.timeline.range
        return self.cut.segments(within=r if r and r[1] - r[0] >= MIN_PIECE else None)

    def _export(self):
        if self.player.playing:
            self.toggle_play()
        segments = self._segments()
        if segments == []:
            self.statusBar().showMessage("Everything in the selection is removed: nothing to export.", 4000)
            return
        self.export.emit(segments, self.cut.crop)

    def closeEvent(self, ev):
        self.tick.stop()
        self.player.close()
        if self.volume.value() != settings.get("videoedit.volume"):
            settings.set("videoedit.volume", self.volume.value())
        super().closeEvent(ev)
        self.closed.emit()
