"""The thing both editors edit: a base image, shapes on top, a crop and an
optional beautify frame.

The crop is non-destructive (a rectangle on the base image), which lets the
in-place editor keep the whole frozen screen as the base and use the
selection as the crop: moving the selection simply shows other pixels, shapes
stay where they were drawn, and blur / magnifier near the edge still see the
real neighbouring pixels.

Undo keeps whole snapshots (crop, shapes, frame).  Shapes are plain Python
data and the base image never changes, so a snapshot is tiny.
"""

import copy
import time

from PyQt6.QtCore import QObject, QPointF, QRect, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QImage, QPainter

from .shapes import Context, Step

FAST_FORMATS = (QImage.Format.Format_ARGB32_Premultiplied, QImage.Format.Format_RGB32)


class Document(QObject):
    changed = pyqtSignal()          # anything visible changed (shapes, crop, frame)

    def __init__(self, image: QImage):
        super().__init__()
        dpr = image.devicePixelRatio()
        if image.format() not in FAST_FORMATS:
            image = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied
                                          if image.hasAlphaChannel() else QImage.Format.Format_RGB32)
            image.setDevicePixelRatio(dpr)
        self.image = image
        self.shapes = []
        self.frame = None           # None or kwargs for beautify.apply (padding, background, radius, shadow)
        self.crop = None            # QRect in device pixels of `image`, None = everything
        self.revision = 0           # bumps on every committed change
        self._undo, self._redo = [], []
        self._pending = None
        self._merge = (None, 0.0)

    @classmethod
    def from_file(cls, path):
        img = QImage(path)
        if img.isNull():
            raise OSError(f"can't read image {path}")
        return cls(img)

    # -- geometry --

    @property
    def dpr(self):
        return self.image.devicePixelRatio()

    def pixel_rect(self):
        """The visible part of the base image, device pixels."""
        full = self.image.rect()
        return full if self.crop is None else self.crop.intersected(full)

    def bounds(self):
        """The visible part in logical (shape) coordinates."""
        r, d = self.pixel_rect(), self.dpr
        return QRectF(r.x() / d, r.y() / d, r.width() / d, r.height() / d)

    def full_bounds(self):
        d = self.dpr
        return QRectF(0, 0, self.image.width() / d, self.image.height() / d)

    def to_pixels(self, r):
        """Logical QRectF -> device-pixel QRect, clipped to the image."""
        d = self.dpr
        x0, y0 = round(r.left() * d), round(r.top() * d)
        x1, y1 = round(r.right() * d), round(r.bottom() * d)
        return QRect(x0, y0, max(0, x1 - x0), max(0, y1 - y0)).intersected(self.image.rect())

    def output_size(self):
        """Size of render() in device pixels."""
        from . import beautify
        r = self.pixel_rect()
        if not self.frame:
            return r.size()
        m = beautify.margins(self.dpr, **self.frame)
        return QSize(r.width() + m[0] + m[2], r.height() + m[1] + m[3])

    # -- painting --

    def numbers(self):
        n, out = 0, {}
        for s in self.shapes:
            if isinstance(s, Step):
                n += 1
                out[id(s)] = n
        return out

    def paint(self, p, base=True, smooth=True):
        """Paint into p, which must use logical document coordinates."""
        if base:
            p.drawImage(QPointF(0, 0), self.image)
        ctx = Context(self.image, self.numbers(), smooth)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        for s in self.shapes:
            p.save()
            s.paint(p, ctx)
            p.restore()

    def render(self):
        """Flattened result: device pixels, dpr set, crop and frame applied."""
        r, d = self.pixel_rect(), self.dpr
        alpha = self.image.hasAlphaChannel()
        out = QImage(r.size(), QImage.Format.Format_ARGB32_Premultiplied)
        out.fill(Qt.GlobalColor.transparent)
        out.setDevicePixelRatio(d)
        p = QPainter(out)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.translate(-r.x() / d, -r.y() / d)
        self.paint(p)
        p.end()
        if self.frame:
            from . import beautify
            out = beautify.apply(out, **self.frame)
        elif not alpha:
            out = out.convertToFormat(QImage.Format.Format_RGB32)
            out.setDevicePixelRatio(d)
        return out

    # -- undo --

    def _state(self):
        return (self.crop, copy.deepcopy(self.shapes), copy.deepcopy(self.frame))

    @staticmethod
    def _same(a, b):
        if a[0] != b[0] or a[2] != b[2] or len(a[1]) != len(b[1]):
            return False
        return all(type(x) is type(y) and vars(x) == vars(y) for x, y in zip(a[1], b[1]))

    def begin(self):
        """Start a change; nested begins are part of the outer one."""
        if self._pending is None:
            self._pending = self._state()

    def commit(self, merge=None):
        """End a change. Records an undo step if something actually changed.
        Changes with the same `merge` key within 1.5 s share one step (slider drags)."""
        before, self._pending = self._pending, None
        if before is None or self._same(before, self._state()):
            self.changed.emit()
            return False
        now = time.monotonic()
        if not (merge and self._merge[0] == merge and now - self._merge[1] < 1.5 and self._undo):
            self._undo.append(before)
            del self._undo[:-200]
        self._merge = (merge, now)
        self._redo.clear()
        self.revision += 1
        self.changed.emit()
        return True

    def cancel(self):
        """Abandon the pending change and restore the state from begin()."""
        if self._pending is not None:
            self._restore(self._pending)
            self._pending = None
            self.changed.emit()

    def _restore(self, st):
        self.crop, self.shapes, self.frame = st[0], copy.deepcopy(st[1]), copy.deepcopy(st[2])

    def can_undo(self):
        return bool(self._undo)

    def can_redo(self):
        return bool(self._redo)

    def undo(self):
        if self._undo:
            self._redo.append(self._state())
            self._restore(self._undo.pop())
            self._merge = (None, 0.0)
            self.revision += 1
            self.changed.emit()

    def redo(self):
        if self._redo:
            self._undo.append(self._state())
            self._restore(self._redo.pop())
            self._merge = (None, 0.0)
            self.revision += 1
            self.changed.emit()

    def modify(self, fn, merge=None):
        """Run fn() as one undoable change."""
        self.begin()
        fn()
        return self.commit(merge)
