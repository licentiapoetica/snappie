"""Full-screen overlay to interactively pick a screen, window, or inner element.

Everything under the cursor is shown at once as nested outlines: the selected
level is the clear, solid one; dashed outlines inside it are the smaller
areas you can scroll down into.  A breadcrumb bar at the top lists the same
levels and can be clicked.
"""

import threading

from PyQt6.QtCore import QObject, QPoint, QRect, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QPainter, QPen
from PyQt6.QtWidgets import QWidget

ACCENT = QColor("#3daee9")
DIM = QColor(0, 0, 0, 120)
PANEL_BG = QColor(0, 0, 0)
CHIP_BG = QColor(255, 255, 255, 18)
CHIP_HOVER = QColor(255, 255, 255, 40)
TEXT = QColor("#eff0f1")
MUTED = QColor("#9aa0a6")
WARN = QColor("#f67400")

# AT-SPI roles that are usually anonymous layout containers
GENERIC_ROLES = {"panel", "filler", "unknown", "section", "layered pane", "split pane", "grouping",
                 "scroll pane", "viewport", "redundant object", "invalid", "element"}
NEAR = 3          # levels whose edges are all within this many px count as the same area
MAX_PREFETCH = 15


def _near(a, b):
    return (abs(a.left() - b.left()) <= NEAR and abs(a.top() - b.top()) <= NEAR
            and abs(a.right() - b.right()) <= NEAR and abs(a.bottom() - b.bottom()) <= NEAR)


class Level:
    __slots__ = ("kind", "title", "detail", "rect", "win")

    def __init__(self, kind, title, detail, rect, win=None):
        self.kind, self.title, self.detail, self.rect, self.win = kind, title, detail, rect, win

    def short(self):
        """Breadcrumb text."""
        if self.kind == "element":
            if self.detail and len(self.detail) <= 18:
                return self.detail
            return self.title.capitalize()
        return self.title

    def describe(self):
        """Tag text on the selected outline."""
        if self.kind == "window":
            return f"Window · {self.detail}"
        if self.kind == "element":
            return f"{self.title.capitalize()} · {self.detail}" if self.detail else self.title.capitalize()
        if self.kind == "screen":
            return f"Screen {self.detail}"
        return self.title


class Selection:
    """rect: global logical area. win: record through that window's own stream
    (unaffected by other windows covering it); None: record the screen area."""

    def __init__(self, rect, win, label):
        self.rect, self.win, self.label = rect, win, label
        self.show_cursor = True


