"""The form for setting up an uploader: one row per field of its plugin (uploaders.Field)."""

import os

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QAction, QIcon
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                             QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox, QVBoxLayout,
                             QWidget)

from . import style, uploaders


def _hint(text):
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet("color: palette(placeholder-text);")
    return label


def _tilde(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path == home or path.startswith(home + os.sep) else path


class _File(QWidget):
    def __init__(self, field):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(field.placeholder)
        self.edit.setClearButtonEnabled(True)
        self.title = field.label
        b = QPushButton(QIcon.fromTheme("document-open"), "Browse…")
        b.clicked.connect(self._browse)
        lay.addWidget(self.edit, 1)
        lay.addWidget(b)

    def _browse(self):
        cur = os.path.expanduser(self.edit.text().strip() or self.edit.placeholderText())
        start = cur if os.path.isdir(cur) else os.path.dirname(cur)
        if not os.path.isdir(start):
            start = os.path.expanduser("~")
        path, _ = QFileDialog.getOpenFileName(self, self.title, start)
        if path:
            self.edit.setText(_tilde(path))


def _widget(field, value):
    """-> (widget, get value)"""
    k = field.kind
    if k in ("text", "password"):
        w = QLineEdit(str(value if value is not None else ""))
        w.setPlaceholderText(field.placeholder)
        if k == "password":
            w.setEchoMode(QLineEdit.EchoMode.Password)
            show = QAction(QIcon.fromTheme("view-visible"), "Show", w)
            show.setCheckable(True)
            show.toggled.connect(lambda on: w.setEchoMode(QLineEdit.EchoMode.Normal if on
                                                          else QLineEdit.EchoMode.Password))
            w.addAction(show, QLineEdit.ActionPosition.TrailingPosition)
            return w, w.text
        return w, lambda: w.text().strip()
    if k == "int":
        w = QSpinBox(minimum=field.minimum, maximum=field.maximum)
        if field.special:
            w.setSpecialValueText(field.special)
        try:
            w.setValue(int(value or 0))
        except (TypeError, ValueError):
            w.setValue(field.minimum)
        return w, w.value
    if k == "bool":
        w = QCheckBox(field.label)
        w.setChecked(bool(value))
        return w, w.isChecked
    if k == "choice":
        w = QComboBox()
        for data, text in field.choices:
            w.addItem(text, data)
        i = w.findData(value)
        w.setCurrentIndex(i if i >= 0 else 0)
        return w, w.currentData
    if k == "file":
        w = _File(field)
        w.edit.setText(str(value or ""))
        return w, lambda: w.edit.text().strip()
    raise ValueError(k)


class UploaderEditor(QDialog):
    """Sets up a new uploader (file=None) or edits one; saves it on OK. `file` is then its file name."""

    def __init__(self, plugin, name="", config=None, file=None, parent=None):
        super().__init__(parent)
        self.plugin, self.file = plugin, file
        self.setWindowTitle(f"Snappie — {'edit' if file else 'add'} {plugin.name} uploader")
        self.setWindowIcon(QIcon.fromTheme(plugin.icon))
        self.setMinimumWidth(560)
        config = dict(plugin.defaults(), **(config or {}))

        v = QVBoxLayout(self)
        if plugin.description:
            d = QLabel(plugin.description)
            d.setWordWrap(True)
            v.addWidget(d)
        why = plugin.available()
        if why:
            w = QLabel(why)
            w.setWordWrap(True)
            w.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            w.setStyleSheet(f"color: {style.RECORD.name()};")
            v.addWidget(w)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.name = QLineEdit(name or plugin.name)
        form.addRow("Name:", self.name)
        self._get = {}
        for f in plugin.fields:
            w, get = _widget(f, config.get(f.key))
            self._get[f.key] = get
            if f.help:     # under the field, as wide as it (a row of its own wraps narrowly)
                box = QWidget()
                col = QVBoxLayout(box)
                col.setContentsMargins(0, 0, 0, 0)
                col.setSpacing(2)
                col.addWidget(w)
                col.addWidget(_hint(f.help))
                w = box
            form.addRow("" if f.kind == "bool" else f.label + ":", w)
        v.addLayout(form)
        if any(f.kind == "password" for f in plugin.fields):
            v.addWidget(_hint("Passwords are kept in the uploader's file in ~/.config/snappie/uploaders, "
                              "readable only by you."))
        v.addStretch(1)

        B = QDialogButtonBox.StandardButton
        buttons = QDialogButtonBox(B.Save | B.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        v.addWidget(buttons)

    def config(self):
        return {key: get() for key, get in self._get.items()}

    def accept(self):
        name = self.name.text().strip()
        if not name:
            QMessageBox.warning(self, "Uploader", "Give the uploader a name.")
            self.name.setFocus()
            return
        cfg = self.config()
        try:
            self.plugin.validate(cfg)
        except uploaders.UploadError as e:
            QMessageBox.warning(self, "Uploader", str(e))
            return
        except Exception as e:  # noqa: BLE001 - user plugin code
            QMessageBox.warning(self, "Uploader", f"The plugin failed to check this: {type(e).__name__}: {e}")
            return
        try:
            self.file = uploaders.save(self.plugin.id, name, cfg, self.file)
        except OSError as e:
            QMessageBox.warning(self, "Uploader", f"Couldn't save the uploader: {e.strerror or e}")
            return
        super().accept()
