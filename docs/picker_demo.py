"""Renders docs/picker.webp: the picker stepping through the levels under the cursor.

Runs on Qt's offscreen platform, so it needs no desktop and touches nothing on screen.
The "desktop" is staged from real Qt widgets (a file manager and an editor window) and
the accessibility tree is read from those widgets, so the picker's own level logic and
painting (`Picker._refresh`, `Overlay.paintEvent`) produce every frame.

    python3 docs/picker_demo.py        # from the checkout; needs Pillow
"""

import json
import os
import sys
import tempfile

W, H = 1440, 900
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "picker.webp")

# one offscreen screen of a fixed size, before Qt starts
_cfg = os.path.join(tempfile.mkdtemp(prefix="snappie-demo-"), "screens.json")
with open(_cfg, "w") as f:
    json.dump({"screens": [{"name": "DP-1", "x": 0, "y": 0, "width": W, "height": H,
                            "logicalDpi": 96, "logicalBaseDpi": 96, "dpr": 1}]}, f)
os.environ["QT_QPA_PLATFORM"] = f"offscreen:configfile={_cfg}"
sys.path.insert(0, os.path.dirname(HERE))

from PIL import Image  # noqa: E402
from PyQt6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt  # noqa: E402
from PyQt6.QtGui import (QBrush, QColor, QFont, QIcon, QImage, QLinearGradient, QPainter,  # noqa: E402
                         QPainterPath, QPalette, QPen)
from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QHBoxLayout, QLabel, QLineEdit,  # noqa: E402
                             QListWidget, QPlainTextEdit, QSplitter, QStatusBar, QToolBar, QToolButton,
                             QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)



def light_palette():
    """Breeze light, whatever color scheme the machine running this uses."""
    pal = QPalette()
    R = QPalette.ColorRole
    for role, color in [(R.Window, "#eff0f1"), (R.WindowText, "#232629"), (R.Base, "#ffffff"),
                        (R.AlternateBase, "#f7f7f7"), (R.Text, "#232629"), (R.Button, "#fcfcfc"),
                        (R.ButtonText, "#232629"), (R.Highlight, "#3daee9"), (R.HighlightedText, "#ffffff"),
                        (R.PlaceholderText, "#7f8c8d"), (R.Light, "#ffffff"), (R.Midlight, "#f6f7f7"),
                        (R.Mid, "#c4c9cd"), (R.Dark, "#888e93"), (R.Shadow, "#474a4c"),
                        (R.ToolTipBase, "#f7f7f7"), (R.ToolTipText, "#232629"), (R.Link, "#2980b9")]:
        pal.setColor(role, QColor(color))
    return pal


app = QApplication(sys.argv)
app.setStyle("Breeze")
app.setPalette(light_palette())
app.setFont(QFont("Noto Sans", 10))
QIcon.setThemeName("breeze")

from snappie import picker as picker_mod  # noqa: E402

TITLE_H = 30
PANEL_H = 52


# -- the staged desktop --

