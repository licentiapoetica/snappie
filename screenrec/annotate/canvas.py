"""The tool engine shared by the in-place overlay and the editor window.

Hosts translate their mouse/key events into document coordinates and call
press / move / release / double_click / key; they paint the document with
`doc.paint(p)` and then `canvas.paint_overlay(p)` (handles, caret, crop
frame) with the painter in document coordinates.  `scale` tells the engine
how many host pixels one document pixel takes, so handles and click
tolerances stay the same size on screen at any zoom.
"""

import math

from PyQt6.QtCore import QLineF, QObject, QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QGuiApplication, QPen

from .. import style
from . import shapes
from .tools import TOOL_KEYS

LEFT = Qt.MouseButton.LeftButton
SHIFT = Qt.KeyboardModifier.ShiftModifier
CTRL = Qt.KeyboardModifier.ControlModifier
ALT = Qt.KeyboardModifier.AltModifier

LIMITS = {"width": (1, 40), "font_size": (8, 160), "zoom": (2, 6), "stamp": (16, 600)}

_CURSORS = {"tl": Qt.CursorShape.SizeFDiagCursor, "br": Qt.CursorShape.SizeFDiagCursor,
            "tr": Qt.CursorShape.SizeBDiagCursor, "bl": Qt.CursorShape.SizeBDiagCursor,
            "t": Qt.CursorShape.SizeVerCursor, "b": Qt.CursorShape.SizeVerCursor,
            "l": Qt.CursorShape.SizeHorCursor, "r": Qt.CursorShape.SizeHorCursor}


def _settings():
    try:
        from .. import settings
        return settings
    except Exception:       # noqa: BLE001 - the editor must work without a config
        return None


class Props:
    """Current tool properties, remembered between captures (annot.* settings).

    Highlighter, marker and redact keep their own colour: yellow highlighting
    and black redaction shouldn't turn red because the last arrow was red.
    """
    TOOL_COLORS = {"highlighter": "#fdbc4b", "marker": "#fdbc4b", "redact": "#000000"}

    def __init__(self):
        s = _settings()
        get = (lambda k, d=None: s.get(k, d)) if s else (lambda k, d=None: d)
        self.color = get("annot.color") or style.PALETTE[0]
        self.width = int(get("annot.width") or 4)
        self.font_size = int(get("annot.font_size") or 18)
        self.font = get("annot.font") or ""
        self.text_bg = bool(get("annot.text_bg", False))
        self.fill = bool(get("annot.fill", False))
        self.zoom = int(get("annot.zoom", 2) or 2)
        self.emoji = get("annot.emoji") or "😂"
        self.stamp = int(get("annot.stamp", 64) or 64)
        self.tool_colors = dict(self.TOOL_COLORS, **(get("annot.tool_colors", {}) or {}))
        self.last_tool = get("annot.tool", "arrow") or "arrow"

    def color_for(self, tool):
        return self.tool_colors.get(tool, self.color)

    def values(self, tool):
        return {"color": self.color_for(tool), "width": self.width, "font_size": self.font_size,
                "font": self.font, "text_bg": self.text_bg, "fill": self.fill, "zoom": self.zoom,
                "emoji": self.emoji, "stamp": self.stamp}

    def set(self, tool, name, value):
        if name == "color" and tool in self.TOOL_COLORS:
            self.tool_colors[tool] = value
            self._save("annot.tool_colors", dict(self.tool_colors))
            return
        setattr(self, name, value)
        self._save(f"annot.{name}", value)

    @staticmethod
    def _save(key, value):
        s = _settings()
        if s:
            try:
                s.set(key, value)
            except OSError:
                pass


