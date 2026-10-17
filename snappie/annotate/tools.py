"""Tool and action tables, plus small vector icons painted with QPainter.

The overlay's black panel uses the painted icons: they're white on black and
accent-text when checked whatever icon theme is installed (a light theme's
dark glyphs would vanish on the black panel).  The editor window prefers the
icon theme and falls back to the painted ones.
"""

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt
from PyQt6.QtGui import (QColor, QFont, QGuiApplication, QIcon, QIconEngine, QImage, QPainter, QPainterPath,
                         QPalette, QPen, QPixmap, QPolygonF)

from .. import style

# id, label, key, theme icon, status hint
TOOLS = [
    ("select", "Select", "V", "edit-select", "Click a shape to select it, drag to move it, drag a handle to resize."
     " Delete removes it, double-click text to edit."),
    ("arrow", "Arrow", "A", "draw-arrow", "Drag to draw an arrow. Shift: 45° steps."),
    ("line", "Line", "L", "draw-line", "Drag to draw a line. Shift: 45° steps."),
    ("rect", "Rectangle", "R", "draw-rectangle", "Drag to draw a rectangle. Shift: square."),
    ("ellipse", "Ellipse", "E", "draw-ellipse", "Drag to draw an ellipse. Shift: circle."),
    ("text", "Text", "T", "draw-text", "Click to type. Enter: new line, Esc or click outside: done."),
    ("pen", "Pen", "P", "draw-freehand", "Draw freehand. Shift: straight line."),
    ("highlighter", "Highlighter", "H", "draw-highlight", "Highlight freehand; text underneath stays readable."),
    ("marker", "Marker", "M", "", "Drag a highlight box over something."),
    ("blur", "Blur", "B", "blurfx", "Drag over the area to blur."),
    ("pixelate", "Pixelate", "X", "pixelate", "Drag over the area to pixelate."),
    ("redact", "Redact", "D", "", "Drag a solid box over the area to hide."),
    ("step", "Step number", "N", "draw-number", "Click to place the next number; drag from it to add a pointer."),
    ("emoji", "Emoji", "J", "face-smile", "Click to place the emoji (pick it in the bar); drag while placing to size it."),
    ("magnifier", "Magnifier", "G", "zoom-in", "Drag from the spot to magnify to where the lens should go."),
    ("crop", "Crop", "C", "transform-crop", "Drag the edges, or draw a new area. Enter: done."),
]
TOOL_KEYS = {key: tid for tid, _, key, _, _ in TOOLS}
TOOL_LABELS = {tid: label for tid, label, _, _, _ in TOOLS}
TOOL_HINTS = {tid: hint for tid, _, _, _, hint in TOOLS}

# id, label, shortcut, theme icon
ACTIONS = [
    ("copy", "Copy", "Ctrl+C", "edit-copy"),
    ("save", "Save", "Ctrl+S", "document-save"),
    ("save_as", "Save as…", "Ctrl+Shift+S", "document-save-as"),
    ("upload", "Upload", "Ctrl+U", "cloud-upload"),
    ("pin", "Pin to screen", "Ctrl+P", "pin"),
    ("ocr", "Copy text (OCR)", "Ctrl+T", "scanner"),
    ("edit", "Open in editor", "Ctrl+E", "document-edit"),
    ("confirm", "Done", "Enter", "dialog-ok-apply"),
    ("cancel", "Cancel", "Esc", "dialog-cancel"),
]
ACTION_LABELS = {a: label for a, label, _, _ in ACTIONS}
ACTION_KEYS = {a: key for a, _, key, _ in ACTIONS}


def _pen(color, w=1.8):
    return QPen(color, w, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)


def _poly(*pts):
    return QPolygonF([QPointF(x, y) for x, y in pts])


