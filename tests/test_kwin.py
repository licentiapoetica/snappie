import json
import os

from PyQt6.QtQml import QJSEngine

from snappie import kwin
from snappie.kwin import script_ids


def test_script_ids_from_introspection():
    xml = ('<node><interface name="org.kde.kwin.Scripting"/>'
           '<node name="Script1"/><node name="Script12"/><node name="Other3"/></node>')
    assert script_ids(xml) == {1, 12}
    assert script_ids("<node/>") == set()


def script_workspace(windows):
    engine = QJSEngine()
    engine.evaluate("var windows = " + json.dumps(windows) + ";" + """
        function signal() {
            return {callbacks: [], connect: function (fn) { this.callbacks.push(fn); },
                    emit: function (arg) { this.callbacks.forEach(function (fn) { fn(arg); }); }};
        }
        windows.forEach(function (w) { w.captionChanged = signal(); });
        var workspace = {windowList: function () { return windows; }, windowAdded: signal()};
    """)
    k = kwin.KWin.__new__(kwin.KWin)
    k._skipped = set()

    def run(source, plugin):
        result = engine.evaluate(source)
        assert not result.isError(), result.toString()
    k._run_script = run
    return k, engine


def test_picker_stays_out_of_switcher_taskbar_and_pager(qapp):
    windows = [{"pid": os.getpid(), "caption": "picker"},
               {"pid": os.getpid(), "caption": "editor"},
               {"pid": os.getpid() + 1, "caption": "picker"}]
    k, engine = script_workspace(windows)
    k.skip_taskbar("picker")
    k.skip_taskbar("editor")
    result = engine.evaluate("windows").toVariant()
    assert result[0]["skipTaskbar"] and result[0]["skipPager"] and result[0]["skipSwitcher"]
    assert result[1]["skipTaskbar"] and result[1]["skipPager"] and result[1]["skipSwitcher"]
    assert "skipTaskbar" not in result[2]
    # The title can arrive after KWin adds the surface.
    engine.evaluate('var late = {pid: windows[0].pid, caption: "", captionChanged: signal()};'
                    'workspace.windowAdded.emit(late); late.caption = "picker"; late.captionChanged.emit();')
    assert engine.evaluate("late.skipTaskbar && late.skipSwitcher").toBool()