class Canvas(QObject):
    changed = pyqtSignal()              # repaint
    selection_changed = pyqtSignal()    # selected shape / active properties changed
    tool_changed = pyqtSignal(str)

    def __init__(self, doc, inplace=False, parent=None):
        super().__init__(parent)
        self.doc = doc
        self.inplace = inplace          # crop = the selection: always editable, re-selectable
        self.props = Props()
        t = self.props.last_tool
        self.tool = t if t in TOOL_KEYS.values() and t != "crop" else "arrow"
        self.scale = 1.0
        self.selected = None
        self.editing = None             # Text shape being typed into
        self.caret = 0
        self.op = None
        self.snap_rects = []            # QRectF (doc coords) to snap a new selection to
        self._caret_on = True
        self._blink = QTimer(self, interval=530, timeout=self._toggle_caret)
        doc.changed.connect(self._doc_changed)

    def detach(self):
        """Stop following the document (its host is going away, the document isn't)."""
        self.finish_edit()
        self.abort()
        self._blink.stop()
        try:
            self.doc.changed.disconnect(self._doc_changed)
        except TypeError:
            pass

    # -- state --

    def tol(self):
        return 4.0 / self.scale

    def crop_active(self):
        return self.inplace or self.tool == "crop"

    def _doc_changed(self):
        live = {id(s) for s in self.doc.shapes}
        if self.selected is not None and id(self.selected) not in live:
            self.selected = None
            self.editing = None
            self._blink.stop()
            self.selection_changed.emit()
        self.changed.emit()

    def set_tool(self, tool):
        self.finish_edit()
        self.abort()
        if tool != "select":
            self.select(None)
        self.tool = tool
        if tool != "crop" and tool != self.props.last_tool:
            self.props.last_tool = tool
            Props._save("annot.tool", tool)
        self.tool_changed.emit(tool)
        self.selection_changed.emit()
        self.changed.emit()

    def select(self, s):
        if s is not self.selected:
            self.selected = s
            self.selection_changed.emit()
            self.changed.emit()

    def shape_at(self, pt, kinds=None):
        tol = self.tol()
        for s in reversed(self.doc.shapes):
            if kinds and s.kind not in kinds:
                continue
            part = s.hit(pt, tol)
            if part:
                return s, part
        return None, None

    # -- properties --

    def active_props(self):
        """(props tuple, values dict) the property bar should show right now."""
        s = self.editing or self.selected
        if s is not None:
            return s.props, {k: getattr(s, k) for k in s.props}
        cls = shapes.KINDS.get(self.tool)
        if cls is None:
            return (), {}
        return cls.props, self.props.values(self.tool)

    def active_kind(self):
        s = self.editing or self.selected
        return s.kind if s is not None else self.tool

    def set_prop(self, name, value):
        lo_hi = LIMITS.get(name)
        if lo_hi:
            value = max(lo_hi[0], min(lo_hi[1], value))
        s = self.editing or self.selected
        self.props.set(s.kind if s is not None else self.tool, name, value)
        if s is not None and name in s.props and getattr(s, name) != value:
            if self.editing is not None:
                setattr(s, name, value)       # part of the open text edit
                self.changed.emit()
            else:
                self.doc.modify(lambda: setattr(s, name, value), merge=("prop", name, id(s)))
        self.selection_changed.emit()

    def bump(self, steps):
        """Mouse wheel: change the width (or font size) of the tool / selected shape."""
        props, values = self.active_props()
        if "stamp" in props:
            self.set_prop("stamp", values["stamp"] + 8 * steps)
        elif "width" in props:
            self.set_prop("width", values["width"] + steps)
        elif "font_size" in props:
            self.set_prop("font_size", values["font_size"] + 2 * steps)

    # -- text editing --

    def start_edit(self, s, caret=None):
        if self.editing is s:
            return
        self.finish_edit()
        self.doc.begin()
        self.editing = s
        self.selected = s
        self.caret = len(s.text) if caret is None else caret
        self._show_caret()
        self.selection_changed.emit()
        self.changed.emit()

    def finish_edit(self):
        s = self.editing
        if s is None:
            return
        self.editing = None
        self._blink.stop()
        if not s.text.strip():
            self.doc.shapes[:] = [x for x in self.doc.shapes if x is not s]
            self.selected = None
        self.doc.commit()
        self.selection_changed.emit()
        self.changed.emit()

    def _show_caret(self):
        self._caret_on = True
        self._blink.start()

    def _toggle_caret(self):
        self._caret_on = not self._caret_on
        self.changed.emit()

    def _insert(self, text):
        s = self.editing
        s.text = s.text[:self.caret] + text + s.text[self.caret:]
        self.caret += len(text)

    def _edit_key(self, ev):
        k, mods, s = ev.key(), ev.modifiers(), self.editing
        ctrl = bool(mods & CTRL)
        if k == Qt.Key.Key_Escape or (ctrl and k in (Qt.Key.Key_Return, Qt.Key.Key_Enter)):
            self.finish_edit()
        elif ctrl and k == Qt.Key.Key_Z:
            self.finish_edit()
            self.doc.undo()
        elif k in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._insert("\n")
        elif k == Qt.Key.Key_Backspace:
            if self.caret:
                n = 1
                if ctrl:  # whole word
                    t = s.text[:self.caret].rstrip()
                    n = self.caret - (len(t) - len(t.split()[-1]) if t.split() else 0)
                s.text = s.text[:self.caret - n] + s.text[self.caret:]
                self.caret -= n
        elif k == Qt.Key.Key_Delete:
            s.text = s.text[:self.caret] + s.text[self.caret + 1:]
        elif k == Qt.Key.Key_Left:
            self.caret = max(0, self.caret - 1)
        elif k == Qt.Key.Key_Right:
            self.caret = min(len(s.text), self.caret + 1)
        elif k == Qt.Key.Key_Home:
            self.caret = s.text.rfind("\n", 0, self.caret) + 1
        elif k == Qt.Key.Key_End:
            e = s.text.find("\n", self.caret)
            self.caret = len(s.text) if e < 0 else e
        elif k in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            line = s.caret_line(self.caret)
            dy = line.dy() if k == Qt.Key.Key_Down else -line.dy()
            self.caret = s.index_at(QPointF(line.x1(), line.center().y() + dy))
        elif ctrl and k == Qt.Key.Key_V:
            self._insert(QGuiApplication.clipboard().text())
        elif ctrl and k == Qt.Key.Key_A:
            self.caret = len(s.text)
        elif ev.text() and ev.text().isprintable() and not ctrl:
            self._insert(ev.text())
        else:
            return k not in (Qt.Key.Key_Tab,)   # swallow the rest while typing
        self._show_caret()
        self.changed.emit()
        return True

    # -- keyboard --

    def key(self, ev):
        """Handle a key press; False if the host should handle it."""
        if self.editing is not None:
            return self._edit_key(ev)
        k, mods = ev.key(), ev.modifiers()
        ctrl, shift = bool(mods & CTRL), bool(mods & SHIFT)
        if k == Qt.Key.Key_Escape and self.op:
            self.abort()
            return True
        if ctrl and k == Qt.Key.Key_Z:
            self.redo() if shift else self.undo()
            return True
        if ctrl and k == Qt.Key.Key_Y:
            self.redo()
            return True
        if k in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and self.selected is not None:
            self.delete_selected()
            return True
        arrows = {Qt.Key.Key_Left: (-1, 0), Qt.Key.Key_Right: (1, 0), Qt.Key.Key_Up: (0, -1),
                  Qt.Key.Key_Down: (0, 1)}
        if k in arrows and not ctrl:
            dx, dy = arrows[k]
            self.nudge(dx, dy, shift)
            return True
        if ctrl and k == Qt.Key.Key_D and self.selected is not None:
            self.duplicate()
            return True
        if not (mods & (CTRL | ALT)) and ev.text():
            tool = TOOL_KEYS.get(ev.text().upper())
            if tool:
                self.set_tool(tool)
                return True
        if k == Qt.Key.Key_Return and self.tool == "crop" and not self.inplace:
            self.set_tool("select")
            return True
        return False

    def undo(self):
        self.finish_edit()
        self.abort()
        self.doc.undo()

    def redo(self):
        self.finish_edit()
        self.abort()
        self.doc.redo()

    def delete_selected(self):
        s = self.selected
        if s is None:
            return
        self.finish_edit()
        if s in self.doc.shapes:
            self.doc.modify(lambda: self.doc.shapes.__setitem__(
                slice(None), [x for x in self.doc.shapes if x is not s]))
        self.select(None)

    def duplicate(self):
        import copy
        s = copy.deepcopy(self.selected)
        s.translate(12, 12)
        self.doc.modify(lambda: self.doc.shapes.append(s))
        self.select(s)

    def nudge(self, dx, dy, big=False):
        step = 10 if big else 1
        s = self.selected
        if s is not None:
            self.doc.modify(lambda: s.translate(dx * step, dy * step), merge=("nudge", id(s)))
        elif self.inplace:
            r = self.doc.bounds()
            if big:     # Shift+arrows resize the selection (right / bottom edge)
                r.setRight(r.right() + dx)
                r.setBottom(r.bottom() + dy)
            else:
                r.translate(dx, dy)
            r = self._clamp(r, keep_size=not big)
            if r.width() >= 1 and r.height() >= 1:
                self.doc.modify(lambda: setattr(self.doc, "crop", self.doc.to_pixels(r)), merge="nudge-crop")

    # -- mouse --

    def _handle_at(self, pts, pt):
        hs = 7.0 / self.scale
        for key, hp in pts:
            if abs(hp.x() - pt.x()) <= hs and abs(hp.y() - pt.y()) <= hs:
                return key
        return None

    def _crop_handles(self):
        r = self.doc.bounds()
        c = r.center()
        return [("tl", r.topLeft()), ("tr", r.topRight()), ("br", r.bottomRight()), ("bl", r.bottomLeft()),
                ("t", QPointF(c.x(), r.top())), ("r", QPointF(r.right(), c.y())),
                ("b", QPointF(c.x(), r.bottom())), ("l", QPointF(r.left(), c.y()))]

    def _crop_edge_at(self, pt):
        key = self._handle_at(self._crop_handles(), pt)
        if key:
            return key
        r, t = self.doc.bounds(), 5.0 / self.scale
        if not r.adjusted(-t, -t, t, t).contains(pt):
            return None
        key = ""
        if abs(pt.y() - r.top()) <= t:
            key += "t"
        elif abs(pt.y() - r.bottom()) <= t:
            key += "b"
        if abs(pt.x() - r.left()) <= t:
            key += "l"
        elif abs(pt.x() - r.right()) <= t:
            key += "r"
        return key or None

    def press(self, pt, button=LEFT, mods=Qt.KeyboardModifier.NoModifier):
        """Returns True if the press was used."""
        if button != LEFT:
            if self.op:
                self.abort()
                return True
            return False
        if self.editing is not None:
            if self.editing.hit(pt, self.tol()):
                self.caret = self.editing.index_at(pt)
                self._show_caret()
                self.changed.emit()
                return True
            self.finish_edit()
        doc, tool = self.doc, self.tool
        bounds = doc.bounds()
        if self.selected is not None and tool != "crop":
            key = self._handle_at(self.selected.handles(), pt)
            if key:
                doc.begin()
                self.op = ["handle", self.selected, key]
                return True
        if self.crop_active():
            key = self._crop_edge_at(pt)
            if key:
                doc.begin()
                self.op = ["crop-handle", key, bounds]
                return True
        inside = bounds.contains(pt)
        if tool == "crop":
            doc.begin()
            cropped = doc.pixel_rect() != doc.image.rect()
            self.op = ["crop-move", pt, bounds] if inside and cropped and not self.inplace else ["crop-new", pt, False]
            return True
        if self.inplace and not inside and not doc.shapes:
            doc.begin()
            self.op = ["crop-new", pt, False]
            return True
        if tool == "select":
            s, part = self.shape_at(pt)
            if s is not None:
                self.select(s)
                doc.begin()
                self.op = ["move", s, part, pt]
                return True
            self.select(None)
            if self.inplace and inside:
                doc.begin()
                self.op = ["crop-move", pt, bounds]
            return True
        if tool == "text":
            s, _ = self.shape_at(pt, kinds=("text",))
            if s is None:
                doc.begin()
                s = shapes.make("text", pt, self.props.values("text"))
                lh = s.bounds().height() - 2 * s.pad()
                s.translate(-s.pad(), -s.pad() - lh / 2)
                doc.shapes.append(s)
            self.start_edit(s, s.index_at(pt) if s.text else 0)
            return True
        if tool not in shapes.KINDS:
            return False
        doc.begin()
        self.select(None)
        s = shapes.make(tool, pt, self.props.values(tool))
        doc.shapes.append(s)
        self.op = ["create", s, pt, False]
        self.changed.emit()
        return True

    def _clamp(self, r, keep_size=False):
        full = self.doc.full_bounds()
        if keep_size:
            r = QRectF(r)
            r.moveLeft(max(full.left(), min(r.left(), full.right() - r.width())))
            r.moveTop(max(full.top(), min(r.top(), full.bottom() - r.height())))
            return r
        return r.normalized().intersected(full)

    def _snap(self, r):
        """Snap the edges of a new selection to nearby window edges."""
        if not self.snap_rects:
            return r
        lim = 8.0 / self.scale
        xs = [v for w in self.snap_rects for v in (w.left(), w.right())]
        ys = [v for w in self.snap_rects for v in (w.top(), w.bottom())]

        def near(v, cands):
            best = min(cands, key=lambda c: abs(c - v))
            return best if abs(best - v) <= lim else v
        return QRectF(QPointF(near(r.left(), xs), near(r.top(), ys)),
                      QPointF(near(r.right(), xs), near(r.bottom(), ys)))

    def _set_crop(self, r):
        r = self._clamp(r)
        if r.width() >= 1 and r.height() >= 1:
            self.doc.crop = self.doc.to_pixels(r)

    def move(self, pt, mods=Qt.KeyboardModifier.NoModifier):
        op = self.op
        if not op:
            return
        shift = bool(mods & SHIFT)
        kind = op[0]
        if kind == "create":
            s, start = op[1], op[2]
            if not op[3] and QLineF(start, pt).length() * self.scale > 3:
                op[3] = True
            if op[3]:
                s.drag_create(start, pt, shift)
        elif kind == "move":
            d = pt - op[3]
            op[1].move_part(op[2], d.x(), d.y())
            op[3] = pt
        elif kind == "handle":
            op[1].drag_handle(op[2], pt, shift)
        elif kind == "crop-new":
            start = op[1]
            if not op[2] and QLineF(start, pt).length() * self.scale > 3:
                op[2] = True
            if op[2]:
                r = QRectF(start, pt).normalized()
                if shift:
                    side = max(r.width(), r.height())
                    r = QRectF(start, QPointF(start.x() + math.copysign(side, pt.x() - start.x()),
                                              start.y() + math.copysign(side, pt.y() - start.y()))).normalized()
                self._set_crop(self._snap(r) if self.inplace else r)
        elif kind == "crop-move":
            r = QRectF(op[2]).translated(pt - op[1])
            r = self._clamp(r, keep_size=True)
            self.doc.crop = self.doc.to_pixels(r)
        elif kind == "crop-handle":
            r = QRectF(op[2])
            key = op[1]
            if "l" in key:
                r.setLeft(pt.x())
            if "r" in key:
                r.setRight(pt.x())
            if "t" in key:
                r.setTop(pt.y())
            if "b" in key:
                r.setBottom(pt.y())
            self._set_crop(r)
        self.changed.emit()

    def release(self, pt, mods=Qt.KeyboardModifier.NoModifier):
        op = self.op
        if not op:
            return
        self.move(pt, mods)
        self.op = None
        kind = op[0]
        if kind == "create":
            s = op[1]
            s.finish_click()
            if s.too_small(self.tol() * 2):
                self.doc.cancel()
            else:
                self.doc.commit()
                if s.kind != "emoji":   # picking the next emoji must not swap the one just placed
                    self.select(s)
        elif kind == "crop-new" and not op[2]:
            hits = [r for r in self.snap_rects if r.contains(pt)] if self.inplace else []
            if hits:
                best = min(hits, key=lambda r: r.width() * r.height())
                self._set_crop(best)
                self.doc.commit()
            else:
                self.doc.cancel()
        else:
            self.doc.commit()
        self.selection_changed.emit()
        self.changed.emit()

    def double_click(self, pt):
        s, _ = self.shape_at(pt, kinds=("text",))
        if s is not None:
            self.start_edit(s, s.index_at(pt))
            return True
        return False

    def abort(self):
        if self.op:
            self.op = None
            self.doc.cancel()
            self.changed.emit()

    def cursor_at(self, pt):
        if self.op:
            k = self.op[0]
            if k in ("move", "crop-move"):
                return Qt.CursorShape.SizeAllCursor
            if k in ("handle", "crop-handle"):
                return _CURSORS.get(self.op[2] if k == "handle" else self.op[1], Qt.CursorShape.CrossCursor)
            return Qt.CursorShape.CrossCursor
        if self.editing is not None and self.editing.hit(pt, self.tol()):
            return Qt.CursorShape.IBeamCursor
        if self.selected is not None and self.tool != "crop":
            key = self._handle_at(self.selected.handles(), pt)
            if key:
                return _CURSORS.get(key, Qt.CursorShape.CrossCursor)
        if self.crop_active():
            key = self._crop_edge_at(pt)
            if key:
                return _CURSORS[key]
        inside = self.doc.bounds().contains(pt)
        if self.tool == "crop":
            cropped = self.doc.pixel_rect() != self.doc.image.rect()
            return Qt.CursorShape.SizeAllCursor if inside and cropped and not self.inplace \
                else Qt.CursorShape.CrossCursor
        if self.inplace and not inside and not self.doc.shapes:
            return Qt.CursorShape.CrossCursor
        if self.tool == "select":
            if self.shape_at(pt)[0] is not None or (self.inplace and inside):
                return Qt.CursorShape.SizeAllCursor
            return Qt.CursorShape.ArrowCursor
        if self.tool == "text":
            return Qt.CursorShape.IBeamCursor
        return Qt.CursorShape.CrossCursor

    # -- painting --

    def _cos_pen(self, color, w=1.0, dashed=False):
        pen = QPen(color, w)
        pen.setCosmetic(True)
        if dashed:
            pen.setDashPattern([4, 3])
        return pen

    def _handle(self, p, pt, fill, size=4.5):
        h = size / self.scale
        p.setPen(self._cos_pen(QColor(0, 0, 0, 160), 1))
        p.setBrush(fill)
        p.drawRect(QRectF(pt.x() - h, pt.y() - h, 2 * h, 2 * h))

    def paint_overlay(self, p, crop_frame=True):
        """Selection handles, text caret and (if active) the crop frame; p in doc coords."""
        from PyQt6.QtGui import QPainter
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        s = self.editing or self.selected
        if s is not None and not (self.op and self.op[0] == "create"):
            if type(s) is shapes.Rect:
                box = None              # its own outline is clear enough
            elif isinstance(s, shapes._Box):
                box = s.rect()
            elif not s.handled or isinstance(s, shapes.Text):
                box = s.bounds()
            else:
                box = None
            if box is not None:
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(self._cos_pen(QColor(0, 0, 0, 120), 1))
                p.drawRect(box)
                p.setPen(self._cos_pen(style.ACCENT, 1, dashed=True))
                p.drawRect(box)
            if s is not self.editing:
                for _, hp in s.handles():
                    self._handle(p, hp, QColor("#ffffff"))
        if self.editing is not None and self._caret_on:
            line = self.editing.caret_line(self.caret)
            c = shapes.contrast(self.editing.color) if self.editing.text_bg else QColor(self.editing.color)
            p.setPen(self._cos_pen(c, 2))
            p.drawLine(line)
        if crop_frame and self.crop_active():
            r = self.doc.bounds()
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(self._cos_pen(style.ACCENT, 2))
            p.drawRect(r)
            for _, hp in self._crop_handles():
                self._handle(p, hp, style.ACCENT, 4)
        p.restore()
