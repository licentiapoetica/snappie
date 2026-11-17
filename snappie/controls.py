"""Small floating recording bar (timer, pause, draw, stop). KWin places it, see kwin.place."""

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPalette, QPen, QPixmap, QPolygonF
from PyQt6.QtWidgets import (QButtonGroup, QCheckBox, QColorDialog, QFrame, QHBoxLayout, QLabel, QMenu, QPushButton,
                             QVBoxLayout, QWidget)

from .style import ACCENT_TEXT, MUTED, PALETTE, RECORD, TEXT, mark_overlay

TITLE = "Snappie — recording"

_BTN = ("QPushButton { background: transparent; color: #eff0f1; border: none; border-radius: 0;"
        " padding: 4px 9px; font-weight: bold; }"
        "QPushButton:hover { background: rgba(255,255,255,40); }"
        "QPushButton:checked { background: #3daee9; color: #0b1a24; }"
        "QPushButton:disabled { color: #5a5f66; }")


class _Dot(QWidget):
    def __init__(self):
        super().__init__()
        self.setFixedSize(12, 12)
        self.on = True
        self.paused = False

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        if self.paused:  # two bars
            p.setBrush(QColor("#fdbc4b"))
            p.drawRect(2, 1, 3, 10)
            p.drawRect(7, 1, 3, 10)
            return
        p.setBrush(RECORD if self.on else QColor(218, 68, 83, 70))
        p.drawEllipse(1, 1, 10, 10)


