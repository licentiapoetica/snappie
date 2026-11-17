"""Shows what an upload server answered, for writing or fixing a custom uploader.

ShareX uploaders pull the link out of the response with `{json:...}`, `{xml:...}`,
`{regex:...}` or `{header:...}`; guessing those paths blind is the hard part of
writing an .sxcu.  This window shows the status, headers and body (JSON as a
tree: clicking a value gives its `{json:...}` path), what the uploader's own URL
fields evaluate to, and evaluates any expression you type against the response.
"""

import html
import json

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFontDatabase, QIcon
from PyQt6.QtWidgets import (QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHeaderView, QLabel,
                             QLineEdit, QPlainTextEdit, QTabWidget, QTreeWidget, QTreeWidgetItem,
                             QVBoxLayout)

from . import style, upload

MAX_NODES = 5000          # tree items; a huge response stays readable in the Raw tab
FIELDS = (("URL", "URL"), ("ThumbnailURL", "Thumbnail URL"), ("DeletionURL", "Deletion URL"),
          ("ErrorMessage", "Error message"))
KEYS = Qt.ItemDataRole.UserRole


def _mono():
    return QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)


def _text_view(text):
    w = QPlainTextEdit(text)
    w.setReadOnly(True)
    w.setFont(_mono())
    w.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
    return w


def _summary(v):
    if isinstance(v, dict):
        return f"{{{len(v)}}}"
    if isinstance(v, list):
        return f"[{len(v)}]"
    return v if isinstance(v, str) else json.dumps(v)


class ResponseDialog(QDialog):
    def __init__(self, response, cfg, filename="", parent=None):
        """response: a requests.Response; cfg: the normalized uploader config (upload.resolve)."""
        super().__init__(parent)
        self.setWindowTitle("Snappie — upload response")
        self.setWindowIcon(QIcon.fromTheme("network-server"))
        self.resize(760, 680)
        self.syn = upload.Syntax(filename=filename, uploader_name=str(cfg.get("Name") or ""))
        self.syn.response = response
        text = response.text or ""
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        v = QVBoxLayout(self)

        req = getattr(response, "request", None)
        bits = [f"<b>HTTP {response.status_code} {response.reason or ''}</b>",
                f"{req.method if req is not None else ''} {response.url}".strip()]
        if response.headers.get("Content-Type"):
            bits.append(response.headers["Content-Type"])
        bits.append(f"{len(response.content or b''):,} bytes")
        if getattr(response, "elapsed", None):
            bits.append(f"{response.elapsed.total_seconds():.2f} s")
        head = QLabel("  ·  ".join(bits[:1] + [html.escape(b) for b in bits[1:]]))
        head.setWordWrap(True)
        head.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        if response.status_code >= 400:
            head.setStyleSheet(f"color: {style.RECORD.name()};")
        v.addWidget(head)

        tabs = QTabWidget()
        if isinstance(data, (dict, list)):
            self.tree = QTreeWidget()
            self.tree.setHeaderLabels(["Key", "Value"])
            self.tree.setUniformRowHeights(True)
            self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            self._nodes = 0
            self._fill(self.tree.invisibleRootItem(), data, [])
            self.tree.expandToDepth(2)
            self.tree.currentItemChanged.connect(self._picked)
            tabs.addTab(self.tree, "JSON")
        raw = json.dumps(data, indent=2, ensure_ascii=False) if data is not None else text
        tabs.addTab(_text_view(raw), "Body")
        tabs.addTab(_text_view("\n".join(f"{k}: {val}" for k, val in response.headers.items())), "Headers")
        v.addWidget(tabs, 1)

        box = QGroupBox("This uploader reads")
        form = QFormLayout(box)
        for key, label in FIELDS:
            if cfg.get(key):
                form.addRow(label + ":", self._result_label(cfg[key]))
        if not cfg.get("URL"):
            form.addRow("URL:", self._result_label("", "(no URL field: the whole response is the link)"))
        v.addWidget(box)

        box = QGroupBox("Try an expression")
        bv = QVBoxLayout(box)
        self.expr = QLineEdit(cfg.get("URL") or "")
        self.expr.setFont(_mono())
        self.expr.setPlaceholderText("{json:files[0].url}   {regex:https?://\\S+}   {header:Location}")
        self.expr.setClearButtonEnabled(True)
        self.result = QLabel()
        self.result.setFont(_mono())
        self.result.setWordWrap(True)
        self.result.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.expr.textChanged.connect(self._evaluate)
        bv.addWidget(self.expr)
        bv.addWidget(self.result)
        hint = QLabel("Click a value in the JSON tab to get its path. Same syntax as in .sxcu files: "
                      "{json:…}, {xml:…}, {regex:pattern|group}, {header:Name}, {response}, {responseurl}.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {style.MUTED.name()};")
        bv.addWidget(hint)
        v.addWidget(box)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        v.addWidget(buttons)
        self._evaluate()

    def _fill(self, parent, value, keys):
        items = value.items() if isinstance(value, dict) else enumerate(value)
        for k, child in items:
            if self._nodes >= MAX_NODES:
                QTreeWidgetItem(parent, ["…", f"(more than {MAX_NODES} values; see the Body tab)"])
                return
            self._nodes += 1
            path = keys + [k]
            item = QTreeWidgetItem(parent, [f"[{k}]" if isinstance(k, int) else str(k), _summary(child)])
            item.setData(0, KEYS, path)
            item.setToolTip(1, "{json:%s}" % upload.json_path_to(path))
            if isinstance(child, (dict, list)):
                item.setForeground(1, style.MUTED)
                self._fill(item, child, path)

    def _picked(self, item, _prev=None):
        if item is not None and item.data(0, KEYS) is not None:
            self.expr.setText("{json:%s}" % upload.json_path_to(item.data(0, KEYS)))

    def _result_label(self, template, fixed=None):
        value = fixed if fixed is not None else self.syn.render(template).strip()
        lab = QLabel()
        lab.setWordWrap(True)
        lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lab.setToolTip(template)
        self._show(lab, value, empty=f"(empty: {template})")
        return lab

    def _evaluate(self):
        text = self.expr.text()
        self._show(self.result, self.syn.render(text) if text.strip() else "",
                   empty="(empty)" if text.strip() else "")

    @staticmethod
    def _show(label, value, empty):
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setStyleSheet("" if value else f"color: {style.MUTED.name()};")
        label.setText(value or empty)
