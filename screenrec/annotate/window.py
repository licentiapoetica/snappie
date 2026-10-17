"""The full editor: a normal window with native widgets around the same
canvas engine the overlay uses.  Zoom, crop, all tools, and a "Frame" panel
for beautified exports (padding, background, rounded corners, shadow).
"""

import os

from PyQt6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import (QAction, QActionGroup, QColor, QGuiApplication, QIcon, QImage, QKeySequence, QPainter,
                         QPalette, QPixmap)
from PyQt6.QtWidgets import (QCheckBox, QColorDialog, QDockWidget, QFontComboBox, QFormLayout, QGridLayout,
                             QLabel, QMainWindow, QMessageBox, QScrollArea, QSizePolicy, QSpinBox, QToolBar,
                             QToolButton, QVBoxLayout, QWidget, QWidgetAction)

from .. import style
from . import beautify
from .canvas import Canvas, _settings
from .document import Document
from .tools import ACTIONS, TOOL_HINTS, TOOLS, swatch_icon, theme_icon

ZOOM_MIN, ZOOM_MAX = 0.05, 16.0


def _checker(size=8):
    pm = QPixmap(size * 2, size * 2)
    pm.fill(QColor("#ffffff"))
    p = QPainter(pm)
    for x, y in ((0, 0), (size, size)):
        p.fillRect(x, y, size, size, QColor("#d6d8da"))
    p.end()
    return pm