def file_manager():
    root = QWidget()
    lay = QVBoxLayout(root)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)

    bar = QToolBar("Main Toolbar")
    bar.setIconSize(QSize(22, 22))
    for icon, name in [("go-previous", "Back"), ("go-next", "Forward"), ("go-up", "Up")]:
        bar.addAction(QIcon.fromTheme(icon), name)
    path = QLineEdit("/home/kibi/Documents")
    path.setAccessibleName("Location")
    bar.addWidget(path)
    for icon, name in [("view-list-details", "Details View"), ("edit-find", "Search")]:
        bar.addAction(QIcon.fromTheme(icon), name)
    lay.addWidget(bar)

    split = QSplitter()
    places = QListWidget()
    places.setAccessibleName("Places")
    places.setIconSize(QSize(22, 22))
    for icon, name in [("user-home", "Home"), ("user-desktop", "Desktop"), ("folder-documents", "Documents"),
                       ("folder-download", "Downloads"), ("folder-music", "Music"),
                       ("folder-pictures", "Pictures"), ("folder-videos", "Videos"), ("user-trash", "Trash")]:
        places.addItem(name)
        places.item(places.count() - 1).setIcon(QIcon.fromTheme(icon))
    places.setCurrentRow(2)
    split.addWidget(places)

    files = QTreeWidget()
    files.setAccessibleName("Files")
    files.setHeaderLabels(["Name", "Size", "Modified"])
    files.setRootIsDecorated(False)
    files.setIconSize(QSize(22, 22))
    files.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
    for icon, name, size, when in [
            ("folder", "Invoices", "12 items", "Yesterday"),
            ("folder", "Travel", "31 items", "Oct 28"),
            ("x-office-document", "Q3 report.odt", "48.2 KiB", "Today, 09:14"),
            ("x-office-spreadsheet", "Budget 2026.ods", "22.7 KiB", "Today, 08:51"),
            ("x-office-presentation", "Kickoff.odp", "1.3 MiB", "Nov 2"),
            ("application-pdf", "Lease agreement.pdf", "310.4 KiB", "Oct 17"),
            ("image-png", "Floor plan.png", "842.0 KiB", "Oct 12"),
            ("text-x-generic", "Notes.txt", "1.1 KiB", "Sep 30")]:
        it = QTreeWidgetItem(files, [name, size, when])
        it.setIcon(0, QIcon.fromTheme(icon))
        it.setSizeHint(0, QSize(0, 30))
    files.setColumnWidth(0, 300)
    files.setColumnWidth(1, 120)
    split.addWidget(files)
    split.setSizes([190, 670])
    lay.addWidget(split, 1)

    status = QStatusBar()
    status.showMessage("8 items, 2.5 MiB")
    status.setAccessibleName("8 items, 2.5 MiB")
    lay.addWidget(status)
    files.setFocus()
    return root


def editor():
    root = QWidget()
    lay = QHBoxLayout(root)
    lay.setContentsMargins(0, 0, 0, 0)
    text = QPlainTextEdit()
    text.setPlainText("# Trip checklist\n\n- passport\n- chargers\n- train tickets (print!)\n"
                      "- book the hotel shuttle\n\n# Ideas\n\n- weekend in the mountains\n")
    lay.addWidget(text)
    return root


# a11y roles for the widget classes used above
ROLES = [(QToolBar, "tool bar"), (QToolButton, "push button"), (QLineEdit, "text"),
         (QSplitter, "split pane"), (QListWidget, "list"), (QTreeWidget, "tree table"),
         (QStatusBar, "status bar"), (QPlainTextEdit, "text"), (QLabel, "label")]


def role_of(w):
    for cls, role in ROLES:
        if isinstance(w, cls):
            return role
    return None


class Element:
    """Like a11y.Element: role, name and global (x, y, w, h)."""

    def __init__(self, role, name, rect):
        self.role, self.name, self.rect = role, name, rect.getRect()


class Inspector:
    """Stands in for a11y.Inspector: the element chain comes from the staged widgets."""

    def __init__(self, apps):
        self.apps = apps   # window id -> (root widget, client origin QPoint)

    def begin(self, windows):
        pass

    def has_tree(self, win):
        return win["id"] in self.apps

    def chain_at(self, win, gx, gy):
        root, origin = self.apps[win["id"]]
        out = []
        node = root
        while True:
            hit = None
            for c in node.findChildren(QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly):
                g = QRect(c.mapTo(root, QPoint(0, 0)) + origin, c.size())
                if c.isVisibleTo(root) and g.contains(gx, gy):
                    hit = c
            if hit is None:
                break
            node = hit
            role = role_of(node)
            if role:
                g = QRect(node.mapTo(root, QPoint(0, 0)) + origin, node.size())
                out.append(Element(role, node.accessibleName(), g))
        # items inside list views
        last = node
        while last is not None and not isinstance(last, (QListWidget, QTreeWidget)):
            last = last.parentWidget()
        if last is not None:
            vp = last.viewport()
            local = vp.mapFrom(root, QPoint(gx, gy) - origin)
            idx = last.indexAt(local)
            if idx.isValid():
                r = last.visualRect(idx)
                g = QRect(vp.mapTo(root, r.topLeft()) + origin, r.size())
                role = "list item" if isinstance(last, QListWidget) else "table cell"
                out.append(Element(role, idx.data(), g))
        return out


