"""Capture history: a JSON-lines file of what was captured/uploaded, plus a window to browse it.

One JSON object per line, oldest first, so adding is an append.  Edits and
removals rewrite the file (it's small: at most MAX_ENTRIES lines).  Broken
lines are skipped, so a crash mid-write loses one entry at most.
"""

import json
import os
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from PyQt6.QtCore import (QAbstractListModel, QFileSystemWatcher, QModelIndex, QObject, QRect, QSize, Qt,
                          QTimer, QUrl, pyqtSignal)
from PyQt6.QtGui import QAction, QDesktopServices, QFont, QIcon, QImage, QImageReader, QPalette, QPixmap
from PyQt6.QtWidgets import (QApplication, QComboBox, QHBoxLayout, QLabel, QLineEdit, QListView, QMainWindow,
                             QMenu, QMessageBox, QPushButton, QSizePolicy, QStyle, QStyledItemDelegate,
                             QToolBar, QVBoxLayout, QWidget)

from . import settings, style

MAX_ENTRIES = 2000
THUMB_MAX = 320            # longest thumbnail side in px
_lock = threading.Lock()


# -- store ------------------------------------------------------------------

def _read():
    """All entries, oldest first."""
    out = []
    try:
        f = open(settings.HISTORY_FILE, encoding="utf-8")
    except OSError:
        return out
    with f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict) and e.get("id") and e.get("path"):
                out.append(e)
    return out


def _write(items):
    os.makedirs(os.path.dirname(settings.HISTORY_FILE), exist_ok=True)
    tmp = settings.HISTORY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for e in items:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    os.replace(tmp, settings.HISTORY_FILE)


def _image_size(path):
    s = QImageReader(path).size()
    return (s.width(), s.height()) if s.isValid() else None


def add(kind, path, url=None, delete_url=None, **info):
    """Record a capture. kind: "image" | "video" | "gif". Returns the new entry."""
    path = os.path.abspath(path)
    entry = {"id": uuid.uuid4().hex[:12], "time": round(time.time(), 3), "kind": kind, "path": path,
             "url": url or "", "delete_url": delete_url or ""}
    try:
        entry["size"] = os.path.getsize(path)
    except OSError:
        pass
    if kind in ("image", "gif") and "width" not in info:
        wh = _image_size(path)
        if wh:
            entry["width"], entry["height"] = wh
    entry.update(info)
    with _lock:
        os.makedirs(os.path.dirname(settings.HISTORY_FILE), exist_ok=True)
        with open(settings.HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        items = _read()
        if len(items) > MAX_ENTRIES + 100:   # compact now and then, not on every add
            for e in items[:-MAX_ENTRIES]:
                _drop_thumb(e)
            _write(items[-MAX_ENTRIES:])
    return entry


def entries():
    """All entries, newest first."""
    with _lock:
        return _read()[::-1]


def _id(entry):
    return entry["id"] if isinstance(entry, dict) else str(entry)


def remove(entry):
    """Forget an entry (the file itself is left alone)."""
    eid = _id(entry)
    with _lock:
        items = _read()
        keep = [e for e in items if e["id"] != eid]
        if len(keep) != len(items):
            _write(keep)
    _drop_thumb({"id": eid})


def update(entry, **changes):
    """Change fields of an entry (e.g. url after uploading). Updates `entry` in place too."""
    eid = _id(entry)
    found = None
    with _lock:
        items = _read()
        for e in items:
            if e["id"] == eid:
                e.update(changes)
                found = e
        if found:
            _write(items)
    if isinstance(entry, dict):
        entry.update(changes)
    return found


# -- thumbnails ---------------------------------------------------------------

def _thumb_candidates(entry):
    base = os.path.join(settings.THUMBS_DIR, entry["id"])
    return base + ".jpg", base + ".png"


def _drop_thumb(entry):
    for p in _thumb_candidates(entry):
        try:
            os.remove(p)
        except OSError:
            pass


def thumbnail(entry):
    """Path of a cached thumbnail, made if needed (blocking; videos run ffmpeg). None if impossible."""
    path = entry.get("path", "")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    for p in _thumb_candidates(entry):
        if os.path.exists(p) and os.path.getmtime(p) >= mtime:
            return p
    os.makedirs(settings.THUMBS_DIR, exist_ok=True)
    jpg, png = _thumb_candidates(entry)
    img = QImage()
    if entry.get("kind") != "video":
        reader = QImageReader(path)
        reader.setAutoTransform(True)
        size = reader.size()
        if size.isValid() and max(size.width(), size.height()) > THUMB_MAX:
            reader.setScaledSize(size.scaled(THUMB_MAX, THUMB_MAX, Qt.AspectRatioMode.KeepAspectRatio))
        img = reader.read()
    if img.isNull():
        return _video_thumb(path, jpg)
    if max(img.width(), img.height()) > THUMB_MAX:
        img = img.scaled(THUMB_MAX, THUMB_MAX, Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    out = png if _transparent(img) else jpg
    return out if img.save(out, quality=85) else None


def _transparent(img):
    """True if any pixel is see-through (many opaque PNGs still have an alpha channel)."""
    if not img.hasAlphaChannel():
        return False
    import numpy as np
    a = img.convertToFormat(QImage.Format.Format_Alpha8)
    bits = a.constBits()
    bits.setsize(a.sizeInBytes())
    rows = np.frombuffer(bits, np.uint8).reshape(a.height(), a.bytesPerLine())
    return bool(rows[:, :a.width()].min() < 255)


def _video_thumb(path, out):
    scale = f"scale='min({THUMB_MAX},iw)':'min({THUMB_MAX},ih)':force_original_aspect_ratio=decrease"
    for seek in (["-ss", "0.5"], []):   # very short clips have no frame at 0.5 s
        try:
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *seek, "-i", path, "-frames:v", "1",
                            "-vf", scale, "-q:v", "4", out], capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if os.path.exists(out) and os.path.getsize(out) > 0:
            return out
    return None


class ThumbLoader(QObject):
    """Makes thumbnails in background threads; `ready(id, QImage)` on the GUI thread."""

    ready = pyqtSignal(str, QImage)
    _done = pyqtSignal(str, QImage)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="thumbs")
        self._pending = {}          # id -> Future
        self._done.connect(self._finish, Qt.ConnectionType.QueuedConnection)

    def request(self, entry):
        if entry["id"] in self._pending:
            return
        self._pending[entry["id"]] = self._pool.submit(self._work, dict(entry))

    def _work(self, entry):
        try:
            p = thumbnail(entry)
            img = QImage(p) if p else QImage()
        except Exception:  # noqa: BLE001 - a missing thumbnail is not worth a crash
            img = QImage()
        self._done.emit(entry["id"], img)

    def _finish(self, eid, img):
        self._pending.pop(eid, None)
        self.ready.emit(eid, img)

    def cancel(self):
        """Drop queued work (e.g. the window was closed); running jobs finish."""
        for eid, fut in list(self._pending.items()):
            if fut.cancel():
                del self._pending[eid]


