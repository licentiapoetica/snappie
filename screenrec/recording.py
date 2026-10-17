"""One recording: KWin stream → Recorder segments → one file.

Pausing finishes the current segment and keeps the KWin stream open; resuming
starts a new segment on it.  A dropped stream reconnects into a new segment
too.  At the end the segments are joined losslessly, then remuxed to MP4 or
converted to GIF / WebP.
"""

import os
import time

from gi.repository import GLib

from . import cast, settings
from .app_util import log


class Recording:
    def __init__(self, app, sel, opts, on_end):
        """sel: picker.Selection. opts: dict (codec, quality, qp, fps, audio, mic, mp4, anim,
        duration, output, delay). on_end(path or None, error or None)."""
        self.app, self.kwin, self.sel, self.opts, self.on_end = app, app.kwin, sel, opts, on_end
        self.stream = None
        self.rec = None
        self.node = None
        self.controls = None
        self.border = None
        self.overlay = None
        self.watcher = None
        self.parts = []          # finished segments
        self._crop = None
        self._user_stopped = False
        self._drops = []
        self.started_at = None
        self.paused = False
        self._paused_total = 0.0
        self._pause_started = None
        self._cursor = None      # (x, y) global logical
        self._target_origin = None
        self._push_pending = False
        self._ended = False

    # -- start --

    def start(self):
        # give KWin a moment to remove the picker overlays before the first frame
        GLib.timeout_add(max(300, int(self.opts.get("delay", 0) * 1000)), self._open_stream)

    def _open_stream(self):
        from PyQt6.QtGui import QGuiApplication
        from .recorder import CODECS, pick_codec
        want = self.opts["codec"]
        try:
            self.codec = pick_codec(want)
        except RuntimeError as e:
            self._fail(str(e))
            return False
        if self.codec != want:
            log(f"{want} encoder not available, using {self.codec}")
        pointer = cast.POINTER_EMBEDDED if self.sel.show_cursor else cast.POINTER_HIDDEN
        r = self.sel.rect
        rect = (r.x(), r.y(), r.width(), r.height())
        scr = QGuiApplication.screenAt(r.center()) or QGuiApplication.primaryScreen()
        if self.sel.win:
            # the window's own stream: correct even when covered, follows moves
            w = self.sel.win
            self.ref, self.rect = [w["frame"], w["buffer"]], rect
            self.stream_args = cast.window_args(w["id"], pointer)
        else:
            _, _, aw, ah = CODECS.get(self.codec, (0, 0, 2, 2))
            self.ref = self.rect = _grow_to_alignment(rect, aw, ah, scr.geometry())
            self.stream_args = cast.region_args(*self.rect, scr.devicePixelRatio(), pointer)
        self.scale = scr.devicePixelRatio()
        self._connect()
        return False

    def _connect(self):
        self.stream = cast.CastStream(self.stream_args, self._stream_ready, self._stream_closed,
                                      self._stream_failed)
        return False

    def _stream_ready(self, node):
        self.node = node
        if not self.rec and not self.paused:
            self._start_segment()

    def _start_segment(self):
        from .recorder import Recorder
        first = self.started_at is None
        if first:
            self.path = self._output_path()
            path = self.path
        else:  # resumed, or reconnected after the stream dropped
            path = f"{self.path[:-4]}.part{len(self.parts) + 1}.mkv"
        o = self.opts
        try:
            self.rec = Recorder(self.node, self.ref, self.rect, path, codec=self.codec,
                                quality=o["quality"], qp=o.get("qp"), fps=o.get("fps") or None,
                                scale=self.scale, crop=self._crop,
                                system_audio=o["audio"] and not o.get("anim"),
                                mic=o["mic"] and not o.get("anim"),
                                overlay=bool(self.sel.win), on_done=self._recorded, on_error=self._fail)
            self.rec.start()
        except Exception as e:
            return self._fail(str(e))
        if self.controls:
            self.controls.set_busy(False)
        if not first:
            log("recording continues")
            self._push_overlay()
            return
        self.started_at = time.monotonic()
        self._show_controls()
        self.app.recording_changed(self)
        GLib.timeout_add_seconds(1, self._tick)
        if self.opts.get("clicks") or self.opts.get("keys"):
            self._start_visualizer()
        log(f"recording → {self.path}")
        log("stop with: the bar's ■ button · tray icon · Ctrl+C · `screenrec stop`")
        if self.opts.get("duration"):
            GLib.timeout_add(int(self.opts["duration"] * 1000), lambda: self.stop() or False)

    def _output_path(self):
        out = self.opts.get("output")
        if out and not os.path.isdir(os.path.expanduser(out)) and os.path.splitext(out)[1]:
            final = os.path.abspath(os.path.expanduser(out))
            os.makedirs(os.path.dirname(final), exist_ok=True)
        else:
            d = os.path.abspath(os.path.expanduser(out)) if out else settings.videos_dir()
            ext = self.opts.get("anim") or ("mp4" if self.opts["mp4"] else "mkv")
            final = settings.unique_path(d, settings.get("rec.filename"), ext)
        self.final_ext = os.path.splitext(final)[1].lower()
        self.final_path = final
        base = os.path.splitext(final)[0]
        return base + (".rec.mkv" if self.final_ext == ".mkv" else ".mkv")  # always record MKV

    # -- status --

    def elapsed(self):
        if self.started_at is None:
            return 0
        paused = self._paused_total + (time.monotonic() - self._pause_started if self._pause_started else 0)
        return int(time.monotonic() - self.started_at - paused)

    def size(self):
        return self.rec.size if self.rec else (self._crop and self._crop[2:])

    def _tick(self):
        if self._ended:
            return False
        t, size = self.elapsed(), self.size()
        if self.controls:
            self.controls.set_status(t, size)
        self.app.recording_changed(self)
        return True

    def describe(self):
        t = self.elapsed()
        state = "Paused" if self.paused else "Recording"
        size = self.size()
        res = f" · {size[0]}×{size[1]}" if size else ""
        return f"{state} {t // 60:02d}:{t % 60:02d}{res}"

    # -- recording bar --

    def _show_controls(self):
        from .controls import TITLE, Controls
        self.controls = Controls(can_pause=True, can_draw=True)
        self.controls.stop_clicked.connect(self.stop)
        self.controls.pause_toggled.connect(lambda on: self.pause() if on else self.resume())
        self.controls.draw_toggled.connect(self.set_drawing)
        self.controls.show()
        rect, target = self._area_for_kwin()
        self.kwin.place(TITLE, rect, target, mode="below")
        from .osd import AreaBorder
        self.border = AreaBorder()
        self.border.show()
        self.kwin.place(AreaBorder.TITLE, AreaBorder.outer(rect), target, mode="exact", pinned=True)

    def _area_for_kwin(self):
        """(rect, target id): the recorded area, relative to the window frame for window streams."""
        x, y, w, h = self.rect
        if self.sel.win:
            fx, fy = self.sel.win["frame"][:2]
            return (x - fx, y - fy, w, h), self.sel.win["id"]
        return (x, y, w, h), None

    def _hide_controls(self):
        from .controls import TITLE
        self.kwin.unplace(TITLE)
        if self.border:
            self.kwin.unplace(self.border.TITLE)
            self.border.hide()
            self.border.deleteLater()
            self.border = None
        if self.controls:
            self.controls.hide()
            self.controls.deleteLater()
            self.controls = None

    # -- pause / resume --

    def toggle_pause(self):
        self.resume() if self.paused else self.pause()

    def pause(self):
        if self.paused or not self.rec or self._user_stopped:
            return
        self.paused = True
        self._pause_started = time.monotonic()
        if self.controls:
            self.controls.set_paused(True)
            self.controls.set_busy(True)
        if self.border:
            self.border.set_paused(True)
        log("paused")
        self.rec.stop()  # → _recorded keeps the stream open
        self.app.recording_changed(self)

    def resume(self):
        if not self.paused or self._user_stopped:
            return
        if self.rec:  # still finishing the paused segment: resume right after
            GLib.timeout_add(100, lambda: self.resume() or False)
            return
        self.paused = False
        self._paused_total += time.monotonic() - self._pause_started
        self._pause_started = None
        if self.controls:
            self.controls.set_paused(False)
            self.controls.set_busy(True)
        if self.border:
            self.border.set_paused(False)
        if self.stream and self.node is not None:
            self._start_segment()
        else:
            self._connect()
        self.app.recording_changed(self)

    # -- live drawing + click/key visualizer --

    def _ensure_overlay(self):
        if self.overlay:
            return self.overlay
        from PyQt6.QtCore import QSize
        from .livedraw import LiveOverlay
        self.overlay = LiveOverlay(QSize(self.rect[2], self.rect[3]))
        self.overlay.changed.connect(self._overlay_changed)
        if hasattr(self.overlay, "drawing_changed"):
            self.overlay.drawing_changed.connect(self._drawing_changed)
        self.overlay.show()
        rect, target = self._area_for_kwin()
        self.kwin.place(self.overlay.TITLE, rect, target, mode="exact", pinned=True)
        return self.overlay

    def set_drawing(self, on):
        if self._ended:
            return
        self._ensure_overlay().set_drawing(on)
        if self.controls:
            self.controls.set_drawing(on)
        if not on and self.sel.win:  # typing should go back to the recorded window
            self.kwin.activate(self.sel.win["id"])

    def toggle_drawing(self):
        on = not (self.overlay and getattr(self.overlay, "drawing", False))
        self.set_drawing(on)

    def _drawing_changed(self, on):
        if self.controls:
            self.controls.set_drawing(on)
        if not on and self.sel.win:
            self.kwin.activate(self.sel.win["id"])

    def _start_visualizer(self):
        from .livedraw import InputWatcher
        self._ensure_overlay()
        try:
            self.watcher = InputWatcher(clicks=bool(self.opts.get("clicks")), keys=bool(self.opts.get("keys")))
        except Exception as e:  # no access to input devices
            log("click/key visualizer unavailable:", e)
            return
        self.watcher.clicked.connect(self._clicked)
        self.watcher.keys.connect(lambda text: self.overlay and self.overlay.keys(text))
        self.watcher.start()
        if self.opts.get("clicks"):
            self.kwin.on_cursor = self._on_cursor
            self.kwin.track_cursor(self.sel.win["id"] if self.sel.win else None)

    def _on_cursor(self, x, y, fx, fy):
        self._cursor = (x, y)
        self._target_origin = (fx, fy)

    def _area_origin(self):
        x, y = self.rect[:2]
        if self.sel.win and self._target_origin:  # the window may have moved since we started
            fx0, fy0 = self.sel.win["frame"][:2]
            x += self._target_origin[0] - fx0
            y += self._target_origin[1] - fy0
        return x, y

    def _clicked(self, button):
        if not self.overlay or not self._cursor or self.paused:
            return
        ax, ay = self._area_origin()
        x, y = self._cursor[0] - ax, self._cursor[1] - ay
        if 0 <= x < self.rect[2] and 0 <= y < self.rect[3]:
            self.overlay.click(x, y, button)

    def _overlay_changed(self):
        # window streams don't contain our overlay window: blend it into the video (≤ 30×/s)
        if not self.sel.win or self._push_pending:
            return
        self._push_pending = True
        GLib.timeout_add(33, self._push_overlay)

    def _push_overlay(self):
        self._push_pending = False
        if not (self.rec and self.overlay and self.rec.size):
            return False
        w, h = self.rec.size
        layer = self.overlay.render_layer(w, h)
        if isinstance(layer, tuple):
            self.rec.set_overlay(*layer)
        else:
            self.rec.set_overlay(layer)
        return False

    def _stop_live(self):
        if self.watcher:
            self.watcher.stop()
            self.watcher = None
        self.kwin.untrack_cursor()
        self.kwin.on_cursor = None
        if self.overlay:
            self.kwin.unplace(self.overlay.TITLE)
            self.overlay.hide()
            self.overlay.deleteLater()
            self.overlay = None

    # -- stream events --

    def _stream_closed(self):
        self.stream = None
        if self.rec or self.paused:
            log("window closed, stopping")
            self.stop()

    def _stream_failed(self, msg):
        if self.parts:  # could not reconnect: keep what was recorded
            log("error: reconnecting failed:", msg)
            self._save()
        else:
            self._fail(msg)

    # -- stop / save --

    def stop(self):
        if self._ended:
            return
        self._user_stopped = True
        if self.controls and self.controls.stop.isEnabled():
            self.controls._stop()  # shows "…", then comes back here via stop_clicked
            return
        self._stop_live()
        if self.rec:
            log("stopping…")
            self.rec.stop()
        elif self.paused or self.parts:
            self._save()
        else:
            self._end(None, None)

    def _recorded(self, path):
        rec, self.rec = self.rec, None
        self._crop = rec.crop_rect if rec else self._crop
        if os.path.exists(path) and os.path.getsize(path) > 0:
            self.parts.append(path)
        if self.paused and not self._user_stopped and not (rec and rec.source_lost):
            if self.controls:
                self.controls.set_busy(False)
            return  # keep the stream for resume
        if self.stream:
            self.stream.close()
            self.stream = None
        if rec and rec.source_lost and not self._user_stopped:
            now = time.monotonic()
            self._drops = [t for t in self._drops if now - t < 60] + [now]
            if len(self._drops) <= 5:
                log("screen stream dropped — reconnecting…")
                if not self.paused:
                    GLib.timeout_add(200, self._connect)
                return
            log("screen stream keeps dropping, giving up")
        self._save()

    def _save(self, failed=None):
        """Join segments, remux / convert, report. failed: error text (still keeps the recording)."""
        from .recorder import join_parts, remux_mp4, to_animation
        self._hide_controls()
        self._stop_live()
        if self.stream:
            self.stream.close()
            self.stream = None
        if not self.parts:
            return self._end(None, failed or "Nothing was recorded")
        path = join_parts(self.parts, self.path)
        if self.final_ext in (".gif", ".webp"):
            self.app.busy_note("Converting to " + self.final_ext[1:].upper() + "…")
            try:
                out = to_animation(path, self.final_path, settings.get("gif.fps"),
                                   settings.get("gif.max_width"))
                os.unlink(path)
                path = out
            except RuntimeError as e:
                failed = failed or str(e)
        elif self.final_ext == ".mp4":
            path = remux_mp4(path, self.codec)
        else:
            os.replace(path, self.final_path)
            path = self.final_path
        self._end(path, failed)

    def _fail(self, msg, salvage=True):
        if self.rec:
            r, self.rec = self.rec, None
            r._teardown()
            if os.path.exists(r.path) and os.path.getsize(r.path) > 0:
                self.parts.append(r.path)
        if salvage and self.parts:
            return self._save(failed=msg.splitlines()[0])
        self._hide_controls()
        self._stop_live()
        if self.stream:
            self.stream.close()
            self.stream = None
        self._end(None, msg)

    def _end(self, path, error):
        if self._ended:
            return
        self._ended = True
        self.on_end(path, error)


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
