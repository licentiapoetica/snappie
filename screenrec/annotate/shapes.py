"""Annotation shapes: plain data (floats, strings, lists) plus painting,
hit-testing and handles.

Coordinates are logical pixels of the document's base image (image pixels /
devicePixelRatio).  Keeping only plain data makes undo a deepcopy and lets the
same shape paint on screen (any zoom) and into the full-resolution export.
Effects that read the picture (blur, pixelate, magnifier) work on the base
image's device pixels and are drawn back pixel-aligned, so they stay crisp at
fractional scaling.
"""

import math
from collections import OrderedDict

from PyQt6.QtCore import QLineF, QPointF, QRectF, Qt
from PyQt6.QtGui import (QColor, QFont, QFontMetricsF, QImage, QPainter, QPainterPath,
                         QPainterPathStroker, QPen, QPolygonF)

SHADOW = QColor(0, 0, 0, 70)
BOX_HANDLES = ("tl", "t", "tr", "r", "br", "b", "bl", "l")


def contrast(color):
    """White or near-black, whichever reads better on `color`."""
    c = QColor(color)
    lum = 0.299 * c.redF() + 0.587 * c.greenF() + 0.114 * c.blueF()
    return QColor("#1b1e20") if lum > 0.62 else QColor("#ffffff")


def snap45(origin, pt):
    """pt moved onto the nearest 45° ray from origin."""
    dx, dy = pt.x() - origin.x(), pt.y() - origin.y()
    length = math.hypot(dx, dy)
    a = round(math.atan2(dy, dx) / (math.pi / 4)) * (math.pi / 4)
    return QPointF(origin.x() + length * math.cos(a), origin.y() + length * math.sin(a))


def seg_dist(p, a, b):
    ax, ay, bx, by = a.x(), a.y(), b.x(), b.y()
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    t = 0.0 if L == 0 else max(0.0, min(1.0, ((p.x() - ax) * dx + (p.y() - ay) * dy) / L))
    return math.hypot(p.x() - ax - t * dx, p.y() - ay - t * dy)


class _Cache(OrderedDict):
    """Tiny LRU for expensive effect images (blur while dragging other shapes, …)."""

    def get_or(self, key, make):
        if key in self:
            self.move_to_end(key)
            return self[key]
        v = self[key] = make()
        while len(self) > 24:
            self.popitem(last=False)
        return v


_cache = _Cache()


class Context:
    """What a shape may need while painting."""

    def __init__(self, base, numbers=None, smooth=True):
        self.base = base                       # QImage, device pixels, dpr set
        self.dpr = base.devicePixelRatio()
        self.numbers = numbers or {}           # id(step shape) -> number
        self.smooth = smooth                   # smooth image scaling (False when zoomed in)

    def device_rect(self, r):
        """Logical QRectF -> integer device-pixel rect clipped to the base image (x, y, w, h)."""
        d = self.dpr
        x0, y0 = max(0, math.floor(r.left() * d)), max(0, math.floor(r.top() * d))
        x1 = min(self.base.width(), math.ceil(r.right() * d))
        y1 = min(self.base.height(), math.ceil(r.bottom() * d))
        return x0, y0, x1 - x0, y1 - y0


class Shape:
    kind = ""
    props = ("color", "width")
    handled = True          # has resize handles (else only a dashed outline when selected)

    def __init__(self, color="#e93d3d", width=4):
        self.color = color
        self.width = width

    # -- overridden --
    def bounds(self):
        return QRectF()

    def paint(self, p, ctx):
        pass

    def hit(self, pt, tol):
        """None or the part that was hit ("body" for most shapes)."""
        return None

    def handles(self):
        return []

    def drag_handle(self, key, pt, shift):
        pass

    def translate(self, dx, dy):
        pass

    def move_part(self, part, dx, dy):
        self.translate(dx, dy)

    def drag_create(self, start, pt, shift):
        pass

    def too_small(self, tol):
        return False

    def finish_click(self):
        """Called when creation ends (fix up shapes made by a click or a tiny drag)."""

    # -- helpers --
    def qcolor(self, alpha=None):
        c = QColor(self.color)
        if alpha is not None:
            c.setAlpha(alpha)
        return c


# ---------------------------------------------------------------- two points