class CanvasView(QWidget):
    """Zoomable view of the document; feeds mouse and keys to the Canvas engine."""
    zoomed = pyqtSignal(float)

    MARGIN = 28

    def __init__(self, canvas):
        super().__init__()
        self.canvas = canvas
        self.doc = canvas.doc
        self.zoom = 1.0
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)
        self._backdrop = (None, None)
        self._checker = _checker()
        canvas.changed.connect(self._changed)
        canvas.tool_changed.connect(lambda _: self._changed())
        self._view = None
        self._changed()

    # -- geometry --

    def cropping(self):
        return self.canvas.tool == "crop"

    def frame_margins(self):
        """Frame margins in logical document units (l, t, r, b)."""
        if not self.doc.frame or self.cropping():
            return (0, 0, 0, 0)
        d = self.doc.dpr
        return tuple(v / d for v in beautify.margins(d, **self.doc.frame))

    def view_rect(self):
        if self.cropping():
            return self.doc.full_bounds()
        l, t, r, b = self.frame_margins()
        return self.doc.bounds().adjusted(-l, -t, r, b)

    def sizeHint(self):
        v = self.view_rect()
        return QSize(round(v.width() * self.zoom) + 2 * self.MARGIN, round(v.height() * self.zoom) + 2 * self.MARGIN)

    def _changed(self):
        v = self.view_rect()
        if v != self._view:
            self._view = v
            self.resize(self.sizeHint())
        self.update()

    def set_zoom(self, z, anchor=None):
        """anchor: widget position that should keep showing the same document point."""
        z = max(ZOOM_MIN, min(ZOOM_MAX, z))
        if abs(z - self.zoom) < 1e-6:
            return
        doc_pt = self.to_doc(anchor) if anchor is not None else None
        self.zoom = z
        self.canvas.scale = z
        self.resize(self.sizeHint())
        self.update()
        self.zoomed.emit(z)
        if doc_pt is not None:
            self._keep(doc_pt, anchor)

    def _keep(self, doc_pt, anchor):
        area = self._area()
        if area is None:
            return
        want = self.from_doc(doc_pt)
        h, v = area.horizontalScrollBar(), area.verticalScrollBar()
        h.setValue(round(h.value() + want.x() - anchor.x()))
        v.setValue(round(v.value() + want.y() - anchor.y()))

    def _area(self):
        p = self.parent()
        while p is not None and not isinstance(p, QScrollArea):
            p = p.parent()
        return p

    def to_doc(self, pos):
        v = self.view_rect()
        return QPointF((pos.x() - self.MARGIN) / self.zoom + v.left(), (pos.y() - self.MARGIN) / self.zoom + v.top())

    def from_doc(self, pt):
        v = self.view_rect()
        return QPointF((pt.x() - v.left()) * self.zoom + self.MARGIN, (pt.y() - v.top()) * self.zoom + self.MARGIN)

    # -- painting --

    def _frame_backdrop(self):
        key = (self.doc.image.cacheKey(), self.doc.pixel_rect(), repr(self.doc.frame))
        if self._backdrop[0] != key:
            content = self.doc.image.copy(self.doc.pixel_rect())
            content.setDevicePixelRatio(self.doc.dpr)
            img, _, _ = beautify.backdrop(content, **self.doc.frame)
            self._backdrop = (key, img)
        return self._backdrop[1]

    def paintEvent(self, ev):
        p = QPainter(self)
        p.fillRect(ev.rect(), self.palette().color(QPalette.ColorRole.Window).darker(112))
        v = self.view_rect()
        p.translate(self.MARGIN, self.MARGIN)
        p.scale(self.zoom, self.zoom)
        p.translate(-v.left(), -v.top())
        smooth = self.zoom * self.devicePixelRatioF() < self.doc.dpr * 0.999
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, smooth)
        bounds = self.doc.bounds()
        if self.cropping():
            full = self.doc.full_bounds()
            p.drawImage(full, self.doc.image)
            p.save()
            p.setClipRect(bounds)
            self.doc.paint(p, base=False, smooth=smooth)
            p.restore()
            p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            from PyQt6.QtGui import QPainterPath
            dim = QPainterPath()
            dim.addRect(full)
            hole = QPainterPath()
            hole.addRect(bounds)
            p.fillPath(dim.subtracted(hole), QColor(0, 0, 0, 140))
        else:
            frame = self.doc.frame
            if frame:
                l, t, _, _ = self.frame_margins()
                p.drawImage(QPointF(bounds.left() - l, bounds.top() - t), self._frame_backdrop())
                p.setClipPath(beautify.clip_path(bounds, frame.get("radius", 0)))
            else:
                p.setClipRect(bounds)
            if self.doc.image.hasAlphaChannel():
                p.save()
                p.resetTransform()
                tl, br = self.from_doc(bounds.topLeft()), self.from_doc(bounds.bottomRight())
                p.drawTiledPixmap(QRectF(tl, br), self._checker)
                p.restore()
            p.drawImage(QPointF(0, 0), self.doc.image)
            self.doc.paint(p, base=False, smooth=smooth)
            p.setClipping(False)
        self.canvas.paint_overlay(p)
        p.end()

    # -- input --

    def event(self, ev):
        # While typing into a text shape, letters must reach the canvas, not the tool shortcuts.
        if ev.type() == QEvent.Type.ShortcutOverride and self.canvas.editing is not None:
            ev.accept()
            return True
        return super().event(ev)

    def mousePressEvent(self, ev):
        self.setFocus()
        self.canvas.press(self.to_doc(ev.position()), ev.button(), ev.modifiers())

    def mouseMoveEvent(self, ev):
        pt = self.to_doc(ev.position())
        self.canvas.move(pt, ev.modifiers())
        self.setCursor(self.canvas.cursor_at(pt))

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self.canvas.release(self.to_doc(ev.position()), ev.modifiers())

    def mouseDoubleClickEvent(self, ev):
        if not self.canvas.double_click(self.to_doc(ev.position())):
            self.mousePressEvent(ev)

    def keyPressEvent(self, ev):
        if self.canvas.key(ev):
            return
        if ev.key() == Qt.Key.Key_Escape and self.canvas.tool == "crop":
            self.canvas.set_tool("select")
        elif ev.key() == Qt.Key.Key_Escape and self.canvas.selected is not None:
            self.canvas.select(None)
        else:
            super().keyPressEvent(ev)

    def wheelEvent(self, ev):
        if ev.modifiers() & Qt.KeyboardModifier.ControlModifier:
            d = ev.angleDelta().y()
            if d:
                self.set_zoom(self.zoom * (1.15 ** (d / 120)), ev.position())
            ev.accept()
        else:
            ev.ignore()     # scroll area scrolls


