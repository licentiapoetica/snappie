import pytest
from PyQt6.QtCore import QPoint, QRect, Qt, QTimer
from PyQt6.QtGui import QImage
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QDialog, QWidget

from snappie import kwin, osd, style
from snappie.picker import Overlay, Picker


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

    def chain_at(self, w, x, y):
        return []


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


def picked(pk):
    got = []
    pk.finished.connect(got.append)
    return got


def test_middle_click_records_the_highlighted_area(qapp):
    pk = Picker([window("a", 1, "Firefox")], NoTree(), own_pid=42, mode="shot")
    pk.can_record = True
    got = picked(pk)
    pk.mouse_press(QPoint(50, 50), Qt.MouseButton.MiddleButton)
    assert len(got) == 1 and got[0].record and got[0].kind == "window" and got[0].win["id"] == "a"


def test_middle_click_while_dragging_records_the_rectangle(qapp):
    pk = Picker([window("a", 1, "Firefox")], NoTree(), own_pid=42, mode="shot")
    pk.can_record = True
    got = picked(pk)
    pk.mouse_press(QPoint(10, 10), Qt.MouseButton.LeftButton)
    pk.mouse_move(QPoint(60, 40))
    pk.mouse_press(QPoint(60, 40), Qt.MouseButton.MiddleButton)
    assert len(got) == 1 and got[0].record and got[0].kind == "region"
    assert got[0].rect == QRect(QPoint(10, 10), QPoint(60, 40)) and got[0].win["id"] == "a"


def test_middle_click_does_nothing_without_the_record_shortcut(qapp):
    pk = Picker([window("a", 1, "Firefox")], NoTree(), own_pid=42, mode="shot")
    got = picked(pk)
    try:
        pk.mouse_press(QPoint(50, 50), Qt.MouseButton.MiddleButton)
        assert got == []
    finally:
        pk._finish(None)


@pytest.fixture
def frozen_picker(qapp):
    scr = qapp.primaryScreen()
    bg = QImage(scr.size(), QImage.Format.Format_RGB32)
    bg.fill(Qt.GlobalColor.white)
    pk = Picker([], NoTree(), own_pid=42, mode="shot", backgrounds={scr.name(): bg})
    pk.mouse_move(scr.geometry().center())
    pk.show()
    qapp.processEvents()
    try:
        yield pk, pk.overlays[0]
    finally:
        pk._finish(None)


def test_hint_bar_fades_and_loupe_tracks_over_it(frozen_picker, monkeypatch):
    pk, overlay = frozen_picker
    normal = overlay.grab().toImage()
    bar = QRect(overlay._hint_bar)
    pixel = bar.topLeft() + QPoint(2, 2)  # panel background, clear of text
    assert normal.pixelColor(pixel).red() == 0
    loupes = []
    monkeypatch.setattr(overlay, "_draw_loupe", lambda p, pk, at: loupes.append(QPoint(at)))

    at = bar.center()
    QTest.mouseMove(overlay, at)
    faded = overlay.grab().toImage()
    assert pk.cursor == overlay.scr.geometry().topLeft() + at
    assert loupes[-1] == at
    assert faded.pixelColor(pixel).red() > 100  # frozen desktop shows through the whole panel

    QTest.mouseMove(overlay, QPoint(10, overlay.height() // 2))
    restored = overlay.grab().toImage()
    assert restored.pixelColor(pixel).red() == 0


def test_loupe_preserves_screen_pixels_after_drawing_breadcrumbs(frozen_picker):
    pk, overlay = frozen_picker
    at = QPoint(100, 150)
    pk.mouse_move(overlay.scr.geometry().topLeft() + at)
    pk._refresh()
    image = overlay.grab().toImage()
    # Inside a zoomed pixel, clear of the grid, border and centre crosshair.
    assert image.pixelColor(at + QPoint(28, 28)).name() == "#ffffff"


def test_click_through_hint_bar_selects_underlying_area(frozen_picker):
    pk, overlay = frozen_picker
    got = picked(pk)
    at = overlay._hint_bar.center()
    QTest.mouseClick(overlay, Qt.MouseButton.LeftButton, pos=at)
    assert len(got) == 1 and got[0].kind == "screen"


def test_drag_can_start_inside_hint_bar(frozen_picker):
    pk, overlay = frozen_picker
    got = picked(pk)
    start = overlay._hint_bar.center()
    end = start - QPoint(70, 80)
    QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(overlay, end)
    QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=end)
    assert len(got) == 1 and got[0].kind == "region"
    origin = overlay.scr.geometry().topLeft()
    assert got[0].rect == QRect(start + origin, end + origin).normalized()


def test_record_cursor_checkbox_still_toggles(frozen_picker):
    pk, overlay = frozen_picker
    pk.mode = "record"
    overlay.grab()
    got = picked(pk)
    before = pk.show_cursor
    QTest.mouseClick(overlay, Qt.MouseButton.LeftButton, pos=overlay._cursor_toggle.center())
    assert pk.show_cursor != before
    assert got == [] and pk.press is None


def test_switching_away_cancels_picker(frozen_picker, qapp):
    pk, overlay = frozen_picker
    got = picked(pk)
    assert overlay.isActiveWindow()
    other = QWidget()
    try:
        other.show()
        other.activateWindow()
        QTest.qWait(150)
        assert got == [None] and pk._done
    finally:
        other.close()


def test_focus_transfer_between_screen_overlays_keeps_picker(frozen_picker, qapp):
    pk, first = frozen_picker
    got = picked(pk)
    second = Overlay(pk, first.scr)
    pk.overlays.append(second)
    second.present()
    QTest.qWait(150)
    assert second.isActiveWindow()
    assert got == [] and not pk._done
    first.activateWindow()
    QTest.qWait(150)
    assert first.isActiveWindow()
    assert got == [] and not pk._done


def test_picker_does_not_cancel_before_it_gains_focus(qapp):
    pk = Picker([], NoTree(), own_pid=42)
    got = picked(pk)
    try:
        pk.focus_changed()
        QTest.qWait(150)
        assert got == [] and not pk._done
    finally:
        pk._finish(None)


@pytest.mark.parametrize("start,end", [(QPoint(20, 20), QPoint(90, 90)),
                                       (QPoint(90, 90), QPoint(20, 20)),
                                       (QPoint(90, 20), QPoint(20, 90)),
                                       (QPoint(20, 90), QPoint(90, 20))])
def test_picker_remembers_release_point_and_drag_direction(qapp, start, end):
    pk = Picker([], NoTree(), own_pid=42, mode="shot")
    got = picked(pk)
    try:
        pk.mouse_press(start, Qt.MouseButton.LeftButton)
        pk.mouse_move(end)
        pk.mouse_release(end, Qt.MouseButton.LeftButton)
        assert len(got) == 1
        assert got[0].pointer == end
        assert got[0].drag_up == (end.y() < start.y())
    finally:
        pk._finish(None)


@pytest.mark.parametrize("use_mouse", [False, True])
def test_picker_distinguishes_click_from_keyboard_selection(qapp, use_mouse):
    pk = Picker([], NoTree(), own_pid=42, mode="shot")
    got = picked(pk)
    at = QPoint(50, 50)
    try:
        pk.mouse_move(at)
        if use_mouse:
            pk.mouse_press(at, Qt.MouseButton.LeftButton)
            pk.mouse_release(at, Qt.MouseButton.LeftButton)
        else:
            pk._refresh()
            pk.select_current()
        assert len(got) == 1
        assert got[0].pointer == (at if use_mouse else None)
        assert not got[0].drag_up
    finally:
        pk._finish(None)
