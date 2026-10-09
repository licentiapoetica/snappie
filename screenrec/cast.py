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


def grab_frame(args, timeout=3.0):
    """Blocking: one RGB frame (H, W, 3) uint8 from a fresh stream. Safe to call from a thread."""
    proc = subprocess.Popen([HELPER, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    pipeline = None
    try:
        kind, _, rest = proc.stdout.readline().strip().partition(" ")
        if kind != "node":
            raise RuntimeError(rest or "capture helper failed")
        pipeline = Gst.parse_launch(
            f"pipewiresrc path={rest} always-copy=true ! capsfilter caps=video/x-raw ! videoconvert"
            " ! video/x-raw,format=RGBx ! appsink name=sink max-buffers=1 drop=true")
        pipeline.set_state(Gst.State.PLAYING)
        sample = pipeline.get_by_name("sink").emit("try-pull-sample", int(timeout * Gst.SECOND))
        if sample is None:
            raise RuntimeError("no frame received")
        s = sample.get_caps().get_structure(0)
        w, h = s.get_value("width"), s.get_value("height")
        buf = sample.get_buffer()
        ok, m = buf.map(Gst.MapFlags.READ)
        try:
            frame = np.ndarray((h, w, 4), np.uint8, m.data)[:, :, :3].copy()
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