class Picker(QObject):
    finished = pyqtSignal(object)  # Selection or None
    _segmented = pyqtSignal(str, object)

    def __init__(self, windows, inspector, own_pid, show_cursor=True):
        super().__init__()
        self.show_cursor = show_cursor
        self.windows = [w for w in reversed(windows)  # topmost first
                        if w["visible"] and (w["normal"] or w["dialog"]) and w["pid"] != own_pid
                        and w["cls"] != "xwaylandvideobridge"]
        self.inspector = inspector
        self.levels = []
        self.level = 1
        self.crumb_hover = None
        self.status = None
        self.cursor = None
        self.press = None
        self.drag = None
        self._level_by_key = {}
        self._key = object()
        self._wheel = 0
        self._done = False
        self._pixel_mode = {}   # window id -> bool, toggled with Tab
        self._segmenters = {}   # window id -> Segmenter | "pending" | None (failed)
        self._queue = []
        self._busy = None
        self._cond = threading.Condition()
        self._segmented.connect(self._on_segmented)
        self._timer = QTimer(self, singleShot=True, interval=16, timeout=self._refresh)
        self.overlays = [Overlay(self, s) for s in QGuiApplication.screens()]
        # capture + analyse windows in the background so areas are ready on hover
        for w in self.windows[:MAX_PREFETCH]:
            if self.pixel_mode(w):
                self._enqueue(w)
        threading.Thread(target=self._worker, daemon=True).start()

    def show(self):
        for o in self.overlays:
            o.present()

    # -- background area detection --

    def _enqueue(self, w, front=False):
        with self._cond:
            if self._segmenters.get(w["id"], "missing") not in ("missing", "pending") or w is self._busy:
                return  # done, failed, or being captured right now
            self._segmenters[w["id"]] = "pending"
            if w in self._queue:
                self._queue.remove(w)
            self._queue.insert(0, w) if front else self._queue.append(w)
            self._cond.notify()

    def _worker(self):
        from .cast import POINTER_HIDDEN, grab_frame, window_args
        from .visual import Segmenter
        while True:
            with self._cond:
                while not self._queue and not self._done:
                    self._cond.wait()
                if self._done:
                    return
                w = self._busy = self._queue.pop(0)
            try:
                frame = grab_frame(window_args(w["id"], POINTER_HIDDEN))
                h, wd = frame.shape[:2]
                ref = min((w["frame"], w["buffer"]), key=lambda r: abs(wd / r[2] - h / r[3]))
                seg = Segmenter(frame, ref)
                cx, cy, cw, ch = w["client"]
                seg.chain_at(cx + cw // 2, cy + ch // 2, w["client"])  # warm the cache here, not in the UI
            except Exception as e:
                print("screenrec: window capture failed:", e)
                seg = None
            with self._cond:
                self._segmenters[w["id"]] = seg
                self._busy = None
            self._segmented.emit(w["id"], seg)

    def _on_segmented(self, wid, seg):
        if self.levels and self.levels[-1].win and self.levels[-1].win["id"] == wid:
            self._refresh()

    def pixel_mode(self, w):
        return self._pixel_mode.get(w["id"], not self.inspector.has_tree(w))

    def toggle_mode(self):
        w = self.levels[-1].win if self.levels else None
        if w:
            self._pixel_mode[w["id"]] = not self.pixel_mode(w)
            self._refresh()

    # -- hierarchy under the cursor --

    def _screen_at(self, p):
        for s in QGuiApplication.screens():
            if s.geometry().contains(p):
                return s
        return QGuiApplication.primaryScreen()

    def _window_at(self, p):
        for w in self.windows:
            if QRect(*w["frame"]).contains(p):
                return w
        return None

    def _inner(self, w, p):
        if not self.pixel_mode(w):
            for el in self.inspector.chain_at(w, p.x(), p.y()):
                yield Level("element", el.role, el.name, QRect(*el.rect), w)
            return
        seg = self._segmenters.get(w["id"], "missing")
        if seg == "missing":
            self._enqueue(w, front=True)
            seg = "pending"
        elif seg == "pending":
            self._enqueue(w, front=True)  # hovered: analyse this one next
        if seg == "pending":
            self.status = "detecting areas…"
        elif seg is None:
            self.status = "can't look inside this window"
        else:
            for r in seg.chain_at(p.x(), p.y(), w["client"]):
                yield Level("area", "Area", "", QRect(*r), w)

    def _refresh(self):
        p = self.cursor
        if p is None:
            return
        scr = self._screen_at(p)
        g = scr.geometry()
        levels = [Level("screen", "Screen", scr.name(), g)]
        self.status = None
        w = self._window_at(p)
        if w:
            levels.append(Level("window", "Window", w["caption"] or w["cls"], QRect(*w["frame"]), w))
            client = QRect(*w["client"])
            if not _near(client, levels[-1].rect):
                levels.append(Level("content", "Content", "", client, w))
            bounds = QRect(*w["buffer"]).intersected(g)
            for lv in self._inner(w, p):
                lv.rect = lv.rect.intersected(bounds)
                if lv.rect.width() < 8 or lv.rect.height() < 8:
                    continue
                last = levels[-1]
                if _near(lv.rect, last.rect):
                    # same area: keep the most descriptive name
                    if lv.kind == "element" and (last.kind != "element" or last.title in GENERIC_ROLES
                                                 or not last.detail) \
                            and (lv.title not in GENERIC_ROLES or lv.detail) and last.kind != "window":
                        last.kind, last.title, last.detail = lv.kind, lv.title, lv.detail
                    continue
                levels.append(lv)
        key = w["id"] if w else ("screen", scr.name())
        if key != self._key:
            self._key = key
            self.level = self._level_by_key.get(key, 1 if w else 0)
        self.levels = levels
        self.level = max(0, min(self.level, len(levels) - 1))
        self._update()

    def current(self):
        if self.drag is not None:
            return self.drag
        if self.levels:
            return self.levels[self.level].rect
        return None

    def _update(self):
        for o in self.overlays:
            o.update()

    # -- input (called by overlays, global coordinates) --

    def mouse_move(self, p):
        self.cursor = p
        self.crumb_hover = None
        if self.press is not None and (self.drag is not None or (p - self.press).manhattanLength() > 6):
            bounds = self._screen_at(self.press).geometry()
            self.drag = QRect(self.press, p).normalized().intersected(bounds)
            self._update()
        else:
            self._timer.start()

    def hover_crumb(self, i):
        if i != self.crumb_hover:
            self.crumb_hover = i
            self._update()

    def mouse_press(self, p, button):
        if button == Qt.MouseButton.LeftButton:
            self.press = p
        elif button == Qt.MouseButton.RightButton:
            self._finish(None)

    def mouse_release(self, p, button):
        if button != Qt.MouseButton.LeftButton or self.press is None:
            return
        if self.drag is not None:
            self.mouse_move(p)
        press, drag = self.press, self.drag
        self.press = self.drag = None
        if drag is not None and drag.width() >= 16 and drag.height() >= 16:
            w = self._window_at(press)
            if w and not QRect(*w["frame"]).contains(drag):
                w = None
            self._finish(Selection(drag, w, "Region"))
        elif drag is None:
            self.cursor = p
            self._refresh()
            self.select_current()
        else:
            self._update()

    def wheel(self, dy):
        self._wheel += dy
        while abs(self._wheel) >= 120:
            step = 1 if self._wheel < 0 else -1  # scroll down = smaller (deeper)
            self._wheel += 120 * step
            self.step(step)

    def step(self, delta):
        self.set_level(self.level + delta)

    def set_level(self, i):
        if not self.levels:
            return
        self.level = max(0, min(i, len(self.levels) - 1))
        self._level_by_key[self._key] = self.level
        self._update()

    def select_current(self):
        if not self.levels:
            return
        lv = self.levels[self.level]
        self._finish(Selection(lv.rect, lv.win, lv.describe()))

    def toggle_cursor(self):
        self.show_cursor = not self.show_cursor
        self._update()

    def _finish(self, sel):
        if self._done:
            return
        if sel is not None:
            sel.show_cursor = self.show_cursor
        with self._cond:
            self._done = True
            self._cond.notify()
        for o in self.overlays:
            o.hide()
            o.deleteLater()
        self.finished.emit(sel)


def _dashed(color, width=1):
    pen = QPen(color, width)
    pen.setDashPattern([4, 4])
    return pen


class Overlay(QWidget):
    def __init__(self, picker, screen):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.picker = picker
        self.scr = screen
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setWindowTitle("screenrec picker")
        self.f_text = QFont(self.font())
        self.f_text.setPointSizeF(self.font().pointSizeF() + 0.5)
        self.f_bold = QFont(self.f_text)
        self.f_bold.setBold(True)
        self.f_small = QFont(self.font())
        self._crumbs = []       # [(local QRect, level index)]
        self._crumb_bar = QRect()
        self._hint_bar = QRect()
        self._cursor_toggle = QRect()
        self._toggle_hover = False

    def present(self):
        self.setScreen(self.scr)
        self.setGeometry(self.scr.geometry())
        self.showFullScreen()
        self.activateWindow()

    def _global(self, ev):
        return self.scr.geometry().topLeft() + ev.position().toPoint()

    def _crumb_at(self, local):
        for r, i in self._crumbs:
            if r.contains(local):
                return i
        return None

    def mouseMoveEvent(self, ev):
        local = ev.position().toPoint()
        if self.picker.press is None and self._crumb_bar.contains(local):
            self.picker.hover_crumb(self._crumb_at(local))  # selection stays frozen over the bar
            return
        if self.picker.press is None and self._hint_bar.contains(local):
            hover = self._cursor_toggle.contains(local)
            if hover != self._toggle_hover:
                self._toggle_hover = hover
                self.update()
            return
        if self._toggle_hover:
            self._toggle_hover = False
        self.picker.mouse_move(self._global(ev))

    def mousePressEvent(self, ev):
        local = ev.position().toPoint()
        if ev.button() == Qt.MouseButton.LeftButton and self._crumb_bar.contains(local):
            i = self._crumb_at(local)
            if i is not None:
                self.picker.set_level(i)
            return
        if ev.button() == Qt.MouseButton.LeftButton and self._hint_bar.contains(local):
            if self._cursor_toggle.contains(local):
                self.picker.toggle_cursor()
            return
        self.picker.mouse_press(self._global(ev), ev.button())

    def mouseReleaseEvent(self, ev):
        self.picker.mouse_release(self._global(ev), ev.button())

    def wheelEvent(self, ev):
        self.picker.wheel(ev.angleDelta().y())

    def keyPressEvent(self, ev):
        k = ev.key()
        if k == Qt.Key.Key_Escape:
            self.picker._finish(None)
        elif k in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.picker.select_current()
        elif k in (Qt.Key.Key_Down, Qt.Key.Key_Right, Qt.Key.Key_PageDown):
            self.picker.step(1)
        elif k in (Qt.Key.Key_Up, Qt.Key.Key_Left, Qt.Key.Key_PageUp):
            self.picker.step(-1)
        elif k == Qt.Key.Key_Tab:
            self.picker.toggle_mode()
        elif k == Qt.Key.Key_C:
            self.picker.toggle_cursor()

    # -- drawing --

    def paintEvent(self, _):
        pk = self.picker
        origin = self.scr.geometry().topLeft()
        dpr = self.scr.devicePixelRatio()
        p = QPainter(self)
        p.fillRect(self.rect(), DIM)
        here = pk.cursor is not None and self.scr.geometry().contains(pk.cursor)

        sel = pk.current()
        if sel is not None:
            local = sel.translated(-origin)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
            p.fillRect(local, Qt.GlobalColor.transparent)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

            if pk.drag is None:
                # the other levels: dashed, brighter for the smaller ones you can scroll into
                for i, lv in enumerate(pk.levels):
                    if i == pk.level or lv.kind == "screen":
                        continue
                    deeper = i > pk.level
                    r = lv.rect.translated(-origin).adjusted(0, 0, -1, -1)
                    p.setBrush(Qt.BrushStyle.NoBrush)
                    # dark line under white dashes: readable on light and dark content
                    p.setPen(QPen(QColor(0, 0, 0, 150 if deeper else 60), 1))
                    p.drawRect(r)
                    p.setPen(_dashed(QColor(255, 255, 255, 230 if deeper else 90)))
                    p.drawRect(r)

            p.setPen(QPen(ACCENT, 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(local.adjusted(1, 1, -1, -1))
            if pk.drag is not None:
                text = "Region"
            elif pk.levels:
                text = pk.levels[pk.level].describe()
            else:
                text = ""
            self._draw_tag(p, local, text, f"{round(sel.width() * dpr)} × {round(sel.height() * dpr)}")

        self._crumbs, self._crumb_bar = [], QRect()
        self._hint_bar = self._cursor_toggle = QRect()
        if here:
            self._draw_crumbs(p, pk)
            self._draw_hint(p)
        p.end()

    def _draw_tag(self, p, r, text, size):
        fm, fb = QFontMetrics(self.f_text), QFontMetrics(self.f_bold)
        h = fb.height() + 10
        avail = max(120, min(r.width() - 12, 700))
        size_w = fb.horizontalAdvance(size)
        text = fm.elidedText(text, Qt.TextElideMode.ElideRight, max(0, avail - size_w - 34))
        w = fb.horizontalAdvance(size) + (fm.horizontalAdvance(text) + 14 if text else 0) + 20
        x, y = r.left() + 6, r.top() + 6
        if r.height() < h + 16:  # too small to fit inside: put it above (or below)
            y = r.top() - h - 4 if r.top() - h - 4 >= 0 else r.bottom() + 4
        x = min(max(4, x), self.width() - w - 4)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(ACCENT)
        p.drawRect(QRect(x, y, w, h))
        p.setPen(QColor("#0b1a24"))
        p.setFont(self.f_bold)
        p.drawText(QPoint(x + 10, y + 5 + fb.ascent()), size)
        if text:
            p.setFont(self.f_text)
            p.drawText(QPoint(x + 10 + size_w + 14, y + 5 + fm.ascent()), text)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)

    def _draw_crumbs(self, p, pk):
        if not pk.levels or pk.drag is not None:
            return
        fm, fb = QFontMetrics(self.f_text), QFontMetrics(self.f_bold)
        sep = "  ›  "
        chips = []
        for i, lv in enumerate(pk.levels):
            t = fm.elidedText(lv.short(), Qt.TextElideMode.ElideRight, 180)
            chips.append((t, (fb if i == pk.level else fm).horizontalAdvance(t) + 20, i))
        extra = pk.status or ""
        w = lv_w = sum(c[1] for c in chips) + fm.horizontalAdvance(sep) * (len(chips) - 1)
        if extra:
            w += fm.horizontalAdvance(extra) + 24
        h = fb.height() + 16
        bar = QRect((self.width() - w - 24) // 2, 18, w + 24, h)
        self._crumb_bar = bar
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(PANEL_BG)
        p.drawRect(bar)
        x = bar.left() + 12
        for n, (t, cw, i) in enumerate(chips):
            r = QRect(x, bar.top() + 5, cw, h - 10)
            self._crumbs.append((r, i))
            cur = i == pk.level
            if cur or i == pk.crumb_hover:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(ACCENT if cur else CHIP_HOVER)
                p.drawRect(r)
            p.setFont(self.f_bold if cur else self.f_text)
            p.setPen(QColor("#0b1a24") if cur else TEXT if i > pk.level else MUTED)
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, t)
            x += cw
            if n < len(chips) - 1:
                p.setFont(self.f_text)
                p.setPen(MUTED)
                sw = fm.horizontalAdvance(sep)
                p.drawText(QRect(x, bar.top(), sw, h), Qt.AlignmentFlag.AlignCenter, sep)
                x += sw
        if extra:
            p.setFont(self.f_text)
            p.setPen(WARN)
            p.drawText(QRect(x + 12, bar.top(), w - lv_w, h), Qt.AlignmentFlag.AlignVCenter, extra)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)

    def _draw_hint(self, p):
        w_ = self.picker.levels[-1].win if self.picker.levels else None
        mode = None
        if w_:
            mode = "detect from pixels" if not self.picker.pixel_mode(w_) else "use accessibility"
        items = [("Scroll ▼ ▲", "smaller / bigger"), ("Click", "record"), ("Drag", "free region")]
        if mode:
            items.append(("Tab", mode))
        items.append(("C", "record cursor"))
        items.append(("Esc", "cancel"))
        fm, fb = QFontMetrics(self.f_text), QFontMetrics(self.f_bold)
        gap, inner = 26, 8
        box = fm.ascent()  # checkbox size
        widths = [fb.horizontalAdvance(k) + 14 + inner + fm.horizontalAdvance(d) + (box + 8 if k == "C" else 0)
                  for k, d in items]
        h = fb.height() + 18
        total = sum(widths) + gap * (len(items) - 1) + 36
        bar = QRect((self.width() - total) // 2, self.height() - h - 36, total, h)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(PANEL_BG)
        p.drawRect(bar)
        self._hint_bar = bar
        x = bar.left() + 18
        for (k, d), wd in zip(items, widths):
            kw = fb.horizontalAdvance(k) + 14
            if k == "C":  # clickable checkbox
                self._cursor_toggle = QRect(x - 6, bar.top() + 2, wd + 12, h - 4)
                if self._toggle_hover:
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(CHIP_BG)
                    p.drawRect(self._cursor_toggle)
            key = QRect(x, bar.top() + 6, kw, h - 12)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(CHIP_HOVER)
            p.drawRect(key)
            p.setFont(self.f_bold)
            p.setPen(TEXT)
            p.drawText(key, Qt.AlignmentFlag.AlignCenter, k)
            tx = x + kw + inner
            if k == "C":
                on = self.picker.show_cursor
                cb = QRect(tx, bar.top() + (h - box) // 2, box, box)
                p.setBrush(ACCENT if on else Qt.BrushStyle.NoBrush)
                p.setPen(QPen(ACCENT if on else MUTED, 1.5))
                p.drawRect(cb)
                if on:
                    p.setPen(QPen(QColor("#000000"), 2))
                    p.drawLine(cb.left() + 3, cb.center().y(), cb.center().x() - 1, cb.bottom() - 3)
                    p.drawLine(cb.center().x() - 1, cb.bottom() - 3, cb.right() - 3, cb.top() + 3)
                tx += box + 8
            p.setFont(self.f_text)
            p.setPen(TEXT if k == "C" else MUTED)
            p.drawText(QRect(tx, bar.top(), wd, h), Qt.AlignmentFlag.AlignVCenter, d)
            x += wd + gap
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
