"""Find inner regions of windows (panels, browser tab content, ...) via AT-SPI.

On Wayland, apps don't know where they are on screen, so AT-SPI "screen"
coordinates are garbage.  "Window" coordinates are reliable though; they are
relative to the app's surface, which is KWin's bufferGeometry (CSD apps like
Chromium, shadows included) or clientGeometry (server-side decorated apps).

AT-SPI doesn't say which KWin window a frame belongs to either: one process
often has several windows (Konsole, Dolphin, Steam), so frames are matched by
title and size, one to one, and a window whose match is ambiguous gets no tree
(the picker then finds areas from pixels) rather than another window's.
Matches are redone for every pick: only which geometry is the surface is kept,
the position always comes from the current window list.
"""

import socket
import time

import dbus

Atspi = None      # loaded by bus_usable(): libatspi aborts the whole process if the bus is gone
WIN = None
MAX_DEPTH = 40
SIZE_SLACK = 2    # px per side: KWin geometry is rounded, toolkits round their own way


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


def surface_of(win, w, h):
    """Which of the window's geometries ("client" or "buffer") an AT-SPI frame of size w × h
    is relative to, or None when it fits neither."""
    best = None
    for key in ("buffer", "client"):  # same size: same surface on Wayland, buffer wins
        off = abs(win[key][2] - w) + abs(win[key][3] - h)
        if off <= 2 * SIZE_SLACK and (best is None or off < best[0]):
            best = (off, key)
    return best and best[1]


def match_frames(windows, frames):
    """Pair KWin windows with AT-SPI frames of the same process. frames: [(name, w, h)].
    Returns {window id: (frame index, surface key)}, one to one. A pair is taken only when
    each is the other's single best candidate (title + size); ties stay unmatched."""
    scores = {}
    for w in windows:
        for i, (name, fw, fh) in enumerate(frames):
            surface = surface_of(w, fw, fh)
            if surface is None:
                continue  # wrong size: another window, or one we can't place
            exact = (fw, fh) == tuple(w[surface][2:])
            scores[w["id"], i] = (2 * (bool(name) and name == w["caption"]) + exact, surface)
    out, used = {}, set()
    while True:
        free = {k: v for k, v in scores.items() if k[0] not in out and k[1] not in used}
        pairs = [k for k in free if _sole_best(free, k, 0) and _sole_best(free, k, 1)]
        if not pairs:
            return out
        for wid, i in pairs:  # a pair taken now frees the rivals of the others next round
            out[wid] = (i, free[wid, i][1])
            used.add(i)


def _sole_best(scores, key, side):
    """Is `key` the only top pair among those with the same window (side 0) or frame (side 1)?"""
    rivals = [s for k, (s, _) in scores.items() if k[side] == key[side]]
    return scores[key][0] == max(rivals) and rivals.count(max(rivals)) == 1


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
        self._windows = []         # the current pick's window list
        self._frames = {}          # window id -> (frame accessible, surface key) or None
        self._matched = set()      # pids matched for this pick
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

    def begin(self, windows):
        """Start a pick: windows moved, opened or closed since the last one, so match again."""
        self._windows = windows
        self._frames.clear()
        self._matched.clear()

    def _match(self, pid):
        """Match all windows of one process with its frames (see match_frames)."""
        frames = []
        try:
            for app in self._app_index(pid).get(pid, []):
                for i in range(app.get_child_count()):
                    fr = app.get_child_at_index(i)
                    if fr is None:
                        continue
                    e = fr.get_extents(WIN)
                    if e.width > 0 and e.height > 0:
                        frames.append((fr, fr.get_name() or "", e.width, e.height))
        except Exception:
            frames = []
        if not frames and time.monotonic() - self._apps_at >= 1.5:
            return  # the app may not be registered yet: look again next time
        self._matched.add(pid)
        wins = [w for w in self._windows if w["pid"] == pid]  # minimized ones claim their frame too
        pairs = match_frames(wins, [f[1:] for f in frames])
        for w in wins:
            i, surface = pairs.get(w["id"], (None, None))
            self._frames[w["id"]] = (frames[i][0], surface) if i is not None else None

    def _frame_for(self, win):
        """The AT-SPI frame of a KWin window and its surface origin, or None."""
        if win["pid"] not in self._matched:
            if not any(w["id"] == win["id"] for w in self._windows):
                self._windows = self._windows + [win]  # begin() wasn't given this one
            self._match(win["pid"])
        hit = self._frames.get(win["id"])
        if hit is None:
            return None
        frame, surface = hit
        return frame, tuple(win[surface][:2])

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
