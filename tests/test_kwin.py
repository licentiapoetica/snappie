import json
import os

import pytest
from PyQt6.QtQml import QJSEngine

from snappie import kwin
from snappie.kwin import script_ids


def test_script_ids_from_introspection():
    xml = ('<node><interface name="org.kde.kwin.Scripting"/>'
           '<node name="Script1"/><node name="Script12"/><node name="Other3"/></node>')
    assert script_ids(xml) == {1, 12}
    assert script_ids("<node/>") == set()


def script_workspace(windows, cursor=(50, 50)):
    engine = QJSEngine()
    engine.evaluate("var windows = " + json.dumps(windows) + ";" + """
        function signal() {
            return {callbacks: [], connect: function (fn) { this.callbacks.push(fn); },
                    emit: function (arg) { this.callbacks.forEach(function (fn) { fn(arg); }); }};
        }
        windows.forEach(function (w) { w.captionChanged = signal(); });
        var workspace = {windowList: function () { return windows; }, windowAdded: signal(),
                         activeWindow: null, cursorPos: """ + json.dumps(dict(zip(("x", "y"), cursor))) + "};")
    k = kwin.KWin.__new__(kwin.KWin)
    k._skipped = {}

    def run(source, plugin):
        result = engine.evaluate(source)
        assert not result.isError(), result.toString()
    k._run_script = run
    return k, engine


def test_picker_stays_in_switcher_without_taskbar_or_pager(qapp):
    windows = [{"pid": os.getpid(), "caption": "picker"},
               {"pid": os.getpid(), "caption": "editor"},
               {"pid": os.getpid() + 1, "caption": "picker"}]
    k, engine = script_workspace(windows)
    k.skip_taskbar("picker", skip_switcher=False)
    k.skip_taskbar("editor")
    result = engine.evaluate("windows").toVariant()
    assert result[0]["skipTaskbar"] and result[0]["skipPager"] and not result[0]["skipSwitcher"]
    assert result[1]["skipTaskbar"] and result[1]["skipPager"] and result[1]["skipSwitcher"]
    assert "skipTaskbar" not in result[2]
    # The title can arrive after KWin adds the surface.
    engine.evaluate('var late = {pid: windows[0].pid, caption: "", captionChanged: signal()};'
                    'workspace.windowAdded.emit(late); late.caption = "picker"; late.captionChanged.emit();')
    assert engine.evaluate("late.skipTaskbar && !late.skipSwitcher").toBool()


@pytest.mark.parametrize("cursor, expected", [((50, 50), 0), ((-50, 50), 1), ((500, 500), 0)])
def test_restore_overlay_focus_on_cursor_screen(qapp, monkeypatch, cursor, expected):
    windows = [{"pid": os.getpid(), "caption": "picker", "minimized": True,
                "frameGeometry": {"x": x, "y": 0, "width": 100, "height": 100}}
               for x in (0, -100)]
    windows += [{"pid": os.getpid() + 1, "caption": "picker", "frameGeometry": windows[0]["frameGeometry"]},
                {"pid": os.getpid(), "caption": "other", "frameGeometry": windows[0]["frameGeometry"]}]
    k, engine = script_workspace(windows, cursor)
    monkeypatch.setattr(kwin.GLib, "timeout_add", lambda *args: None)
    k.activate_overlay("picker")
    assert engine.evaluate(f"workspace.activeWindow === windows[{expected}]").toBool()
    assert not engine.evaluate("workspace.activeWindow.minimized").toBool()
