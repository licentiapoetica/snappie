"""Find inner regions of windows (panels, browser tab content, ...) via AT-SPI.

On Wayland, apps don't know where they are on screen, so AT-SPI "screen"
coordinates are garbage.  "Window" coordinates are reliable though; they are
relative to the app's surface, which is KWin's bufferGeometry (CSD apps like
Chromium, shadows included) or clientGeometry (server-side decorated apps).
"""

import socket
import time

import dbus

Atspi = None      # loaded by bus_usable(): libatspi aborts the whole process if the bus is gone
WIN = None
MAX_DEPTH = 40


def bus_usable(bus):
    """Can we reach the accessibility bus? Checked before loading libatspi, which calls
    abort() (uncatchable) when the bus socket is dead."""
    global Atspi, WIN
    if Atspi is not None:
        return True
    try:
        addr = str(bus.get_object("org.a11y.Bus", "/org/a11y/bus").GetAddress(dbus_interface="org.a11y.Bus"))
        path = abstract = None
        for part in addr.split(";")[0].split(":", 1)[1].split(","):
            k, _, v = part.partition("=")
            if k == "path":
                path = v
            elif k == "abstract":
                abstract = "\0" + v
        target = path or abstract
        if not target:
            return False
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sk:
            sk.settimeout(1)
            sk.connect(target)
    except (dbus.DBusException, OSError, IndexError, ValueError):
        return False
    import gi
    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi as _Atspi
    Atspi, WIN = _Atspi, _Atspi.CoordType.WINDOW
    return True


def enable_accessibility(bus):
    """Ask toolkits to expose their accessibility trees. Returns previous value."""
    try:
        obj = bus.get_object("org.a11y.Bus", "/org/a11y/bus")
        props = dbus.Interface(obj, "org.freedesktop.DBus.Properties")
        prev = bool(props.Get("org.a11y.Status", "IsEnabled"))
        if not prev:
            props.Set("org.a11y.Status", "IsEnabled", dbus.Boolean(True))
        return prev
    except dbus.DBusException:
        return True


def restore_accessibility(bus, prev):
    if prev:
        return
    try:
        obj = bus.get_object("org.a11y.Bus", "/org/a11y/bus")
        dbus.Interface(obj, "org.freedesktop.DBus.Properties").Set(
            "org.a11y.Status", "IsEnabled", dbus.Boolean(False))
    except dbus.DBusException:
        pass


class Element:
    __slots__ = ("role", "name", "rect", "kind")

    def __init__(self, role, name, rect):
        self.role, self.name, self.rect = role, name, rect  # rect: global logical (x, y, w, h)
        self.kind = "element"


class Inspector:
    def __init__(self, own_pid, bus=None):
        self.own_pid = own_pid
        self._apps = None          # pid -> [Atspi app]
        self._apps_at = 0.0
        self._frames = {}          # window id -> (frame accessible, (ox, oy)) or None
        self.usable = bus_usable(bus or dbus.SessionBus())
        if self.usable:
            Atspi.set_timeout(400, 2000)
        else:
            print("snappie: accessibility bus unreachable, finding inner areas from pixels only")

    def _app_index(self, want_pid=None):
        stale = want_pid is not None and want_pid not in (self._apps or {}) \
            and time.monotonic() - self._apps_at > 1.5  # apps register a moment after a11y is enabled
        if self._apps is None or stale:
            self._apps_at = time.monotonic()
            self._apps = {}
            desktop = Atspi.get_desktop(0)
            for i in range(desktop.get_child_count()):
                try:
                    app = desktop.get_child_at_index(i)
                    if app is None:
                        continue
                    pid = app.get_process_id()
                    if pid != self.own_pid:
                        self._apps.setdefault(pid, []).append(app)
                except Exception:
                    continue
        return self._apps

    def _frame_for(self, win):
        """Locate the AT-SPI frame of a KWin window and its surface origin."""
        if win["id"] in self._frames:
            return self._frames[win["id"]]
        found = None
        try:
            bx, by, bw, bh = win["buffer"]
            cx, cy, cw, ch = win["client"]
            candidates = []
            for app in self._app_index(win["pid"]).get(win["pid"], []):
                for i in range(app.get_child_count()):
                    fr = app.get_child_at_index(i)
                    if fr is None:
                        continue
                    e = fr.get_extents(WIN)
                    if e.width <= 0 or e.height <= 0:
                        continue
                    candidates.append((fr, fr.get_name() or "", e.width, e.height))
            best = None
            for fr, name, w, h in candidates:
                score = (name == win["caption"]) * 2 + ((w, h) in ((bw, bh), (cw, ch)))
                if best is None or score > best[0]:
                    best = (score, fr, w, h)
            if best:
                _, fr, w, h = best
                origin = (cx, cy) if (w, h) == (cw, ch) and (w, h) != (bw, bh) else (bx, by)
                found = (fr, origin)
        except Exception:
            found = None
        if found or time.monotonic() - self._apps_at < 1.5:
            self._frames[win["id"]] = found
        return found

    def has_tree(self, win):
        return self.usable and self._frame_for(win) is not None

    def chain_at(self, win, gx, gy):
        """Elements under a global point, outermost first."""
        if not self.usable:
            return []
        hit = self._frame_for(win)
        if not hit:
            return []
        node, (ox, oy) = hit
        wx, wy = gx - ox, gy - oy
        frame = node
        try:
            # descend to the deepest hit (some toolkits skip levels in one call) ...
            for _ in range(MAX_DEPTH):
                child = node.get_accessible_at_point(wx, wy, WIN)
                if child is None or child == node:
                    break
                node = child
            # ... then walk back up so every container in between becomes a level
            path = []
            while node is not None and node != frame and len(path) < MAX_DEPTH:
                path.append(node)
                node = node.get_parent()
        except Exception:
            return []
        out = []
        for node in reversed(path):
            try:
                e = node.get_extents(WIN)
                if e.width <= 0 or e.height <= 0:
                    continue
                if not (e.x <= wx < e.x + e.width and e.y <= wy < e.y + e.height):
                    continue
                out.append(Element(node.get_role_name() or "element", node.get_name() or "",
                                   (e.x + ox, e.y + oy, e.width, e.height)))
            except Exception:
                continue
        return out