def _draw(p, name, c):
    """Draw icon `name` in a 24×24 box with colour c."""
    faint = QColor(c)
    faint.setAlphaF(0.45)
    p.setPen(_pen(c))
    p.setBrush(Qt.BrushStyle.NoBrush)
    if name == "select":
        p.drawPolygon(_poly((7, 3.5), (7, 18.5), (10.6, 15.2), (13.4, 21), (15.8, 19.9), (13.1, 14.2), (18, 14)))
    elif name == "arrow":
        p.drawLine(QPointF(5, 19), QPointF(15.5, 8.5))
        p.setBrush(c)
        p.drawPolygon(_poly((19.5, 4.5), (17.8, 12.2), (11.8, 6.2)))
    elif name == "line":
        p.drawLine(QPointF(5, 19), QPointF(19, 5))
    elif name == "rect":
        p.drawRect(QRectF(4, 6, 16, 12))
    elif name == "ellipse":
        p.drawEllipse(QRectF(3.5, 5.5, 17, 13))
    elif name == "text":
        p.setPen(_pen(c, 2.2))
        p.drawLine(QPointF(6, 5.5), QPointF(18, 5.5))
        p.drawLine(QPointF(12, 5.5), QPointF(12, 19.5))
        p.setPen(_pen(c, 1.4))
        p.drawLine(QPointF(9.5, 19.5), QPointF(14.5, 19.5))
    elif name == "pen":
        path = QPainterPath(QPointF(4, 17))
        path.cubicTo(7, 6, 10, 6, 11, 12)
        path.cubicTo(12, 18, 16, 19, 20, 8)
        p.drawPath(path)
    elif name == "highlighter":
        path = QPainterPath(QPointF(5, 16))
        path.cubicTo(9, 8, 13, 18, 19, 9)
        p.setPen(QPen(faint, 6.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawPath(path)
        p.setPen(_pen(c, 1.6))
        p.drawPath(path)
    elif name == "marker":
        p.setPen(_pen(c, 1.4))
        for y, x2 in ((6, 19), (12, 20), (18, 15)):
            p.drawLine(QPointF(4, y), QPointF(x2, y))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(faint)
        p.drawRect(QRectF(2.5, 8.5, 19, 7))
    elif name == "blur":
        path = QPainterPath(QPointF(12, 3))
        path.cubicTo(12, 3, 5.5, 10, 5.5, 14.5)
        path.arcTo(QRectF(5.5, 8, 13, 13), 180, 180)
        path.cubicTo(18.5, 10, 12, 3, 12, 3)
        p.drawPath(path)
        p.setPen(_pen(faint, 1.6))
        p.drawArc(QRectF(8.5, 11, 7, 7), 200 * 16, 70 * 16)
    elif name == "pixelate":
        p.setPen(Qt.PenStyle.NoPen)
        for i in range(3):
            for j in range(3):
                a = (1.0, 0.35, 0.7)[(i + 2 * j) % 3]
                q = QColor(c)
                q.setAlphaF(a)
                p.setBrush(q)
                p.drawRect(QRectF(4 + i * 5.5, 4 + j * 5.5, 5, 5))
    elif name == "redact":
        p.setPen(_pen(c, 1.4))
        p.drawLine(QPointF(4, 6), QPointF(20, 6))
        p.drawLine(QPointF(4, 18.5), QPointF(14, 18.5))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        p.drawRect(QRectF(4, 9.5, 16, 5.5))
    elif name == "step":
        p.drawEllipse(QPointF(12, 12), 8.5, 8.5)
        f = QFont()
        f.setPixelSize(11)
        f.setBold(True)
        num = QPainterPath()
        num.addText(0, 0, f, "1")
        br = num.boundingRect()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        p.drawPath(num.translated(12 - br.center().x(), 12 - br.center().y()))
    elif name == "emoji":
        p.drawEllipse(QPointF(12, 12), 8.5, 8.5)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        p.drawEllipse(QPointF(9, 10), 1.3, 1.6)
        p.drawEllipse(QPointF(15, 10), 1.3, 1.6)
        p.setPen(_pen(c, 1.6))
        p.setBrush(Qt.BrushStyle.NoBrush)
        smile = QPainterPath(QPointF(8, 13.5))
        smile.quadTo(QPointF(12, 18), QPointF(16, 13.5))
        p.drawPath(smile)
    elif name == "magnifier":
        p.drawEllipse(QPointF(10, 10), 6, 6)
        p.setPen(_pen(c, 2.8))
        p.drawLine(QPointF(14.6, 14.6), QPointF(19.5, 19.5))
    elif name == "crop":
        p.drawPolyline(_poly((7, 2.5), (7, 17), (21.5, 17)))
        p.drawPolyline(_poly((2.5, 7), (17, 7), (17, 21.5)))
    elif name in ("undo", "redo"):
        if name == "redo":
            p.translate(24, 0)
            p.scale(-1, 1)
        path = QPainterPath(QPointF(19, 18.5))
        path.cubicTo(19, 11, 14, 8.5, 5.5, 9)
        p.drawPath(path)
        p.drawPolyline(_poly((9.5, 4.5), (5, 9), (9.5, 13.5)))
    elif name == "copy":
        p.drawPolyline(_poly((8.5, 7.5), (8.5, 3.5), (20, 3.5), (20, 16), (16, 16)))
        p.drawRect(QRectF(4, 8, 12, 12.5))
    elif name in ("save", "save_as"):
        if name == "save_as":
            p.translate(-1.5, -1.5)
            p.scale(0.85, 0.85)
        p.drawPolygon(_poly((4, 4), (16.5, 4), (20, 7.5), (20, 20), (4, 20)))
        p.drawPolyline(_poly((8, 4), (8, 9), (15, 9), (15, 4)))
        p.drawRect(QRectF(7.5, 13.5, 9, 6.5))
        if name == "save_as":
            p.resetTransform()
            p.setPen(_pen(c, 2.4))
            p.drawLine(QPointF(15.5, 21.5), QPointF(21.5, 15.5))
    elif name == "upload":
        p.drawLine(QPointF(12, 15.5), QPointF(12, 4))
        p.drawPolyline(_poly((7, 9), (12, 4), (17, 9)))
        p.drawPolyline(_poly((4, 15), (4, 20), (20, 20), (20, 15)))
    elif name == "pin":
        p.drawPolygon(_poly((9, 3.5), (15, 3.5), (14, 9.5), (17.5, 13.5), (6.5, 13.5), (10, 9.5)))
        p.drawLine(QPointF(12, 13.5), QPointF(12, 21))
    elif name == "ocr":
        for pts in (((4, 8), (4, 4), (8, 4)), ((16, 4), (20, 4), (20, 8)),
                    ((20, 16), (20, 20), (16, 20)), ((8, 20), (4, 20), (4, 16))):
            p.drawPolyline(_poly(*pts))
        p.drawLine(QPointF(8.5, 8.5), QPointF(15.5, 8.5))
        p.drawLine(QPointF(12, 8.5), QPointF(12, 16))
    elif name == "edit":
        p.drawPolyline(_poly((10.5, 5), (5, 5), (5, 19), (19, 19), (19, 13.5)))
        p.drawLine(QPointF(11.5, 12.5), QPointF(19.5, 4.5))
        p.drawPolyline(_poly((13.5, 4.5), (19.5, 4.5), (19.5, 10.5)))
    elif name == "confirm":
        p.setPen(_pen(c, 2.4))
        p.drawPolyline(_poly((5, 12.5), (10, 17.5), (19.5, 7)))
    elif name == "cancel":
        p.setPen(_pen(c, 2.2))
        p.drawLine(QPointF(6.5, 6.5), QPointF(17.5, 17.5))
        p.drawLine(QPointF(17.5, 6.5), QPointF(6.5, 17.5))
    elif name == "fill":
        p.setBrush(faint)
        p.drawRect(QRectF(4.5, 5.5, 15, 13))
    elif name == "text_bg":
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        p.drawRect(QRectF(3, 4, 18, 16))
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
        p.setPen(_pen(c, 2.2))
        p.drawLine(QPointF(8, 8.5), QPointF(16, 8.5))
        p.drawLine(QPointF(12, 8.5), QPointF(12, 16.5))
    elif name in ("minus", "plus"):
        p.drawLine(QPointF(6, 12), QPointF(18, 12))
        if name == "plus":
            p.drawLine(QPointF(12, 6), QPointF(12, 18))
    elif name == "more":
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        for x in (6, 12, 18):
            p.drawEllipse(QPointF(x, 12), 1.6, 1.6)


class PaintedIcon(QIconEngine):
    """Vector icon; crisp at any size and scale factor."""

    def __init__(self, name, colors=None):
        super().__init__()
        self.name = name
        self.colors = colors        # {"normal", "on", "disabled"} or None = application palette
        self._cache = {}

    def clone(self):
        return PaintedIcon(self.name, self.colors)

    def _color(self, mode, state):
        if self.colors:
            if mode == QIcon.Mode.Disabled:
                return QColor(self.colors["disabled"])
            return QColor(self.colors["on" if state == QIcon.State.On else "normal"])
        pal = QGuiApplication.palette()
        if mode == QIcon.Mode.Disabled:
            return pal.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText)
        if mode == QIcon.Mode.Selected:
            return pal.color(QPalette.ColorRole.HighlightedText)
        return pal.color(QPalette.ColorRole.ButtonText)

    def image(self, size, mode, state, scale=1.0):
        key = (size.width(), size.height(), mode, state, scale,
               None if self.colors else QGuiApplication.palette().cacheKey())
        img = self._cache.get(key)
        if img is None:
            img = self._cache[key] = self._render(size, mode, state, scale)
        return img

    def _render(self, size, mode, state, scale):
        px = QSize(max(1, round(size.width() * scale)), max(1, round(size.height() * scale)))
        img = QImage(px, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(Qt.GlobalColor.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        s = min(px.width(), px.height()) / 24.0
        p.translate((px.width() - 24 * s) / 2, (px.height() - 24 * s) / 2)
        p.scale(s, s)
        _draw(p, self.name, self._color(mode, state))
        p.end()
        img.setDevicePixelRatio(scale)
        return img

    def paint(self, painter, rect, mode, state):
        scale = painter.device().devicePixelRatioF() if painter.device() else 1.0
        painter.drawImage(QRectF(rect), self.image(rect.size(), mode, state, scale))

    def pixmap(self, size, mode, state):
        return QPixmap.fromImage(self.image(size, mode, state))

    def scaledPixmap(self, size, mode, state, scale):
        return QPixmap.fromImage(self.image(size, mode, state, scale))


PANEL_COLORS = {"normal": style.TEXT, "on": style.ACCENT_TEXT, "disabled": QColor("#5a5f66")}


def panel_icon(name):
    """White-on-black icon for the overlay panel."""
    return QIcon(PaintedIcon(name, PANEL_COLORS))


def theme_icon(theme, name):
    """Theme icon if the theme has it, else the painted one."""
    if theme and QIcon.hasThemeIcon(theme):
        return QIcon.fromTheme(theme)
    return QIcon(PaintedIcon(name))


def swatch_icon(color, size=18, selected=False, custom=False):
    """Square colour chip for palettes."""
    pm = QImage(size * 2, size * 2, QImage.Format.Format_ARGB32_Premultiplied)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    r = QRectF(3, 3, size * 2 - 6, size * 2 - 6)
    if custom:   # rainbow-ish chip with a plus
        from PyQt6.QtGui import QConicalGradient
        g = QConicalGradient(r.center(), 0)
        for i, col in enumerate(("#e93d3d", "#fdbc4b", "#27ae60", "#3daee9", "#9b59b6", "#e93d3d")):
            g.setColorAt(i / 5, QColor(col))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(g)
        p.drawRect(r)
        p.setPen(QPen(QColor("#000000"), 3.2, cap=Qt.PenCapStyle.RoundCap))
        c = r.center()
        p.drawLine(QPointF(c.x() - 6, c.y()), QPointF(c.x() + 6, c.y()))
        p.drawLine(QPointF(c.x(), c.y() - 6), QPointF(c.x(), c.y() + 6))
    else:
        p.setPen(QPen(QColor(255, 255, 255, 70), 2))
        p.setBrush(QColor(color))
        p.drawRect(r.adjusted(2, 2, -2, -2))
    if selected:
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor("#ffffff"), 3))
        p.drawRect(r.adjusted(-0.5, -0.5, 0.5, 0.5))
    p.end()
    pm.setDevicePixelRatio(2)
    return QIcon(QPixmap.fromImage(pm))

