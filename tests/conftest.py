import os

# Tests that need a QGuiApplication (images, fonts) run without a desktop, e.g. in CI: Qt would
# abort the whole run looking for a display.  Set before any test module imports Qt.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
