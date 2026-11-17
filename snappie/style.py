"""Shared look: black square panels, Breeze blue accent, white text.

Overlays (picker, in-place editor, recording bar) paint these by hand.
Normal windows (editor, history, settings) use the Plasma theme and only take
the accent from here.
"""

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor

ACCENT = QColor("#3daee9")
ACCENT_TEXT = QColor("#0b1a24")      # text on an accent background
DIM = QColor(0, 0, 0, 120)           # darkens what's outside the selection
PANEL_BG = QColor(0, 0, 0)
CHIP_BG = QColor(255, 255, 255, 18)
CHIP_HOVER = QColor(255, 255, 255, 40)
TEXT = QColor("#eff0f1")
MUTED = QColor("#9aa0a6")
WARN = QColor("#f67400")
RECORD = QColor("#da4453")

# Annotation palette (first one is the default)
PALETTE = ["#e93d3d", "#f67400", "#fdbc4b", "#27ae60", "#3daee9", "#9b59b6", "#ffffff", "#000000"]

# Stylesheet for small overlay buttons (toolbar, recording bar)
PANEL_QSS = """
QWidget#panel { background: #000000; }
QToolButton, QPushButton {
    background: transparent; color: #eff0f1; border: none; border-radius: 0; padding: 6px;
}
QToolButton:hover, QPushButton:hover { background: rgba(255,255,255,40); }
QToolButton:checked, QPushButton:checked { background: #3daee9; color: #0b1a24; }
QToolButton:disabled, QPushButton:disabled { color: #5a5f66; }
QToolTip { background: #000000; color: #eff0f1; border: 1px solid #3daee9; }
"""


def mark_overlay(widget):
    """A capture overlay (picker, in-place editor, recording bar, pins, ...), unlike our normal windows:
    - it stays usable while one of our modal dialogs (uploader editor, message box) is open. Qt blocks
      input to all other windows of the process then, so the picker took no clicks and no Esc. A
      window-modal window without a parent isn't blocked by earlier modals and blocks nothing.
    - the picker doesn't offer it as a window to capture (overlay_titles)."""
    widget.setWindowModality(Qt.WindowModality.WindowModal)
    widget.setProperty(_OVERLAY, True)


def is_overlay(widget):
    return bool(widget.property(_OVERLAY))


def overlay_titles():
    """Titles of our overlay windows that exist now."""
    from PyQt6.QtWidgets import QApplication
    return {w.windowTitle() for w in QApplication.topLevelWidgets() if is_overlay(w)}


def modal_dialog_open():
    """Is one of our modal dialogs showing? (Overlays are window-modal too, but block nothing.)"""
    from PyQt6.QtWidgets import QApplication
    return any(w.isVisible() and w.isModal() and not is_overlay(w) for w in QApplication.topLevelWidgets())


_OVERLAY = "snappie_overlay"
