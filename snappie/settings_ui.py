"""Settings dialog: native widgets in tabs, saved through settings.save on OK / Apply.

Every widget is bound to one settings key; only values that differ from the
stored ones are written, so untouched keys keep following DEFAULTS.
Uploader files (import / remove) are changed right away, like file operations.
"""

import html
import os
import tempfile
import time

from PyQt6.QtCore import QProcess, QSize, Qt, QUrl
from PyQt6.QtGui import QColor, QDesktopServices, QFont, QIcon, QImage, QPainter, QPixmap
from PyQt6.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox, QColorDialog, QComboBox, QDialog,
                             QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFontComboBox, QFormLayout,
                             QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
                             QListWidgetItem, QMenu, QMessageBox, QPushButton, QScrollArea, QSlider, QSpinBox,
                             QTableWidget,
                             QTableWidgetItem, QTabWidget, QToolButton, QVBoxLayout, QWidget)

from . import ocr, settings, style, upload, uploaders, version

REPO = "https://github.com/licentiapoetica/snappie"

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


class _AppList(QWidget):
    """App names for recording only some apps' sound: the apps playing right now plus the
    chosen ones, as checkboxes; apps that aren't playing can be added by name."""

    def __init__(self):
        super().__init__()
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        self.list = QListWidget()
        self.list.setMaximumHeight(130)
        v.addWidget(self.list)
        row = QHBoxLayout()
        self.name = QLineEdit(placeholderText="App name, binary or Flatpak id")
        self.name.returnPressed.connect(self._add)
        add = QPushButton(QIcon.fromTheme("list-add"), "Add")
        add.clicked.connect(self._add)
        refresh = QToolButton(icon=QIcon.fromTheme("view-refresh"), toolTip="List the apps playing sound now")
        refresh.clicked.connect(lambda: self.setValue(self.value(), rescan=True))
        row.addWidget(self.name, 1)
        row.addWidget(add)
        row.addWidget(refresh)
        v.addLayout(row)
        self._playing = None

    def _item(self, name, checked):
        for i in range(self.list.count()):
            it = self.list.item(i)
            if it.text().lower() == name.lower():
                if checked:
                    it.setCheckState(Qt.CheckState.Checked)
                return
        it = QListWidgetItem(name)
        it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        it.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self.list.addItem(it)

    def _add(self):
        name = self.name.text().strip()
        if name:
            self._item(name, True)
            self.name.clear()

    def value(self):
        return [self.list.item(i).text() for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.CheckState.Checked]

    def setValue(self, v, rescan=False):
        if self._playing is None or rescan:
            from . import appaudio
            self._playing = appaudio.playing_apps()
        self.list.clear()
        for name in v or []:
            self._item(name, True)
        for name in self._playing:
            self._item(name, False)


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

    def __init__(self, title="Annotation color"):
        super().__init__()
        self.title = title
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
        c = QColorDialog.getColor(QColor(self.color), self, self.title)
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
        self.tabs.addTab(_scroll(self._gif()), QIcon.fromTheme("image-gif"), "GIF && Convert")
        self.tabs.addTab(self._uploads(), QIcon.fromTheme("cloud-upload"), "Uploads")
        self.tabs.addTab(_scroll(self._sounds()), QIcon.fromTheme("preferences-desktop-sound"), "Sounds")
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

        about = _hint(f'Snappie {html.escape(version())} · <a href="{REPO}">GitHub</a>')
        about.setWordWrap(False)
        about.setOpenExternalLinks(True)
        about.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)  # copyable
        about.setToolTip("Mention this version when reporting a problem")
        bottom = QHBoxLayout()
        bottom.addWidget(about)
        bottom.addStretch(1)
        bottom.addWidget(self.buttons)

        lay = QVBoxLayout(self)
        lay.addWidget(self.tabs, 1)
        lay.addLayout(bottom)
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
        from .app import _autostart_enabled
        for key, (_, put) in self._fields.items():
            put(source[key] if source is not None else settings.get(key))
        # not a setting: an autostart entry (on by default, install.sh adds it)
        self.autostart.setChecked(True if source is not None else _autostart_enabled())
        self._sync_audio()
        if source is None:   # restoring defaults keeps the chosen uploader
            self._default_uploader = settings.get("upload.uploader") or ""
        self._refresh_uploaders()

    def values(self):
        out = {k: get() for k, (get, _) in self._fields.items()}
        out["upload.uploader"] = self._default_uploader
        return out

    def apply(self):
        from .app import _autostart_enabled, _set_autostart
        changes = {k: v for k, v in self.values().items() if v != settings.get(k)}
        if changes:
            settings.save(**changes)
        if self.autostart.isChecked() != _autostart_enabled():
            try:
                _set_autostart(self.autostart.isChecked())
            except OSError as e:
                QMessageBox.warning(self, "Start with Plasma", f"Couldn't change the autostart entry: {e}")
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
        position = self._bind("shot.toolbar_position", _combo([
            ("Selection center", "selection"), ("Near cursor", "cursor")]))
        position.setToolTip("Near cursor: above an upward drag, below otherwise; flips sides at screen edges.")
        form.addRow("Toolbar position:", position)
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
        audio = self._bind("rec.audio", QCheckBox("Record desktop audio"))
        form.addRow(audio)
        mode = self._bind("rec.audio_apps", _combo([
            ("All apps", "all"), ("Only the apps ticked below", "only"), ("All apps except the ones ticked below", "except")]))
        mode.setToolTip("Leave out voice chat, music and notifications, or record just a game.\n"
                        "Change it while recording with the ♪ button on the recording bar.")
        form.addRow("Apps:", mode)
        apps = self._bind("rec.audio_app_list", _AppList())
        form.addRow("", apps)
        from . import appaudio
        apps_hint = _hint("Apps show up here while they play sound. They still play to your speakers.")
        if not appaudio.available():
            from .deps import install_hint
            apps_hint.setText(f"Picking apps needs the PipeWire tools ({install_hint(['pw-tools'])}). Without "
                              "them, \"All apps\" records the whole desktop and the other choices no desktop audio.")
        form.addRow("", apps_hint)

        def sync():
            mode.setEnabled(audio.isChecked())
            apps.setEnabled(audio.isChecked() and mode.currentData() != "all")
        audio.toggled.connect(sync)
        mode.currentIndexChanged.connect(sync)
        self._sync_audio = sync
        mic = self._bind("rec.mic", QCheckBox("Record the microphone"))
        mic.setToolTip("Mute and unmute it while recording with the microphone button on the bar.")
        form.addRow(mic)
        ask = self._bind("rec.audio_ask", QCheckBox("Ask which apps to record before starting"))
        ask.setToolTip("Shows the apps playing sound and the microphone under the area; Enter starts.\n"
                       "What you tick there becomes the setting above.")
        ask.setEnabled(appaudio.available())
        form.addRow(ask)
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
        form.addRow("Quality:", self._bind("gif.quality", QSpinBox(minimum=1, maximum=100)))
        form.addRow(_hint("Wider recordings are scaled down. GIFs get big quickly: 10–15 fps and "
                          "960 px is a good balance. Quality 80 or more keeps all 256 GIF colors; "
                          "100 makes lossless WebP."))
        v.addWidget(box)

        box, form = _form("Converting to MP4 / WebM")
        fps = self._bind("convert.video_fps", QSpinBox(minimum=0, maximum=120, suffix=" fps"))
        fps.setSpecialValueText("As the source")
        form.addRow("Frame rate:", fps)
        width = self._bind("convert.video_max_width", QSpinBox(minimum=0, maximum=16384, singleStep=40,
                                                                suffix=" px"))
        width.setSpecialValueText("Keep")
        form.addRow("Maximum width:", width)
        form.addRow("Quality:", self._bind("convert.video_quality", QSpinBox(minimum=1, maximum=100)))
        form.addRow("Behind transparency:", self._bind("convert.background", _ColorPicker("Background")))
        form.addRow(_hint("Defaults for snappie convert, Dolphin's Convert entries and the video editor's "
                          "export. The convert window can change them for one file."))
        v.addWidget(box)
        v.addStretch(1)
        return page

    def _uploads(self):
        page = QWidget()
        v = QVBoxLayout(page)
        box = QGroupBox("Uploaders")
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
        b_add = QPushButton(QIcon.fromTheme("list-add"), "Add")
        self.add_menu = QMenu(b_add)
        self.add_menu.setToolTipsVisible(True)
        self.add_menu.aboutToShow.connect(self._fill_add_menu)
        b_add.setMenu(self.add_menu)
        self.b_edit = QPushButton(QIcon.fromTheme("document-edit"), "Edit…")
        self.b_edit.clicked.connect(self._edit)
        self.b_default = QPushButton(QIcon.fromTheme("starred-symbolic", QIcon.fromTheme("emblem-favorite")),
                                     "Set as Default")
        self.b_default.clicked.connect(self._set_default)
        self.b_remove = QPushButton(QIcon.fromTheme("list-remove"), "Remove")
        self.b_remove.clicked.connect(self._remove)
        self.b_test = QPushButton(QIcon.fromTheme("cloud-upload"), "Test Upload")
        self.b_test.setToolTip("Uploads a small generated image with the selected uploader")
        self.b_test.clicked.connect(self._test_upload)
        b_certs = QPushButton(QIcon.fromTheme("security-medium"), "Trusted Servers…")
        b_certs.setToolTip("Self-signed certificates and SSH servers you trusted for uploads")
        b_certs.clicked.connect(self._trusted_certs)
        for b in (b_add, self.b_edit, self.b_default, self.b_remove):
            col.addWidget(b)
        col.addStretch(1)
        col.addWidget(b_certs)
        col.addWidget(self.b_test)
        h.addLayout(col)
        v.addWidget(box, 1)

        row = QHBoxLayout()
        self.test_result = QLabel()
        self.test_result.setWordWrap(True)
        self.test_result.setOpenExternalLinks(True)
        self.test_result.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        row.addWidget(self.test_result, 1)
        self.b_response = QPushButton(QIcon.fromTheme("document-preview"), "Show Response…")
        self.b_response.setToolTip("What the server answered to the test upload, and what the uploader "
                                   "reads from it ({json:…} paths, headers)")
        self.b_response.clicked.connect(self._show_response)
        self.b_response.hide()
        row.addWidget(self.b_response, 0, Qt.AlignmentFlag.AlignTop)
        v.addLayout(row)

        box, form = _form("After uploading")
        form.addRow(self._bind("upload.copy_url", QCheckBox("Copy the URL to the clipboard")))
        form.addRow(self._bind("upload.plasma_progress",
                               QCheckBox("Show progress in Plasma's notifications (with a Cancel button)")))
        v.addWidget(box)
        v.addWidget(_hint("Add imports ShareX .sxcu files (catbox.moe, Zipline, XBackBone, Chevereto, … offer "
                          "them), or sets up an SFTP server or a command (rclone, scp, a script). Plugins for "
                          "other services go in the plugins folder (see the Add menu)."))
        self._entries = {}
        self._uploader = None
        self._response = None     # (response, uploader config, file name) of the last test upload
        return page

    def _sounds(self):
        from . import sound
        page = QWidget()
        v = QVBoxLayout(page)
        box = QGroupBox("Play a sound when")
        grid = QGridLayout(box)
        grid.setColumnStretch(1, 1)
        for row, (event, (label, names)) in enumerate(sound.EVENTS.items()):
            check = self._bind(f"sound.{event}", QCheckBox(label))
            path = self._bind(f"sound.{event}.file", QLineEdit())
            default = sound.theme_file(names)
            path.setPlaceholderText(f"System sound: {os.path.splitext(os.path.basename(default))[0]}"
                                    if default else "No system sound found: choose a file")
            path.setClearButtonEnabled(True)
            browse = QToolButton()
            browse.setIcon(QIcon.fromTheme("document-open"))
            browse.setToolTip("Choose a sound file")
            browse.clicked.connect(lambda _=False, e=path, ev=event: self._pick_sound(e, ev))
            test = QToolButton()
            test.setIcon(QIcon.fromTheme("media-playback-start"))
            test.setToolTip("Play")
            test.clicked.connect(lambda _=False, e=event, f=path: self._test_sound(e, f.text().strip()))
            path.setEnabled(check.isChecked())
            check.toggled.connect(path.setEnabled)
            for col, w in enumerate((check, path, browse, test)):
                grid.addWidget(w, row, col)
        v.addWidget(box)
        box, form = _form()
        self.sound_volume = self._bind("sound.volume", QSlider(Qt.Orientation.Horizontal))
        self.sound_volume.setRange(0, 100)
        form.addRow("Volume:", self.sound_volume)
        v.addWidget(box)
        v.addWidget(_hint("Empty means the sound from your sound theme (System Settings → Sounds). "
                          "The recording starts after its sound has played, so the sound isn't recorded."))
        v.addStretch(1)
        return page

    def _pick_sound(self, edit, event):
        start = os.path.dirname(edit.text()) if edit.text() else "/usr/share/sounds"
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a sound", start,
            "Sounds (*.oga *.ogg *.opus *.wav *.flac *.mp3 *.m4a *.aac);;All files (*)")
        if path:
            edit.setText(path)
            self._test_sound(event, path)    # hear it, and find out now if it can't be played

    def _test_sound(self, event, custom):
        from . import sound
        path = sound.file_for(event, custom)

        def failed(message):
            QMessageBox.warning(self, "Sound",
                                f"Snappie can't play {os.path.basename(path)}:\n{message}\n\n"
                                "Choose a WAV, OGG or FLAC file, or install the GStreamer plugins "
                                "for this format.")
        if not path:
            QMessageBox.warning(self, "Sound", "No sound file found.")
        elif not sound.play_file(path, self.sound_volume.value(), on_error=failed):
            failed("GStreamer couldn't start playing it.")

    def _hotkeys(self):
        page = QWidget()
        v = QVBoxLayout(page)
        from . import share_plugin
        box, form = _form("KDE integration")
        self.autostart = QCheckBox("Start Snappie with Plasma (in the system tray)")
        form.addRow(self.autostart)
        form.addRow(self._bind("dolphin_menu", QCheckBox(
            "Snappie in Dolphin's right-click menu (upload, annotate, pin, OCR, cut, convert)")))
        form.addRow(self._bind("share_menu", QCheckBox(
            "“Upload with Snappie” in the Share menus of Spectacle, Gwenview, Okular, Dolphin…")))
        form.addRow(_hint(share_plugin.describe(share_plugin.status())))
        v.addWidget(box)
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
        self._entries = {}
        for e in uploaders.entries():
            fname, display = e.file, e.name
            self._entries[fname] = e
            item = QListWidgetItem(display)
            item.setData(Qt.ItemDataRole.UserRole, fname)
            item.setIcon(QIcon.fromTheme("dialog-warning") if e.error
                         else QIcon.fromTheme(e.plugin.icon, QIcon.fromTheme("cloud-upload")))
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
        e = self._entries.get(name)
        self.b_edit.setEnabled(bool(e) and e.editable)
        self.b_edit.setToolTip("" if not e or e.editable else
                               "ShareX uploaders are changed in ShareX or a text editor, then imported again")
        self.b_default.setEnabled(bool(name) and name != self._default_uploader)
        self.b_remove.setEnabled(bool(name))
        self.b_test.setEnabled(bool(e) and not e.error and self._uploader is None)
        if not e:
            self.up_details.setText("No uploaders yet. Add one: import a ShareX .sxcu file, or set up an "
                                    "SFTP server or a command." if not self.up_list.count() else "")
            return
        kind = e.plugin.name if e.plugin else e.plugin_id or "Unknown"
        why = e.plugin.available() if e.plugin and not e.error else ""
        lines = [e.summary(), why, f"{kind}  ·  File: {name}"]
        self.up_details.setText("\n".join(x for x in lines if x))

    def _fill_add_menu(self):
        m = self.add_menu
        m.clear()
        a = m.addAction(QIcon.fromTheme("document-import"), "Import ShareX Uploader (.sxcu)…")
        a.triggered.connect(self._import)
        m.addSeparator()
        for p in uploaders.plugins().values():
            if not p.editable:
                continue
            a = m.addAction(QIcon.fromTheme(p.icon, QIcon.fromTheme("cloud-upload")), p.name + "…")
            a.setToolTip("\n".join(x for x in (p.description, p.available()) if x))
            a.triggered.connect(lambda _=False, p=p: self._add(p))
        for f, err in uploaders.load_errors.items():
            a = m.addAction(QIcon.fromTheme("dialog-warning"), f"{f} doesn't load")
            a.setToolTip(err)
            a.triggered.connect(lambda _=False, f=f, err=err: QMessageBox.warning(
                self, "Uploader plugin", f"{f} doesn't load:\n{err}\n\nMore in ~/.local/state/snappie/snappie.log"))
        m.addSeparator()
        a = m.addAction(QIcon.fromTheme("folder-open"), "Open Plugins Folder")
        a.setToolTip(_tilde(settings.PLUGINS_DIR))
        a.triggered.connect(self._open_plugins)

    def _open_plugins(self):
        os.makedirs(settings.PLUGINS_DIR, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(settings.PLUGINS_DIR))

    def _add(self, plugin):
        from .uploader_editor import UploaderEditor
        dlg = UploaderEditor(plugin, parent=self)
        if dlg.exec() and dlg.file:
            if not self._default_uploader:
                self._default_uploader = dlg.file
            self._refresh_uploaders(select=dlg.file)

    def _edit(self):
        from .uploader_editor import UploaderEditor
        e = self._entries.get(self._selected_uploader())
        if e is None or not e.editable:
            return
        dlg = UploaderEditor(e.plugin, e.name, e.config, e.file, parent=self)
        if dlg.exec():
            self._refresh_uploaders(select=dlg.file)

    def _import(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Import custom uploader", os.path.expanduser("~"),
                                                "ShareX custom uploader (*.sxcu *.json);;All files (*)")
        last = None
        for p in paths:
            try:
                last = uploaders.import_sxcu(p)
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
        uploaders.remove(name)
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
        self._response = None
        self.b_response.hide()
        self._uploader = upload.Uploader(self)
        self._uploader.finished.connect(lambda res, err: self._test_done(path, name, res, err))
        self.b_test.setEnabled(False)
        self.test_result.setStyleSheet("")
        self.test_result.setText(f"Uploading a test image with {self.up_list.currentItem().text()}…")
        self._uploader.start(path, name)

    def _test_done(self, path, name, res, err):
        try:
            os.remove(path)
        except OSError:
            pass
        error = getattr(self._uploader, "error", None)
        response = self._uploader.response
        self._uploader = None
        if response is not None:
            try:
                e = uploaders.get(name)
                cfg = upload.load_config(e.config) if e.plugin_id == "sxcu" else None
                self._response = (response, cfg, os.path.basename(path))
                self.b_response.show()
            except upload.UploadError:
                pass
        self._uploader_selected()
        if isinstance(error, upload.Untrusted) and error.can_trust and ask_trust(self, error):
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

    def _show_response(self):
        if self._response:
            from .response_view import ResponseDialog
            dlg = ResponseDialog(*self._response, parent=self)
            dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            dlg.show()


