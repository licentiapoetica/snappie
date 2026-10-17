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
        active: workspace.activeWindow === w, fullscreen: w.fullScreen,
        visible: onDesk && !w.minimized && !w.hidden,
        frame: rect(w.frameGeometry), client: rect(w.clientGeometry), buffer: rect(w.bufferGeometry)
    });
}
callDBus("%SERVICE%", "/", "%IFACE%", "windows", JSON.stringify(out));
"""

# Places one of our own windows (found by pid + caption).  Wayland apps can't
# position their own windows, so KWin does it.
#   mode "exact":    frame = rect (relative to the target window's frame if given)
#   mode "below":    flush under rect's left edge (above it, or inside, when there's
#                    no room) - the recording bar
#   mode "at":       top-left at rect[0], rect[1], own size (pinned screenshots)
# The window stays on top, out of the taskbar, follows the target window, and
# stops being auto-placed once the user moves it (unless opts.pinned).
_PLACE_JS = r"""
var pid = %PID%, caption = %CAPTION%, targetId = %TARGET%, rel = %REL%, mode = %MODE%, opts = %OPTS%;
var ctl = null, target = null, manual = false, placing = false;

function area() {
    if (!target) return [rel[0], rel[1], rel[2], rel[3]];
    var g = target.frameGeometry;
    return [g.x + rel[0], g.y + rel[1], rel[2], rel[3]];
}
function place() {
    if (!ctl || manual) return;
    var r = area(), g = ctl.frameGeometry, w = g.width, h = g.height, x, y;
    if (mode == "exact") {
        x = r[0]; y = r[1]; w = r[2]; h = r[3];
    } else if (mode == "at") {
        x = r[0]; y = r[1];
    } else {
        var out = workspace.screenAt({x: r[0] + r[2] / 2, y: r[1] + r[3] / 2});
        var s = out ? workspace.clientArea(KWin.MaximizeArea, out, workspace.currentDesktop)
                    : {x: -1e6, y: -1e6, width: 2e6, height: 2e6};
        x = Math.min(Math.max(r[0], s.x), s.x + s.width - w);
        y = r[1] + r[3];                                         // below
        if (y + h > s.y + s.height) y = r[1] - h;                // above
        if (y < s.y) y = r[1] + r[3] - h;                        // inside, bottom
    }
    placing = true;
    ctl.frameGeometry = {x: Math.round(x), y: Math.round(y), width: w, height: h};
    placing = false;
    if (opts.once) manual = true;
}
function adopt(w) {
    if (ctl || w.pid != pid || w.caption != caption) return;
    ctl = w;
    ctl.keepAbove = opts.keepAbove !== false;
    ctl.skipTaskbar = opts.skipTaskbar !== false;
    ctl.skipSwitcher = opts.skipTaskbar !== false;
    ctl.skipPager = true;
    if (opts.allDesktops !== false) ctl.onAllDesktops = true;
    if (!opts.pinned)
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

# Reports the cursor position (and the target window's frame origin, so moving
# windows are handled) at most ~120 times a second.
_CURSOR_JS = r"""
var targetId = %TARGET%, target = null, last = 0;
var all = workspace.windowList();
for (var i = 0; i < all.length; i++)
    if (targetId && all[i].internalId.toString() == targetId) target = all[i];
function send() {
    var now = Date.now();
    if (now - last < 8) return;
    last = now;
    var p = workspace.cursorPos, fx = 0, fy = 0;
    if (target) { fx = target.frameGeometry.x; fy = target.frameGeometry.y; }
    callDBus("%SERVICE%", "/", "%IFACE%", "cursor", p.x, p.y, fx, fy);
}
workspace.cursorPosChanged.connect(send);
if (target) target.frameGeometryChanged.connect(function () { last = 0; send(); });
send();
"""

_ACTIVATE_JS = r"""
var id = %TARGET%, all = workspace.windowList();
for (var i = 0; i < all.length; i++)
    if (all[i].internalId.toString() == id) workspace.activeWindow = all[i];
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

    @dbus.service.method(IFACE, in_signature="dddd")
    def cursor(self, x, y, fx, fy):
        if self.owner.on_cursor:
            self.owner.on_cursor(float(x), float(y), float(fx), float(fy))

    @dbus.service.method(IFACE, in_signature="as", out_signature="s")
    def run(self, argv):
        """Another `screenrec` invocation forwards its command line here."""
        if not self.owner.on_command:
            return "error: busy"
        return self.owner.on_command([str(a) for a in argv]) or ""


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
        self.on_cursor = None      # (x, y, target frame x, target frame y)
        self.on_command = None     # argv -> reply text
        self._placed = {}          # caption -> plugin name
        self._cursor_plugin = None
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

    def place(self, caption, rect, target_id=None, mode="exact", **opts):
        """Let KWin position our window titled `caption` (see _PLACE_JS for modes and opts:
        keepAbove, skipTaskbar, allDesktops, once, pinned). rect is (x, y, w, h), relative
        to the target window's frame when target_id is given, else global logical."""
        self.unplace(caption)
        plugin = f"screenrec_place_{len(self._placed)}_{abs(hash(caption)) % 100000}"
        src = (_PLACE_JS.replace("%PID%", str(os.getpid()))
               .replace("%CAPTION%", json.dumps(caption))
               .replace("%TARGET%", json.dumps(target_id or ""))
               .replace("%REL%", json.dumps([int(v) for v in rect]))
               .replace("%MODE%", json.dumps(mode))
               .replace("%OPTS%", json.dumps(opts)))
        self._run_script(src, plugin)
        self._placed[caption] = plugin

    def unplace(self, caption):
        plugin = self._placed.pop(caption, None)
        if plugin:
            try:
                self._unload(plugin)
            except dbus.DBusException:
                pass

    # recording bar (kept for the old call sites)
    def place_controls(self, caption, target_id, rect):
        self.place(caption, rect, target_id, mode="below")

    def unplace_controls(self, caption=None):
        from .controls import TITLE
        self.unplace(caption or TITLE)

    def track_cursor(self, target_id=None):
        """Start calling self.on_cursor on pointer motion (until untrack_cursor)."""
        self.untrack_cursor()
        self._cursor_plugin = "screenrec_cursor"
        self._run_script(_CURSOR_JS.replace("%TARGET%", json.dumps(target_id or "")), self._cursor_plugin)

    def untrack_cursor(self):
        if self._cursor_plugin:
            try:
                self._unload(self._cursor_plugin)
            except dbus.DBusException:
                pass
            self._cursor_plugin = None

    def cursor_pos(self, timeout=1.0):
        """Current pointer position (global logical) as (x, y), or None."""
        got, prev = [], self.on_cursor
        self.on_cursor = lambda x, y, fx, fy: got.append((x, y))
        try:
            self.track_cursor()
            ctx = GLib.MainContext.default()
            deadline = time.monotonic() + timeout
            while not got and time.monotonic() < deadline:
                ctx.iteration(False) or time.sleep(0.005)
        finally:
            self.untrack_cursor()
            self.on_cursor = prev
        return (round(got[0][0]), round(got[0][1])) if got else None

    def activate(self, window_id):
        self._run_script(_ACTIVATE_JS.replace("%TARGET%", json.dumps(window_id)), "screenrec_activate")
        GLib.timeout_add(500, lambda: self._unload("screenrec_activate") and False)

    def cleanup(self):
        for caption in list(self._placed):
            self.unplace(caption)
        self.untrack_cursor()


def request_stop(bus):
    """Ask an already-running instance to stop recording."""
    bus.get_object(SERVICE, "/").stop(dbus_interface=IFACE)