class _TwoPoint(Shape):
    def __init__(self, x1, y1, color="#e93d3d", width=4):
        super().__init__(color, width)
        self.x1, self.y1, self.x2, self.y2 = x1, y1, x1, y1

    @property
    def p1(self):
        return QPointF(self.x1, self.y1)

    @property
    def p2(self):
        return QPointF(self.x2, self.y2)

    def bounds(self):
        m = self.width * 3 + 4
        return QRectF(self.p1, self.p2).normalized().adjusted(-m, -m, m, m)

    def hit(self, pt, tol):
        return "body" if seg_dist(pt, self.p1, self.p2) <= tol + self.width / 2 else None

    def handles(self):
        return [("p1", self.p1), ("p2", self.p2)]

    def drag_handle(self, key, pt, shift):
        if key == "p1":
            pt = snap45(self.p2, pt) if shift else pt
            self.x1, self.y1 = pt.x(), pt.y()
        else:
            pt = snap45(self.p1, pt) if shift else pt
            self.x2, self.y2 = pt.x(), pt.y()

    def drag_create(self, start, pt, shift):
        self.drag_handle("p2", pt, shift)

    def translate(self, dx, dy):
        self.x1 += dx
        self.x2 += dx
        self.y1 += dy
        self.y2 += dy

    def too_small(self, tol):
        return QLineF(self.p1, self.p2).length() < tol


