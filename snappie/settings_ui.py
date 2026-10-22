"""Settings dialog: native widgets in tabs, saved through settings.save on OK / Apply.

Every widget is bound to one settings key; only values that differ from the
stored ones are written, so untouched keys keep following DEFAULTS.
Uploader files (import / remove) are changed right away, like file operations.
"""

import html
import os
import tempfile
import time

from PyQt6.QtCore import QProcess, QSize, Qt
from PyQt6.QtGui import QColor, QFont, QIcon, QImage, QPainter, QPixmap
from PyQt6.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox, QColorDialog, QComboBox, QDialog,
                             QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFontComboBox, QFormLayout,
                             QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
                             QListWidgetItem, QMessageBox, QPushButton, QScrollArea, QSpinBox, QTableWidget,
                             QTableWidgetItem, QTabWidget, QToolButton, QVBoxLayout, QWidget)

from . import ocr, settings, style, upload

SHOT_AFTER = [("copy", "Copy to clipboard"), ("save", "Save to file"), ("upload", "Upload"),
              ("pin", "Pin to screen"), ("ocr", "Recognize text (OCR)"), ("edit", "Open in editor")]
REC_AFTER = [("copy", "Copy the file"), ("upload", "Upload")]


class _Checklist(QWidget):
    """A list-valued setting as checkboxes (order of the value follows the options)."""

    def __init__(self, options, columns=2):
        super().__init__()
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        self.boxes = {}
        rows = (len(options) + columns - 1) // columns
        for i, (key, text) in enumerate(options):
            cb = QCheckBox(text)
            self.boxes[key] = cb
            grid.addWidget(cb, i % rows, i // rows)

    def value(self):
        return [k for k, cb in self.boxes.items() if cb.isChecked()]

    def setValue(self, v):
        for k, cb in self.boxes.items():
            cb.setChecked(k in (v or []))


class _Folder(QWidget):
    def __init__(self, default):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(_tilde(default))
        self.edit.setClearButtonEnabled(True)
        b = QPushButton(QIcon.fromTheme("document-open-folder"), "Browse…")
        b.clicked.connect(self._browse)
        lay.addWidget(self.edit, 1)
        lay.addWidget(b)

    def _browse(self):
        start = os.path.expanduser(self.edit.text() or self.edit.placeholderText())
        d = QFileDialog.getExistingDirectory(self, "Choose a folder", start)
        if d:
            self.edit.setText(_tilde(d))

    def value(self):
        return self.edit.text().strip()

    def setValue(self, v):
        self.edit.setText(v or "")


class _ColorPicker(QWidget):
    """Palette swatches plus a custom colour; value is "#rrggbb"."""

    def __init__(self):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self.group = QButtonGroup(self)
        self.color = style.PALETTE[0]
        for c in style.PALETTE:
            b = QToolButton()
            b.setCheckable(True)
            b.setFixedSize(26, 26)
            b.setToolTip(c)
            b.setStyleSheet(f"QToolButton {{ background: {c}; border: 1px solid #5a5f66; border-radius: 0; }}"
                            f"QToolButton:checked {{ border: 3px solid {style.ACCENT.name()}; }}")
            b.clicked.connect(lambda _=False, col=c: self.setValue(col))
            self.group.addButton(b)
            lay.addWidget(b)
        self.custom = QPushButton("Custom…")
        self.custom.clicked.connect(self._pick)
        lay.addSpacing(6)
        lay.addWidget(self.custom)
        lay.addStretch(1)

    def _pick(self):
        c = QColorDialog.getColor(QColor(self.color), self, "Annotation color")
        if c.isValid():
            self.setValue(c.name())

    def value(self):
        return self.color

    def setValue(self, v):
        self.color = QColor(v).name() if QColor(v).isValid() else style.PALETTE[0]
        for b in self.group.buttons():
            b.setChecked(b.toolTip().lower() == self.color.lower())
        pm = QPixmap(14, 14)
        pm.fill(QColor(self.color))
        self.custom.setIcon(QIcon(pm))


def _tilde(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path


def _default_dir(kind, fallback, sub="Snappie"):
    return os.path.join(settings._xdg_dir(kind, fallback), sub)


def _hint(text):
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet("color: palette(placeholder-text);")
    return label


def _scroll(widget):
    area = QScrollArea()
    area.setWidget(widget)
    area.setWidgetResizable(True)
    area.setFrameShape(QScrollArea.Shape.NoFrame)
    return area


def _form(title=None):
    box = QGroupBox(title) if title else QWidget()
    form = QFormLayout(box)
    form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
    return box, form


def _combo(items):
    c = QComboBox()
    for text, data in items:
        c.addItem(text, data)
    return c


def test_image():
    """A small generated PNG for trying an uploader; returns its path (caller deletes it)."""
    img = QImage(360, 120, QImage.Format.Format_RGB32)
    img.fill(style.PANEL_BG)
    p = QPainter(img)
    p.fillRect(0, 0, 8, 120, style.ACCENT)
    f = QFont()
    f.setPixelSize(26)
    f.setBold(True)
    p.setFont(f)
    p.setPen(style.TEXT)
    p.drawText(28, 54, "snappie test upload")
    f.setPixelSize(16)
    f.setBold(False)
    p.setFont(f)
    p.setPen(style.MUTED)
    p.drawText(28, 86, time.strftime("%Y-%m-%d %H:%M:%S"))
    p.end()
    fd, path = tempfile.mkstemp(prefix="snappie-test-", suffix=".png")
    os.close(fd)
    img.save(path)
    return path


class SettingsDialog(QDialog):
    TITLE = "Snappie — settings"

    def __init__(self, hotkeys=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.TITLE)
        self.setWindowIcon(QIcon.fromTheme("configure"))
        self.resize(660, 700)
        self.hotkeys = hotkeys
        self._fields = {}           # key -> (get, set)

        self.tabs = QTabWidget()
        self.tabs.addTab(_scroll(self._screenshots()), QIcon.fromTheme("camera-photo"), "Screenshots")
        self.tabs.addTab(_scroll(self._recording()), QIcon.fromTheme("media-record"), "Recording")
        self.tabs.addTab(_scroll(self._gif()), QIcon.fromTheme("image-gif"), "GIF")
        self.tabs.addTab(self._uploads(), QIcon.fromTheme("cloud-upload"), "Uploads")
        self.tabs.addTab(self._hotkeys(), QIcon.fromTheme("preferences-desktop-keyboard"), "Hotkeys")
        self.tabs.addTab(_scroll(self._annotation()), QIcon.fromTheme("draw-freehand"), "Annotation")

        B = QDialogButtonBox.StandardButton
        self.buttons = QDialogButtonBox(B.Ok | B.Cancel | B.Apply | B.RestoreDefaults)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.buttons.button(B.Apply).clicked.connect(self.apply)
        self.buttons.button(B.RestoreDefaults).setToolTip("Reset every setting in this dialog to its default "
                                                          "(saved when you press OK or Apply)")
        self.buttons.button(B.RestoreDefaults).clicked.connect(self.restore_defaults)

        lay = QVBoxLayout(self)
        lay.addWidget(self.tabs, 1)
        lay.addWidget(self.buttons)
        self.load()

    # -- binding
    def _bind(self, key, w):
        if isinstance(w, QCheckBox):
            get, put = w.isChecked, lambda v: w.setChecked(bool(v))
        elif isinstance(w, (QSpinBox, QDoubleSpinBox)):
            get, put = w.value, lambda v: w.setValue(v or 0)
        elif isinstance(w, QComboBox):
            def put(v, w=w):
                i = w.findData(v)
                w.setCurrentIndex(i if i >= 0 else 0)
            get = w.currentData
        elif isinstance(w, QLineEdit):
            get, put = lambda: w.text().strip(), lambda v: w.setText(str(v or ""))
        else:
            get, put = w.value, w.setValue
        self._fields[key] = (get, put)
        return w

    def load(self, source=None):
        """Fill the widgets from the stored settings, or from `source` (a dict like DEFAULTS)."""
        for key, (_, put) in self._fields.items():
            put(source[key] if source is not None else settings.get(key))
        if source is None:   # restoring defaults keeps the chosen uploader
            self._default_uploader = settings.get("upload.uploader") or ""
        self._refresh_uploaders()

    def values(self):
        out = {k: get() for k, (get, _) in self._fields.items()}
        out["upload.uploader"] = self._default_uploader
        return out

    def apply(self):
        changes = {k: v for k, v in self.values().items() if v != settings.get(k)}
        if changes:
            settings.save(**changes)
        return changes

    def accept(self):
        self.apply()
        super().accept()

    def restore_defaults(self):
        self.load(dict(settings.DEFAULTS))

    # -- tabs
    def _screenshots(self):
        page = QWidget()
        v = QVBoxLayout(page)

        box, form = _form("Files")
        form.addRow("Folder:", self._bind("shot.dir", _Folder(_default_dir("PICTURES", "~/Pictures"))))
        name = self._bind("shot.filename", QLineEdit())
        fmt = self._bind("shot.format", _combo([("PNG", "png"), ("JPEG", "jpg"), ("WebP", "webp")]))
        form.addRow("File name:", name)
        preview = self._preview_label(name, lambda: fmt.currentData())
        form.addRow("", preview)
        row = QHBoxLayout()
        row.addWidget(fmt)
        row.addSpacing(12)
        row.addWidget(QLabel("Quality:"))
        quality = self._bind("shot.quality", QSpinBox(minimum=1, maximum=100, suffix=" %"))
        row.addWidget(quality)
        row.addStretch(1)
        form.addRow("Format:", row)
        fmt.currentIndexChanged.connect(lambda: quality.setEnabled(fmt.currentData() != "png"))
        fmt.currentIndexChanged.connect(preview.refresh)
        v.addWidget(box)

        box, form = _form("Capture")
        form.addRow(self._bind("shot.cursor", QCheckBox("Include the mouse cursor")))
        form.addRow(self._bind("shot.magnifier", QCheckBox("Show a magnifier while picking")))
        form.addRow(self._bind("shot.annotate", QCheckBox("Annotate in place right after picking")))
        form.addRow("Window shots:", self._bind("shot.window_mode", _combo([
            ("Plain", "plain"), ("Transparent (the window's own shape)", "transparent"),
            ("With a drop shadow", "shadow")])))
        v.addWidget(box)

        box, form = _form("After capture")
        form.addRow(_hint("Done when you press Enter in the editor, or right away if annotating is off."))
        form.addRow(self._bind("shot.after", _Checklist(SHOT_AFTER, columns=3)))
        v.addWidget(box)

        box, form = _form("Text recognition")
        lang = self._bind("ocr.lang", QLineEdit())
        lang.setPlaceholderText("eng")
        lang.setToolTip("Tesseract language codes, several joined with +, e.g. eng+deu")
        form.addRow("Language:", lang)
        form.addRow("", _hint("tesseract is installed." if ocr.available() else ocr.install_hint()))
        v.addWidget(box)
        v.addStretch(1)
        return page

    def _recording(self):
        page = QWidget()
        v = QVBoxLayout(page)

        box, form = _form("Files")
        form.addRow("Folder:", self._bind("rec.dir", _Folder(_default_dir("VIDEOS", "~/Videos"))))
        name = self._bind("rec.filename", QLineEdit())
        container = self._bind("rec.container", _combo([
            ("MP4", "mp4"), ("MKV", "mkv"), ("WebM (VP9, VP8 or AV1)", "webm")]))
        form.addRow("File name:", name)
        preview = self._preview_label(name, container.currentData)
        form.addRow("", preview)
        container.setToolTip("Always recorded as MKV (safe if something crashes), then remuxed losslessly.\n"
                             "WebM holds only VP9, VP8 and AV1: other codecs record as VP9.")
        form.addRow("Container:", container)
        container.currentIndexChanged.connect(preview.refresh)
        v.addWidget(box)

        box, form = _form("Video and sound")
        form.addRow("Codec:", self._bind("rec.codec", _combo([
            ("HEVC (H.265)", "hevc"), ("AV1 (smallest files)", "av1"), ("H.264 (most compatible)", "h264"),
            ("VP9", "vp9"), ("VP8", "vp8")])))
        form.addRow("Quality:", self._bind("rec.quality", _combo([
            ("High", "high"), ("Good", "good"), ("Small files", "small")])))
        fps = self._bind("rec.fps", QSpinBox(minimum=0, maximum=240, suffix=" fps"))
        fps.setSpecialValueText("Native (variable)")
        form.addRow("Frame rate:", fps)
        form.addRow(self._bind("cursor", QCheckBox("Record the mouse cursor")))
        form.addRow(self._bind("rec.audio", QCheckBox("Record desktop audio")))
        form.addRow(self._bind("rec.mic", QCheckBox("Record the microphone")))
        v.addWidget(box)

        box, form = _form("While recording")
        form.addRow(self._bind("rec.clicks", QCheckBox("Show mouse clicks")))
        form.addRow(self._bind("rec.keys", QCheckBox("Show pressed keys")))
        fade = self._bind("rec.draw_fade", QDoubleSpinBox(minimum=0, maximum=60, singleStep=0.5,
                                                          decimals=1, suffix=" s"))
        fade.setSpecialValueText("Never")
        form.addRow("Drawings fade after:", fade)
        v.addWidget(box)

        box, form = _form("After recording")
        form.addRow(self._bind("rec.after", _Checklist(REC_AFTER, columns=2)))
        v.addWidget(box)
        v.addStretch(1)
        return page

    def _gif(self):
        page = QWidget()
        v = QVBoxLayout(page)
        box, form = _form("Animated images")
        form.addRow("Format:", self._bind("gif.format", _combo([
            ("GIF", "gif"), ("WebP (smaller, full color)", "webp")])))
        form.addRow("Frame rate:", self._bind("gif.fps", QSpinBox(minimum=1, maximum=60, suffix=" fps")))
        width = self._bind("gif.max_width", QSpinBox(minimum=0, maximum=7680, singleStep=40, suffix=" px"))
        width.setSpecialValueText("Native width")
        form.addRow("Maximum width:", width)
        form.addRow(_hint("Wider recordings are scaled down. GIFs get big quickly: 10–15 fps and "
                          "960 px is a good balance."))
        v.addWidget(box)
        v.addStretch(1)
        return page

    def _uploads(self):
        page = QWidget()
        v = QVBoxLayout(page)
        box = QGroupBox("Custom uploaders (ShareX .sxcu)")
        h = QHBoxLayout(box)
        left = QVBoxLayout()
        self.up_list = QListWidget()
        self.up_list.setIconSize(QSize(16, 16))
        self.up_list.currentItemChanged.connect(lambda *_: self._uploader_selected())
        self.up_list.itemDoubleClicked.connect(lambda *_: self._set_default())
        left.addWidget(self.up_list, 1)
        self.up_details = _hint("")
        self.up_details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        left.addWidget(self.up_details)
        h.addLayout(left, 1)

        col = QVBoxLayout()
        b_import = QPushButton(QIcon.fromTheme("document-import"), "Import…")
        b_import.clicked.connect(self._import)
        self.b_default = QPushButton(QIcon.fromTheme("starred-symbolic", QIcon.fromTheme("emblem-favorite")),
                                     "Set as Default")
        self.b_default.clicked.connect(self._set_default)
        self.b_remove = QPushButton(QIcon.fromTheme("list-remove"), "Remove")
        self.b_remove.clicked.connect(self._remove)
        self.b_test = QPushButton(QIcon.fromTheme("cloud-upload"), "Test Upload")
        self.b_test.setToolTip("Uploads a small generated image with the selected uploader")
        self.b_test.clicked.connect(self._test_upload)
        b_certs = QPushButton(QIcon.fromTheme("security-medium"), "Trusted Certificates…")
        b_certs.setToolTip("Self-signed certificates you allowed for upload servers")
        b_certs.clicked.connect(self._trusted_certs)
        for b in (b_import, self.b_default, self.b_remove):
            col.addWidget(b)
        col.addStretch(1)
        col.addWidget(b_certs)
        col.addWidget(self.b_test)
        h.addLayout(col)
        v.addWidget(box, 1)

        self.test_result = QLabel()
        self.test_result.setWordWrap(True)
        self.test_result.setOpenExternalLinks(True)
        self.test_result.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        v.addWidget(self.test_result)

        box, form = _form("After uploading")
        form.addRow(self._bind("upload.copy_url", QCheckBox("Copy the URL to the clipboard")))
        v.addWidget(box)
        v.addWidget(_hint("Hosts like catbox.moe, Zipline, XBackBone and Chevereto offer .sxcu files; "
                          "you can also export them from ShareX on Windows."))
        self._uploader = None
        return page

    def _hotkeys(self):
        page = QWidget()
        v = QVBoxLayout(page)
        if self.hotkeys is None:
            v.addWidget(_hint("Global shortcuts belong to the running snappie tray app. Start it and "
                              "open Settings from its menu to see them here, or set them in System Settings "
                              "→ Keyboard → Shortcuts."))
            b = QPushButton(QIcon.fromTheme("preferences-desktop-keyboard"), "Open System Settings…")
            b.clicked.connect(lambda: QProcess.startDetached("systemsettings", ["kcm_keys"]))
            row = QHBoxLayout()
            row.addWidget(b)
            row.addStretch(1)
            v.addLayout(row)
            v.addStretch(1)
            return page
        table = QTableWidget(0, 2)
        table.setHorizontalHeaderLabels(["Action", "Shortcut"])
        table.verticalHeader().hide()
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setAlternatingRowColors(True)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        table.setColumnWidth(1, 200)
        self.hotkey_table = table
        self._fill_hotkeys()
        v.addWidget(table, 1)
        v.addWidget(_hint("Shortcuts are global (KDE's KGlobalAccel), so they're changed in System Settings."))
        row = QHBoxLayout()
        b = QPushButton(QIcon.fromTheme("preferences-desktop-keyboard"), "Change in System Settings…")
        b.clicked.connect(self.hotkeys.open_system_settings)
        refresh = QPushButton(QIcon.fromTheme("view-refresh"), "Refresh")
        refresh.clicked.connect(self._fill_hotkeys)
        row.addWidget(b)
        row.addWidget(refresh)
        row.addStretch(1)
        v.addLayout(row)
        return page

    def _fill_hotkeys(self):
        rows = list(self.hotkeys.actions())
        self.hotkey_table.setRowCount(len(rows))
        for i, (_id, label, keys) in enumerate(rows):
            self.hotkey_table.setItem(i, 0, QTableWidgetItem(label))
            item = QTableWidgetItem(keys or "None")
            if not keys:
                item.setForeground(self.palette().placeholderText())
            self.hotkey_table.setItem(i, 1, item)

    def _annotation(self):
        page = QWidget()
        v = QVBoxLayout(page)
        box, form = _form("Defaults for new annotations")
        form.addRow("Color:", self._bind("annot.color", _ColorPicker()))
        form.addRow("Line width:", self._bind("annot.width", QSpinBox(minimum=1, maximum=40, suffix=" px")))
        form.addRow("Text size:", self._bind("annot.font_size", QSpinBox(minimum=6, maximum=144)))
        font = QFontComboBox()
        system = QCheckBox("System font")
        system.toggled.connect(lambda on: font.setEnabled(not on))
        row = QHBoxLayout()
        row.addWidget(system)
        row.addWidget(font, 1)
        form.addRow("Font:", row)

        def get_font():
            return "" if system.isChecked() else font.currentFont().family()

        def set_font(v):
            system.setChecked(not v)
            font.setEnabled(bool(v))
            if v:
                font.setCurrentFont(QFont(v))
        self._fields["annot.font"] = (get_font, set_font)
        form.addRow(self._bind("annot.text_bg", QCheckBox("Text on a filled box")))
        form.addRow(_hint("The editor remembers the last values you use; these are where it starts."))
        v.addWidget(box)
        v.addStretch(1)
        return page

    # -- helpers
    def _preview_label(self, edit, ext):
        label = QLabel()
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        label.setToolTip("strftime codes: %Y year, %m month, %d day, %H hour, %M minute, %S second")

        def refresh():
            pattern = edit.text().strip()
            try:
                text = time.strftime(pattern) if pattern else ""
                bad = not text or "/" in text
            except ValueError:
                bad = True
            if bad:
                label.setText("Enter a name, e.g. screenshot_%Y-%m-%d_%H-%M-%S" if not pattern
                              else "Invalid pattern (no “/” allowed)")
                label.setStyleSheet(f"color: {style.RECORD.name()};")
            else:
                label.setText(f"{text}.{ext()}")
                label.setStyleSheet("color: palette(placeholder-text);")
        label.refresh = refresh
        edit.textChanged.connect(refresh)
        return label

    # -- uploads
    def _refresh_uploaders(self, select=None):
        cur = select or (self.up_list.currentItem().data(Qt.ItemDataRole.UserRole)
                         if self.up_list.currentItem() else None)
        self.up_list.clear()
        for fname, display in upload.list_uploaders():
            item = QListWidgetItem(display)
            item.setData(Qt.ItemDataRole.UserRole, fname)
            if fname == self._default_uploader:
                item.setText(f"{display}  (default)")
                f = item.font()
                f.setBold(True)
                item.setFont(f)
                item.setIcon(QIcon.fromTheme("starred-symbolic", QIcon.fromTheme("emblem-favorite")))
            self.up_list.addItem(item)
            if fname == (cur or self._default_uploader):
                self.up_list.setCurrentItem(item)
        if self.up_list.count() and not self.up_list.currentItem():
            self.up_list.setCurrentRow(0)
        self._uploader_selected()

    def _selected_uploader(self):
        item = self.up_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _uploader_selected(self):
        name = self._selected_uploader()
        self.b_default.setEnabled(bool(name) and name != self._default_uploader)
        self.b_remove.setEnabled(bool(name))
        self.b_test.setEnabled(bool(name) and self._uploader is None)
        if not name:
            self.up_details.setText("No uploaders yet. Import a ShareX custom uploader (.sxcu) file."
                                    if not self.up_list.count() else "")
            return
        try:
            cfg = upload.resolve(name)
        except upload.UploadError as e:
            self.up_details.setText(str(e))
            return
        dest = cfg.get("DestinationType") or "any"
        self.up_details.setText(f"{cfg['RequestMethod']} {cfg.get('RequestURL', '')}\n"
                                f"Body: {cfg['Body']}  ·  For: {dest}  ·  File: {name}")

    def _import(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Import custom uploader", os.path.expanduser("~"),
                                                "ShareX custom uploader (*.sxcu *.json);;All files (*)")
        last = None
        for p in paths:
            try:
                last = upload.import_sxcu(p)
            except (upload.UploadError, OSError) as e:
                QMessageBox.warning(self, "Import uploader", f"Couldn't import {os.path.basename(p)}:\n{e}")
        if last:
            if not self._default_uploader:
                self._default_uploader = last
            self._refresh_uploaders(select=last)

    def _set_default(self):
        name = self._selected_uploader()
        if name:
            self._default_uploader = name
            self._refresh_uploaders(select=name)

    def _remove(self):
        name = self._selected_uploader()
        if not name:
            return
        if QMessageBox.question(self, "Remove uploader", f"Remove “{self.up_list.currentItem().text()}”?") \
                != QMessageBox.StandardButton.Yes:
            return
        upload.remove_uploader(name)
        if self._default_uploader == name:
            self._default_uploader = ""
        self._refresh_uploaders()

    def _trusted_certs(self):
        TrustedCertsDialog(self).exec()

    def _test_upload(self):
        name = self._selected_uploader()
        if not name or self._uploader is not None:
            return
        path = test_image()
        self._uploader = upload.Uploader(self)
        self._uploader.finished.connect(lambda res, err: self._test_done(path, res, err))
        self.b_test.setEnabled(False)
        self.test_result.setStyleSheet("")
        self.test_result.setText(f"Uploading a test image with {self.up_list.currentItem().text()}…")
        self._uploader.start(path, name)

    def _test_done(self, path, res, err):
        try:
            os.remove(path)
        except OSError:
            pass
        error = getattr(self._uploader, "error", None)
        self._uploader = None
        self._uploader_selected()
        if isinstance(error, upload.UntrustedCertificate) and ask_trust(self, error):
            self._test_upload()
            return
        if res is None:
            self.test_result.setStyleSheet(f"color: {style.RECORD.name()};")
            self.test_result.setText(f"Test upload failed: {err}")
            return
        def link(u):
            u = html.escape(u)
            return f'<a href="{u}">{u}</a>'
        extra = f"<br>Deletion URL: {link(res.delete_url)}" if res.delete_url else ""
        self.test_result.setText(f"Uploaded: {link(res.url)}{extra}")


def ask_trust(parent, error):
    """Ask whether to trust a server's self-signed certificate (pins it). True if trusted."""
    where = error.host if error.port == 443 else f"{error.host}:{error.port}"
    box = QMessageBox(QMessageBox.Icon.Warning, "Untrusted certificate",
                      f"<b>{html.escape(where)}</b> uses a certificate that isn't signed by a trusted "
                      "authority — usually a self-signed certificate on your own server.", parent=parent)
    box.setInformativeText(
        "Only trust it if this is your server and the fingerprint is the one it should have. "
        "Uploads will then accept exactly this certificate for this server (if it ever changes, "
        "they stop until you trust the new one).<br><br>SHA-256:<br><tt>"
        + upload.pretty_fingerprint(error.fingerprint) + "</tt>")
    trust = box.addButton("Trust This Certificate", QMessageBox.ButtonRole.AcceptRole)
    box.addButton(QMessageBox.StandardButton.Cancel)
    box.exec()
    if box.clickedButton() is not trust or not error.fingerprint:
        return False
    upload.trust_certificate(error.host, error.port, error.fingerprint)
    return True


class TrustedCertsDialog(QDialog):
    """Lists pinned self-signed certificates; Remove forgets one."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Trusted certificates")
        self.resize(640, 300)
        v = QVBoxLayout(self)
        self.list = QListWidget()
        v.addWidget(QLabel("Upload servers whose self-signed certificate you trusted:"))
        v.addWidget(self.list, 1)
        row = QHBoxLayout()
        self.b_remove = QPushButton(QIcon.fromTheme("list-remove"), "Remove")
        self.b_remove.clicked.connect(self._remove)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(self.b_remove)
        row.addStretch(1)
        row.addWidget(close)
        v.addLayout(row)
        self._fill()

    def _fill(self):
        self.list.clear()
        for hp, fp in sorted((settings.get("upload.trusted_certs") or {}).items()):
            item = QListWidgetItem(f"{hp}\n    {upload.pretty_fingerprint(fp)}")
            item.setData(Qt.ItemDataRole.UserRole, hp)
            self.list.addItem(item)
        self.b_remove.setEnabled(self.list.count() > 0)

    def _remove(self):
        item = self.list.currentItem()
        if item is None:
            return
        host, _, port = item.data(Qt.ItemDataRole.UserRole).rpartition(":")
        upload.untrust_certificate(host, int(port))
        self._fill()
