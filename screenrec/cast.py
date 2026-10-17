"""KWin screen-cast streams (no portal dialog) via the screenrec-cast helper."""

import os
import subprocess

import numpy as np
import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

Gst.init(None)

HERE = os.path.dirname(os.path.abspath(__file__))
HELPER_SRC = os.path.join(os.path.dirname(HERE), "helper")
HELPER = os.path.join(os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")),
                      "screenrec", "screenrec-cast")

POINTER_HIDDEN, POINTER_EMBEDDED = 1, 2


def ensure_helper():
    """Build + authorize the helper on first run (or when its source changed)."""
    src = os.path.join(HELPER_SRC, "screenrec-cast.c")
    if os.path.exists(HELPER) and os.path.getmtime(HELPER) >= os.path.getmtime(src):
        return
    r = subprocess.run([os.path.join(HELPER_SRC, "build.sh")], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("building the capture helper failed (needs gcc, wayland-devel):\n" + r.stderr)


def window_args(window_id, pointer):
    return ["window", window_id, str(pointer)]


def region_args(x, y, w, h, scale, pointer):
    return ["region", str(x), str(y), str(w), str(h), f"{scale:g}", str(pointer)]


class CastStream:
    """Keeps a KWin stream alive while the helper runs. Callbacks run on the GLib loop."""

    def __init__(self, args, on_node, on_closed, on_error):
        self.on_node, self.on_closed, self.on_error = on_node, on_closed, on_error
        self.proc = subprocess.Popen([HELPER, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     text=True, bufsize=1)
        self._watch = GLib.io_add_watch(self.proc.stdout, GLib.PRIORITY_DEFAULT,
                                        GLib.IO_IN | GLib.IO_HUP, self._readable)

    def _readable(self, f, cond):
        line = f.readline()
        if not line:
            self._watch = None
            if self.proc:
                self.on_closed()
            return False
        kind, _, rest = line.strip().partition(" ")
        if kind == "node":
            self.on_node(int(rest))
        elif kind == "closed":
            self.on_closed()
        elif kind == "error":
            self.on_error(rest)
        return True

    def close(self):
        proc, self.proc = self.proc, None
        if self._watch:
            GLib.source_remove(self._watch)
            self._watch = None
        if proc:
            try:
                proc.stdin.close()
                proc.wait(2)
            except (OSError, subprocess.TimeoutExpired):
                proc.kill()


def grab_frame(args, timeout=3.0, alpha=False):
    """Blocking: one frame from a fresh stream, (H, W, 3) RGB uint8, or (H, W, 4) RGBA with
    alpha=True (window streams carry real transparency). Safe to call from a thread."""
    proc = subprocess.Popen([HELPER, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    pipeline = None
    fmt = "RGBA" if alpha else "RGBx"
    try:
        kind, _, rest = proc.stdout.readline().strip().partition(" ")
        if kind != "node":
            raise RuntimeError(rest or "capture helper failed")
        pipeline = Gst.parse_launch(
            f"pipewiresrc path={rest} always-copy=true ! capsfilter caps=video/x-raw ! videoconvert"
            f" ! video/x-raw,format={fmt} ! appsink name=sink max-buffers=1 drop=true")
        pipeline.set_state(Gst.State.PLAYING)
        sample = pipeline.get_by_name("sink").emit("try-pull-sample", int(timeout * Gst.SECOND))
        if sample is None:
            raise RuntimeError("no frame received")
        s = sample.get_caps().get_structure(0)
        w, h = s.get_value("width"), s.get_value("height")
        buf = sample.get_buffer()
        ok, m = buf.map(Gst.MapFlags.READ)
        try:
            frame = np.ndarray((h, w, 4), np.uint8, m.data)
            frame = frame.copy() if alpha else frame[:, :, :3].copy()
        finally:
            buf.unmap(m)
        return frame
    finally:
        if pipeline:
            pipeline.set_state(Gst.State.NULL)
        try:
            proc.stdin.close()
            proc.wait(2)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()


def to_qimage(frame):
    """numpy RGB / RGBA frame -> QImage (owns its pixels)."""
    from PyQt6.QtGui import QImage
    h, w, c = frame.shape
    frame = np.ascontiguousarray(frame)
    fmt = QImage.Format.Format_RGBA8888 if c == 4 else QImage.Format.Format_RGB888
    return QImage(frame.data, w, h, w * c, fmt).copy()


def grab_window(window_id, pointer=POINTER_HIDDEN):
    """A window's own pixels as an RGBA QImage (frame geometry, correct even when covered)."""
    return to_qimage(grab_frame(window_args(window_id, pointer), alpha=True))


def grab_screens(screens, pointer=POINTER_HIDDEN):
    """Grab several QScreens at once. Returns {screen name: QImage in device pixels} with
    devicePixelRatio set; screens that failed are missing. Call from the GUI thread."""
    import threading
    out, threads = {}, []

    def one(name, g, dpr):
        try:
            out[name] = grab_frame(region_args(g.x(), g.y(), g.width(), g.height(), dpr, pointer))
        except Exception as e:
            print("screenrec: screen capture failed:", name, e)

    for s in screens:
        t = threading.Thread(target=one, args=(s.name(), s.geometry(), s.devicePixelRatio()))
        t.start()
        threads.append((t, s))
    images = {}
    for t, s in threads:
        t.join()
        if s.name() in out:
            img = to_qimage(out[s.name()])
            img.setDevicePixelRatio(img.width() / s.geometry().width())
            images[s.name()] = img
    return images