class Line(_TwoPoint):
    kind = "line"

    def paint(self, p, ctx):
        for color, off in ((SHADOW, 1.0), (self.qcolor(), 0)):
            pen = QPen(color, self.width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.drawLine(self.p1 + QPointF(off * 0.6, off), self.p2 + QPointF(off * 0.6, off))


class Arrow(_TwoPoint):
    kind = "arrow"

    def geometry(self):
        """(shaft end, head polygon) — the head grows with the stroke width."""
        a, b = self.p1, self.p2
        dx, dy = b.x() - a.x(), b.y() - a.y()
        length = math.hypot(dx, dy) or 1e-6
        ux, uy = dx / length, dy / length
        head = 9 + self.width * 3.0
        head = min(head, length * 0.85)
        half = head * 0.58
        back = head * 0.78                        # concave back: looks sharper than a triangle
        base = QPointF(b.x() - ux * head, b.y() - uy * head)
        nx, ny = -uy, ux
        poly = QPolygonF([b, QPointF(base.x() + nx * half, base.y() + ny * half),
                          QPointF(b.x() - ux * back, b.y() - uy * back),
                          QPointF(base.x() - nx * half, base.y() - ny * half)])
        shaft_end = QPointF(b.x() - ux * back * 0.9, b.y() - uy * back * 0.9)
        return shaft_end, poly

    def paint(self, p, ctx):
        end, poly = self.geometry()
        for color, off in ((SHADOW, 1.0), (self.qcolor(), 0)):
            p.save()
            p.translate(off * 0.6, off)
            p.setPen(QPen(color, self.width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(self.p1, end)
            p.setPen(QPen(color, max(1.0, self.width * 0.35), Qt.PenStyle.SolidLine,
                          Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            p.setBrush(color)
            p.drawPolygon(poly)
            p.restore()

    def hit(self, pt, tol):
        if super().hit(pt, tol):
            return "body"
        return "body" if self.geometry()[1].containsPoint(pt, Qt.FillRule.OddEvenFill) else None


# ---------------------------------------------------------------- boxes

class _Box(Shape):
    filled_hit = True        # clicking inside selects it

    def __init__(self, x1, y1, color="#e93d3d", width=4):
        super().__init__(color, width)
        self.x1, self.y1, self.x2, self.y2 = x1, y1, x1, y1

    def rect(self):
        return QRectF(QPointF(self.x1, self.y1), QPointF(self.x2, self.y2)).normalized()

    def bounds(self):
        m = self.width / 2 + 2
        return self.rect().adjusted(-m, -m, m, m)

    def handles(self):
        r = self.rect()
        c = r.center()
        return [("tl", r.topLeft()), ("t", QPointF(c.x(), r.top())), ("tr", r.topRight()),
                ("r", QPointF(r.right(), c.y())), ("br", r.bottomRight()),
                ("b", QPointF(c.x(), r.bottom())), ("bl", r.bottomLeft()), ("l", QPointF(r.left(), c.y()))]

    def drag_handle(self, key, pt, shift):
        r = self.rect()
        x1, y1, x2, y2 = r.left(), r.top(), r.right(), r.bottom()
        if "l" in key:
            x1 = pt.x()
        if "r" in key:
            x2 = pt.x()
        if "t" in key:
            y1 = pt.y()
        if "b" in key:
            y2 = pt.y()
        if shift and len(key) == 2:     # corner: keep it square, opposite corner fixed
            ax, ay = (x2 if "l" in key else x1), (y2 if "t" in key else y1)
            mx, my = (x1 if "l" in key else x2), (y1 if "t" in key else y2)
            s = max(abs(mx - ax), abs(my - ay))
            mx = ax + math.copysign(s, mx - ax)
            my = ay + math.copysign(s, my - ay)
            x1, x2 = (mx, ax) if "l" in key else (ax, mx)
            y1, y2 = (my, ay) if "t" in key else (ay, my)
        self.x1, self.y1, self.x2, self.y2 = x1, y1, x2, y2

    def drag_create(self, start, pt, shift):
        x, y = pt.x(), pt.y()
        if shift:
            s = max(abs(x - start.x()), abs(y - start.y()))
            x = start.x() + math.copysign(s, x - start.x())
            y = start.y() + math.copysign(s, y - start.y())
        self.x2, self.y2 = x, y

    def translate(self, dx, dy):
        self.x1 += dx
        self.x2 += dx
        self.y1 += dy
        self.y2 += dy

    def too_small(self, tol):
        r = self.rect()
        return r.width() < tol and r.height() < tol or r.width() < 2 or r.height() < 2

    def hit(self, pt, tol):
        r = self.rect()
        if self.filled_hit:
            return "body" if r.adjusted(-tol, -tol, tol, tol).contains(pt) else None
        outer = r.adjusted(-tol - self.width / 2, -tol - self.width / 2, tol + self.width / 2, tol + self.width / 2)
        inner = r.adjusted(tol + self.width / 2, tol + self.width / 2, -tol - self.width / 2, -tol - self.width / 2)
        return "body" if outer.contains(pt) and not (inner.isValid() and inner.contains(pt)) else None


class Rect(_Box):
    kind = "rect"
    props = ("color", "width", "fill")

    def __init__(self, x1, y1, color="#e93d3d", width=4, fill=False):
        super().__init__(x1, y1, color, width)
        self.fill = fill

    @property
    def filled_hit(self):
        return self.fill

    def _draw(self, p, r):
        p.drawRect(r)

    def paint(self, p, ctx):
        r = self.rect()
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(SHADOW, self.width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.SquareCap,
                      Qt.PenJoinStyle.MiterJoin))
        if not self.fill:
            self._draw(p, r.translated(0.6, 1.0))
        p.setPen(QPen(self.qcolor(), self.width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.SquareCap,
                      Qt.PenJoinStyle.MiterJoin))
        if self.fill:
            p.setBrush(self.qcolor(80))
        self._draw(p, r)


class Ellipse(Rect):
    kind = "ellipse"

    def _draw(self, p, r):
        p.drawEllipse(r)

    def hit(self, pt, tol):
        r = self.rect()
        rx, ry = r.width() / 2, r.height() / 2
        if rx < 1 or ry < 1:
            return "body" if r.adjusted(-tol, -tol, tol, tol).contains(pt) else None
        c = r.center()
        dx, dy = pt.x() - c.x(), pt.y() - c.y()
        d = math.hypot(dx / rx, dy / ry)        # 1 on the outline
        if self.fill and d <= 1:
            return "body"
        slack = (tol + self.width / 2) / min(rx, ry)
        return "body" if abs(d - 1) <= slack else None


class Marker(_Box):
    """Translucent highlight box; multiply keeps the text under it readable."""
    kind = "marker"
    props = ("color",)

    def paint(self, p, ctx):
        p.save()
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Multiply)
        p.fillRect(self.rect(), self.qcolor(190))
        p.restore()


class Redact(_Box):
    kind = "redact"
    props = ("color",)

    def paint(self, p, ctx):
        p.fillRect(self.rect(), self.qcolor())


class _Effect(_Box):
    """Box whose content is computed from the base image."""
    props = ("width",)

    def effect(self, sub, ctx):
        raise NotImplementedError

    def margin(self, ctx):
        return 0

    def paint(self, p, ctx):
        r = self.rect()
        if r.width() < 1 or r.height() < 1:
            return
        x, y, w, h = ctx.device_rect(r)
        if w <= 0 or h <= 0:
            return
        m = self.margin(ctx)
        bx, by = max(0, x - m), max(0, y - m)
        bw = min(ctx.base.width(), x + w + m) - bx
        bh = min(ctx.base.height(), y + h + m) - by
        key = (self.kind, ctx.base.cacheKey(), x, y, w, h, self.width)

        def make():
            sub = ctx.base.copy(bx, by, bw, bh)
            sub.setDevicePixelRatio(1.0)
            out = self.effect(sub, ctx).copy(x - bx, y - by, w, h)
            out.setDevicePixelRatio(ctx.dpr)
            return out
        img = _cache.get_or(key, make)
        p.save()
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, ctx.smooth and self.kind == "blur")
        p.drawImage(QRectF(x / ctx.dpr, y / ctx.dpr, w / ctx.dpr, h / ctx.dpr), img)
        p.restore()


class Blur(_Effect):
    kind = "blur"

    def sigma(self, ctx):
        return max(1.0, self.width * 1.6) * ctx.dpr

    def margin(self, ctx):
        return int(self.sigma(ctx) * 3)

    def effect(self, sub, ctx):
        from .imageops import blur
        return blur(sub, self.sigma(ctx))


class Pixelate(_Effect):
    kind = "pixelate"

    def effect(self, sub, ctx):
        from .imageops import pixelate
        return pixelate(sub, round(max(3.0, self.width * 2.5) * ctx.dpr))


# ---------------------------------------------------------------- freehand

class Pen(Shape):
    kind = "pen"
    handled = False

    def __init__(self, x, y, color="#e93d3d", width=4):
        super().__init__(color, width)
        self.pts = [x, y]

    def points(self):
        return [QPointF(self.pts[i], self.pts[i + 1]) for i in range(0, len(self.pts), 2)]

    def path(self):
        pts = self.points()
        path = QPainterPath(pts[0])
        if len(pts) == 1:
            path.lineTo(pts[0] + QPointF(0.01, 0))
        elif len(pts) == 2:
            path.lineTo(pts[1])
        else:  # quadratic through the midpoints: smooth, still goes through the ends
            for i in range(1, len(pts) - 1):
                mid = (pts[i] + pts[i + 1]) / 2
                path.quadTo(pts[i], mid)
            path.lineTo(pts[-1])
        return path

    def stroke(self):
        return self.width

    def bounds(self):
        m = self.stroke() / 2 + 2
        return self.path().boundingRect().adjusted(-m, -m, m, m)

    def drag_create(self, start, pt, shift):
        if shift:  # straight line from the start
            pt = snap45(start, pt)
            self.pts[2:] = [pt.x(), pt.y()]
            return
        lx, ly = self.pts[-2], self.pts[-1]
        if math.hypot(pt.x() - lx, pt.y() - ly) >= 1.5:
            self.pts += [pt.x(), pt.y()]

    def translate(self, dx, dy):
        self.pts = [v + (dx if i % 2 == 0 else dy) for i, v in enumerate(self.pts)]

    def hit(self, pt, tol):
        pts = self.points()
        lim = tol + self.stroke() / 2
        if len(pts) == 1:
            return "body" if QLineF(pts[0], pt).length() <= lim else None
        for a, b in zip(pts, pts[1:]):
            if seg_dist(pt, a, b) <= lim:
                return "body"
        return None

    def paint(self, p, ctx):
        path = self.path()
        p.setBrush(Qt.BrushStyle.NoBrush)
        for color, off in ((SHADOW, 1.0), (self.qcolor(), 0)):
            p.setPen(QPen(color, self.width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                          Qt.PenJoinStyle.RoundJoin))
            p.drawPath(path.translated(off * 0.6, off))


class Highlighter(Pen):
    """Wide translucent freehand stroke, multiplied so dark text stays dark."""
    kind = "highlighter"

    def stroke(self):
        return self.width * 3 + 8

    def paint(self, p, ctx):
        # one fill of the stroke outline: overlapping parts don't get darker
        st = QPainterPathStroker()
        st.setWidth(self.stroke())
        st.setCapStyle(Qt.PenCapStyle.RoundCap)
        st.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        outline = st.createStroke(self.path()).simplified()
        p.save()
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Multiply)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self.qcolor(190))
        p.drawPath(outline)
        p.restore()


