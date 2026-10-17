"""Frame a screenshot: rounded corners, soft drop shadow, padding, background.

apply(img, padding=0, background=None, radius=0, shadow=False) -> QImage

padding and radius are logical pixels (scaled by the image's dpr).
background: None (transparent), "#rrggbb", or ("gradient", "#c1", "#c2").
Existing transparency is kept: a window grab with transparent rounded corners
gets its shadow from its real outline.
"""

import math

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QImage, QLinearGradient, QPainter, QPainterPath

from . import imageops

FMT = QImage.Format.Format_ARGB32_Premultiplied

# Background presets offered by the editor's Frame panel
GRADIENTS = [
    ("gradient", "#3daee9", "#9b59b6"),
    ("gradient", "#f67400", "#e93d3d"),
    ("gradient", "#27ae60", "#3daee9"),
    ("gradient", "#fdbc4b", "#f67400"),
    ("gradient", "#ff7eb3", "#7f53ac"),
    ("gradient", "#232629", "#4d5257"),
]
SOLIDS = ["#ffffff", "#eff0f1", "#31363b", "#000000", "#3daee9"]

SHADOW_SIGMA = 12       # logical px
SHADOW_OFFSET = 6
SHADOW_OPACITY = 0.5


def margins(dpr, padding=0, background=None, radius=0, shadow=False):
    """(left, top, right, bottom) device pixels added around the image."""
    pad = round(padding * dpr)
    if not shadow or background:
        return (pad, pad, pad, pad)
    m = math.ceil(3 * SHADOW_SIGMA * dpr)    # transparent: grow until the shadow fits
    off = round(SHADOW_OFFSET * dpr)
    return (max(pad, m), max(pad, m - off), max(pad, m), max(pad, m + off))


def fill_background(p, rect, background):
    if not background:
        return
    if isinstance(background, (tuple, list)) and len(background) == 3 and background[0] == "gradient":
        g = QLinearGradient(QPointF(rect.topLeft()), QPointF(rect.bottomRight()))
        g.setColorAt(0, QColor(background[1]))
        g.setColorAt(1, QColor(background[2]))
        p.fillRect(rect, g)
    else:
        p.fillRect(rect, QColor(background))


def rounded(img, radius_px):
    """Copy with rounded corners (antialiased, multiplied into existing alpha)."""
    out = img.convertToFormat(FMT)
    if radius_px <= 0:
        return out
    return imageops.mask(out, clip_path(QRectF(0, 0, out.width(), out.height()), radius_px))


def backdrop(img, padding=0, background=None, radius=0, shadow=False):
    """Everything but the image itself: background + shadow of img's outline.
    Returns (QImage with dpr set, (left, top) device-pixel offset of the image)."""
    dpr = img.devicePixelRatio()
    src = img.copy()
    src.setDevicePixelRatio(1.0)
    src = rounded(src, radius * dpr)
    l, t, r, b = margins(dpr, padding, background, radius, shadow)
    out = QImage(src.width() + l + r, src.height() + t + b, FMT)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    fill_background(p, out.rect(), background)
    if shadow:
        sh, m = imageops.shadow(src, SHADOW_SIGMA * dpr, SHADOW_OPACITY)
        p.drawImage(l - m, t - m + round(SHADOW_OFFSET * dpr), sh)
    p.end()
    out.setDevicePixelRatio(dpr)
    return out, (l, t), src


def apply(img, padding=0, background=None, radius=0, shadow=False):
    if not (padding or background or radius or shadow):
        return img
    dpr = img.devicePixelRatio()
    out, (l, t), src = backdrop(img, padding, background, radius, shadow)
    out.setDevicePixelRatio(1.0)
    p = QPainter(out)
    p.drawImage(l, t, src)
    p.end()
    out.setDevicePixelRatio(dpr)
    return out


def clip_path(rect, radius):
    """Rounded-rect path for previews (logical units)."""
    path = QPainterPath()
    r = min(radius, rect.width() / 2, rect.height() / 2)
    path.addRoundedRect(rect, r, r)
    return path