def ask_trust(parent, error):
    """Ask whether to trust a server (an upload.Untrusted error: self-signed certificate, unknown
    SSH host key) and remember it if so. True if trusted."""
    box = QMessageBox(QMessageBox.Icon.Warning, error.title, error.question, parent=parent)
    box.setInformativeText(f"{error.advice}<br><br>{html.escape(error.fingerprint_label)}:<br>"
                           f"<tt>{html.escape(error.fingerprint_text)}</tt>")
    trust = box.addButton(error.button, QMessageBox.ButtonRole.AcceptRole)
    box.addButton(QMessageBox.StandardButton.Cancel)
    box.exec()
    if box.clickedButton() is not trust or not error.can_trust:
        return False
    error.trust()
    return True


class TrustedCertsDialog(QDialog):
    """Lists the servers trusted for uploads (pinned self-signed certificates, SSH host keys);
    Remove forgets one."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Trusted servers")
        self.resize(640, 300)
        v = QVBoxLayout(self)
        self.list = QListWidget()
        v.addWidget(QLabel("Upload servers whose self-signed certificate or SSH host key you trusted:"))
        v.addWidget(self.list, 1)
        v.addWidget(_hint("SSH servers in ~/.ssh/known_hosts are trusted too; they aren't listed here."))
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
            item = QListWidgetItem(QIcon.fromTheme("security-medium"),
                                   f"{hp}  —  TLS certificate\n    SHA-256 {upload.pretty_fingerprint(fp)}")
            item.setData(Qt.ItemDataRole.UserRole, ("tls", hp))
            self.list.addItem(item)
        for host, value in sorted(upload.trusted_host_keys().items()):
            keytype, _, b64 = str(value).partition(" ")
            item = QListWidgetItem(QIcon.fromTheme("network-server"),
                                   f"{host}  —  SSH host key ({keytype})\n    {upload.ssh_fingerprint(b64)}")
            item.setData(Qt.ItemDataRole.UserRole, ("ssh", host))
            self.list.addItem(item)
        self.b_remove.setEnabled(self.list.count() > 0)

    def _remove(self):
        item = self.list.currentItem()
        if item is None:
            return
        kind, name = item.data(Qt.ItemDataRole.UserRole)
        if kind == "ssh":
            upload.untrust_host_key(name)
        else:
            host, _, port = name.rpartition(":")
            upload.untrust_certificate(host, int(port))
        self._fill()
