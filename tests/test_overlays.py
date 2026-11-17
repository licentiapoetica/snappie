from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QDialog, QWidget

from snappie import osd, style


class Keys(QWidget):
    def __init__(self, got):
        super().__init__()
        self.got = got

    def keyPressEvent(self, ev):
        self.got.append((self, ev.key()))


def test_overlays_take_input_while_a_modal_dialog_runs(qapp):
    """A dialog in exec() (uploader editor) blocked the picker: no clicks, not even Esc."""
    got = []
    plain = Keys(got)
    overlays = [Keys(got), Keys(got)]   # one per screen: they mustn't block each other either
    for o in overlays:
        style.never_blocked(o)

    def during():
        for w in [plain] + overlays:
            w.show()
            QTest.keyClick(w.windowHandle(), Qt.Key.Key_Escape)
        dlg.reject()

    dlg = QDialog()
    QTimer.singleShot(0, during)
    dlg.exec()
    assert got == [(o, Qt.Key.Key_Escape) for o in overlays]
    for w in [plain] + overlays:
        w.close()


def test_capture_windows_are_never_blocked(qapp):
    for w in (osd.Countdown(3), osd.ScrollBarOsd()):
        assert w.windowModality() == Qt.WindowModality.WindowModal
