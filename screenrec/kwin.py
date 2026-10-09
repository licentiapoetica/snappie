"""Talk to KWin through its scripting D-Bus API.

KWin scripts run inside the compositor and can see every window (geometry,
stacking order, pid, internal UUID).  They report back to us by calling a
D-Bus method on our own service, so this module also owns that service.
"""

import json
import os
import tempfile
import time

import dbus
import dbus.service
from gi.repository import GLib

SERVICE = "io.github.screenrec"
IFACE = "io.github.screenrec"

_LIST_JS = r"""
// geometry can be fractional (e.g. after dragging a window); we work in whole logical pixels
function rect(g) {
    var x = Math.round(g.x), y = Math.round(g.y);
    return [x, y, Math.round(g.x + g.width) - x, Math.round(g.y + g.height) - y];
}
var out = [];
var ws = workspace.stackingOrder;
for (var i = 0; i < ws.length; i++) {
    var w = ws[i];
    var onDesk = w.onAllDesktops || w.desktops.indexOf(workspace.currentDesktop) >= 0;
    out.push({
        id: w.internalId.toString(), caption: w.caption, cls: w.resourceClass,
        pid: w.pid, normal: w.normalWindow, dialog: w.dialog, minimized: w.minimized,
        visible: onDesk && !w.minimized && !w.hidden,
        frame: rect(w.frameGeometry), client: rect(w.clientGeometry), buffer: rect(w.bufferGeometry)
    });
}
callDBus("%SERVICE%", "/", "%IFACE%", "windows", JSON.stringify(out));
"""

# Keeps our control bar flush under the recorded area's left edge (above it, or
# inside, when there's no room), on top, out of the taskbar, and following the
# recorded window.
# Wayland apps can't position their own windows, so KWin does it.
_CONTROLS_JS = r"""
var pid = %PID%, caption = %CAPTION%, targetId = %TARGET%, rel = %REL%;
var ctl = null, target = null, manual = false, placing = false;

function area() {
    if (!target) return [rel[0], rel[1], rel[2], rel[3]];
    var g = target.frameGeometry;
    return [g.x + rel[0], g.y + rel[1], rel[2], rel[3]];
}
function place() {
    if (!ctl || manual) return;
    var r = area(), g = ctl.frameGeometry, w = g.width, h = g.height;
    var out = workspace.screenAt({x: r[0] + r[2] / 2, y: r[1] + r[3] / 2});
    var s = out ? workspace.clientArea(KWin.MaximizeArea, out, workspace.currentDesktop)
                : {x: -1e6, y: -1e6, width: 2e6, height: 2e6};
    var x = Math.min(Math.max(r[0], s.x), s.x + s.width - w);
    var y = r[1] + r[3];                                         // below
    if (y + h > s.y + s.height) y = r[1] - h;                    // above
    if (y < s.y) y = r[1] + r[3] - h;                            // inside, bottom
    placing = true;
    ctl.frameGeometry = {x: Math.round(x), y: Math.round(y), width: w, height: h};
    placing = false;
}
function adopt(w) {
    if (ctl || w.pid != pid || w.caption != caption) return;
    ctl = w;
    ctl.keepAbove = true;
    ctl.skipTaskbar = true;
    ctl.skipSwitcher = true;
    ctl.skipPager = true;
    ctl.onAllDesktops = true;
    ctl.interactiveMoveResizeStarted.connect(function () { manual = true; });
    ctl.frameGeometryChanged.connect(function () { if (!placing && !manual) place(); });
    place();
}
var all = workspace.windowList();
for (var i = 0; i < all.length; i++) {
    if (targetId && all[i].internalId.toString() == targetId) target = all[i];
}
if (target) target.frameGeometryChanged.connect(place);
workspace.windowAdded.connect(adopt);
for (var j = 0; j < all.length; j++) adopt(all[j]);
"""


class _Service(dbus.service.Object):
    def __init__(self, bus, owner):
        super().__init__(bus, "/")
        self.owner = owner

    @dbus.service.method(IFACE, in_signature="s")
    def windows(self, payload):
        self.owner._windows_reply = json.loads(payload)

    @dbus.service.method(IFACE)
    def stop(self):
        if self.owner.on_stop:
            self.owner.on_stop()


class AlreadyRunning(Exception):
    pass


class KWin:
    def __init__(self, bus):
        self.bus = bus
        if bus.name_has_owner(SERVICE):
            raise AlreadyRunning()
        self._name = dbus.service.BusName(SERVICE, bus, do_not_queue=True)
        self._svc = _Service(bus, self)
        self._windows_reply = None
        self.on_stop = None
        self._controls_plugin = None
        kwin = bus.get_object("org.kde.KWin", "/Scripting")
        self._load = kwin.get_dbus_method("loadScript", "org.kde.kwin.Scripting")
        self._unload = kwin.get_dbus_method("unloadScript", "org.kde.kwin.Scripting")

    def _run_script(self, source, plugin):
        self._unload(plugin)  # leftovers from a crashed run
        fd, path = tempfile.mkstemp(prefix="screenrec-", suffix=".js")
        with os.fdopen(fd, "w") as f:
            f.write(source.replace("%SERVICE%", SERVICE).replace("%IFACE%", IFACE))
        try:
            sid = self._load(path, plugin, signature="ss")
            self.bus.get_object("org.kde.KWin", f"/Scripting/Script{sid}").run(
                dbus_interface="org.kde.kwin.Script")
        finally:
            os.unlink(path)

    def list_windows(self, timeout=3.0):
        """Return KWin windows in stacking order (bottom first)."""
        self._windows_reply = None
        plugin = "screenrec_list"
        self._run_script(_LIST_JS, plugin)
        ctx = GLib.MainContext.default()
        deadline = time.monotonic() + timeout
        while self._windows_reply is None and time.monotonic() < deadline:
            ctx.iteration(False) or time.sleep(0.005)
        self._unload(plugin)
        if self._windows_reply is None:
            raise RuntimeError("KWin did not answer the window-list script")
        return self._windows_reply

    def place_controls(self, caption, target_id, rect):
        """Let KWin position our control bar near `rect` (relative to the target
        window's frame when target_id is given, else absolute)."""
        self.unplace_controls()
        self._controls_plugin = "screenrec_controls"
        src = (_CONTROLS_JS.replace("%PID%", str(os.getpid()))
               .replace("%CAPTION%", json.dumps(caption))
               .replace("%TARGET%", json.dumps(target_id or ""))
               .replace("%REL%", json.dumps(list(rect))))
        self._run_script(src, self._controls_plugin)

    def unplace_controls(self):
        if self._controls_plugin:
            try:
                self._unload(self._controls_plugin)
            except dbus.DBusException:
                pass
            self._controls_plugin = None


def request_stop(bus):
    """Ask an already-running instance to stop recording."""
    bus.get_object(SERVICE, "/").stop(dbus_interface=IFACE)
