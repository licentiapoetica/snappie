"""Renders docs/demo.webp: the whole flow, pick → annotate → upload → link copied.

Same staging as picker_demo.py (offscreen Qt, a desktop built from real widgets), and every
Snappie part in it is the real code: the picker picks, the in-place editor shows a Document
with real shapes, the tray icon is icons.tray_icon / icons.progress_icon.  Only the cursor,
the click ring and the notification are drawn here.

    python3 docs/flow_demo.py          # from the checkout; needs Pillow
"""

import os
import sys
import tempfile

# the editor remembers the last tool in the settings: keep it away from the real ones
_home = tempfile.mkdtemp(prefix="snappie-demo-home-")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_home, "config")
os.environ["XDG_DATA_HOME"] = os.path.join(_home, "data")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import picker_demo as pd  # noqa: E402  (sets up the offscreen screen and the QApplication)

from PIL import Image  # noqa: E402
from PyQt6.QtCore import QPoint, QPointF, QRect, QRectF, Qt  # noqa: E402
from PyQt6.QtGui import QColor, QFont, QIcon, QImage, QPainter, QPainterPath, QPen  # noqa: E402
from PyQt6.QtWidgets import QApplication, QListWidget, QTreeWidget  # noqa: E402

from snappie import icons  # noqa: E402
from snappie.annotate import InPlaceEditor  # noqa: E402
from snappie.annotate.shapes import Arrow, Rect, Step, Text  # noqa: E402

OUT = os.path.join(HERE, "demo.webp")
W, H = pd.W, pd.H
URL = "https://i.example.org/Xk3vQ.png"


def to_pil(img):
    img = img.convertToFormat(QImage.Format.Format_RGB888)
    return Image.frombuffer("RGB", (img.width(), img.height()), bytes(img.constBits().asarray(img.sizeInBytes())),
                            "raw", "RGB", img.bytesPerLine())


def click_ring(p, at, t):
    """An expanding ring where the mouse clicks (t: 0..1)."""
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(QColor(61, 174, 233, int(230 * (1 - t))), 3))
    p.setBrush(Qt.BrushStyle.NoBrush)
    r = 8 + 22 * t
    p.drawEllipse(QPointF(at), r, r)


def arrow_cursor(p, at):
    """A plain pointer for the desktop part (the picker and editor use a cross)."""
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    x, y = at.x(), at.y()
    path = QPainterPath(QPointF(x, y))
    for dx, dy in [(0, 19), (5, 14), (9, 22), (12, 21), (8, 13), (14, 13)]:
        path.lineTo(x + dx, y + dy)
    path.closeSubpath()
    p.setPen(QPen(QColor("#ffffff"), 1.5))
    p.setBrush(QColor("#232629"))
    p.drawPath(path)


def key_badge(p, at, text):
    """A keycap next to the cursor: the key that was pressed."""
    f = QFont("Noto Sans", 10)
    f.setBold(True)
    p.setFont(f)
    r = QRect(at.x() + 16, at.y() + 14, p.fontMetrics().horizontalAdvance(text) + 20, 26)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(QColor("#3daee9"), 1.5))
    p.setBrush(QColor(0, 0, 0, 220))
    p.drawRoundedRect(QRectF(r), 5, 5)
    p.setPen(QColor("#fcfcfc"))
    p.drawText(r, Qt.AlignmentFlag.AlignCenter, text)


def tray_zoom(p, icon, caption):
    """A magnified view of the tray icon, so the digits read at README size."""
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    src = QRectF(pd.TRAY_ICON).center()
    box = QRectF(src.x() - 75, pd.PANEL.top() - 170, 150, 150)
    p.setPen(QPen(QColor(61, 174, 233, 200), 1.5, Qt.PenStyle.DashLine))
    p.drawLine(QPointF(src.x(), box.bottom()), QPointF(src.x(), pd.PANEL.top() + 6))
    p.setPen(QPen(QColor("#3daee9"), 2))
    p.setBrush(QColor(32, 35, 38, 245))
    p.drawRoundedRect(box, 10, 10)
    p.drawPixmap(QRectF(box.center().x() - 40, box.top() + 18, 80, 80).toRect(), icon.pixmap(80, 80))
    p.setPen(QColor("#fcfcfc"))
    p.setFont(QFont("Noto Sans", 10))
    p.drawText(QRectF(box.left(), box.bottom() - 42, box.width(), 30), Qt.AlignmentFlag.AlignCenter, caption)


