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

from PyQt6.QtCore import QObject, QPointF, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QBrush, QColor, QIcon, QImage, QKeySequence, QPainter, QPen, QPolygonF
from PyQt6.QtWidgets import (QComboBox, QLabel, QMainWindow, QPushButton, QSizePolicy, QToolBar, QVBoxLayout,
                             QWidget)

from . import convert, style
from .app_util import log
from .videocut import VideoCut

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
        self.pipe.set_property("flags", int(self.pipe.get_property("flags")) & ~0x4)    # no subtitles
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

    def seek(self, t, exact=True):
        Gst = self.Gst
        flags = Gst.SeekFlags.FLUSH | (Gst.SeekFlags.ACCURATE if exact else Gst.SeekFlags.KEY_UNIT)
        self.pipe.seek_simple(Gst.Format.TIME, flags, max(0, int(t * 1e9)))

    def set_muted(self, on):
        self.pipe.set_property("mute", on)

    def close(self):
        if self.pipe is not None:
            self.pipe.get_bus().remove_signal_watch()
            self.pipe.set_state(self.Gst.State.NULL)
            self.pipe = None


class VideoView(QWidget):
    """The current frame, fitted, with the crop box over it (editable in crop mode)."""
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

    def _fit(self):
        W, H = self.cut.width, self.cut.height
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
            p.drawImage(r, self.image)
        box = self.box()
        if box:
            b = self._to_view(box)
            shade = QColor(0, 0, 0, 150 if self.cropping else 200)
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
    """The pieces (kept / removed), split points and the playhead.  Click or drag to seek;
    double-click a piece to remove or restore it."""
    seek = pyqtSignal(float, bool)      # seconds, exact
    toggle = pyqtSignal(int)

    def __init__(self, cut):
        super().__init__()
        self.cut = cut
        self.pos = 0.0
        self.selected = 0
        self._dragging = False
        self.setMinimumHeight(64)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

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
            if i == self.selected:
                p.setPen(QPen(QColor("white"), 2))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRect(r.adjusted(1, 1, -1, -1))
        p.setPen(QPen(QColor("#f5f6f7"), 2))
        for s, _, _ in self.cut.pieces[1:]:
            p.drawLine(QPointF(self._x(s), b.top()), QPointF(self._x(s), b.bottom()))
        # playhead
        x = self._x(self.pos)
        p.setPen(QPen(QColor("#ffcc00"), 2))
        p.drawLine(QPointF(x, b.top() - 4), QPointF(x, b.bottom() + 2))
        p.setBrush(QColor("#ffcc00"))
        p.setPen(Qt.PenStyle.NoPen)
        top = b.top()
        p.drawPolygon(QPolygonF([QPointF(x - 5, top - 8), QPointF(x + 5, top - 8), QPointF(x, top - 2)]))
        p.end()

    def mousePressEvent(self, ev):
        if ev.button() != Qt.MouseButton.LeftButton:
            return
        self._dragging = True
        t = self._t(ev.position().x())
        self.selected = self.cut.piece_at(t)
        self.seek.emit(t, False)
        self.set_pos(t)

    def mouseMoveEvent(self, ev):
        if self._dragging:
            t = self._t(ev.position().x())
            self.seek.emit(t, False)
            self.set_pos(t)

    def mouseReleaseEvent(self, ev):
        if self._dragging:
            self._dragging = False
            t = self._t(ev.position().x())
            self.seek.emit(t, True)
            self.set_pos(t)

    def mouseDoubleClickEvent(self, ev):
        i = self.cut.piece_at(self._t(ev.position().x()))
        self.selected = i
        self.toggle.emit(i)


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
        central = QWidget()
        lay = QVBoxLayout(central)
        lay.setContentsMargins(6, 6, 6, 6)
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
        tb = QToolBar("Edit")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, tb)
        self.play_act = self._act("Play", "media-playback-start", self.toggle_play, ["Space", "K"])
        tb.addAction(self.play_act)
        tb.addWidget(self.time)
        tb.addSeparator()
        tb.addAction(self._act("Split", "edit-cut", self.split, ["S"], "Split at the playhead"))
        self.remove_act = self._act("Remove", "edit-delete", self.toggle_selected, ["Delete", "Backspace"],
                                    "Remove or restore the selected part (or double-click it)")
        tb.addAction(self.remove_act)
        tb.addAction(self._act("Start here", "go-first", lambda: self._apply(self.cut.trim_start(self._pos())),
                               ["I"], "Remove everything before the playhead"))
        tb.addAction(self._act("End here", "go-last", lambda: self._apply(self.cut.trim_end(self._pos())),
                               ["O"], "Remove everything after the playhead"))
        tb.addSeparator()
        self.crop_act = self._act("Crop", "transform-crop", self._crop_mode, ["C"], "Draw or adjust the crop box",
                                  checkable=True)
        tb.addAction(self.crop_act)
        self.aspect = QComboBox()
        for label, ratio in ASPECTS:
            self.aspect.addItem(label, ratio)
        self.aspect.setToolTip("Crop box shape")
        self.aspect.currentIndexChanged.connect(self._aspect)
        tb.addWidget(self.aspect)
        self.bars_act = self._act("Remove black bars", "zoom-fit-best", self.detect_bars,
                                  tip="Crop away black bars")
        tb.addAction(self.bars_act)
        tb.addAction(self._act("No crop", "edit-clear", lambda: self._apply(self.cut.set_crop(None))))
        tb.addSeparator()
        tb.addAction(self._act("Undo", "edit-undo", lambda: self._apply(self.cut.undo()), ["Ctrl+Z"]))
        tb.addAction(self._act("Redo", "edit-redo", lambda: self._apply(self.cut.redo()),
                               ["Ctrl+Shift+Z", "Ctrl+Y"]))
        self.mute_act = self._act("Mute", "audio-volume-muted", lambda on: self.player.set_muted(on), ["M"],
                                  checkable=True)
        tb.addAction(self.mute_act)
        # Export sits in the status bar, not the toolbar: a narrow window moves the toolbar's
        # last buttons into an overflow menu, and this one has to stay in sight
        exp = self._act("Export…", "document-save-as", self._export, ["Ctrl+E", "Ctrl+S"],
                        "Choose a format and save the result")
        button = QPushButton(exp.icon(), exp.text())
        button.setToolTip(exp.toolTip())
        button.setDefault(True)
        button.clicked.connect(exp.trigger)
        self.statusBar().addPermanentWidget(button)
        for keys, dt in ((["Left"], -1), (["Right"], 1), (["Shift+Left", "J"], -50), (["Shift+Right", "L"], 50)):
            self._act("", "", lambda _=False, d=dt: self.step(d), keys)
        self._act("", "", lambda: self._seek(0, True), ["Home"])
        self._act("", "", lambda: self._seek(self.cut.duration, True), ["End"])
        self._act("", "", self.close, ["Ctrl+W"])

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
        self._sync_play()

    def toggle_play(self):
        if self.player.playing:
            self.player.pause()
        else:
            t = self._pos()
            kept = self.cut.kept()
            if t >= kept[-1][1] - 0.05:
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
        self.player.seek(t, exact)
        self.timeline.set_pos(t)
        self._show_time()

    def step(self, frames):
        if self.player.playing:
            self.player.pause()
            self._sync_play()
        fps = self.info.fps or 30
        self._seek(self._pos() + frames / fps, True)

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
        o = convert.Options("mp4", segments=self.cut.segments(), crop=self.cut.crop)
        w, h = (self.cut.crop[2], self.cut.crop[3]) if self.cut.crop else (self.info.width, self.info.height)
        parts = len(self.cut.kept())
        text = f"Result: {fmt_time(convert.out_duration(self.info, o))} · {w}×{h}"
        if parts > 1:
            text += f" · {parts} parts joined"
        self.status.setText(text)
        sel = self.cut.pieces[self.timeline.selected]
        self.remove_act.setText("Restore" if not sel[2] else "Remove")
        self._show_time()

    def split(self):
        if self.cut.split(self._pos()):
            self.timeline.selected = self.cut.piece_at(self._pos())
            self._changed()

    def toggle_selected(self):
        self._toggle(self.timeline.selected)

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
            self.statusBar().showMessage(f"Black bars removed: {box[2]}×{box[3]} left.", 5000)
        else:
            self.statusBar().showMessage(result.get("error") or "No black bars found.", 5000)

    def _export(self):
        if self.player.playing:
            self.toggle_play()
        self.export.emit(self.cut.segments(), self.cut.crop)

    def closeEvent(self, ev):
        self.tick.stop()
        self.player.close()
        super().closeEvent(ev)
        self.closed.emit()