class ColorButton(QToolButton):
    """Colour chip with a palette popup and a 'Custom…' entry."""
    picked = pyqtSignal(str)

    def __init__(self, parent=None, tip="Colour"):
        super().__init__(parent)
        self.setToolTip(tip)
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._color = style.PALETTE[0]
        from PyQt6.QtWidgets import QMenu
        menu = QMenu(self)
        grid_w = QWidget()
        grid = QGridLayout(grid_w)
        grid.setContentsMargins(6, 6, 6, 6)
        grid.setSpacing(4)
        for i, c in enumerate(style.PALETTE):
            b = QToolButton()
            b.setAutoRaise(True)
            b.setIcon(swatch_icon(c, 22))
            b.setIconSize(QSize(22, 22))
            b.setToolTip(c)
            b.clicked.connect(lambda _=False, c=c: (menu.close(), self.set_color(c), self.picked.emit(c)))
            grid.addWidget(b, i // 4, i % 4)
        wa = QWidgetAction(menu)
        wa.setDefaultWidget(grid_w)
        menu.addAction(wa)
        menu.addSeparator()
        menu.addAction(QIcon.fromTheme("color-picker"), "Custom colour…", self._custom)
        self.setMenu(menu)
        self.set_color(self._color)

    def _custom(self):
        c = QColorDialog.getColor(QColor(self._color), self.window(), "Annotation colour")
        if c.isValid():
            self.set_color(c.name())
            self.picked.emit(c.name())

    def set_color(self, c):
        self._color = c
        self.setIcon(swatch_icon(c, 20))

    def color(self):
        return self._color


class FramePanel(QWidget):
    """Beautify options, stored in doc.frame (None = no frame)."""

    DEFAULT = {"padding": 48, "background": beautify.GRADIENTS[0], "radius": 10, "shadow": True}

    def __init__(self, doc):
        super().__init__()
        self.doc = doc
        s = _settings()
        last = (s.get("annot.frame", None) if s else None) or {}
        self._last = dict(self.DEFAULT, **{k: v for k, v in last.items() if k in self.DEFAULT})
        if isinstance(self._last["background"], list):
            self._last["background"] = tuple(self._last["background"])
        lay = QVBoxLayout(self)
        self.enabled = QCheckBox("Add a frame around the image")
        lay.addWidget(self.enabled)
        form = QFormLayout()
        self.padding = QSpinBox(suffix=" px", maximum=400, singleStep=4)
        self.radius = QSpinBox(suffix=" px", maximum=200)
        self.shadow = QCheckBox("Drop shadow")
        form.addRow("Padding", self.padding)
        form.addRow("Corners", self.radius)
        form.addRow("", self.shadow)
        lay.addLayout(form)
        lay.addWidget(QLabel("Background"))
        grid = QGridLayout()
        grid.setSpacing(4)
        self.bg_buttons = []
        presets = [None] + list(beautify.GRADIENTS) + list(beautify.SOLIDS)
        for i, bg in enumerate(presets):
            b = QToolButton()
            b.setCheckable(True)
            b.setAutoRaise(True)
            b.setIconSize(QSize(34, 24))
            b.setIcon(self._bg_icon(bg))
            b.setToolTip("Transparent" if bg is None else (f"{bg[1]} → {bg[2]}" if isinstance(bg, tuple) else bg))
            b.clicked.connect(lambda _=False, bg=bg: self._set("background", bg))
            grid.addWidget(b, i // 4, i % 4)
            self.bg_buttons.append((bg, b))
        self.custom_bg = QToolButton(text="Custom…")
        self.custom_bg.clicked.connect(self._custom_bg)
        grid.addWidget(self.custom_bg, len(presets) // 4 + 1, 0, 1, 4)
        lay.addLayout(grid)
        lay.addStretch(1)
        self.enabled.toggled.connect(self._toggle)
        self.padding.valueChanged.connect(lambda v: self._set("padding", v))
        self.radius.valueChanged.connect(lambda v: self._set("radius", v))
        self.shadow.toggled.connect(lambda v: self._set("shadow", v))
        doc.changed.connect(self.sync)
        self.sync()

    @staticmethod
    def _bg_icon(bg):
        img = QImage(68, 48, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(Qt.GlobalColor.transparent)
        p = QPainter(img)
        if bg is None:
            p.drawTiledPixmap(img.rect(), _checker(6))
        else:
            beautify.fill_background(p, img.rect(), bg)
        p.setPen(QColor(0, 0, 0, 60))
        p.drawRect(img.rect().adjusted(0, 0, -1, -1))
        p.end()
        img.setDevicePixelRatio(2)
        return QIcon(QPixmap.fromImage(img))

    def _custom_bg(self):
        cur = self.doc.frame.get("background") if self.doc.frame else None
        c = QColorDialog.getColor(QColor(cur if isinstance(cur, str) else "#ffffff"), self.window(), "Background")
        if c.isValid():
            self._set("background", c.name())

    def _toggle(self, on):
        if on == bool(self.doc.frame):
            return
        self.doc.modify(lambda: setattr(self.doc, "frame", dict(self._last) if on else None))

    def _set(self, key, value):
        if not self.doc.frame:
            self._last[key] = value
            self.enabled.setChecked(True)
            return
        if self.doc.frame.get(key) == value:
            return
        frame = dict(self.doc.frame, **{key: value})
        self.doc.modify(lambda: setattr(self.doc, "frame", frame), merge=("frame", key))
        self._last = dict(frame)
        s = _settings()
        if s:
            try:
                s.set("annot.frame", {k: list(v) if isinstance(v, tuple) else v for k, v in frame.items()})
            except OSError:
                pass

    def sync(self):
        f = self.doc.frame
        cur = f or self._last
        for w in (self.enabled, self.padding, self.radius, self.shadow):
            w.blockSignals(True)
        self.enabled.setChecked(bool(f))
        self.padding.setValue(int(cur.get("padding", 0)))
        self.radius.setValue(int(cur.get("radius", 0)))
        self.shadow.setChecked(bool(cur.get("shadow")))
        for w in (self.enabled, self.padding, self.radius, self.shadow):
            w.blockSignals(False)
        bg = cur.get("background")
        bg = tuple(bg) if isinstance(bg, list) else bg
        for b_bg, b in self.bg_buttons:
            b.setChecked(b_bg == bg)


class EditorWindow(QMainWindow):
    action = pyqtSignal(str, object)        # copy, save, save_as, upload, pin, ocr, closed

    def __init__(self, doc: Document, path: str | None = None):
        super().__init__()
        self.doc = doc
        self.path = path
        self._used = doc.revision           # revision at the last action (copy/save/…)
        self._closing = False
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.canvas = Canvas(doc, parent=self)
        self.view = CanvasView(self.canvas)
        self.area = QScrollArea()
        self.area.setWidget(self.view)
        self.area.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.area.setWidgetResizable(False)
        self.area.setFrameShape(QScrollArea.Shape.NoFrame)
        self.setCentralWidget(self.area)
        self._build_actions()
        self._build_props()
        self._build_frame_dock()
        self._build_status()
        self.canvas.tool_changed.connect(self._tool_changed)
        self.canvas.selection_changed.connect(self._sync_props)
        self.canvas.changed.connect(self._sync_state)
        self.view.zoomed.connect(lambda _: self._sync_state())
        self._tool_changed(self.canvas.tool)
        self._sync_state()
        self._initial_size()
        close = self._act("Close", QIcon.fromTheme("window-close"), self.close, "Ctrl+W")
        close.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        self.view.setFocus()

    @classmethod
    def open_file(cls, path):
        doc = Document.from_file(path)
        scr = QGuiApplication.primaryScreen()
        if scr:   # a screenshot from this machine: annotate at the same scale as in-place
            doc.image.setDevicePixelRatio(scr.devicePixelRatio())
        return cls(doc, path)

    # -- building --

    def _act(self, text, icon, slot, shortcut=None, tip=None, checkable=False):
        a = QAction(icon, text, self)
        if shortcut:
            a.setShortcuts([QKeySequence(s) for s in (shortcut if isinstance(shortcut, list) else [shortcut])])
        tip = tip or text
        a.setToolTip(f"{tip} ({a.shortcut().toString(QKeySequence.SequenceFormat.NativeText)})"
                     if shortcut else tip)
        a.setCheckable(checkable)
        a.triggered.connect(slot)
        self.addAction(a)
        return a

    def _build_actions(self):
        main = QToolBar("Main")
        main.setObjectName("main")
        main.setMovable(False)
        main.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, main)
        self.actions_ = {}
        for aid, label, key, icon in ACTIONS:
            if aid in ("confirm", "cancel", "edit"):
                continue
            a = self._act(label, theme_icon(icon, aid), lambda _=False, n=aid: self._emit(n), key)
            self.actions_[aid] = a
            main.addAction(a)
            if aid == "save_as":
                main.addSeparator()
        main.addSeparator()
        self.undo_act = self._act("Undo", theme_icon("edit-undo", "undo"), self.canvas.undo, "Ctrl+Z")
        self.redo_act = self._act("Redo", theme_icon("edit-redo", "redo"), self.canvas.redo,
                                  ["Ctrl+Shift+Z", "Ctrl+Y"])
        for a in (self.undo_act, self.redo_act):
            main.addAction(a)
            main.widgetForAction(a).setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        main.addWidget(spacer)
        zoom_out = self._act("Zoom out", QIcon.fromTheme("zoom-out"), lambda: self._zoom_step(1 / 1.25),
                             ["Ctrl+-"])
        zoom_in = self._act("Zoom in", QIcon.fromTheme("zoom-in"), lambda: self._zoom_step(1.25),
                            ["Ctrl++", "Ctrl+="])
        fit = self._act("Fit", QIcon.fromTheme("zoom-fit-best"), self.zoom_fit, "Ctrl+0", "Fit to window")
        actual = self._act("100%", QIcon.fromTheme("zoom-original"), self.zoom_actual, "Ctrl+1", "Actual size")
        for a in (zoom_out, zoom_in, fit, actual):
            main.addAction(a)
            main.widgetForAction(a).setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)

        tools = QToolBar("Tools")
        tools.setObjectName("tools")
        tools.setMovable(False)
        tools.setIconSize(QSize(22, 22))
        self.addToolBar(Qt.ToolBarArea.LeftToolBarArea, tools)
        group = QActionGroup(self)
        self.tool_acts = {}
        for tid, label, key, icon, hint in TOOLS:
            a = self._act(label, theme_icon(icon, tid), lambda _=False, t=tid: self.canvas.set_tool(t), key,
                          checkable=True)
            group.addAction(a)
            tools.addAction(a)
            self.tool_acts[tid] = a
            if tid in ("select", "text", "redact", "magnifier"):
                tools.addSeparator()

    def _build_props(self):
        bar = QToolBar("Properties")
        bar.setObjectName("props")
        bar.setMovable(False)
        self.addToolBarBreak()
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, bar)
        self.props_bar = bar
        self.prop_acts = {}

        def add(name, *widgets):
            acts = [bar.addWidget(w) for w in widgets]
            acts.append(bar.addSeparator())
            self.prop_acts[name] = acts

        self.color_btn = ColorButton(tip="Colour")
        self.color_btn.picked.connect(lambda c: self.canvas.set_prop("color", c))
        add("color", self.color_btn)
        self.width_label = QLabel(" Width ")
        self.width_spin = QSpinBox(minimum=1, maximum=40, suffix=" px")
        self.width_spin.valueChanged.connect(lambda v: self.canvas.set_prop("width", v))
        add("width", self.width_label, self.width_spin)
        self.fill_box = QCheckBox("Fill")
        self.fill_box.setToolTip("Fill the shape (translucent)")
        self.fill_box.toggled.connect(lambda v: self.canvas.set_prop("fill", v))
        add("fill", self.fill_box)
        self.font_box = QFontComboBox()
        self.font_box.setMaximumWidth(220)
        self.font_box.currentFontChanged.connect(lambda f: self.canvas.set_prop("font", f.family()))
        add("font", QLabel(" Font "), self.font_box)
        self.size_label = QLabel(" Size ")
        self.size_spin = QSpinBox(minimum=8, maximum=160)
        self.size_spin.valueChanged.connect(lambda v: self.canvas.set_prop("font_size", v))
        add("font_size", self.size_label, self.size_spin)
        self.bg_box = QCheckBox("Box")
        self.bg_box.setToolTip("Text on a filled box")
        self.bg_box.toggled.connect(lambda v: self.canvas.set_prop("text_bg", v))
        add("text_bg", self.bg_box)
        self.zoom_spin = QSpinBox(minimum=2, maximum=6, suffix="×")
        self.zoom_spin.valueChanged.connect(lambda v: self.canvas.set_prop("zoom", v))
        add("zoom", QLabel(" Zoom "), self.zoom_spin)
        from PyQt6.QtWidgets import QToolButton
        from ..emoji import Picker, button_font
        self.emoji_btn = QToolButton()
        self.emoji_btn.setText("😂")
        self.emoji_btn.setFont(button_font(18))
        self.emoji_btn.setToolTip("Pick the emoji to stamp")

        def pick_emoji():
            picker = Picker(self.emoji_btn, "Click on the image to place it")
            picker.chosen.connect(lambda e: self.canvas.set_prop("emoji", e))
            picker.open_at(self.emoji_btn)
        self.emoji_btn.clicked.connect(pick_emoji)
        add("emoji", QLabel(" Emoji "), self.emoji_btn)
        self.stamp_spin = QSpinBox(minimum=16, maximum=600, singleStep=8, suffix=" px")
        self.stamp_spin.valueChanged.connect(lambda v: self.canvas.set_prop("stamp", v))
        add("stamp", QLabel(" Size "), self.stamp_spin)
        self.props_hint = QLabel()
        self.props_hint.setContentsMargins(8, 0, 8, 0)
        self.props_hint.setEnabled(False)
        bar.addWidget(self.props_hint)

    def _build_frame_dock(self):
        self.frame_panel = FramePanel(self.doc)
        dock = QDockWidget("Frame", self)
        dock.setObjectName("frame")
        dock.setWidget(self.frame_panel)
        dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable
                         | QDockWidget.DockWidgetFeature.DockWidgetMovable)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        self.frame_dock = dock
        toggle = dock.toggleViewAction()
        toggle.setIcon(QIcon.fromTheme("select-rectangular"))
        toggle.setToolTip("Show the frame options (padding, background, shadow)")
        self.findChild(QToolBar, "main").addAction(toggle)
        if not self.doc.frame:
            dock.hide()

    def _build_status(self):
        sb = self.statusBar()
        self.hint = QLabel()
        sb.addWidget(self.hint, 1)
        self.size_label_sb = QLabel()
        self.zoom_label = QLabel()
        sb.addPermanentWidget(self.size_label_sb)
        sb.addPermanentWidget(self.zoom_label)

    def _initial_size(self):
        scr = self.screen() or QGuiApplication.primaryScreen()
        avail = scr.availableGeometry() if scr else None
        hint = self.view.sizeHint()
        w, h = hint.width() + 120, hint.height() + 160
        if avail is not None:
            w, h = min(w, int(avail.width() * 0.85)), min(h, int(avail.height() * 0.85))
        self.resize(max(w, 900), max(h, 560))
        b = self.doc.bounds()
        if avail is not None and (b.width() > w - 140 or b.height() > h - 200):
            vw, vh = w - 140, h - 200
            self.view.set_zoom(min(vw / b.width(), vh / b.height()))
        else:
            self.zoom_actual()

    # -- state --

    def _emit(self, name):
        self.canvas.finish_edit()
        self._used = self.doc.revision
        self._sync_state()
        self.action.emit(name, self.doc)

    def mark_saved(self, path=None):
        """Tell the window its document was saved (updates the title / unsaved-changes check)."""
        if path:
            self.path = path
        self._used = self.doc.revision
        self._sync_state()

    def _tool_changed(self, tool):
        a = self.tool_acts.get(tool)
        if a and not a.isChecked():
            a.setChecked(True)
        self.hint.setText(TOOL_HINTS.get(tool, ""))
        self.view.setCursor(Qt.CursorShape.CrossCursor)
        self._sync_props()

    def _sync_props(self):
        props, values = self.canvas.active_props()
        for name, acts in self.prop_acts.items():
            for a in acts:
                a.setVisible(name in props)
        widgets = (self.width_spin, self.fill_box, self.size_spin, self.bg_box, self.zoom_spin, self.font_box,
                   self.stamp_spin)
        for w in widgets:
            w.blockSignals(True)
        kind = self.canvas.active_kind()
        if "color" in props:
            self.color_btn.set_color(values["color"])
        if "width" in props:
            self.width_spin.setValue(int(values["width"]))
            self.width_label.setText({"blur": " Strength ", "pixelate": " Block size "}.get(kind, " Width "))
            self.width_spin.setSuffix("" if kind in ("blur", "pixelate") else " px")
        if "fill" in props:
            self.fill_box.setChecked(bool(values["fill"]))
        if "font_size" in props:
            self.size_spin.setValue(int(values["font_size"]))
        if "text_bg" in props:
            self.bg_box.setChecked(bool(values["text_bg"]))
        if "zoom" in props:
            self.zoom_spin.setValue(int(values["zoom"]))
        if "emoji" in props:
            self.emoji_btn.setText(values["emoji"])
        if "stamp" in props:
            self.stamp_spin.setValue(int(values["stamp"]))
        if "font" in props:
            from PyQt6.QtGui import QFont
            self.font_box.setCurrentFont(QFont(values["font"]) if values["font"] else QFont())
        for w in widgets:
            w.blockSignals(False)
        sel = self.canvas.selected
        self.props_hint.setText("" if props else ("Select a shape to change it" if kind == "select" else ""))
        if sel is not None:
            self.props_hint.setText("Editing the selected shape · Delete removes it")

    def _sync_state(self):
        self.undo_act.setEnabled(self.doc.can_undo())
        self.redo_act.setEnabled(self.doc.can_redo())
        size = self.doc.output_size()
        self.size_label_sb.setText(f"{size.width()} × {size.height()} px")
        self.zoom_label.setText(f"{self.display_zoom() * 100:.0f} %")
        name = os.path.basename(self.path) if self.path else "Screenshot"
        dirty = " *" if self.doc.revision != self._used else ""
        self.setWindowTitle(f"{name}{dirty} — screenrec editor")

    # -- zoom --

    def display_zoom(self):
        """Zoom in device pixels per image pixel (100 % = pixel for pixel)."""
        return self.view.zoom * self.view.devicePixelRatioF() / self.doc.dpr

    def zoom_actual(self):
        self.view.set_zoom(self.doc.dpr / self.view.devicePixelRatioF())
        self._sync_state()

    def zoom_fit(self):
        v = self.view.view_rect()
        vp = self.area.viewport().size()
        m = 2 * CanvasView.MARGIN
        z = min((vp.width() - m) / max(1.0, v.width()), (vp.height() - m) / max(1.0, v.height()))
        self.view.set_zoom(min(z, self.doc.dpr / self.view.devicePixelRatioF()))
        self._sync_state()

    def _zoom_step(self, f):
        vp = self.area.viewport()
        anchor = self.view.mapFrom(vp, vp.rect().center())
        self.view.set_zoom(self.view.zoom * f, QPointF(anchor))

    # -- closing --

    def closeEvent(self, ev):
        self.canvas.finish_edit()
        if not self._closing and self.doc.revision != self._used:
            box = QMessageBox(QMessageBox.Icon.Question, "Unsaved changes",
                              "The image has changes that weren't saved, copied or uploaded.",
                              QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
                              | QMessageBox.StandardButton.Cancel, self)
            r = box.exec()
            if r == QMessageBox.StandardButton.Cancel:
                ev.ignore()
                return
            if r == QMessageBox.StandardButton.Save:
                self._emit("save")
        self._closing = True
        self.canvas.detach()
        self.doc.changed.disconnect(self.frame_panel.sync)
        self.action.emit("closed", self.doc)
        super().closeEvent(ev)
