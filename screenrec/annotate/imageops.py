"""Pixel effects on QImages through numpy: blur, pixelate, soft shadows.

Everything works on premultiplied ARGB, so blurring transparent pixels
doesn't bleed black into the result.
"""

import math

import numpy as np
from PyQt6.QtGui import QImage

FMT = QImage.Format.Format_ARGB32_Premultiplied


def mask(img, path, antialias=True):
    """Keep only the part of img (premultiplied) inside path; in place."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QColor, QPainter
    m = QImage(img.size(), FMT)
    m.fill(Qt.GlobalColor.transparent)
    q = QPainter(m)
    q.setRenderHint(QPainter.RenderHint.Antialiasing, antialias)
    q.fillPath(path, QColor(0, 0, 0))
    q.end()
    q = QPainter(img)
    q.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
    q.drawImage(0, 0, m)
    q.end()
    return img


def to_array(img):
    """HxWx4 uint8 copy (B, G, R, A in memory order, premultiplied)."""
    if img.format() != FMT:
        img = img.convertToFormat(FMT)
    w, h = img.width(), img.height()
    ptr = img.constBits()
    ptr.setsize(img.sizeInBytes())
    a = np.frombuffer(ptr, np.uint8).reshape(h, img.bytesPerLine())[:, :w * 4]
    return a.reshape(h, w, 4).copy()


def from_array(a, dpr=1.0):
    a = np.ascontiguousarray(np.clip(a, 0, 255).astype(np.uint8) if a.dtype != np.uint8 else a)
    h, w = a.shape[:2]
    img = QImage(a.tobytes(), w, h, w * 4, FMT).copy()
    img.setDevicePixelRatio(dpr)
    return img


def _box0(a, r):
    """Box filter along axis 0 (edge pixels repeated)."""
    p = np.pad(a, ((r + 1, r),) + ((0, 0),) * (a.ndim - 1), mode="edge")
    c = np.cumsum(p, axis=0, dtype=np.float32)
    return (c[2 * r + 1:] - c[:-2 * r - 1]) * (1.0 / (2 * r + 1))


def gaussian(a, sigma):
    """Three box passes per axis ≈ a gaussian; float32 result, same shape."""
    a = a.astype(np.float32)
    if sigma < 0.5:
        return a
    r = max(1, round((math.sqrt(4 * sigma * sigma + 1) - 1) / 2))
    for _ in range(3):
        a = _box0(a, r)
        a = _box0(a.swapaxes(0, 1), r).swapaxes(0, 1)
    return a


def blur(img, sigma):
    """Blurred copy of img (same size and dpr)."""
    return from_array(gaussian(to_array(img), sigma), img.devicePixelRatio())


def pixelate(img, block):
    """Mosaic of block×block averages, aligned to the image's top-left."""
    a = to_array(img).astype(np.float32)
    h, w = a.shape[:2]
    b = max(1, int(block))
    ph, pw = -h % b, -w % b
    if ph or pw:
        a = np.pad(a, ((0, ph), (0, pw), (0, 0)), mode="edge")
    H, W = a.shape[0] // b, a.shape[1] // b
    m = a.reshape(H, b, W, b, 4).mean(axis=(1, 3))
    out = np.repeat(np.repeat(m, b, axis=0), b, axis=1)[:h, :w]
    return from_array(out + 0.5, img.devicePixelRatio())


def shadow(img, sigma, opacity=0.5, color=(0, 0, 0)):
    """Soft shadow of img's alpha: a (w+2m)×(h+2m) image, m = 3·sigma.

    Blurred at reduced resolution (it's all low frequencies anyway), so even
    a 4K window shadow takes a few milliseconds.
    """
    from PyQt6.QtCore import Qt
    m = int(math.ceil(3 * sigma))
    w, h = img.width(), img.height()
    step = max(1, int(sigma / 3))
    small = img if step == 1 else img.scaled(max(1, w // step), max(1, h // step),
                                             Qt.AspectRatioMode.IgnoreAspectRatio,
                                             Qt.TransformationMode.SmoothTransformation)
    alpha = to_array(small)[:, :, 3].astype(np.float32)
    sm = int(math.ceil(m / step))
    alpha = np.pad(alpha, sm)
    alpha = gaussian(alpha, sigma / step) * opacity
    out = np.zeros(alpha.shape + (4,), np.float32)
    for i, c in enumerate((color[2], color[1], color[0])):   # BGR, premultiplied
        out[:, :, i] = alpha * (c / 255.0)
    out[:, :, 3] = alpha
    res = from_array(out + 0.5)
    if step != 1:
        res = res.scaled(w + 2 * m, h + 2 * m, Qt.AspectRatioMode.IgnoreAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    return res, m
