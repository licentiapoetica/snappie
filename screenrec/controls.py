"""Small floating recording bar (timer, pause, draw, stop). KWin places it, see kwin.place."""

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from .style import RECORD

TITLE = "screenrec — recording"

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


class Controls(QWidget):
    stop_clicked = pyqtSignal()
    pause_toggled = pyqtSignal(bool)
    draw_toggled = pyqtSignal(bool)

    def __init__(self, can_pause=True, can_draw=True):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
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
        self.pause.setToolTip("Pause / resume  (screenrec pause)")
        self.pause.toggled.connect(self._paused)
        self.draw = QPushButton("✎")
        self.draw.setCheckable(True)
        self.draw.setToolTip("Draw on the recording  (screenrec draw)\n"
                             "1–8 colors · wheel width · Shift arrow · R box · Z undo · C clear · Esc done")
        self.draw.toggled.connect(self.draw_toggled)
        for b in (self.pause, self.draw):
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

    def set_busy(self, busy):
        """While a segment is being finished/started (pause, resume): no double clicks."""
        self.pause.setEnabled(not busy)

    def _stop(self):
        self.stop.setEnabled(False)
        self.pause.setEnabled(False)
        self.draw.setEnabled(False)
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