def notification(p, t):
    """Plasma-like popup above the tray (t: 0..1 slides it in)."""
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    w, h = 400, 118
    x = W - w - 16
    y = pd.PANEL.top() - h - 10 + int(24 * (1 - t))
    p.setOpacity(t)
    p.setPen(QPen(QColor(255, 255, 255, 30), 1))
    p.setBrush(QColor(42, 46, 50, 245))
    p.drawRoundedRect(QRectF(x, y, w, h), 8, 8)
    p.drawPixmap(x + 14, y + 12, icons.app_icon().pixmap(20, 20))
    f = QFont("Noto Sans", 9)
    p.setFont(f)
    p.setPen(QColor("#bdc3c7"))
    p.drawText(QRect(x + 42, y + 12, 200, 20), Qt.AlignmentFlag.AlignVCenter, "Snappie")
    p.drawText(QRect(x + w - 120, y + 12, 80, 20), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
               "Just now")
    p.setPen(QPen(QColor("#bdc3c7"), 1.3))
    cx, cy = x + w - 22, y + 22
    p.drawLine(QPointF(cx - 4, cy - 4), QPointF(cx + 4, cy + 4))
    p.drawLine(QPointF(cx - 4, cy + 4), QPointF(cx + 4, cy - 4))
    f.setPointSize(11)
    f.setBold(True)
    p.setFont(f)
    p.setPen(QColor("#fcfcfc"))
    p.drawText(QRect(x + 14, y + 38, w - 28, 24), Qt.AlignmentFlag.AlignVCenter, "Uploaded — link copied")
    f.setBold(False)
    f.setPointSize(10)
    p.setFont(f)
    p.setPen(QColor("#3daee9"))
    p.drawText(QRect(x + 14, y + 60, w - 28, 20), Qt.AlignmentFlag.AlignVCenter, URL)
    for n, label in enumerate(["Open link", "Copy link"]):
        b = QRectF(x + 14 + n * 104, y + h - 36, 96, 26)
        p.setPen(QPen(QColor(255, 255, 255, 40), 1))
        p.setBrush(QColor(255, 255, 255, 18))
        p.drawRoundedRect(b, 4, 4)
        p.setPen(QColor("#fcfcfc"))
        p.drawText(b, Qt.AlignmentFlag.AlignCenter, label)
    p.setOpacity(1)


