"""Screenshot annotation: a shared shape/tool engine with two hosts.

- InPlaceEditor: Flameshot-like full-screen overlay right after picking.
- EditorWindow: normal window with zoom, crop and a beautify "Frame" panel.

Both edit a Document (base image + shapes + crop + frame); Document.render()
gives the flattened full-resolution result.
"""

from . import beautify
from .document import Document
from .inplace import InPlaceEditor
from .window import EditorWindow

__all__ = ["Document", "InPlaceEditor", "EditorWindow", "beautify"]