# ---------------------------------------------------------------- text

class Text(Shape):
    kind = "text"
    props = ("color", "font_size", "text_bg", "font")
    handled = False

    def __init__(self, x, y, color="#e93d3d", font_size=18, text_bg=False, font=""):
        super().__init__(color, 0)
        self.x, self.y = x, y
        self.text = ""
        self.font_size = font_size
        self.text_bg = text_bg
        self.font = font

    def qfont(self):
        f = QFont(self.font) if self.font else QFont()
        f.setPixelSize(max(4, round(self.font_size)))
        f.setWeight(QFont.Weight.DemiBold)
        return f

    def pad(self):
        return self.font_size * (0.32 if self.text_bg else 0.12)

    def _lines(self):
        return self.text.split("\n")

    def bounds(self):
        fm = QFontMetricsF(self.qfont())
        w = max(fm.horizontalAdvance(t) for t in self._lines())
        w = max(w, self.font_size * 0.3)
        h = fm.lineSpacing() * len(self._lines())
        pad = self.pad()
        return QRectF(self.x, self.y, w + 2 * pad, h + 2 * pad)

    def caret_line(self, index):
        fm = QFontMetricsF(self.qfont())
        before = self.text[:index].split("\n")
        row, col = len(before) - 1, before[-1]
        pad = self.pad()
        x = self.x + pad + fm.horizontalAdvance(col)
        y = self.y + pad + row * fm.lineSpacing()
        return QLineF(x, y + 1, x, y + fm.lineSpacing() - 1)

    def index_at(self, pt):
        fm = QFontMetricsF(self.qfont())
        pad = self.pad()
        lines = self._lines()
        row = int((pt.y() - self.y - pad) // fm.lineSpacing())
        row = max(0, min(row, len(lines) - 1))
        line, x = lines[row], pt.x() - self.x - pad
        col = len(line)
        for i in range(len(line) + 1):
            if fm.horizontalAdvance(line[:i]) >= x:
                col = i if i == 0 or fm.horizontalAdvance(line[:i]) - x < x - fm.horizontalAdvance(line[:i - 1]) \
                    else i - 1
                break
        return sum(len(t) + 1 for t in lines[:row]) + col

    def hit(self, pt, tol):
        return "body" if self.bounds().adjusted(-tol, -tol, tol, tol).contains(pt) else None

    def translate(self, dx, dy):
        self.x += dx
        self.y += dy

    def paint(self, p, ctx):
        if not self.text:
            return
        font = self.qfont()
        fm = QFontMetricsF(font)
        pad = self.pad()
        color = self.qcolor()
        path = QPainterPath()
        for i, line in enumerate(self._lines()):
            path.addText(self.x + pad, self.y + pad + i * fm.lineSpacing() + fm.ascent(), font, line)
        if self.text_bg:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(SHADOW)
            b = self.bounds()
            p.drawRect(b.translated(0.6, 1.0))
            p.setBrush(color)
            p.drawRect(b)
            p.setBrush(contrast(color))
            p.drawPath(path)
            return
        # thin outline so it reads on any background: dark for coloured / light text,
        # light only for very dark text
        lum = 0.299 * color.redF() + 0.587 * color.greenF() + 0.114 * color.blueF()
        halo = QColor(255, 255, 255, 170) if lum < 0.25 else QColor(0, 0, 0, 80)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(halo, max(1.5, self.font_size / 9), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                      Qt.PenJoinStyle.RoundJoin))
        p.drawPath(path)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        p.drawPath(path)


