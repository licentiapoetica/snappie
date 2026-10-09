"""screenrec — pick a window (or a part of it) and record it, KDE Plasma / Wayland."""

import argparse
import os
import signal
import subprocess
import sys
import time

import dbus
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

from . import a11y, cast
from .kwin import KWin, AlreadyRunning, request_stop

try:
    from gi.repository import GLibUnix
    _signal_add = GLibUnix.signal_add
except ImportError:
    _signal_add = GLib.unix_signal_add


def videos_dir():
    try:
        d = subprocess.run(["xdg-user-dir", "VIDEOS"], capture_output=True, text=True).stdout.strip()
    except OSError:
        d = ""
    if not d or d == os.path.expanduser("~"):
        d = os.path.expanduser("~/Videos")
    return os.path.join(d, "screenrec")


def parse_args():
    ap = argparse.ArgumentParser(prog="screenrec", description=__doc__)
    ap.add_argument("-o", "--output", help="output file (.mp4/.mkv) or directory")
    ap.add_argument("-c", "--codec", choices=["hevc", "av1", "h264"], default="hevc",
                    help="video codec, hardware encoded via VAAPI (default: hevc)")
    ap.add_argument("-q", "--quality", choices=["high", "good", "small"], default="good",
                    help="quality preset (default: good)")
    ap.add_argument("--qp", type=int, help="constant quantizer instead of capped VBR (overrides --quality)")
    ap.add_argument("--fps", type=int, help="force a constant frame rate (default: native, up to the "
                                            "monitor refresh rate, variable when the screen is idle)")
    ap.add_argument("--no-audio", action="store_true", help="don't record desktop audio")
    ap.add_argument("--mic", action="store_true", help="also record (mix in) the default microphone")
    ap.add_argument("--no-cursor", action="store_true",
                    help="don't record the mouse cursor (otherwise: checkbox / C key in the picker, remembered)")
    ap.add_argument("--mkv", action="store_true", help="keep MKV instead of remuxing to MP4")
    ap.add_argument("--delay", type=float, default=0, help="seconds to wait before recording")
    ap.add_argument("--duration", type=float, help="stop automatically after N seconds")
    ap.add_argument("--stop", action="store_true", help="stop a running recording (bind this to a shortcut)")
    return ap.parse_args()


def notify(bus, title, body, path=None, icon="media-record"):
    try:
        hints = {}
        if path:
            hints["x-kde-urls"] = dbus.Array(["file://" + path], signature="s")
        bus.get_object("org.freedesktop.Notifications", "/org/freedesktop/Notifications").Notify(
            "screenrec", dbus.UInt32(0), icon, title, body, dbus.Array([], signature="s"),
            dbus.Dictionary(hints, signature="sv"), 6000, dbus_interface="org.freedesktop.Notifications")
    except dbus.DBusException:
        pass


SETTINGS = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
                        "screenrec", "settings.json")


def load_settings():
    import json
    try:
        with open(SETTINGS) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(**changes):
    import json
    data = dict(load_settings(), **changes)
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    with open(SETTINGS, "w") as f:
        json.dump(data, f)


def log(*a):
    print("screenrec:", *a, file=sys.stderr, flush=True)