# -- window -----------------------------------------------------------------------

def _human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024


def _when(t):
    lt, now = time.localtime(t), time.localtime()
    if lt[:3] == now[:3]:
        return time.strftime("Today %H:%M", lt)
    if lt.tm_year == now.tm_year:
        return time.strftime("%b %-d, %H:%M", lt)
    return time.strftime("%b %-d %Y", lt)


def _exists(entry):
    return os.path.exists(entry.get("path", ""))


class _Model(QAbstractListModel):
    EntryRole = Qt.ItemDataRole.UserRole + 1

    def __init__(self, parent):
        super().__init__(parent)
        self.all, self.rows = [], []
        self.kind, self.query = "all", ""
        self.thumbs = {}            # id -> QPixmap (null = failed)
        self.exists = {}            # id -> bool, checked on reload
        self.loader = ThumbLoader(self)
        self.loader.ready.connect(self._thumb_ready)

    def reload(self):
        self.all = entries()
        self.exists = {e["id"]: _exists(e) for e in self.all}
        self.refilter()

    def refilter(self):
        q = self.query.lower()
        self.beginResetModel()
        self.rows = [e for e in self.all
                     if (self.kind == "all" or (self.kind == "image") == (e.get("kind") == "image"))
                     and (not q or q in os.path.basename(e["path"]).lower() or q in (e.get("url") or "").lower())]
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        e = self.rows[index.row()]
        if role == self.EntryRole:
            return e
        if role == Qt.ItemDataRole.DisplayRole:
            return os.path.basename(e["path"])
        if role == Qt.ItemDataRole.ToolTipRole:
            tip = e["path"] + ("" if self.exists.get(e["id"]) else "\n(file is missing)")
            return tip + (f"\n{e['url']}" if e.get("url") else "")
        return None

    def thumb(self, e):
        pm = self.thumbs.get(e["id"])
        if pm is None and self.exists.get(e["id"]):
            self.loader.request(e)
        return pm

    def _thumb_ready(self, eid, img):
        self.thumbs[eid] = QPixmap.fromImage(img)
        for i, e in enumerate(self.rows):
            if e["id"] == eid:
                ix = self.index(i)
                self.dataChanged.emit(ix, ix)