# ---------------------------------------------------------------- step numbers

class Step(Shape):
    """Numbered circle; dragging out of it while creating adds a pointer tail."""
    kind = "step"
    props = ("color", "font_size")
    handled = True

    def __init__(self, x, y, color="#e93d3d", font_size=18):
        super().__init__(color, 0)
        self.cx, self.cy = x, y
        self.tail = None            # [x, y] or None
        self.font_size = font_size

    def radius(self):
        return self.font_size * 0.8 + 3

    def center(self):
        return QPointF(self.cx, self.cy)

    def bounds(self):
        r = self.radius() + 3
        b = QRectF(self.cx - r, self.cy - r, 2 * r, 2 * r)
        if self.tail:
            b = b.united(QRectF(self.tail[0] - 2, self.tail[1] - 2, 4, 4))
        return b

    def _tail_poly(self):
        if not self.tail:
            return None
        c, t = self.center(), QPointF(*self.tail)
        r = self.radius()
        d = QLineF(c, t).length()
        if d <= r * 1.1:
            return None
        a = math.atan2(t.y() - c.y(), t.x() - c.x())
        spread = math.radians(32)
        return QPolygonF([QPointF(c.x() + r * math.cos(a + spread), c.y() + r * math.sin(a + spread)), t,
                          QPointF(c.x() + r * math.cos(a - spread), c.y() + r * math.sin(a - spread)), c])

    def hit(self, pt, tol):
        if QLineF(self.center(), pt).length() <= self.radius() + tol:
            return "body"
        poly = self._tail_poly()
        if poly is not None and poly.containsPoint(pt, Qt.FillRule.OddEvenFill):
            return "body"
        return None

    def handles(self):
        return [("tail", QPointF(*self.tail))] if self.tail else []

    def drag_handle(self, key, pt, shift):
        if key == "tail":
            pt = snap45(self.center(), pt) if shift else pt
            self.tail = [pt.x(), pt.y()]

    def drag_create(self, start, pt, shift):
        if QLineF(self.center(), pt).length() > self.radius() * 1.2:
            self.drag_handle("tail", pt, shift)
        else:
            self.tail = None

    def translate(self, dx, dy):
        self.cx += dx
        self.cy += dy
        if self.tail:
            self.tail = [self.tail[0] + dx, self.tail[1] + dy]

    def paint(self, p, ctx):
        n = ctx.numbers.get(id(self), 1)
        c, r = self.center(), self.radius()
        color = self.qcolor()
        poly = self._tail_poly()
        path = QPainterPath()
        path.addEllipse(c, r, r)
        if poly is not None:
            tp = QPainterPath()
            tp.addPolygon(poly)
            path = path.united(tp)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(SHADOW)
        p.drawPath(path.translated(0.6, 1.2))
        p.setBrush(color)
        ring = contrast(color)
        ring.setAlpha(230)
        p.setPen(QPen(ring, max(1.5, r * 0.11)))
        p.drawPath(path)
        f = QFont()
        f.setPixelSize(max(6, round(r * (1.1 if n < 10 else 0.9))))
        f.setWeight(QFont.Weight.Bold)
        num = QPainterPath()
        num.addText(0, 0, f, str(n))
        br = num.boundingRect()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(contrast(color))
        p.drawPath(num.translated(c.x() - br.center().x(), c.y() - br.center().y()))


