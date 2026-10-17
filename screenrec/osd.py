"""Small on-screen panels: delay countdown, scrolling-capture bar."""

from PyQt6.QtCore import QRect, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from .style import ACCENT, MUTED, PANEL_BG, TEXT


class Countdown(QWidget):
    """Big seconds counter in the middle of a screen. done() fires after it has hidden itself
    (with a short pause so it's gone from the screen before anything is captured)."""
    TITLE = "screenrec — countdown"
    done = pyqtSignal()
    cancelled = pyqtSignal()

    def __init__(self, seconds):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setWindowTitle(self.TITLE)
        self.left = max(1, int(round(seconds)))
        self.setFixedSize(150, 150)
        self._timer = QTimer(self, interval=1000, timeout=self._tick)

    def start(self, kwin, screen_geo):
        self.show()
        g = screen_geo
        kwin.place(self.TITLE, (g.x() + (g.width() - 150) // 2, g.y() + (g.height() - 150) // 2, 150, 150),
                   mode="exact")
        self._kwin = kwin
        self._timer.start()

    def _tick(self):
        self.left -= 1
        if self.left > 0:
            self.update()
            return
        self._timer.stop()
        self._close()
        QTimer.singleShot(250, self.done.emit)

    def cancel(self):
        self._timer.stop()
        self._close()
        self.cancelled.emit()

    def _close(self):
        self._kwin.unplace(self.TITLE)
        self.hide()
        self.deleteLater()

    def mousePressEvent(self, ev):
        self.cancel()

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), PANEL_BG)
        p.setPen(ACCENT)
        p.drawRect(self.rect().adjusted(0, 0, -1, -1))
        f = QFont(self.font())
        f.setPixelSize(72)
        f.setBold(True)
        p.setFont(f)
        p.setPen(TEXT)
        p.drawText(self.rect().adjusted(0, -12, 0, -12), Qt.AlignmentFlag.AlignCenter, str(self.left))
        f.setPixelSize(12)
        f.setBold(False)
        p.setFont(f)
        p.setPen(MUTED)
        p.drawText(QRect(0, 108, 150, 30), Qt.AlignmentFlag.AlignCenter, "click to cancel")


class ScrollBarOsd(QWidget):
    """'Scrolling capture · 1234 px  [Stop]' shown under the area being captured."""
    TITLE = "screenrec — scrolling capture"
    stop_clicked = pyqtSignal()

    def __init__(self):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setWindowTitle(self.TITLE)
        self.label = QLabel("Scrolling capture…")
        self.label.setStyleSheet("color: #eff0f1; font-weight: bold;")
        self.info = QLabel("")
        self.info.setStyleSheet("color: #9aa0a6;")
        f = self.info.font()
        f.setFeature(QFont.Tag("tnum"), 1)
        self.info.setFont(f)
        self.stop = QPushButton("Done")
        self.stop.setCursor(Qt.CursorShape.PointingHandCursor)
        self.stop.setStyleSheet("QPushButton { background: #3daee9; color: #0b1a24; border: none;"
                                " border-radius: 0; padding: 4px 12px; font-weight: bold; }"
                                "QPushButton:hover { background: #5cbcee; }")
        self.stop.clicked.connect(self.stop_clicked)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 6, 6, 6)
        lay.setSpacing(10)
        lay.addWidget(self.label)
        lay.addWidget(self.info)
        lay.addWidget(self.stop)
        self.setFixedSize(self.sizeHint().expandedTo(self.sizeHint()))

    def set_height(self, px):
        self.info.setText(f"{px} px")
        self.setFixedSize(self.sizeHint())

    def set_text(self, text):
        self.label.setText(text)
        self.setFixedSize(self.sizeHint())

    def paintEvent(self, _):
        QPainter(self).fillRect(self.rect(), QColor(0, 0, 0))
