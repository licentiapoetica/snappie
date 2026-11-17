"""The convert window: pick a format and tune it, see what comes out, convert.

Opened by `snappie convert FILE` (and Dolphin's "Convert…"), and by the video editor's Export,
which passes its own cuts and crop instead of the start/end fields here.
"""

import os
import threading

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QPixmap
from PyQt6.QtWidgets import (QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                             QFormLayout, QHBoxLayout, QLabel, QPushButton, QSlider, QSpinBox, QVBoxLayout,
                             QWidget)

from . import convert, settings

TITLE = "Snappie — Convert"


def _time(t):
    m, s = divmod(max(0.0, t), 60)
    return f"{int(m)}:{s:04.1f}"


class ConvertDialog(QDialog):
    """convert(opts, out_path) is emitted when the user clicks Convert."""
    convert = pyqtSignal(object, str)
    edit_video = pyqtSignal()
    _in_gui = pyqtSignal(object)    # a callable from the black-bar thread

    def __init__(self, info, segments=None, crop=None, parent=None):
        super().__init__(parent)
        self.info = info
        self.fixed_cuts = segments is not None or crop is not None    # from the video editor
        self.segments, self.crop = segments, crop
        self._detecting = False
        self._in_gui.connect(lambda fn: fn(), Qt.ConnectionType.QueuedConnection)
        self.setWindowTitle(TITLE)
        self.setWindowIcon(QIcon.fromTheme("snappie"))
        have = convert.encoders()
        v = QVBoxLayout(self)

        head = QLabel(f"<b>{os.path.basename(info.path)}</b><br><small>{info.describe()}</small>")
        head.setTextFormat(Qt.TextFormat.RichText)
        v.addWidget(head)

        form = QFormLayout()
        v.addLayout(form)
        self.fmt = QComboBox()
        for key, label in convert.FORMATS.items():
            why = convert.missing_encoder(key, have)
            if key in convert.VIDEO_FORMATS and not why:
                label += f" ({convert.ENCODER_LABEL.get(convert.video_encoder(key, have), '')})"
            self.fmt.addItem(label, key)
            if why:
                item = self.fmt.model().item(self.fmt.count() - 1)
                item.setEnabled(False)
                item.setToolTip(why)
        form.addRow("Format:", self.fmt)

        self.copy = QCheckBox("Copy the video as it is when nothing changes (lossless, fast)")
        form.addRow("", self.copy)
        self.fps = QSpinBox(minimum=0, maximum=120, suffix=" fps")
        self.fps.setSpecialValueText("As the source")
        form.addRow("Frame rate:", self.fps)
        self.width = QSpinBox(minimum=0, maximum=16384, singleStep=40, suffix=" px")
        self.width.setSpecialValueText("Keep")
        form.addRow("Maximum width:", self.width)
        qrow = QHBoxLayout()
        self.quality = QSlider(Qt.Orientation.Horizontal, minimum=1, maximum=100)
        self.quality_label = QLabel()
        self.quality_label.setMinimumWidth(150)
        qrow.addWidget(self.quality, 1)
        qrow.addWidget(self.quality_label)
        form.addRow("Quality:", qrow)

        if self.fixed_cuts:
            parts = convert.kept(info, convert.Options(segments=segments))
            text = f"{len(parts)} part{'s' if len(parts) != 1 else ''} kept" if segments else "Whole video"
            if crop:
                text += f", cropped to {int(crop[2])}×{int(crop[3])}"
            form.addRow("Edit:", QLabel(text))
        else:
            trow = QHBoxLayout()
            self.start = QDoubleSpinBox(decimals=2, maximum=max(0.0, info.duration), singleStep=0.5, suffix=" s")
            self.end = QDoubleSpinBox(decimals=2, maximum=max(0.0, info.duration), singleStep=0.5, suffix=" s")
            self.end.setValue(info.duration)
            trow.addWidget(self.start)
            trow.addWidget(QLabel("to"))
            trow.addWidget(self.end)
            trow.addStretch(1)
            if not info.piped and not info.image:
                ed = QPushButton(QIcon.fromTheme("document-edit"), "Cut and crop…")
                ed.setToolTip("Open the video editor: remove parts anywhere, crop")
                ed.clicked.connect(self._edit)
                trow.addWidget(ed)
            form.addRow("Keep:", trow)
            self.bars = QCheckBox("Remove black bars")
            self.bars.setEnabled(not info.piped)
            form.addRow("Crop:", self.bars)
            self.start.valueChanged.connect(self._update)
            self.end.valueChanged.connect(self._update)
            self.bars.toggled.connect(self._bars)

        self.speed = QDoubleSpinBox(minimum=0.1, maximum=16, singleStep=0.25, decimals=2, suffix=" ×", value=1)
        self.repeat = QSpinBox(minimum=1, maximum=50, suffix=" ×")
        srow = QHBoxLayout()
        srow.addWidget(self.speed)
        self.repeat_label = QLabel("Play it")
        srow.addWidget(self.repeat_label)
        srow.addWidget(self.repeat)
        srow.addStretch(1)
        form.addRow("Speed:", srow)
        self.background = QPushButton()
        self.background.clicked.connect(self._pick_background)
        self.background_label = QLabel("Behind transparency:")
        form.addRow(self.background_label, self.background)
        self.audio = QCheckBox("Keep the sound")
        self.audio.setChecked(True)
        form.addRow("", self.audio)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        v.addWidget(self.summary)
        self.remember = QCheckBox()
        v.addWidget(self.remember)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.go = buttons.addButton("Convert", QDialogButtonBox.ButtonRole.AcceptRole)
        self.go.setIcon(QIcon.fromTheme("media-playback-start"))
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        v.addWidget(buttons)

        self._bg = QColor(settings.get("convert.background") or "#ffffff")
        start = settings.get("convert.last_format") or ("mp4" if info.image else "gif")
        i = self.fmt.findData(start)
        if i < 0 or not self.fmt.model().item(i).isEnabled():
            i = next((n for n in range(self.fmt.count()) if self.fmt.model().item(n).isEnabled()), 0)
        self.fmt.setCurrentIndex(i)
        self._kind = None
        self._format_changed()
        self.fmt.currentIndexChanged.connect(self._format_changed)
        for w in (self.fps, self.width, self.speed, self.repeat):
            w.valueChanged.connect(self._update)
        self.quality.valueChanged.connect(self._update)
        self.copy.toggled.connect(self._update)
        self.audio.toggled.connect(self._update)

    # -- state

    def options(self):
        o = convert.Options(self.fmt.currentData(), self.fps.value(), self.width.value(), self.quality.value(),
                            speed=round(self.speed.value(), 2), repeat=self.repeat.value(),
                            background=self._bg.name(), audio=self.audio.isChecked(), copy=self.copy.isChecked())
        if self.fixed_cuts:
            o.segments, o.crop = self.segments, self.crop
        else:
            s, e = self.start.value(), self.end.value()
            if s > 0.005 or e < self.info.duration - 0.005:
                o.segments = [(s, e)]
            o.crop = self.crop if self.bars.isChecked() else None
        if o.fmt in convert.ANIMATIONS:
            o.repeat = 1
        return o

    def _format_changed(self):
        fmt = self.fmt.currentData()
        kind = "anim" if fmt in convert.ANIMATIONS else "video"
        if kind != self._kind:        # each kind has its own saved defaults
            self._kind = kind
            d = convert.Options.defaults(fmt)
            for w, val in ((self.fps, d.fps), (self.width, d.width), (self.quality, d.quality)):
                w.blockSignals(True)
                w.setValue(val)
                w.blockSignals(False)
            what = "GIF / WebP" if kind == "anim" else "videos"
            self.remember.setText(f"Use these as the defaults for {what}")
        video = kind == "video"
        self.copy.setVisible(video and not self.info.image)
        self.copy.setChecked(video and not self.info.image)
        self.repeat.setVisible(video)
        self.repeat_label.setVisible(video)
        self.background.setVisible(video and self.info.alpha)
        self.background_label.setVisible(video and self.info.alpha)
        self.audio.setVisible(video and self.info.has_audio)
        self._update()

    def _update(self):
        o = self.options()
        pm = QPixmap(16, 16)
        pm.fill(self._bg)
        self.background.setIcon(QIcon(pm))
        self.background.setText(self._bg.name())
        copying = convert.can_copy(self.info, o)
        self.quality.setEnabled(not copying)
        self.quality_label.setText("—" if copying else convert.describe_quality(o.fmt, o.quality))
        w, h = convert.out_size(self.info, o)
        parts = convert.kept(self.info, o)
        out = convert.output_path(self.info.path, o.fmt)
        bits = [f"→ {os.path.basename(out)}", f"{w}×{h}", _time(convert.out_duration(self.info, o))]
        if copying:
            bits.append("streams copied, no quality loss")
        if self._detecting:
            bits.append("looking for black bars…")
        ok = bool(parts)
        self.summary.setText(" · ".join(bits) if ok else "Nothing left to convert: the end is before the start.")
        self.go.setEnabled(ok and not self._detecting)

    # -- actions

    def _bars(self, on):
        if not on or self.crop is not None:
            self._update()
            return
        self._detecting = True
        self._update()
        result = {}

        def work():
            try:
                result["box"] = convert.detect_bars(self.info)
            except (RuntimeError, OSError):
                result["box"] = None
            self._in_gui.emit(done)

        def done():
            self._detecting = False
            self.crop = result.get("box")
            if self.crop is None:
                self.bars.setChecked(False)
                self.bars.setText("Remove black bars (none found)")
            else:
                self.bars.setText(f"Remove black bars ({self.crop[2]}×{self.crop[3]} left)")
            self._update()
        threading.Thread(target=work, daemon=True).start()

    def _pick_background(self):
        c = QColorDialog.getColor(self._bg, self, "Background")
        if c.isValid():
            self._bg = c
            self._update()

    def _edit(self):
        self.edit_video.emit()
        self.close()

    def _accept(self):
        o = self.options()
        settings.save(**{"convert.last_format": o.fmt})
        if self.remember.isChecked():
            o.save_defaults()
        self.convert.emit(o, convert.output_path(self.info.path, o.fmt))
        self.accept()