def main():
    fm, ed = pd.file_manager(), pd.editor()
    wins = [pd.window("editor", (90, 80, 620, 440), "notes.md — Kate", False, ed, "kate"),
            pd.window("files", (380, 150, 900, 580), "Documents — Dolphin", True, fm, "system-file-manager")]
    contents = [(w, (ed if w["id"] == "editor" else fm).grab()) for w in wins]
    bg = pd.desktop(contents, tray=icons.tray_icon(color="#fcfcfc"))
    apps = {w["id"]: (ed if w["id"] == "editor" else fm, QPoint(*w["client"][:2])) for w in wins}
    screen = QApplication.primaryScreen()
    files, places = fm.findChild(QTreeWidget), fm.findChild(QListWidget)
    origin = QPoint(*wins[1]["client"][:2])

    def cell(view, row, col=0):
        r = view.visualRect(view.model().index(row, col))
        return view.viewport().mapTo(fm, r.center()) + origin

    frames = []   # (PIL image, ms)

    def add(img, ms):
        frames.append((to_pil(img), ms))

    # -- 1. pick: hover, then scroll up from the file to the whole file list --
    pk = pd.picker_mod.Picker(wins, pd.Inspector(apps), own_pid=0, mode="shot",
                              backgrounds={screen.name(): bg}, magnifier=False)
    ov = pk.overlays[0]
    ov.setGeometry(screen.geometry())

    def picker_frame(at, wheel=None, ring=None):
        img = ov.grab().toImage()
        p = QPainter(img)
        if ring is not None:
            click_ring(p, at, ring)
        pd.cursor(p, at)
        if wheel is not None:
            pd.wheel_badge(p, at, wheel)
        p.end()
        return img

    for at, ms in [(cell(places, 4), 700), (cell(files, 3) + QPoint(-40, 0), 900)]:
        pk.cursor = at
        pk._refresh()
        add(picker_frame(at), ms)
    while pk.levels[pk.level].short() != "Files" and pk.level > 0:
        pk.step(-1)
        add(picker_frame(at, wheel=True), 800)
    picked = QRect(pk.levels[pk.level].rect)
    for t in (0.2, 0.6):
        add(picker_frame(at, ring=t), 120)

    # -- 2. annotate in place: a step number, an arrow, a box, a label --
    editor = InPlaceEditor(screen, bg, picked, [QRect(*w["frame"]) for w in wins])
    editor.present()
    QApplication.processEvents()
    doc, canvas = editor.doc, editor.canvas

    def editor_frame(at, ms, ring=None):
        QApplication.processEvents()
        img = editor.grab().toImage()
        p = QPainter(img)
        if ring is not None:
            click_ring(p, at, ring)
        pd.cursor(p, at)
        p.end()
        add(img, ms)

    target = cell(files, 2, 0) + QPoint(60, 0)           # "Q3 report.odt"
    budget = files.visualRect(files.model().index(3, 0)).united(files.visualRect(files.model().index(3, 2)))
    budget = QRect(files.viewport().mapTo(fm, budget.topLeft()) + origin, budget.size()).adjusted(-4, -3, 4, 3)
    editor_frame(at, 600)

    canvas.set_tool("step")
    step_at = QPoint(picked.right() - 30, target.y())    # end of the row, clear of the text
    doc.shapes.append(Step(step_at.x(), step_at.y()))
    doc.changed.emit()
    editor_frame(step_at, 450, ring=0.3)

    canvas.set_tool("arrow")
    start = target + QPoint(300, 120)
    arrow = Arrow(start.x(), start.y(), width=5)
    doc.shapes.append(arrow)
    for t in (0.25, 0.5, 0.75, 1.0):
        end = start + (target + QPoint(70, 6) - start) * t
        arrow.x2, arrow.y2 = end.x(), end.y()
        doc.changed.emit()
        editor_frame(end, 110 if t < 1 else 350)

    canvas.set_tool("rect")
    box = Rect(budget.left(), budget.top(), width=3, color="#fdbc4b")
    doc.shapes.append(box)
    for t in (0.33, 0.66, 1.0):
        corner = budget.topLeft() + (budget.bottomRight() - budget.topLeft()) * t
        box.x2, box.y2 = corner.x(), corner.y()
        doc.changed.emit()
        editor_frame(corner, 110 if t < 1 else 350)

    canvas.set_tool("text")
    label_at = start + QPoint(-40, 10)
    label = Text(label_at.x(), label_at.y(), font_size=24, text_bg=True)
    doc.shapes.append(label)
    words = "send this one!"
    for n in range(2, len(words) + 1, 3):
        label.text = words[:n]
        doc.changed.emit()
        editor_frame(label_at + QPoint(12, -10), 90)
    label.text = words
    doc.changed.emit()
    editor_frame(label_at + QPoint(12, -10), 700)

    # Enter: done (copy + upload, as set in Settings → Screenshots)
    QApplication.processEvents()
    img = editor.grab().toImage()
    p = QPainter(img)
    pd.cursor(p, label_at + QPoint(40, 70))
    key_badge(p, label_at + QPoint(40, 70), "Enter ⏎")
    p.end()
    add(img, 700)
    editor.hide()

    # -- 3. upload: the tray icon counts up, then the link is copied --
    def desk_frame(icon, ms, note=None, zoom=None, at=QPoint(W - 330, H - 360)):
        img = pd.desktop(contents, tray=icon)
        p = QPainter(img)
        if zoom is not None:
            tray_zoom(p, icon, zoom)
        if note is not None:
            notification(p, note)
        arrow_cursor(p, at)
        p.end()
        add(img, ms)

    for pct in (3, 17, 38, 61, 84, 99):
        desk_frame(icons.progress_icon(pct), 260, zoom=f"Uploading… {pct}%")
    plain = icons.tray_icon(color="#fcfcfc")
    for t in (0.35, 0.7, 1.0):
        desk_frame(plain, 70, note=t)
    desk_frame(plain, 2800, note=1.0)

    imgs = [f for f, _ in frames]
    imgs[0].save(OUT, save_all=True, append_images=imgs[1:], duration=[d for _, d in frames], loop=0,
                 lossless=False, quality=85, method=6)
    print(f"wrote {OUT}: {len(imgs)} frames, {sum(d for _, d in frames) / 1000:.1f} s, "
          f"{os.path.getsize(OUT) // 1024} KiB")


if __name__ == "__main__":
    main()
