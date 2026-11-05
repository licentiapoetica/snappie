"""Black square panels for the in-place overlay: tools + actions, and the
property strip (colours, width, fill, font size, …).

They're child widgets of the overlay (no separate windows, so nothing for
Wayland to misplace) and lay themselves out in rows that fit the screen.
Dragging a panel by its grip or any gap between buttons asks the overlay to
move it (the overlay keeps both panels together); a double-click puts them back.
"""

from PyQt6.QtCore import QPoint, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import QColorDialog, QLabel, QToolButton, QWidget

from .. import style
from .tools import ACTIONS, TOOLS, panel_icon, swatch_icon

BTN = 34
SEP = 9
PAD = 4


class _Sep(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.setFixedSize(SEP, BTN)
        self.setStyleSheet("background: transparent;")

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(SEP // 2, 7, 1, BTN - 14, QColor(255, 255, 255, 50))


class _Grip(QWidget):
    """Dotted handle; the press goes through to the panel, which does the dragging."""
    W = 14

    def __init__(self, parent):
        super().__init__(parent)
        self.setFixedSize(self.W, BTN)
        self.setStyleSheet("background: transparent;")
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setToolTip("Drag to move the toolbar · double-click to put it back")

    def paintEvent(self, _):
        p = QPainter(self)
        c = QColor(255, 255, 255, 110)
        for col in (0, 1):
            for row in range(4):
                p.fillRect(4 + col * 4, BTN // 2 - 7 + row * 4, 2, 2, c)


class Panel(QWidget):
    """Black square panel whose items flow into as many rows as needed."""
    drag = pyqtSignal(QPoint)       # wanted top-left, in the parent's coordinates
    drag_reset = pyqtSignal()

    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("panel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setStyleSheet(style.PANEL_QSS + "QLabel { color: #eff0f1; background: transparent; }")
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.items = []
        self._grab = None

    def grip(self):
        g = _Grip(self)
        self.items.append(g)
        return g

    def button(self, icon, tip, checkable=False, slot=None, text=None):
        b = QToolButton(self)
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.setFixedSize(BTN, BTN)
        b.setIconSize(QSize(20, 20))
        if icon is not None:
            b.setIcon(icon)
        if text:
            b.setText(text)
        b.setToolTip(tip)
        b.setCheckable(checkable)
        if slot:
            b.clicked.connect(slot)
        self.items.append(b)
        return b

    def sep(self):
        s = _Sep(self)
        self.items.append(s)
        return s

    def relayout(self, max_width):
        """Flow visible items into rows no wider than max_width; resize the panel."""
        items = []
        for w in self.items:
            if not w.isVisibleTo(self):
                continue
            if isinstance(w, _Sep) and (not items or isinstance(items[-1], _Sep)):
                w.move(-100, -100)          # no leading or doubled separators
                continue
            items.append(w)
        while items and isinstance(items[-1], _Sep):
            items.pop().move(-100, -100)
        x = y = PAD
        row_h = width = 0
        for w in items:
            ww, hh = w.width(), w.height()
            if x + ww + PAD > max_width and x > PAD:
                x, y, row_h = PAD, y + row_h, 0
                if isinstance(w, _Sep):
                    w.move(-100, -100)
                    continue
            w.move(x, y)
            x += ww
            row_h = max(row_h, hh)
            width = max(width, x)
        self.resize(width + PAD, y + row_h + PAD)
        return self.size()

    def wheelEvent(self, ev):
        ev.accept()     # don't let the overlay change the width behind the panel

    # Presses between buttons used to fall through to the canvas; now they drag the panel.
    # Positions are in the parent's coordinates: global ones aren't reliable on Wayland.
    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self._grab = ev.position().toPoint()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        ev.accept()

    def mouseMoveEvent(self, ev):
        if self._grab is not None:
            self.drag.emit(self.mapToParent(ev.position().toPoint()) - self._grab)
        ev.accept()

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton and self._grab is not None:
            self._grab = None
            self.setCursor(Qt.CursorShape.ArrowCursor)
        ev.accept()

    def mouseDoubleClickEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self.drag_reset.emit()
        ev.accept()


class ToolPanel(Panel):
    tool = pyqtSignal(str)
    action = pyqtSignal(str)

    def __init__(self, parent):
        super().__init__(parent)
        self.tools = {}
        self.grip()
        for tid, label, key, _, hint in TOOLS:
            name = "Reselect area" if tid == "crop" else label
            b = self.button(panel_icon(tid), f"<b>{name}</b> &nbsp;<span style='color:#9aa0a6'>{key}</span>",
                            checkable=True, slot=lambda _=False, t=tid: self.tool.emit(t))
            self.tools[tid] = b
            if tid in ("select", "text", "redact", "magnifier"):
                self.sep()
        self.sep()
        self.undo = self.button(panel_icon("undo"), "<b>Undo</b> &nbsp;<span style='color:#9aa0a6'>Ctrl+Z</span>",
                                slot=lambda: self.action.emit("undo"))
        self.redo = self.button(panel_icon("redo"), "<b>Redo</b> &nbsp;<span style='color:#9aa0a6'>Ctrl+Shift+Z"
                                "</span>", slot=lambda: self.action.emit("redo"))
        self.sep()
        for aid, label, key, _ in ACTIONS:
            if aid == "confirm":
                self.sep()
            self.button(panel_icon(aid), f"<b>{label}</b> &nbsp;<span style='color:#9aa0a6'>{key}</span>",
                        slot=lambda _=False, a=aid: self.action.emit(a))

    def set_tool(self, tool):
        for tid, b in self.tools.items():
            b.setChecked(tid == tool)


class _Value(QLabel):
    """Number between − and +; the mouse wheel changes it too."""
    wheel = pyqtSignal(int)

    def __init__(self, parent, width=44):
        super().__init__(parent)
        self.setFixedSize(width, BTN)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def wheelEvent(self, ev):
        d = ev.angleDelta().y()
        if d:
            self.wheel.emit(1 if d > 0 else -1)
        ev.accept()


class PropsPanel(Panel):
    """Colour palette and the properties of the current tool / selected shape."""
    changed = pyqtSignal(str, object)       # (property, value)

    def __init__(self, parent):
        super().__init__(parent)
        self.swatches = []
        for c in style.PALETTE:
            b = self.button(swatch_icon(c), c, checkable=True, slot=lambda _=False, c=c: self.changed.emit("color", c))
            self.swatches.append((c, b))
        self.custom = self.button(swatch_icon("#000", custom=True), "Custom colour…", slot=self._pick)
        self._color = style.PALETTE[0]
        self.groups = {"color": [b for _, b in self.swatches] + [self.custom, self.sep()]}
        self.groups["width"] = self._stepper("width", "Width", 1)
        self.fill = self.button(panel_icon("fill"), "<b>Fill</b> (translucent)", checkable=True,
                                slot=lambda on: self.changed.emit("fill", on))
        self.groups["fill"] = [self.fill]
        self.groups["font_size"] = self._stepper("font_size", "Size", 2)
        self.text_bg = self.button(panel_icon("text_bg"), "<b>Text on a box</b>", checkable=True,
                                   slot=lambda on: self.changed.emit("text_bg", on))
        self.groups["text_bg"] = [self.text_bg]
        self.groups["zoom"] = self._stepper("zoom", "Zoom", 1)
        from ..emoji import button_font
        self.emoji = self.button(None, "<b>Emoji</b> — pick the stamp", text="😂", slot=self._pick_emoji)
        self.emoji.setFont(button_font(18))
        self.groups["emoji"] = [self.emoji]
        self.groups["stamp"] = self._stepper("stamp", "Size", 8)
        self._values = {}

    def _stepper(self, name, tip, step):
        minus = self.button(panel_icon("minus"), f"Smaller {tip.lower()} (mouse wheel)",
                            slot=lambda: self._step(name, -step))
        val = _Value(self)
        val.setToolTip(f"{tip} — scroll to change")
        val.wheel.connect(lambda d: self._step(name, d * step))
        self.items.append(val)
        plus = self.button(panel_icon("plus"), f"Bigger {tip.lower()} (mouse wheel)",
                           slot=lambda: self._step(name, step))
        setattr(self, f"_{name}_label", val)
        return [minus, val, plus, self.sep()]

    def _step(self, name, d):
        if name in self._values:
            self.changed.emit(name, self._values[name] + d)

    def _pick_emoji(self):
        from ..emoji import Picker
        picker = Picker(self, "Click on the screenshot to place it")
        picker.chosen.connect(lambda e: self.changed.emit("emoji", e))
        picker.open_at(self.emoji)

    def _pick(self):
        c = QColorDialog.getColor(QColor(self._color), self.window(), "Annotation colour")
        if c.isValid():
            self.changed.emit("color", c.name())

    def show_props(self, kind, props, values):
        """Show only what applies to `kind`; False if nothing does."""
        self._values = dict(values)
        for name, widgets in self.groups.items():
            for w in widgets:
                w.setVisible(name in props)
        if "color" in props:
            self._color = values["color"]
            known = False
            for c, b in self.swatches:
                on = QColor(c) == QColor(values["color"])
                known |= on
                b.setChecked(False)
                b.setIcon(swatch_icon(c, selected=on))
            self.custom.setIcon(swatch_icon(values["color"], selected=not known) if not known
                                else swatch_icon("#000", custom=True))
        if "width" in props:
            unit = {"blur": "", "pixelate": ""}.get(kind, " px")
            self._width_label.setText(f"{values['width']}{unit}")
            tip = {"blur": "Blur strength", "pixelate": "Block size"}.get(kind, "Width")
            self._width_label.setToolTip(f"{tip} — scroll to change")
        if "font_size" in props:
            self._font_size_label.setText(str(values["font_size"]))
        if "zoom" in props:
            self._zoom_label.setText(f"{values['zoom']}×")
        if "emoji" in props:
            self.emoji.setText(values["emoji"])
        if "stamp" in props:
            self._stamp_label.setText(f"{int(values['stamp'])} px")
        self.fill.setChecked(bool(values.get("fill")))
        self.text_bg.setChecked(bool(values.get("text_bg")))
        return bool(props)
