import pytest
from PyQt6.QtCore import QPoint, QRect, Qt
from PyQt6.QtGui import QImage
from PyQt6.QtTest import QTest

from snappie import settings
from snappie.annotate.inplace import GAP, MARGIN, InPlaceEditor


class Screen:
    def __init__(self, geometry):
        self._geometry = geometry

    def geometry(self):
        return QRect(self._geometry)


@pytest.fixture
def editor_factory(qapp, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "SETTINGS", str(tmp_path / "settings.json"))
    editors = []

    def make(pointer=QPoint(800, 500), drag_up=False, mode="cursor", width=1600, height=1000,
             origin=QPoint(), dpr=1):
        settings.save(**{"shot.toolbar_position": mode, "annot.tool": "arrow"})
        screen = Screen(QRect(origin.x(), origin.y(), width, height))
        background = QImage(round(width * dpr), round(height * dpr), QImage.Format.Format_RGB32)
        background.setDevicePixelRatio(dpr)
        background.fill(Qt.GlobalColor.white)
        rect = QRect(width // 4, height // 4, width // 2, height // 2).translated(origin)
        ed = InPlaceEditor(screen, background, rect, pointer=pointer, drag_up=drag_up)
        ed.resize(width, height)
        ed.show()
        ed._place_panels(force=True)
        editors.append(ed)
        return ed

    yield make
    for ed in editors:
        ed.close()


def panel_bounds(ed):
    rect = ed.tools.geometry()
    if ed.props.isVisibleTo(ed):
        rect = rect.united(ed.props.geometry())
    return rect


def assert_on_screen(ed):
    available = ed.rect().adjusted(MARGIN, MARGIN, -MARGIN, -MARGIN)
    assert available.contains(ed.tools.geometry())
    if ed.props.isVisibleTo(ed):
        assert available.contains(ed.props.geometry())
        assert not ed.tools.geometry().intersects(ed.props.geometry())


@pytest.mark.parametrize("drag_up", [False, True])
def test_toolbar_follows_release_direction(editor_factory, drag_up):
    at = QPoint(800, 500)
    ed = editor_factory(pointer=at, drag_up=drag_up)
    group = panel_bounds(ed)
    if drag_up:
        assert group.bottom() < at.y() - GAP
    else:
        assert group.top() == at.y() + GAP
    assert abs(ed.tools.geometry().center().x() - at.x()) <= 1
    assert_on_screen(ed)


@pytest.mark.parametrize("at,drag_up", [(QPoint(5, 5), True), (QPoint(1595, 995), False)])
def test_toolbar_flips_and_shifts_at_screen_corners(editor_factory, at, drag_up):
    ed = editor_factory(pointer=at, drag_up=drag_up)
    if drag_up:
        assert panel_bounds(ed).top() == at.y() + GAP
    else:
        assert panel_bounds(ed).bottom() < at.y() - GAP
    assert_on_screen(ed)


def test_toolbar_overlaps_selection_when_neither_side_fits(editor_factory):
    ed = editor_factory(pointer=QPoint(800, 75), height=150)
    assert panel_bounds(ed).intersects(ed.doc.bounds().toAlignedRect())
    assert_on_screen(ed)


def test_wrapped_toolbar_accounts_for_properties_strip(editor_factory):
    ed = editor_factory(pointer=QPoint(240, 580), width=480, height=600)
    assert ed.tools.height() > 42
    assert panel_bounds(ed).bottom() < 580 - GAP
    assert_on_screen(ed)


@pytest.mark.parametrize("dpr", [1, 1.25, 2])
def test_toolbar_uses_logical_coordinates_on_offset_screen(editor_factory, dpr):
    origin = QPoint(-1920, -100)
    ed = editor_factory(pointer=origin + QPoint(800, 500), origin=origin, dpr=dpr)
    assert ed.tools.y() == 500 + GAP
    assert abs(ed.tools.geometry().center().x() - 800) <= 1
    assert_on_screen(ed)


def test_pointer_outside_editor_screen_is_clamped(editor_factory):
    ed = editor_factory(pointer=QPoint(2200, -200))
    assert ed.tools.geometry().right() == ed.width() - MARGIN - 1
    assert_on_screen(ed)


@pytest.mark.parametrize("mode,pointer", [("selection", QPoint(300, 100)), ("cursor", None)])
def test_default_and_keyboard_selection_use_selection_placement(editor_factory, mode, pointer):
    ed = editor_factory(mode=mode, pointer=pointer)
    sel = ed.doc.bounds().toAlignedRect()
    assert ed.tools.y() == sel.bottom() + GAP
    assert abs(ed.tools.geometry().center().x() - sel.center().x()) <= 1


def drag(ed, start, end):
    QTest.mousePress(ed, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(ed, end)
    QTest.mouseRelease(ed, Qt.MouseButton.LeftButton, pos=end)


@pytest.mark.parametrize("operation", ["move", "resize", "reselect"])
def test_selection_adjustments_reanchor_toolbar(editor_factory, operation):
    ed = editor_factory()
    if operation == "move":
        ed.canvas.set_tool("select")
        start = ed.doc.bounds().center().toPoint()
        end = start - QPoint(100, 100)
    elif operation == "resize":
        start = ed.doc.bounds().bottomRight().toPoint()
        end = start + QPoint(100, 100)
    else:
        start, end = QPoint(1500, 900), QPoint(900, 500)
    before = ed.doc.bounds()
    drag(ed, start, end)
    assert ed.doc.bounds() != before
    assert ed._anchor == (end, end.y() < start.y())
    assert_on_screen(ed)


def test_annotation_and_pointer_motion_leave_toolbar_still(editor_factory):
    ed = editor_factory()
    before = ed.tools.geometry()
    drag(ed, QPoint(600, 400), QPoint(900, 600))
    assert len(ed.doc.shapes) == 1
    QTest.mouseMove(ed, QPoint(100, 100))
    assert ed.tools.geometry() == before


def test_manual_position_overrides_selection_adjustments_until_reset(editor_factory):
    ed = editor_factory()
    ed.tools.drag.emit(QPoint(40, 40))
    before = ed.tools.geometry()
    start = ed.doc.bounds().bottomRight().toPoint()
    end = start + QPoint(80, 60)
    drag(ed, start, end)
    assert ed.tools.geometry() == before
    ed.tools.drag_reset.emit()
    assert ed.tools.geometry() != before
    assert panel_bounds(ed).top() == end.y() + GAP
    assert_on_screen(ed)