class _Delegate(QStyledItemDelegate):
    CELL = QSize(232, 206)
    THUMB = QSize(216, 144)

    def sizeHint(self, option, index):
        return self.CELL

    def paint(self, p, option, index):
        model = index.model()
        e = index.data(_Model.EntryRole)
        missing = not model.exists.get(e["id"])
        p.save()
        style_ = option.widget.style() if option.widget else QApplication.style()
        style_.drawPrimitive(QStyle.PrimitiveElement.PE_PanelItemViewItem, option, p, option.widget)
        r = option.rect
        tr = QRect(r.x() + (r.width() - self.THUMB.width()) // 2, r.y() + 8, self.THUMB.width(), self.THUMB.height())
        pal = option.palette
        if missing:
            p.setOpacity(0.4)
        p.fillRect(tr, pal.color(QPalette.ColorRole.Window).darker(115))
        pm = model.thumb(e)
        if pm is not None and not pm.isNull():
            s = pm.size().scaled(tr.size(), Qt.AspectRatioMode.KeepAspectRatio)
            if pm.width() <= tr.width() and pm.height() <= tr.height():
                s = pm.size()   # don't blow up tiny images
            target = QRect(0, 0, s.width(), s.height())
            target.moveCenter(tr.center())
            p.setRenderHint(p.RenderHint.SmoothPixmapTransform)
            p.drawPixmap(target, pm)
        else:
            name = "image-missing" if missing else ("video-x-generic" if e.get("kind") == "video" else "image-x-generic")
            icon = QIcon.fromTheme(name)
            if not icon.isNull():
                ir = QRect(0, 0, 48, 48)
                ir.moveCenter(tr.center())
                icon.paint(p, ir)

        # badges: file type (top-left), URL (top-right)
        f = QFont(option.font)
        f.setPointSizeF(max(6.5, f.pointSizeF() * 0.75))
        f.setBold(True)
        p.setFont(f)
        fm = p.fontMetrics()
        ext = os.path.splitext(e["path"])[1][1:].upper() or e.get("kind", "").upper()
        label = ("▶ " + ext) if e.get("kind") in ("video", "gif") else ext
        if missing:
            label = "MISSING"
        br = QRect(tr.x() + 4, tr.y() + 4, fm.horizontalAdvance(label) + 10, fm.height() + 4)
        p.fillRect(br, style.PANEL_BG)
        p.setPen(style.TEXT)
        p.drawText(br, Qt.AlignmentFlag.AlignCenter, label)
        if e.get("url"):
            ur = QRect(0, br.y(), fm.horizontalAdvance("URL") + 10, br.height())
            ur.moveRight(tr.right() - 4)
            p.fillRect(ur, style.ACCENT)
            p.setPen(style.ACCENT_TEXT)
            p.drawText(ur, Qt.AlignmentFlag.AlignCenter, "URL")

        # name + details
        selected = option.state & QStyle.StateFlag.State_Selected
        text = pal.color(QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.Text)
        p.setFont(option.font)
        fm = p.fontMetrics()
        y = tr.bottom() + 6
        nr = QRect(r.x() + 8, y, r.width() - 16, fm.height())
        p.setPen(text)
        p.drawText(nr, Qt.AlignmentFlag.AlignHCenter, fm.elidedText(os.path.basename(e["path"]),
                                                                     Qt.TextElideMode.ElideMiddle, nr.width()))
        bits = [_when(e.get("time", 0))]
        if e.get("width"):
            bits.append(f"{e['width']}×{e.get('height', '?')}")
        if e.get("size"):
            bits.append(_human_size(e["size"]))
        small = QFont(option.font)
        small.setPointSizeF(option.font.pointSizeF() * 0.85)
        p.setFont(small)
        dr = QRect(nr.x(), nr.bottom() + 3, nr.width(), p.fontMetrics().height())
        muted = text if selected else pal.color(QPalette.ColorRole.PlaceholderText)
        p.setPen(muted)
        p.drawText(dr, Qt.AlignmentFlag.AlignHCenter,
                   p.fontMetrics().elidedText("  ·  ".join(bits), Qt.TextElideMode.ElideRight, dr.width()))
        p.restore()


class HistoryWindow(QMainWindow):
    """Browse past captures. App-level actions are emitted; the ones in `local`
    are done right here (they need nothing from the app)."""

    action = pyqtSignal(str, object)
    TITLE = "Snappie — history"
    LOCAL = {"open", "folder", "copy_path", "copy_url", "open_url", "delete", "remove"}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.TITLE)
        self.setWindowIcon(QIcon.fromTheme("view-history"))
        self.resize(1000, 680)
        self.local = set(self.LOCAL)

        tb = QToolBar("Capture", self)
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        tb.setIconSize(QSize(22, 22))
        for text, icon, name in (("New Screenshot", "camera-photo", "new_shot"),
                                 ("Record", "media-record", "new_record"),
                                 ("Record GIF", "image-gif", "new_gif")):
            a = tb.addAction(QIcon.fromTheme(icon, QIcon.fromTheme("video-x-generic")), text)
            a.triggered.connect(lambda _=False, n=name: self.action.emit(n, None))
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)
        self.kind = QComboBox()
        for text, k in (("All", "all"), ("Images", "image"), ("Videos & GIFs", "video")):
            self.kind.addItem(text, k)
        tb.addWidget(self.kind)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search name or URL…")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(220)
        self.search.addAction(QIcon.fromTheme("search"), QLineEdit.ActionPosition.LeadingPosition)
        tb.addWidget(self.search)
        a = tb.addAction(QIcon.fromTheme("configure"), "Settings")
        a.triggered.connect(lambda: self.action.emit("settings", None))
        self.addToolBar(tb)

        self.model = _Model(self)
        self.view = QListView()
        self.view.setViewMode(QListView.ViewMode.IconMode)
        self.view.setResizeMode(QListView.ResizeMode.Adjust)
        self.view.setMovement(QListView.Movement.Static)
        self.view.setUniformItemSizes(True)
        self.view.setSpacing(6)
        self.view.setSelectionMode(QListView.SelectionMode.SingleSelection)
        self.view.setModel(self.model)
        self.view.setItemDelegate(_Delegate(self.view))
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._context_menu)
        self.view.doubleClicked.connect(lambda ix: self._do("open"))
        self.view.activated.connect(lambda ix: self._do("open"))
        self.view.selectionModel().currentChanged.connect(lambda *_: self._sync_buttons())
        self.empty = QLabel("Nothing captured yet.\nScreenshots and recordings show up here.")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setStyleSheet("color: palette(placeholder-text);")

        self.buttons = {}
        bar = QHBoxLayout()
        bar.setContentsMargins(8, 4, 8, 8)
        for name, text, icon in (("open", "Open", "document-open"), ("folder", "Show in Folder", "folder-open"),
                                 ("copy", "Copy", "edit-copy"), ("copy_url", "Copy URL", "edit-link"),
                                 ("upload", "Upload", "cloud-upload"), ("edit", "Edit", "document-edit"),
                                 ("pin", "Pin", "window-pin"), ("delete", "Delete", "edit-delete")):
            b = QPushButton(QIcon.fromTheme(icon), text)
            b.clicked.connect(lambda _=False, n=name: self._do(n))
            self.buttons[name] = b
            bar.addWidget(b)
            if name in ("folder", "copy_url", "pin"):
                bar.addSpacing(10)
        bar.addStretch(1)

        central = QWidget()
        lay = QVBoxLayout(central)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.view, 1)
        lay.addWidget(self.empty, 1)
        lay.addLayout(bar)
        self.setCentralWidget(central)
        self.status = self.statusBar()

        self.kind.currentIndexChanged.connect(self._filter)
        self.search.textChanged.connect(self._filter)

        # follow changes made by the app (new captures, URLs after uploading)
        self._reload_timer = QTimer(self, singleShot=True, interval=250, timeout=self.refresh)
        self._watcher = QFileSystemWatcher(self)
        os.makedirs(settings.DATA_DIR, exist_ok=True)
        self._watcher.addPath(settings.DATA_DIR)
        self._watcher.directoryChanged.connect(lambda _: self._reload_timer.start())
        self._watcher.fileChanged.connect(lambda _: self._reload_timer.start())
        self.refresh()

    # -- data
    def refresh(self):
        cur = self.current()
        if os.path.exists(settings.HISTORY_FILE) and settings.HISTORY_FILE not in self._watcher.files():
            self._watcher.addPath(settings.HISTORY_FILE)   # re-add: rewrites replace the file
        self.model.reload()
        self._after_filter(cur["id"] if cur else None)

    def _filter(self):
        cur = self.current()
        self.model.kind = self.kind.currentData()
        self.model.query = self.search.text().strip()
        self.model.refilter()
        self._after_filter(cur["id"] if cur else None)

    def _after_filter(self, keep_id):
        rows = self.model.rows
        row = next((i for i, e in enumerate(rows) if e["id"] == keep_id), 0 if rows else -1)
        if row >= 0:
            self.view.setCurrentIndex(self.model.index(row))
        has = bool(self.model.all)
        self.view.setVisible(has)
        self.empty.setVisible(not has)
        self._sync_buttons()

    def current(self):
        ix = self.view.currentIndex()
        return ix.data(_Model.EntryRole) if ix.isValid() else None

    def _sync_buttons(self):
        e = self.current()
        ok = e is not None and self.model.exists.get(e["id"], False)
        image = ok and e.get("kind") == "image"
        enabled = {"open": ok, "folder": ok, "copy": ok, "copy_url": bool(e and e.get("url")),
                   "upload": ok, "edit": image, "pin": image, "delete": e is not None}
        for name, b in self.buttons.items():
            b.setEnabled(enabled[name])
        n = len(self.model.rows)
        if e is None:
            self.status.showMessage(f"{n} item{'s' * (n != 1)}")
            return
        bits = [e["path"]]
        if not ok:
            bits.append("missing")
        if e.get("url"):
            bits.append(e["url"])
        self.status.showMessage("   ·   ".join(bits))

    # -- actions
    def _context_menu(self, pos):
        ix = self.view.indexAt(pos)
        if not ix.isValid():
            return
        self.view.setCurrentIndex(ix)
        e = self.current()
        ok = self.model.exists.get(e["id"], False)
        image = ok and e.get("kind") == "image"
        m = QMenu(self)

        def add(text, name, icon, enabled=True):
            a = QAction(QIcon.fromTheme(icon), text, m)
            a.setEnabled(enabled)
            a.triggered.connect(lambda: self._do(name))
            m.addAction(a)

        add("Open", "open", "document-open", ok)
        add("Show in Folder", "folder", "folder-open", ok)
        m.addSeparator()
        add("Copy", "copy", "edit-copy", ok)
        add("Copy Path", "copy_path", "edit-copy-path")
        add("Copy URL", "copy_url", "edit-link", bool(e.get("url")))
        add("Open URL", "open_url", "internet-web-browser", bool(e.get("url")))
        m.addSeparator()
        add("Upload", "upload", "cloud-upload", ok)
        add("Edit", "edit", "document-edit", image)
        add("Pin to Screen", "pin", "window-pin", image)
        m.addSeparator()
        add("Remove from History", "remove", "list-remove")
        add("Delete File…", "delete", "edit-delete")
        m.exec(self.view.viewport().mapToGlobal(pos))

    def _do(self, name):
        e = self.current()
        if e is None:
            return
        if name not in self.local:
            self.action.emit(name, e)
            return
        path, url = e["path"], e.get("url") or ""
        clip = QApplication.clipboard()
        if name == "open":
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        elif name == "folder":
            _show_in_folder(path)
        elif name == "copy_path":
            clip.setText(path)
            self.status.showMessage("Path copied", 3000)
        elif name == "copy_url" and url:
            clip.setText(url)
            self.status.showMessage("URL copied", 3000)
        elif name == "open_url" and url:
            QDesktopServices.openUrl(QUrl(url))
        elif name == "remove":
            remove(e)
            self.refresh()
        elif name == "delete":
            self._delete(e)

    def _delete(self, e):
        path = e["path"]
        if os.path.exists(path):
            q = QMessageBox(QMessageBox.Icon.Question, "Delete file",
                            f"Move “{os.path.basename(path)}” to the trash?", parent=self)
            q.setInformativeText("It is also removed from the history.")
            q.setStandardButtons(QMessageBox.StandardButton.Cancel)
            trash = q.addButton("Move to Trash", QMessageBox.ButtonRole.DestructiveRole)
            trash.setIcon(QIcon.fromTheme("user-trash"))
            q.exec()
            if q.clickedButton() is not trash:
                return
            from PyQt6.QtCore import QFile
            if not QFile.moveToTrash(path):
                try:
                    os.remove(path)
                except OSError as err:
                    QMessageBox.warning(self, "Delete file", f"Couldn't delete the file: {err.strerror}")
                    return
        remove(e)
        self.refresh()
        self.action.emit("delete", e)    # informational: already done

    def closeEvent(self, ev):
        self.model.loader.cancel()
        super().closeEvent(ev)


def _show_in_folder(path):
    """Open the file manager with the file selected (falls back to opening the folder)."""
    try:
        import dbus
        fm = dbus.SessionBus().get_object("org.freedesktop.FileManager1", "/org/freedesktop/FileManager1")
        fm.ShowItems([QUrl.fromLocalFile(path).toString()], "", dbus_interface="org.freedesktop.FileManager1")
    except Exception:  # noqa: BLE001
        QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(path)))