def _dark_icon(name, size):
    """A symbolic icon in light glyphs for the dark panel, as Plasma tints them there (Qt alone
    renders Breeze's symbolic icons in the dark text colour)."""
    from PyQt6.QtGui import QPixmap
    src = QIcon.fromTheme(name).pixmap(size, size)
    pm = QPixmap(src.size())
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.drawPixmap(0, 0, src)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    p.fillRect(pm.rect(), QColor("#fcfcfc"))
    p.end()
    return pm


def draw_window(p, frame, caption, content, active, icon=None):
    """A Breeze window: app icon left, title centred, minimise / maximise / close glyphs right."""
    r = QRect(*frame)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    for i in range(22, 0, -2):          # Breeze's wide, soft shadow
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 6 if active else 4))
        p.drawRoundedRect(QRectF(r.adjusted(-i, -i + 6, i, i + 8)), 5 + i, 5 + i)
    path = QPainterPath()
    path.addRoundedRect(QRectF(r), 5, 5)
    p.save()
    p.setClipPath(path)
    p.fillRect(r, QColor("#ffffff"))
    bar = QRect(r.left(), r.top(), r.width(), TITLE_H)
    p.fillRect(bar, QColor("#dee0e2" if active else "#eff0f1"))
    if icon:
        p.drawPixmap(bar.left() + 8, bar.top() + (TITLE_H - 16) // 2, QIcon.fromTheme(icon).pixmap(16, 16))
    p.setPen(QColor("#232629" if active else "#8b9196"))
    p.setFont(QFont("Noto Sans", 10))
    p.drawText(bar, Qt.AlignmentFlag.AlignCenter, caption)
    glyph = QPen(QColor("#232629" if active else "#8b9196"), 1.4)
    glyph.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(glyph)
    cy = bar.center().y() + 0.5
    for n, kind in enumerate(["min", "max", "close"]):
        cx = bar.right() - 16 - (2 - n) * 28
        if kind == "min":
            p.drawPolyline([QPointF(cx - 4.5, cy - 2), QPointF(cx, cy + 2.5), QPointF(cx + 4.5, cy - 2)])
        elif kind == "max":
            p.drawPolyline([QPointF(cx - 4.5, cy + 2), QPointF(cx, cy - 2.5), QPointF(cx + 4.5, cy + 2)])
        else:
            p.drawLine(QPointF(cx - 4, cy - 4), QPointF(cx + 4, cy + 4))
            p.drawLine(QPointF(cx - 4, cy + 4), QPointF(cx + 4, cy - 4))
    p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    p.drawPixmap(r.left(), r.top() + TITLE_H, content)
    p.restore()
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setPen(QPen(QColor(0, 0, 0, 40), 1))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(QRectF(r).adjusted(0.5, 0.5, -0.5, -0.5), 5, 5)


def wallpaper(p):
    """Abstract dark waves in Plasma's blues (drawn here; not KDE's artwork)."""
    g = QLinearGradient(0, 0, 0, H)
    g.setColorAt(0, QColor("#0c1424"))
    g.setColorAt(1, QColor("#16233d"))
    p.fillRect(QRect(0, 0, W, H), g)
    for y0, amp, c1, c2, alpha in [(250, 120, "#1d99f3", "#3daee9", 70), (430, 160, "#2c6bd6", "#7b5bd6", 80),
                                   (600, 110, "#1d99f3", "#29d6c4", 60), (720, 90, "#3daee9", "#9b59b6", 45)]:
        path = QPainterPath(QPointF(-50, y0))
        path.cubicTo(QPointF(W * 0.3, y0 - amp * 1.6), QPointF(W * 0.6, y0 + amp * 1.4), QPointF(W + 50, y0 - amp * 0.4))
        path.lineTo(W + 50, y0 - amp * 0.4 + 140)
        path.cubicTo(QPointF(W * 0.6, y0 + amp * 1.4 + 140), QPointF(W * 0.3, y0 - amp * 1.6 + 140),
                     QPointF(-50, y0 + 140))
        path.closeSubpath()
        lg = QLinearGradient(0, 0, W, 0)
        a, b = QColor(c1), QColor(c2)
        a.setAlpha(alpha)
        b.setAlpha(alpha // 3)
        lg.setColorAt(0, a)
        lg.setColorAt(1, b)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(lg))
        p.drawPath(path)


# the floating Plasma panel, and the spot of Snappie's icon in its system tray
PANEL = QRect(8, H - PANEL_H + 4, W - 16, PANEL_H - 10)
TRAY_ICON = QRect(PANEL.right() - 238, PANEL.center().y() - 10, 22, 22)


def panel(p, tray=None):
    """Plasma 6's floating panel: launcher, task manager, system tray, clock."""
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setPen(QPen(QColor(255, 255, 255, 22), 1))
    p.setBrush(QColor(32, 35, 38, 240))
    p.drawRoundedRect(QRectF(PANEL), 8, 8)
    x, cy = PANEL.left() + 8, PANEL.center().y()
    p.drawPixmap(x, cy - 13, _dark_icon("start-here-kde-plasma", 26))
    x += 44
    for name, state in [("system-file-manager", "active"), ("kate", "open"), ("firefox", None),
                        ("utilities-terminal", None)]:
        cell = QRectF(x - 6, PANEL.top() + 3, 40, PANEL.height() - 6)
        if state == "active":
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255, 28))
            p.drawRoundedRect(cell, 4, 4)
        icon = QIcon.fromTheme(name, QIcon.fromTheme("internet-web-browser"))
        p.drawPixmap(x + 1, cy - 13, icon.pixmap(26, 26))
        if state:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#3daee9") if state == "active" else QColor(255, 255, 255, 110))
            w = 16 if state == "active" else 6
            p.drawRoundedRect(QRectF(cell.center().x() - w / 2, cell.bottom() - 3, w, 2.5), 1, 1)
        x += 44
    # system tray: Snappie first, then the usual ones
    if tray is not None:
        p.drawPixmap(TRAY_ICON, tray.pixmap(22, 22))
    tx = TRAY_ICON.right() + 12
    for name in ["kdeconnect-tray", "audio-volume-high", "network-wireless", "battery-090", "go-up"]:
        size = 16 if name == "go-up" else 22
        p.drawPixmap(tx, cy - size // 2, _dark_icon(name, size))
        tx += 30
    p.setPen(QColor("#fcfcfc"))
    f = QFont("Noto Sans", 10)
    p.setFont(f)
    clock = QRect(PANEL.right() - 80, PANEL.top(), 70, PANEL.height())
    p.drawText(clock.adjusted(0, 3, 0, -PANEL.height() // 2 + 2), Qt.AlignmentFlag.AlignCenter, "10:42")
    f.setPointSize(8)
    p.setFont(f)
    p.drawText(clock.adjusted(0, PANEL.height() // 2 - 2, 0, -3), Qt.AlignmentFlag.AlignCenter, "17.11.26")
    p.setRenderHint(QPainter.RenderHint.Antialiasing, False)


def desktop(windows, tray=None):
    img = QImage(W, H, QImage.Format.Format_ARGB32_Premultiplied)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    wallpaper(p)
    for w, content in windows:
        draw_window(p, w["frame"], w["caption"], content, w["active"], w.get("icon"))
    panel(p, tray)
    p.end()
    return img


def window(wid, frame, caption, active, widget, icon=None):
    x, y, w, h = frame
    widget.resize(w, h - TITLE_H)
    widget.ensurePolished()
    widget.show()           # offscreen: lays out the children, nothing appears anywhere
    app.processEvents()
    client = (x, y + TITLE_H, w, h - TITLE_H)
    return {"id": wid, "frame": frame, "client": client, "buffer": frame, "caption": caption,
            "cls": "demo", "pid": 1, "visible": True, "normal": True, "dialog": False,
            "active": active, "icon": icon}


# -- frames --

def cursor(p, at):
    """The picker's cross cursor."""
    x, y = at.x(), at.y()
    for color, width in [(QColor(0, 0, 0, 200), 3), (QColor("#ffffff"), 1)]:
        p.setPen(QPen(color, width))
        p.drawLine(x - 9, y, x - 3, y)
        p.drawLine(x + 3, y, x + 9, y)
        p.drawLine(x, y - 9, x, y - 3)
        p.drawLine(x, y + 3, x, y + 9)


def wheel_badge(p, at, up):
    """A small 'scroll' note next to the cursor while the wheel turns."""
    text = "scroll ▲" if up else "scroll ▼"
    f = QFont("Noto Sans", 9)
    f.setBold(True)
    p.setFont(f)
    r = QRect(at.x() + 14, at.y() + 12, p.fontMetrics().horizontalAdvance(text) + 14, 22)
    p.fillRect(r, QColor(0, 0, 0))
    p.setPen(QColor("#3daee9"))
    p.drawText(r, Qt.AlignmentFlag.AlignCenter, text)


def main():
    fm = file_manager()
    ed = editor()
    wins = [window("editor", (90, 80, 620, 440), "notes.md — Kate", False, ed, "kate"),
            window("files", (380, 150, 900, 580), "Documents — Dolphin", True, fm, "system-file-manager")]
    bg = desktop([(w, (ed if w["id"] == "editor" else fm).grab()) for w in wins])
    apps = {w["id"]: (ed if w["id"] == "editor" else fm, QPoint(*w["client"][:2])) for w in wins}

    screen = QApplication.primaryScreen()
    pk = picker_mod.Picker(wins, Inspector(apps), own_pid=0, mode="shot",
                           backgrounds={screen.name(): bg}, magnifier=False)
    ov = pk.overlays[0]
    ov.setGeometry(screen.geometry())

    def frame(at, wheel=None):
        img = ov.grab().toImage()
        p = QPainter(img)
        cursor(p, at)
        if wheel is not None:
            wheel_badge(p, at, wheel)
        p.end()
        img = img.convertToFormat(QImage.Format.Format_RGB888)
        return Image.frombuffer("RGB", (img.width(), img.height()), bytes(img.constBits().asarray(img.sizeInBytes())),
                                "raw", "RGB", img.bytesPerLine())

    def hover(at):
        pk.cursor = at
        pk._refresh()

    def cell_center(view, row, col=0):
        vp = view.viewport()
        r = view.visualRect(view.model().index(row, col))
        return vp.mapTo(fm, r.center()) + QPoint(*wins[1]["client"][:2])

    files = fm.findChild(QTreeWidget)
    places = fm.findChild(QListWidget)
    frames = []   # (image, ms)

    # hovering picks the smallest area under the cursor
    at = cell_center(places, 4)
    hover(at)
    frames.append((frame(at), 1300))
    at = cell_center(files, 2) + QPoint(-5, 0)
    hover(at)
    frames.append((frame(at), 1500))
    print("levels:", " › ".join(lv.short() for lv in pk.levels))
    # wheel up: bigger, up to the window and the whole screen
    while pk.level > 0:
        pk.step(-1)
        kind = pk.levels[pk.level].kind
        frames.append((frame(at, wheel=True), 1500 if kind == "window" else 950))
    frames.append((frame(at), 900))
    # and back down
    while pk.level < len(pk.levels) - 1:
        pk.step(1)
        frames.append((frame(at, wheel=False), 450))
    frames.append((frame(at), 1100))

    imgs = [f for f, _ in frames]
    imgs[0].save(OUT, save_all=True, append_images=imgs[1:], duration=[d for _, d in frames], loop=0,
                 lossless=False, quality=88, method=6)
    print(f"wrote {OUT}: {len(imgs)} frames, {os.path.getsize(OUT) // 1024} KiB")


if __name__ == "__main__":
    main()
