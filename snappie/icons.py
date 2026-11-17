"""Snappie's magpie: the app icon (color, on a blue tile) and the tray icon.

The tray icon is a one-color silhouette drawn in the panel's text color, like
Plasma's own tray icons, so it reads on light and dark panels. Recording adds a
red dot, pausing amber bars.
"""

import os
import shutil

from PyQt6.QtCore import QByteArray, QRectF, Qt
from PyQt6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PyQt6.QtSvg import QSvgRenderer

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")
APP_SVG = os.path.join(HERE, "snappie.svg")
TRAY_SVG = os.path.join(HERE, "snappie-tray.svg")
ICON_NAME = "snappie"
THEME_DIR = os.path.join(os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")),
                         "icons", "hicolor", "scalable", "apps")


def install():
    """Put the app icon where the launcher, task manager and notifications find it (Icon=snappie)."""
    dest = os.path.join(THEME_DIR, ICON_NAME + ".svg")
    try:
        with open(APP_SVG, "rb") as a, open(dest, "rb") as b:
            if a.read() == b.read():
                return
    except OSError:
        pass
    os.makedirs(THEME_DIR, exist_ok=True)
    shutil.copyfile(APP_SVG, dest)


def app_icon():
    return QIcon(APP_SVG)


def _panel_color():
    from PyQt6.QtGui import QPalette
    from PyQt6.QtWidgets import QApplication
    return QApplication.palette().color(QPalette.ColorRole.WindowText)


def tray_icon(state=None, color=None, progress=None):
    """state: None, "recording" or "paused".  progress: 0..1 draws an upload pie instead."""
    color = QColor(color) if color else _panel_color()
    with open(TRAY_SVG) as f:
        svg = f.read().replace("COLOR", color.name())
    renderer = QSvgRenderer(QByteArray(svg.encode()))
    icon = QIcon()
    for size in (16, 22, 24, 32, 48, 64):
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        renderer.render(p, QRectF(0, size * 0.07, size, size * 0.86))   # the drawing is 60×52
        if progress is not None and not state:
            d = size * 0.5
            r = QRectF(size - d, size - d, d, d).adjusted(1, 1, -1, -1)
            p.setPen(QPen(QColor(0, 0, 0, 150), max(1.0, size / 22)))
            p.setBrush(QColor(255, 255, 255, 200))
            p.drawEllipse(r)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#3daee9"))
            # at least a sliver, so a starting upload is visible too
            p.drawPie(r, 90 * 16, -int(max(0.04, min(1.0, progress)) * 360 * 16))
        if state:
            d = size * 0.46
            r = QRectF(size - d, size - d, d, d)
            p.setPen(Qt.PenStyle.NoPen)
            if state == "paused":
                p.setBrush(QColor("#fdbc4b"))
                w = d * 0.32
                p.drawRect(QRectF(r.left() + d * 0.12, r.top() + d * 0.1, w, d * 0.8))
                p.drawRect(QRectF(r.right() - d * 0.12 - w, r.top() + d * 0.1, w, d * 0.8))
            else:
                p.setBrush(QColor("#da4453"))
                p.drawEllipse(r.adjusted(d * 0.06, d * 0.06, -d * 0.06, -d * 0.06))
        p.end()
        icon.addPixmap(pm)
    return icon
