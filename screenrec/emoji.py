"""Emoji stamps: the list, a picker popup, and rendering them as color images.

Used by live drawing on recordings (stamps that fade) and by the screenshot
editor (stamps that stay, as normal shapes).
"""

from PyQt6.QtCore import QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QImage, QPainter, QPen
from PyQt6.QtWidgets import QGridLayout, QLabel, QPushButton, QVBoxLayout, QWidget

FONT = "Noto Color Emoji"

EMOJIS = ("😂 🤣 😍 😎 🤯 😱 😭 😡 🥳 🤔 😏 🙄 😴 🥲 😬 🫠 "
          "🤡 💀 👻 🤖 👀 🗿 🐐 🐛 👍 👎 👏 🙌 🙏 💪 🤝 🫡 "
          "✌️ 🤌 👉 👈 👆 👇 ❤️ 💔 🔥 ✨ ⭐ 💯 ✅ ❌ ⚠️ ❓ "
          "❗ 💡 🎉 🎯 🚀 💥 💣 🏆 💰 ⏰ 📌 🔍 ☕ 🍕 🍿 🤓").split()

_cache = {}


def image(emoji, px=160):
    """The emoji drawn in color on a transparent px × px image (cached). Callers scale it;
    ask for at least the size it will be shown at in device pixels to keep it sharp."""
    bucket = 160 if px <= 160 else 320 if px <= 320 else 640
    key = (emoji, bucket)
    img = _cache.get(key)
    if img is None:
        img = QImage(bucket, bucket, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(0)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        f = QFont(FONT)
        f.setPixelSize(int(bucket * 0.8))
        p.setFont(f)
        p.drawText(QRectF(0, 0, bucket, bucket), Qt.AlignmentFlag.AlignCenter, emoji)
        p.end()
        _cache[key] = img
    return img


def recent():
    from . import settings
    return [e for e in (settings.get("emoji.recent") or []) if isinstance(e, str)]


def remember(e, keep=10):
    from . import settings
    settings.set("emoji.recent", ([e] + [x for x in recent() if x != e])[:keep])


class Picker(QWidget):
    """Popup grid of emojis (recently used first). Emits chosen(emoji) and closes."""
    chosen = pyqtSignal(str)
    COLS = 10

    def __init__(self, parent, hint="Click to place it"):
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)
        rec = recent()[:self.COLS]
        if rec:
            lay.addWidget(self._label("Recent"))
            lay.addLayout(self._grid(rec))
            lay.addWidget(self._label("All"))
        lay.addLayout(self._grid(EMOJIS))
        if hint:
            lay.addWidget(self._label(hint))

    def _label(self, text):
        lbl = QLabel(text)
        lbl.setStyleSheet("color: #9aa0a6; font-size: 11px;")
        return lbl

    def _grid(self, items):
        g = QGridLayout()
        g.setSpacing(2)
        g.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)  # short rows line up
        f = QFont(FONT)
        f.setPixelSize(22)
        for i, e in enumerate(items):
            b = QPushButton(e)
            b.setFont(f)
            b.setFixedSize(36, 36)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.setStyleSheet("QPushButton { background: transparent; border: none; border-radius: 0; }"
                            "QPushButton:hover { background: rgba(255,255,255,40); }")
            b.clicked.connect(lambda _=False, em=e: self._pick(em))
            g.addWidget(b, i // self.COLS, i % self.COLS)
        return g

    def _pick(self, e):
        remember(e, self.COLS)
        self.chosen.emit(e)
        self.close()

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0))
        p.setPen(QPen(QColor("#3daee9"), 1))
        p.drawRect(self.rect().adjusted(0, 0, -1, -1))

    def open_at(self, button):
        """Show under `button` (Wayland: positioned relative to its window; the compositor
        flips it up when there's no room below)."""
        self.adjustSize()
        self.move(button.mapToGlobal(button.rect().bottomLeft()))
        self.show()


def button_font(px=16):
    f = QFont(FONT)
    f.setPixelSize(px)
    return f