class App:
    def __init__(self, args, bus, kwin):
        from PyQt6.QtWidgets import QApplication
        self.args, self.bus, self.kwin = args, bus, kwin
        self.qapp = QApplication.instance()
        self.stream = None
        self.rec = None
        self.tray = None
        self.controls = None
        self.sel = None
        self.started_at = None
        self.exit_code = 0
        kwin.on_stop = self.stop

    # -- selection --

    def run_picker(self):
        from .picker import Picker
        windows = self.kwin.list_windows()
        show_cursor = False if self.args.no_cursor else load_settings().get("cursor", True)
        self.picker = Picker(windows, a11y.Inspector(os.getpid()), os.getpid(), show_cursor=show_cursor)
        self.picker.finished.connect(self._picked)
        self.picker.show()

    def _picked(self, sel):
        if sel is None:
            log("cancelled")
            return self.quit()
        self.sel = sel
        if not self.args.no_cursor:
            save_settings(cursor=sel.show_cursor)  # remember the checkbox for next time
        r = sel.rect
        log(f"selected {sel.label!r} at {r.x()},{r.y()} {r.width()}x{r.height()}")
        # give KWin a moment to remove the overlays before the first frame
        GLib.timeout_add(max(300, int(self.args.delay * 1000)), self._open_stream)

    # -- stream + recording --

    def _open_stream(self):
        from .recorder import CODECS, pick_codec
        self.codec = pick_codec(self.args.codec)
        if self.codec != self.args.codec:
            log(f"{self.args.codec} encoder not available, using {self.codec}")
        pointer = cast.POINTER_EMBEDDED if self.sel.show_cursor else cast.POINTER_HIDDEN
        r = self.sel.rect
        rect = (r.x(), r.y(), r.width(), r.height())
        if self.sel.win:
            # the window's own stream: correct even when covered, follows moves
            w = self.sel.win
            self.ref, self.rect = [w["frame"], w["buffer"]], rect
            args = cast.window_args(w["id"], pointer)
            from PyQt6.QtGui import QGuiApplication
            scr = QGuiApplication.screenAt(r.center()) or QGuiApplication.primaryScreen()
        else:
            from PyQt6.QtGui import QGuiApplication
            scr = QGuiApplication.screenAt(r.center()) or QGuiApplication.primaryScreen()
            _, _, aw, ah = CODECS.get(self.codec, (0, 0, 2, 2))
            self.ref = self.rect = _grow_to_alignment(rect, aw, ah, scr.geometry())
            args = cast.region_args(*self.rect, scr.devicePixelRatio(), pointer)
        self.scale = scr.devicePixelRatio()
        self.stream = cast.CastStream(args, self._stream_ready, self._stream_closed, self._fail)
        return False

    def _stream_ready(self, node):
        from .recorder import Recorder
        if self.rec:
            return
        self.path = self._output_path()
        try:
            self.rec = Recorder(node, self.ref, self.rect, self.path, codec=self.codec,
                                quality=self.args.quality, qp=self.args.qp, fps=self.args.fps,
                                scale=self.scale,
                                system_audio=not self.args.no_audio, mic=self.args.mic,
                                on_done=self._recorded, on_error=self._fail)
            self.rec.start()
        except Exception as e:
            return self._fail(str(e))
        self.started_at = time.monotonic()
        self._show_controls()
        self._show_tray()
        GLib.timeout_add_seconds(1, self._tick)
        log(f"recording → {self.path}")
        log("stop with: tray icon click · Ctrl+C · `screenrec --stop`")
        if self.args.duration:
            GLib.timeout_add(int(self.args.duration * 1000), lambda: self.stop() or False)

    def _stream_closed(self):
        if self.rec:
            log("window closed, stopping")
            self.stop()

    def _output_path(self):
        stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        out = self.args.output
        if out and not os.path.isdir(out) and os.path.splitext(out)[1]:
            final = os.path.abspath(os.path.expanduser(out))
        else:
            d = os.path.abspath(os.path.expanduser(out)) if out else videos_dir()
            final = os.path.join(d, f"screenrec_{stamp}.mp4")
        os.makedirs(os.path.dirname(final), exist_ok=True)
        self.final_ext = ".mkv" if self.args.mkv else os.path.splitext(final)[1].lower()
        return os.path.splitext(final)[0] + ".mkv"  # always record MKV: survives crashes

    def _show_controls(self):
        from .controls import TITLE, Controls
        self.controls = Controls()
        self.controls.stop_clicked.connect(self.stop)
        self.controls.show()
        x, y, w, h = self.rect
        if self.sel.win:
            fx, fy = self.sel.win["frame"][:2]
            self.kwin.place_controls(TITLE, self.sel.win["id"], (x - fx, y - fy, w, h))
        else:
            self.kwin.place_controls(TITLE, None, (x, y, w, h))

    def _hide_controls(self):
        self.kwin.unplace_controls()
        if self.controls:
            self.controls.hide()
            self.controls.deleteLater()
            self.controls = None

    def _show_tray(self):
        from PyQt6.QtGui import QAction, QIcon
        from PyQt6.QtWidgets import QMenu, QSystemTrayIcon
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(QIcon.fromTheme("media-record", QIcon.fromTheme("media-playback-stop")))
        menu = QMenu()
        act = QAction(QIcon.fromTheme("media-playback-stop"), "Stop recording", menu)
        act.triggered.connect(self.stop)
        menu.addAction(act)
        self.tray.setContextMenu(menu)
        self._tray_menu = menu
        self.tray.activated.connect(lambda reason: self.stop()
                                    if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray.show()
        self._tick()

    def _tick(self):
        if not self.rec:
            return False
        t = int(time.monotonic() - self.started_at)
        size = self.rec.size
        if self.controls:
            self.controls.set_status(t, size)
        if self.tray:
            res = f" · {size[0]}×{size[1]}" if size else ""
            self.tray.setToolTip(f"Recording {t // 60:02d}:{t % 60:02d}{res} — click to stop")
        return True

    def stop(self):
        if self.rec:
            if self.controls and self.controls.stop.isEnabled():
                self.controls._stop()  # shows "Saving…", then comes back here via stop_clicked
                return
            log("stopping…")
            self.rec.stop()
        else:
            self.quit()

    def _recorded(self, path):
        from .recorder import remux_mp4
        self.rec = None
        self._hide_controls()
        if self.tray:
            self.tray.hide()
        if self.stream:
            self.stream.close()
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            return self._fail("Nothing was recorded")
        if self.final_ext == ".mp4":
            path = remux_mp4(path, self.codec)
        mb = os.path.getsize(path) / 1e6
        log(f"saved {path} ({mb:.1f} MB)")
        notify(self.bus, "Recording saved", f"{os.path.basename(path)} · {mb:.1f} MB", path=path)
        print(path)
        self.quit()

    def _fail(self, msg):
        log("error:", msg)
        notify(self.bus, "Recording failed", msg, icon="dialog-error")
        self.exit_code = 1
        if self.rec:
            r, self.rec = self.rec, None
            r._teardown()
        self.quit()

    def quit(self):
        self._hide_controls()
        if self.stream:
            self.stream.close()
        self.qapp.exit(self.exit_code)


def _grow_to_alignment(rect, aw, ah, bounds):
    """Grow a region (around its centre) so the encoder needs no padding."""
    x, y, w, h = rect
    nw, nh = -(-w // aw) * aw, -(-h // ah) * ah
    if nw > bounds.width():
        nw -= aw
    if nh > bounds.height():
        nh -= ah
    x = min(max(x - (nw - w) // 2, bounds.x()), bounds.x() + bounds.width() - nw)
    y = min(max(y - (nh - h) // 2, bounds.y()), bounds.y() + bounds.height() - nh)
    return (x, y, nw, nh)


def _excepthook(kind, value, tb):
    # PyQt aborts the process on an exception inside a slot; log it and keep going instead
    import traceback
    traceback.print_exception(kind, value, tb)


def main():
    sys.excepthook = _excepthook
    args = parse_args()
    DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()
    if args.stop:
        try:
            request_stop(bus)
        except dbus.DBusException:
            log("no recording is running")
            return 1
        return 0
    try:
        kwin = KWin(bus)
    except AlreadyRunning:
        log("already running — use `screenrec --stop` to stop it")
        return 1
    try:
        cast.ensure_helper()
    except RuntimeError as e:
        log(e)
        return 1

    prev_a11y = a11y.enable_accessibility(bus)
    os.environ.setdefault("QT_LOGGING_RULES", "qt.accessibility.atspi=false")
    from PyQt6.QtWidgets import QApplication
    qapp = QApplication(sys.argv[:1])
    qapp.setApplicationName("screenrec")
    qapp.setQuitOnLastWindowClosed(False)
    app = App(args, bus, kwin)
    for sig in (signal.SIGINT, signal.SIGTERM):
        _signal_add(GLib.PRIORITY_DEFAULT, sig, lambda: app.stop() or True)
    GLib.idle_add(lambda: app.run_picker() or False)
    try:
        return qapp.exec() or app.exit_code
    finally:
        a11y.restore_accessibility(bus, prev_a11y)


if __name__ == "__main__":
    sys.exit(main())
