"""Shared look: black square panels, Breeze blue accent, white text.

Overlays (picker, in-place editor, recording bar) paint these by hand.
Normal windows (editor, history, settings) use the Plasma theme and only take
the accent from here.
"""

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
