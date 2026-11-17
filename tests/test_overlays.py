from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QDialog, QWidget

from snappie import kwin, osd, style
from snappie.picker import Picker


class Keys(QWidget):
    def __init__(self, got):
        super().__init__()
        self.got = got

    def keyPressEvent(self, ev):
        self.got.append((self, ev.key()))


def during_modal_dialog(fn):
    """Run fn while a QDialog is in exec(), like the uploader editor; returns what fn returns."""
    out = []
    dlg = QDialog()

    def run():
        out.append(fn())
        dlg.reject()
    QTimer.singleShot(0, run)
    dlg.exec()
    return out[0]


def test_overlays_take_input_while_a_modal_dialog_runs(qapp):
    """A dialog in exec() (uploader editor) blocked the picker: no clicks, not even Esc."""
    got = []
    plain = Keys(got)
    overlays = [Keys(got), Keys(got)]   # one per screen: they mustn't block each other either
    for o in overlays:
        style.mark_overlay(o)

    def keys():
        for w in [plain] + overlays:
            w.show()
            QTest.keyClick(w.windowHandle(), Qt.Key.Key_Escape)
    during_modal_dialog(keys)
    assert got == [(o, Qt.Key.Key_Escape) for o in overlays]
    for w in [plain] + overlays:
        w.close()


def test_capture_windows_are_overlays(qapp):
    for w in (osd.Countdown(3), osd.ScrollBarOsd()):
        assert w.windowModality() == Qt.WindowModality.WindowModal
        assert style.is_overlay(w)


def test_modal_dialog_open_ignores_overlays(qapp):
    pin = QWidget()
    style.mark_overlay(pin)
    pin.show()
    assert not style.modal_dialog_open()        # a pin is window-modal, but no dialog
    assert during_modal_dialog(style.modal_dialog_open)
    pin.close()


class NoTree:
    def begin(self, windows):
        pass

    def has_tree(self, w):
        return True   # no window grabs in the background


def window(wid, pid, caption):
    return {"id": wid, "pid": pid, "caption": caption, "cls": "x", "visible": True, "normal": True,
            "dialog": False, "dock": False, "frame": (0, 0, 100, 100), "client": (0, 0, 100, 100),
            "buffer": (0, 0, 100, 100)}


def test_picker_offers_our_windows_but_not_our_overlays(qapp):
    bar = QWidget()
    bar.setWindowTitle("Snappie — recording")
    style.mark_overlay(bar)
    wins = [window("a", 1, "Firefox"), window("b", 42, "Snappie — settings"),
            window("c", 42, "Snappie — edit SFTP uploader"), window("d", 42, "Snappie — recording"),
            window("e", 42, "Snappie — recording <2>‎"), window("f", 7, "Snappie — recording")]
    pk = Picker(wins, NoTree(), own_pid=42)
    try:
        assert [w["id"] for w in pk.stack] == ["f", "c", "b", "a"]   # topmost first
    finally:
        pk._finish(None)
        bar.close()


class FakeEffects:
    def __init__(self, loaded):
        self.loaded = set(loaded)
        self.calls = []

    def get_object(self, service, path):
        return self

    def isEffectLoaded(self, name, dbus_interface=None):
        return name in self.loaded

    def unloadEffect(self, name, dbus_interface=None):
        self.calls.append(("unload", name))
        self.loaded.discard(name)

    def loadEffect(self, name, dbus_interface=None):
        self.calls.append(("load", name))
        self.loaded.add(name)


def fake_kwin(bus):
    k = kwin.KWin.__new__(kwin.KWin)
    k.bus = bus
    k._suspended = set()
    return k


def test_suspended_effects_come_back(monkeypatch):
    bus = FakeEffects({"dialogparent"})
    monkeypatch.setattr(kwin.dbus, "Interface", lambda obj, iface: obj)
    k = fake_kwin(bus)
    assert k.suspend_effect("dialogparent")
    assert not k.suspend_effect("dialogparent")   # already unloaded
    assert not k.suspend_effect("blur")           # the user doesn't use it: leave it alone
    k.resume_effect("dialogparent")
    k.resume_effect("dialogparent")
    k.resume_effect("blur")
    assert bus.calls == [("unload", "dialogparent"), ("load", "dialogparent")]
