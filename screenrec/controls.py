"""Small floating recording bar (timer + stop). KWin places it, see kwin.place_controls."""

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

TITLE = "screenrec — recording"


class _Dot(QWidget):
    def __init__(self):
        super().__init__()
        self.setFixedSize(12, 12)
        self.on = True

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#da4453") if self.on else QColor(218, 68, 83, 70))
        p.drawEllipse(1, 1, 10, 10)


class Controls(QWidget):
    stop_clicked = pyqtSignal()

    def __init__(self):
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
        self.stop = QPushButton("■")
        self.stop.setToolTip("Stop recording")
        self.stop.setCursor(Qt.CursorShape.PointingHandCursor)
        self.stop.setStyleSheet(
            "QPushButton { background: #da4453; color: white; border: none; border-radius: 0;"
            " padding: 4px 10px; font-weight: bold; }"
            "QPushButton:hover { background: #e5606d; }"
            "QPushButton:disabled { background: #5a5f66; color: #c8c8c8; }")
        self.stop.clicked.connect(self._stop)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 8, 8, 8)
        lay.setSpacing(10)
        lay.addWidget(self.dot)
        lay.addWidget(self.time)
        lay.addWidget(self.info)
        lay.addWidget(self.stop)
        self._blink = QTimer(self, interval=600, timeout=self._toggle_dot)
        self._blink.start()
        self.adjustSize()
        self.setFixedSize(self.sizeHint())

    def _toggle_dot(self):
        self.dot.on = not self.dot.on
        self.dot.update()

    def _stop(self):
        self.stop.setEnabled(False)
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