# ---------------------------------------------------------------- magnifier

class Magnifier(Shape):
    """Zoom lens callout: a circle at (sx, sy) shown enlarged in a lens at (lx, ly)."""
    kind = "magnifier"
    props = ("color", "width", "zoom")

    def __init__(self, x, y, color="#e93d3d", width=4, zoom=2):
        super().__init__(color, width)
        self.sx, self.sy = x, y
        self.lx, self.ly = x, y
        self.r = 70.0
        self.zoom = zoom

    def src_r(self):
        return self.r / self.zoom

    def bounds(self):
        m = self.width + 6
        r, rs = self.r, self.src_r()
        return QRectF(self.lx - r, self.ly - r, 2 * r, 2 * r).united(
            QRectF(self.sx - rs, self.sy - rs, 2 * rs, 2 * rs)).adjusted(-m, -m, m, m)

    def hit(self, pt, tol):
        if math.hypot(pt.x() - self.lx, pt.y() - self.ly) <= self.r + tol:
            return "lens"
        if math.hypot(pt.x() - self.sx, pt.y() - self.sy) <= self.src_r() + tol:
            return "src"
        return None

    def handles(self):
        a = math.radians(45)
        return [("r", QPointF(self.lx + self.r * math.cos(a), self.ly + self.r * math.sin(a))),
                ("src", QPointF(self.sx, self.sy))]

    def drag_handle(self, key, pt, shift):
        if key == "r":
            self.r = max(16.0, min(600.0, math.hypot(pt.x() - self.lx, pt.y() - self.ly)))
        elif key == "src":
            self.sx, self.sy = pt.x(), pt.y()

    def drag_create(self, start, pt, shift):
        pt = snap45(start, pt) if shift else pt
        self.lx, self.ly = pt.x(), pt.y()

    def finish_click(self):
        if math.hypot(self.lx - self.sx, self.ly - self.sy) < self.r * 0.5:   # no real drag: put it up-right
            d = self.r * 1.25
            self.lx, self.ly = self.sx + d, self.sy - d

    def too_small(self, tol):
        return math.hypot(self.lx - self.sx, self.ly - self.sy) < self.r * 0.5

    def translate(self, dx, dy):
        self.sx += dx
        self.lx += dx
        self.sy += dy
        self.ly += dy

    def move_part(self, part, dx, dy):
        if part == "lens":
            self.lx += dx
            self.ly += dy
        elif part == "src":
            self.sx += dx
            self.sy += dy
        else:
            self.translate(dx, dy)

    def _lens_image(self, ctx, D):
        """Antialiased round crop of the zoomed base pixels, D×D device px."""
        base, d = ctx.base, ctx.dpr
        half = self.src_r() * d
        src = QRectF(self.sx * d - half, self.sy * d - half, 2 * half, 2 * half)
        key = ("lens", base.cacheKey(), round(src.x(), 2), round(src.y(), 2), round(src.width(), 2), D)

        def make():
            img = QImage(D, D, QImage.Format.Format_ARGB32_Premultiplied)
            img.fill(QColor("#31363b"))
            q = QPainter(img)
            q.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
            q.drawImage(QRectF(0, 0, D, D), base, src)
            q.end()
            from .imageops import mask
            circle = QPainterPath()
            circle.addEllipse(QRectF(0, 0, D, D))
            mask(img, circle)
            img.setDevicePixelRatio(d)
            return img
        return _cache.get_or(key, make)

    def paint(self, p, ctx):
        d = ctx.dpr
        color = self.qcolor()
        lw = max(1.5, self.width * 0.75)
        s, L = QPointF(self.sx, self.sy), QPointF(self.lx, self.ly)
        rs, r = self.src_r(), self.r
        # cone between the circles (outer tangents)
        dist = QLineF(s, L).length()
        if dist > r + rs:
            base = math.atan2(L.y() - s.y(), L.x() - s.x())
            alpha = math.acos(max(-1.0, min(1.0, (rs - r) / dist)))
            pts = []
            for sign in (1, -1):
                th = base + sign * alpha
                pts.append((QPointF(s.x() + rs * math.cos(th), s.y() + rs * math.sin(th)),
                            QPointF(L.x() + r * math.cos(th), L.y() + r * math.sin(th))))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(self.qcolor(18))
            p.drawPolygon(QPolygonF([pts[0][0], pts[0][1], pts[1][1], pts[1][0]]))
            p.setPen(QPen(self.qcolor(200), max(1.0, lw * 0.6)))
            for a, b in pts:
                p.drawLine(a, b)
        # source spot
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(color, max(1.2, lw * 0.7)))
        p.drawEllipse(s, rs, rs)
        # lens: soft shadow, zoomed pixels, ring
        p.setPen(Qt.PenStyle.NoPen)
        for i, a in enumerate((26, 18, 10)):
            p.setBrush(QColor(0, 0, 0, a))
            p.drawEllipse(L + QPointF(0, 2.5), r + 1 + i * 1.6, r + 1 + i * 1.6)
        x0, y0 = round((L.x() - r) * d), round((L.y() - r) * d)
        D = max(2, round(2 * r * d))
        p.save()
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        p.drawImage(QRectF(x0 / d, y0 / d, D / d, D / d), self._lens_image(ctx, D))
        p.restore()
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(color, lw))
        p.drawEllipse(QRectF(x0 / d, y0 / d, D / d, D / d))


KINDS = {c.kind: c for c in (Line, Arrow, Rect, Ellipse, Marker, Redact, Blur, Pixelate, Pen, Highlighter,
                            Text, Step, Magnifier)}


def make(tool, pt, values):
    """New shape for `tool` at pt with the current property values."""
    cls = KINDS[tool]
    x, y = pt.x(), pt.y()
    kw = {k: values[k] for k in cls.props if k in values and k != "font"}
    if tool == "text":
        return cls(x, y, **{k: values[k] for k in cls.props if k in values})
    if tool == "step":
        return cls(x, y, values["color"], values["font_size"])
    s = cls(x, y)
    for k, v in kw.items():
        setattr(s, k, v)
    return s