def _tool_icon(kind, color):
    """Small line-art icon for a drawing tool, in one color (white normally, dark when active)."""
    pm = QPixmap(36, 36)
    pm.setDevicePixelRatio(2)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(color, 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    if kind == "pen":
        path = QPainterPath(QPointF(3, 13))
        path.cubicTo(QPointF(6, 3), QPointF(9, 17), QPointF(15, 5))
        p.drawPath(path)
    elif kind == "highlighter":
        c = QColor(color)
        c.setAlphaF(0.55)
        p.setPen(QPen(c, 6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
        p.drawLine(QPointF(3, 9), QPointF(15, 9))
    elif kind == "arrow":
        p.drawLine(QPointF(3, 15), QPointF(12, 6))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        p.drawPolygon(QPolygonF([QPointF(15.5, 2.5), QPointF(8, 4.5), QPointF(13.5, 10)]))
    elif kind == "line":
        p.drawLine(QPointF(3, 15), QPointF(15, 3))
    elif kind == "rect":
        p.drawRect(QRectF(3, 4.5, 12, 9))
    elif kind == "ellipse":
        p.drawEllipse(QRectF(2.5, 4, 13, 10))
    p.end()
    return pm


def _mic_icon(color, muted=False):
    """Microphone line art; crossed out when muted."""
    pm = QPixmap(36, 36)
    pm.setDevicePixelRatio(2)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(color, 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    p.drawRoundedRect(QRectF(6.5, 2, 5, 9), 2.5, 2.5)
    path = QPainterPath(QPointF(4, 8.5))
    path.arcTo(QRectF(4, 4.5, 10, 9), 180, 180)
    p.drawPath(path)
    p.drawLine(QPointF(9, 13.5), QPointF(9, 16))
    p.drawLine(QPointF(6.5, 16), QPointF(11.5, 16))
    if muted:
        p.setPen(QPen(RECORD, 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(3, 3), QPointF(15, 15))
    p.end()
    return pm


def _dark(widget):
    """The bar's colors for normal widgets (checkboxes) on its black background."""
    pal = widget.palette()
    for role, color in ((QPalette.ColorRole.Window, "#000000"), (QPalette.ColorRole.WindowText, "#eff0f1"),
                        (QPalette.ColorRole.Base, "#232629"), (QPalette.ColorRole.Text, "#eff0f1"),
                        (QPalette.ColorRole.Button, "#31363b"), (QPalette.ColorRole.ButtonText, "#eff0f1")):
        pal.setColor(role, QColor(color))
    widget.setPalette(pal)


def _sep():
    f = QFrame()
    f.setFixedSize(1, 18)
    f.setStyleSheet("background: rgba(255,255,255,45);")
    return f


class DrawTools(QWidget):
    """Tool, color, size, keep / undo / clear for drawing on the recording. Lives in the bar
    (outside the recorded area, so it never shows up in the video)."""
    tool_changed = pyqtSignal(str)
    color_changed = pyqtSignal(QColor)
    width_step = pyqtSignal(int)
    keep_changed = pyqtSignal(bool)
    emoji_chosen = pyqtSignal(str)
    undo = pyqtSignal()
    clear = pyqtSignal()

    TOOLS = [("pen", "Pen  (P)"), ("highlighter", "Highlighter  (H)"), ("arrow", "Arrow  (A, or Shift / right drag)"),
             ("line", "Line  (L)"), ("rect", "Rectangle  (R)"), ("ellipse", "Ellipse  (E)")]

    def __init__(self):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        lay.addWidget(_sep())
        lay.addSpacing(4)
        self.tools = QButtonGroup(self)
        self.tools.setExclusive(True)
        self._tool_btn = {}
        for kind, tip in self.TOOLS:
            b = self._button(tip, checkable=True)
            ic = QIcon()
            ic.addPixmap(_tool_icon(kind, TEXT), QIcon.Mode.Normal, QIcon.State.Off)
            ic.addPixmap(_tool_icon(kind, ACCENT_TEXT), QIcon.Mode.Normal, QIcon.State.On)
            b.setIcon(ic)
            b.setIconSize(QSize(18, 18))
            b.clicked.connect(lambda _=False, k=kind: self.tool_changed.emit(k))
            self.tools.addButton(b)
            self._tool_btn[kind] = b
            lay.addWidget(b)
        self.emoji = self._button("Emoji stamp: pick one, then click to place it (it fades out)",
                                  text="😂", checkable=True)
        from .emoji import button_font
        self.emoji.setFont(button_font(16))
        self.emoji.clicked.connect(self._open_emojis)
        self.tools.addButton(self.emoji)
        self._tool_btn["emoji"] = self.emoji
        lay.addWidget(self.emoji)
        lay.addSpacing(4)
        lay.addWidget(_sep())
        lay.addSpacing(6)
        self._swatches = []
        for i, c in enumerate(PALETTE):
            sw = QPushButton()
            sw.setFixedSize(18, 18)
            sw.setCheckable(True)
            sw.setToolTip(f"{c}  ({i + 1})")
            sw.setCursor(Qt.CursorShape.PointingHandCursor)
            sw.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            sw.setStyleSheet(self._swatch_qss(c))
            sw.clicked.connect(lambda _=False, col=c: self.color_changed.emit(QColor(col)))
            self._swatches.append((c, sw))
            lay.addWidget(sw)
        self.custom = self._button("Other color…", text="…")
        self.custom.clicked.connect(self._pick_color)
        lay.addWidget(self.custom)
        lay.addSpacing(4)
        lay.addWidget(_sep())
        lay.addSpacing(2)
        minus = self._button("Thinner  (wheel down / −)", text="−")
        minus.clicked.connect(lambda: self.width_step.emit(-1))
        self.size = QLabel("4 px")
        self.size.setStyleSheet("color: #eff0f1;")
        self.size.setToolTip("Size — scroll here or on the drawing to change it")
        f = self.size.font()
        f.setFeature(QFont.Tag("tnum"), 1)
        self.size.setFont(f)
        self.size.setMinimumWidth(self.size.fontMetrics().horizontalAdvance("24 px") + 4)
        self.size.setAlignment(Qt.AlignmentFlag.AlignCenter)
        plus = self._button("Thicker  (wheel up / +)", text="+")
        plus.clicked.connect(lambda: self.width_step.emit(1))
        lay.addWidget(minus)
        lay.addWidget(self.size)
        lay.addWidget(plus)
        lay.addSpacing(2)
        lay.addWidget(_sep())
        lay.addSpacing(2)
        self.keep = self._button("Keep drawings (otherwise they fade out after a few seconds)",
                                 text="Keep", checkable=True)
        self.keep.toggled.connect(self.keep_changed)
        undo = self._button("Undo  (Z)", text="↶")
        undo.clicked.connect(self.undo)
        clear = self._button("Clear all  (C)", text="Clear")
        clear.clicked.connect(self.clear)
        lay.addWidget(self.keep)
        lay.addWidget(undo)
        lay.addWidget(clear)
        self._color = QColor(PALETTE[0])
        self._tool = "pen"

    def _button(self, tip, text="", checkable=False):
        b = QPushButton(text)
        b.setToolTip(tip)
        b.setCheckable(checkable)
        b.setStyleSheet(_BTN.replace("padding: 4px 9px", "padding: 4px 6px"))
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        return b

    @staticmethod
    def _swatch_qss(c):
        return (f"QPushButton {{ background: {c}; border: 1px solid rgba(255,255,255,60); border-radius: 0; }}"
                f"QPushButton:hover {{ border: 1px solid #eff0f1; }}"
                f"QPushButton:checked {{ border: 2px solid #eff0f1; }}")

    def _open_emojis(self):
        # keep the button showing the current tool until an emoji is actually chosen
        self._tool_btn.get(self._tool, self.emoji).setChecked(True)
        from .emoji import Picker
        picker = Picker(self, "Click on the recording to place it · it fades out by itself")
        picker.chosen.connect(self.emoji_chosen)
        picker.open_at(self.emoji)

    def _pick_color(self):
        c = QColorDialog.getColor(self._color, self.window(), "Drawing color")
        if c.isValid():
            self.color_changed.emit(c)

    def wheelEvent(self, ev):
        d = ev.angleDelta().y()
        if d:
            self.width_step.emit(1 if d > 0 else -1)

    def set_state(self, tool, color, width, keep, emoji=None):
        """Show the overlay's current pen without emitting anything."""
        self._tool = tool
        if emoji:
            self.emoji.setText(emoji)
        b = self._tool_btn.get(tool)
        if b:
            b.setChecked(True)
        self._color = QColor(color)
        name = self._color.name().lower()
        for c, sw in self._swatches:
            sw.setChecked(c.lower() == name)
        custom = all(c.lower() != name for c, _ in self._swatches)
        self.custom.setStyleSheet(_BTN.replace("padding: 4px 9px", "padding: 4px 6px") +
                                  (f"QPushButton {{ border-bottom: 3px solid {name}; }}" if custom else ""))
        self.size.setText(f"{int(width)} px")
        self.keep.blockSignals(True)
        self.keep.setChecked(bool(keep))
        self.keep.blockSignals(False)


class Controls(QWidget):
    stop_clicked = pyqtSignal()
    pause_toggled = pyqtSignal(bool)
    draw_toggled = pyqtSignal(bool)

    def __init__(self, can_pause=True, can_draw=True):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        mark_overlay(self)
        self.setWindowTitle(TITLE)
        self.dot = _Dot()
        self.time = QLabel("00:00")
        f = self.time.font()
        f.setBold(True)
        f.setFeature(QFont.Tag("tnum"), 1)  # tabular digits: the timer doesn't wobble
        self.time.setFont(f)
        self.info = QLabel("")
        self.info.setStyleSheet("color: #9aa0a6;")
        self.time.setStyleSheet("color: #eff0f1;")

        self.pause = QPushButton("❚❚")
        self.pause.setCheckable(True)
        self.pause.setToolTip("Pause / resume  (snappie pause)")
        self.pause.toggled.connect(self._paused)
        self.draw = QPushButton("✎")
        self.draw.setCheckable(True)
        self.draw.setToolTip("Draw on the recording  (snappie draw)\n"
                             "1–8 colors · wheel width · Shift arrow · R box · Z undo · C clear · Esc done")
        self.draw.toggled.connect(self.draw_toggled)
        self.audio = QPushButton("♪")
        self.audio.setToolTip("Which apps' sound is recorded (changes apply right away)")
        self.audio.clicked.connect(self._audio_menu)
        self.audio.hide()
        self._audio_items = self._audio_set = None
        self.mic = QPushButton()
        self.mic.setCheckable(True)
        self.mic.setToolTip("Microphone on / off")
        ic = QIcon()
        ic.addPixmap(_mic_icon(QColor("#9aa0a6"), muted=True), QIcon.Mode.Normal, QIcon.State.Off)
        ic.addPixmap(_mic_icon(ACCENT_TEXT), QIcon.Mode.Normal, QIcon.State.On)
        self.mic.setIcon(ic)
        self.mic.setIconSize(QSize(18, 18))
        self.mic.hide()
        for b in (self.pause, self.draw, self.audio, self.mic):
            b.setStyleSheet(_BTN)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.pause.setVisible(can_pause)
        self.draw.setVisible(can_draw)

        self.stop = QPushButton("■")
        self.stop.setToolTip("Stop recording")
        self.stop.setCursor(Qt.CursorShape.PointingHandCursor)
        self.stop.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.stop.setStyleSheet(
            "QPushButton { background: #da4453; color: white; border: none; border-radius: 0;"
            " padding: 4px 10px; font-weight: bold; }"
            "QPushButton:hover { background: #e5606d; }"
            "QPushButton:disabled { background: #5a5f66; color: #c8c8c8; }")
        self.stop.clicked.connect(self._stop)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 6, 6, 6)
        lay.setSpacing(8)
        lay.addWidget(self.dot)
        lay.addWidget(self.time)
        lay.addWidget(self.info)
        lay.addSpacing(4)
        lay.addWidget(self.pause)
        lay.addWidget(self.draw)
        lay.addWidget(self.audio)
        lay.addWidget(self.mic)
        self.tools = DrawTools()
        self.tools.hide()
        lay.addWidget(self.tools)
        lay.addSpacing(4)
        lay.addWidget(self.stop)
        self._blink = QTimer(self, interval=600, timeout=self._toggle_dot)
        self._blink.start()
        self.adjustSize()
        self.setFixedSize(self.sizeHint())

    def _toggle_dot(self):
        self.dot.on = not self.dot.on
        self.dot.update()

    def _paused(self, on):
        self.dot.paused = on
        self.dot.update()
        self.pause.setText("▶" if on else "❚❚")
        self.pause_toggled.emit(on)

    def set_paused(self, on):
        """Reflect a pause started elsewhere (hotkey) without re-emitting."""
        self.pause.blockSignals(True)
        self.pause.setChecked(on)
        self.pause.blockSignals(False)
        self.dot.paused = on
        self.dot.update()
        self.pause.setText("▶" if on else "❚❚")

    def set_drawing(self, on):
        self.draw.blockSignals(True)
        self.draw.setChecked(on)
        self.draw.blockSignals(False)
        self.tools.setVisible(on)
        self.adjustSize()
        self.setFixedSize(self.sizeHint())

    def set_audio_menu(self, items, set_app, mic, set_mic):
        """items() -> [(app name, recorded?)]; set_app(name, on) when one is ticked.
        mic: whether the microphone starts on; set_mic(on) when it's toggled."""
        self._audio_items, self._audio_set = items, set_app
        self.audio.show()
        self.mic.setChecked(mic)
        self.mic.toggled.connect(set_mic)
        self.mic.show()
        self.adjustSize()
        self.setFixedSize(self.sizeHint())

    def _audio_menu(self):
        menu = QMenu(self)
        items = self._audio_items()
        if not items:
            menu.addAction("No app is playing sound").setEnabled(False)
        for name, on in items:
            act = menu.addAction(name)
            act.setCheckable(True)
            act.setChecked(on)
            act.toggled.connect(lambda checked, n=name: self._audio_set(n, checked))
        menu.aboutToHide.connect(menu.deleteLater)
        menu.popup(self.audio.mapToGlobal(self.audio.rect().bottomLeft()))

    def set_busy(self, busy):
        """While a segment is being finished/started (pause, resume): no double clicks."""
        self.pause.setEnabled(not busy)

    def _stop(self):
        self.stop.setEnabled(False)
        self.pause.setEnabled(False)
        self.draw.setEnabled(False)
        self.audio.setEnabled(False)
        self.mic.setEnabled(False)
        self.tools.setEnabled(False)
        self.stop.setText("…")
        self._blink.stop()
        self.stop_clicked.emit()

    def set_status(self, seconds, size):
        self.time.setText(f"{seconds // 60:02d}:{seconds % 60:02d}")
        if size:
            self.info.setText(f"{size[0]}×{size[1]}")
        self.setFixedSize(self.sizeHint())

    def paintEvent(self, _):
        QPainter(self).fillRect(self.rect(), QColor(0, 0, 0))

    def mousePressEvent(self, ev):
        # drag the bar anywhere (Wayland: the compositor moves it); KWin then stops auto-placing it
        if ev.button() == Qt.MouseButton.LeftButton and self.windowHandle():
            self.windowHandle().startSystemMove()


class SoundChooser(QWidget):
    """Asked before a recording starts: which apps it hears, and the microphone.  Placed under
    the area like the recording bar, and gone before the first frame."""
    TITLE = "Snappie — sound"
    accepted = pyqtSignal(dict, bool)   # {app name: on}, mic
    cancelled = pyqtSignal()

    def __init__(self, items, mic):
        """items: [(app name, on)] for the apps playing now."""
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        mark_overlay(self)
        self.setWindowTitle(self.TITLE)
        _dark(self)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 10, 10)
        lay.setSpacing(6)
        title = QLabel("Record sound from")
        f = title.font()
        f.setBold(True)
        title.setFont(f)
        lay.addWidget(title)
        self.boxes = {}
        for name, on in items:
            cb = QCheckBox(name)
            cb.setChecked(on)
            self.boxes[name] = cb
            lay.addWidget(cb)
        if not items:
            note = QLabel("No app is playing sound right now.\nApps that start later follow the settings.")
            note.setStyleSheet("color: #9aa0a6;")
            lay.addWidget(note)
        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet("background: rgba(255,255,255,45);")
        lay.addWidget(line)
        self.mic = QCheckBox("My microphone")
        self.mic.setIcon(QIcon(_mic_icon(QColor("#eff0f1"))))
        self.mic.setChecked(mic)
        lay.addWidget(self.mic)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setStyleSheet(_BTN)
        cancel.clicked.connect(self._cancel)
        record = QPushButton("● Record")
        record.setStyleSheet(
            "QPushButton { background: #da4453; color: white; border: none; border-radius: 0;"
            " padding: 4px 12px; font-weight: bold; } QPushButton:hover { background: #e5606d; }")
        record.clicked.connect(self._accept)
        record.setDefault(True)
        for b in (cancel, record):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            row.addWidget(b)
        lay.addLayout(row)
        record.setFocus()
        self._done = False
        self.adjustSize()
        self.setFixedSize(self.sizeHint())

    def _accept(self):
        if not self._done:
            self._done = True
            self.accepted.emit({n: cb.isChecked() for n, cb in self.boxes.items()}, self.mic.isChecked())

    def _cancel(self):
        if not self._done:
            self._done = True
            self.cancelled.emit()

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._accept()
        elif ev.key() == Qt.Key.Key_Escape:
            self._cancel()
        else:
            super().keyPressEvent(ev)

    def paintEvent(self, _):
        QPainter(self).fillRect(self.rect(), QColor(0, 0, 0))

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton and self.windowHandle():
            self.windowHandle().startSystemMove()
