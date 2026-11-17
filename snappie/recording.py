"""One recording: KWin stream → Recorder segments → one file.

Pausing finishes the current segment and keeps the KWin stream open; resuming
starts a new segment on it.  A dropped stream reconnects into a new segment
too.  At the end the segments are joined losslessly, then remuxed to MP4 /
WebM or converted to GIF / WebP.
"""

import os
import time

from gi.repository import GLib

from . import cast, settings
from .app_util import log


class Recording:
    def __init__(self, app, sel, opts, on_end):
        """sel: picker.Selection. opts: dict (codec, quality, qp, fps, audio, apps, mic, container, anim,
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
        self.app_audio = None    # appaudio.AppAudio: the apps and mic the recording hears
        self._audio_device = None
        self._audio_off = False  # couldn't pick apps: no desktop audio rather than all of it
        self._mic_fallback = False
        self.chooser = None

    # -- start --

    def start(self):
        from . import appaudio
        if self.opts.get("ask_audio") and not self.opts.get("anim") and appaudio.available():
            self._ask_audio()
        else:
            self._begin()

    def _begin(self, ticks=None, mic=None):
        # give KWin a moment to remove the picker overlays (and the sound chooser) before the first
        # frame; the start sound plays before the stream opens, so the desktop audio doesn't record it
        from . import sound

        def go():
            sound.play("record_start", then=lambda: None if self._ended else self._open_stream())
            return False
        self._start_app_audio(ticks, mic)
        GLib.timeout_add(max(300, int(self.opts.get("delay", 0) * 1000)), go)

    def _audio_rule(self):
        """(mode, names) for the desktop audio before any ticks; no desktop audio = no app (yet)."""
        if not self.opts.get("audio"):
            return "only", []
        return self.opts.get("apps") or ("all", [])

    def _ask_audio(self):
        from . import appaudio
        from .controls import SoundChooser
        mode, names = self._audio_rule()
        try:
            items = appaudio.choices(appaudio.dump(), appaudio.Selection(mode, names), os.getpid())
        except Exception as e:
            log("can't list apps playing sound:", e)
            items = []
        self.chooser = SoundChooser([(n, on) for n, on, _ in items], bool(self.opts.get("mic")))
        keys = {n: k for n, _, k in items}

        def accepted(ticks, mic):
            self._close_chooser()
            if self.opts.get("audio") and ticks:
                new_mode, new_names = appaudio.remembered(mode, names, {n: (on, keys[n]) for n, on in ticks.items()})
                settings.save(**{"rec.audio_apps": new_mode, "rec.audio_app_list": new_names})
            if mic != bool(settings.get("rec.mic")):
                settings.save(**{"rec.mic": mic})
            if self.sel.win:  # typing should go back to the recorded window
                self.kwin.activate(self.sel.win["id"])
            self._begin(ticks, mic)

        def cancelled():
            self._close_chooser()
            log("recording cancelled")
            self._end(None, None)
        self.chooser.accepted.connect(accepted)
        self.chooser.cancelled.connect(cancelled)
        self.chooser.show()
        r = self.sel.rect
        rect, target = (r.x(), r.y(), r.width(), r.height()), None
        if self.sel.win:
            fx, fy = self.sel.win["frame"][:2]
            rect, target = (r.x() - fx, r.y() - fy, r.width(), r.height()), self.sel.win["id"]
        self.kwin.place(SoundChooser.TITLE, rect, target, mode="below", activate=True)
        self._show_border(rect, target, ready=True)  # keep the picked area outlined while choosing

    def _close_chooser(self):
        if self.chooser:
            self.kwin.unplace(self.chooser.TITLE)
            self.chooser.hide()
            self.chooser.deleteLater()
            self.chooser = None

    def _start_app_audio(self, ticks=None, mic=None):
        if self.opts.get("anim"):
            return
        from .appaudio import AppAudio, available
        mode, names = self._audio_rule()
        mic = bool(self.opts.get("mic")) if mic is None else mic
        if not available():
            # the plain way: the default output's monitor (+ the mic), all or nothing
            self._mic_fallback = mic
            if self.opts.get("audio") and mode != "all":
                self._no_app_audio("needs the PipeWire tools (see `snappie deps`)")
            return
        audio = AppAudio(mode, names, mic, ticks)
        try:
            audio.start()
        except Exception as e:
            self._mic_fallback = mic
            if self.opts.get("audio"):
                self._no_app_audio(str(e))
            return
        self.app_audio, self._audio_device = audio, audio.device

    def _no_app_audio(self, why):
        # recording everything would include what was meant to be left out (voice chat)
        log("can't pick the apps to record:", why)
        self._audio_off = True
        self.app.notifier.notify("Recording without desktop sound",
                                 f"Couldn't pick the apps to record: {why}", icon="dialog-warning")

    def _open_stream(self):
        from PyQt6.QtGui import QGuiApplication
        from .recorder import CODECS, pick_codec
        want = self.opts["codec"]
        webm = self._wanted_ext() == "webm"
        try:
            self.codec = pick_codec(want, webm=webm)
        except RuntimeError as e:
            self._fail(str(e))
            return False
        if self.codec != want:
            log(f"using {self.codec} instead of {want}" + (" (WebM)" if webm else " (no encoder for it)"))
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
            if self.rect != rect:  # grown over the outline: it would be in the first frames
                self._hide_border()
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
                                system_audio=bool(self.app_audio) or (o["audio"] and not o.get("anim")
                                                                      and not self._audio_off),
                                audio_device=self._audio_device,
                                mic=self._mic_fallback and not self.app_audio and not o.get("anim"),
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
        log("stop with: the bar's ■ button · tray icon · Ctrl+C · `snappie stop`")
        if self.opts.get("duration"):
            GLib.timeout_add(int(self.opts["duration"] * 1000), lambda: self.stop() or False)

    def _output_file(self):
        """The file named by --output, or None for a directory / the default folder."""
        out = self.opts.get("output")
        if out and not os.path.isdir(os.path.expanduser(out)) and os.path.splitext(out)[1]:
            return os.path.abspath(os.path.expanduser(out))
        return None

    def _wanted_ext(self):
        """gif | webp | mp4 | mkv | webm (or whatever --output ends in)."""
        f = self._output_file()
        if f:
            return os.path.splitext(f)[1][1:].lower()
        return self.opts.get("anim") or self.opts["container"]

    def _output_path(self):
        from .recorder import final_container
        final = self._output_file()
        ext = self._wanted_ext()
        if ext in ("mp4", "mkv", "webm") and final_container(self.codec, ext) != ext:
            log(f"{self.codec} can't go in {ext.upper()}, saving as {final_container(self.codec, ext).upper()}")
            ext = final_container(self.codec, ext)
            final = final and os.path.splitext(final)[0] + "." + ext
        if final:
            os.makedirs(os.path.dirname(final), exist_ok=True)
        else:
            out = self.opts.get("output")
            d = os.path.abspath(os.path.expanduser(out)) if out else settings.videos_dir()
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
        if self.app_audio:
            a = self.app_audio
            self.controls.set_audio_menu(a.menu, a.set_app, a.mic, a.set_mic)
        self.controls.show()
        rect, target = self._area_for_kwin()
        self.kwin.place(TITLE, rect, target, mode="below")
        self._show_border(rect, target)

    def _show_border(self, rect, target, ready=False):
        from .osd import AreaBorder
        if not self.border:
            self.border = AreaBorder()
        self.border.set_ready(ready)
        self.border.show()
        self.kwin.place(AreaBorder.TITLE, AreaBorder.outer(rect), target, mode="exact", pinned=True)

    def _hide_border(self):
        if self.border:
            self.kwin.unplace(self.border.TITLE)
            self.border.hide()
            self.border.deleteLater()
            self.border = None

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
        self._hide_border()
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
        self.overlay.drawing_changed.connect(self._drawing_changed)
        self.overlay.pen_changed.connect(self._sync_tools)
        if self.controls:
            t, o = self.controls.tools, self.overlay
            t.tool_changed.connect(o.set_tool)
            t.color_changed.connect(o.set_color)
            t.width_step.connect(o.step_width)
            t.undo.connect(o.undo)
            t.clear.connect(o.clear)
            t.emoji_chosen.connect(o.set_emoji)
            t.keep_changed.connect(lambda keep: o.set_fade(0 if keep else (settings.get("rec.draw_fade") or 3.0)))
            self._sync_tools()
        self.overlay.show()
        rect, target = self._area_for_kwin()
        self.kwin.place(self.overlay.TITLE, rect, target, mode="exact", pinned=True)
        return self.overlay

    def set_drawing(self, on):
        if self._ended:
            return
        log(f"drawing {'on' if on else 'off'} ({'blended into the window stream' if self.sel.win else 'on screen, region stream'})")
        self._ensure_overlay().set_drawing(on)
        if self.controls:
            self.controls.set_drawing(on)
        if not on and self.sel.win:  # typing should go back to the recorded window
            self.kwin.activate(self.sel.win["id"])

    def _sync_tools(self):
        o = self.overlay
        if self.controls and o:
            self.controls.tools.set_state(o.tool, o.color, o.width_, o.fade <= 0, o.emoji)

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
            if self.overlay and self.overlay.strokes and not getattr(self, "_logged_skip", False):
                self._logged_skip = True
                log(f"drawing not blended: rec={bool(self.rec)} size={self.rec.size if self.rec else None}")
            return False
        w, h = self.rec.size
        layer = self.overlay.render_layer(w, h)
        if layer is not None and not getattr(self, "_logged_layer", False):
            self._logged_layer = True
            log(f"drawing layer {layer[0].width()}x{layer[0].height()} at {layer[1:]} → video {w}x{h}"
                if isinstance(layer, tuple) else "drawing layer rendered")
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
        from . import convert
        from .recorder import join_parts, remux
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
                out = convert.run(path, self.final_path, convert.Options.defaults(self.final_ext[1:]))
                os.unlink(path)
                path = out
            except RuntimeError as e:
                failed = failed or str(e)
        elif self.final_ext in (".mp4", ".webm"):
            path = remux(path, self.codec, self.final_ext[1:])
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
        self._close_chooser()
        self._hide_border()
        if self.app_audio:
            self.app_audio.stop()
            self.app_audio = None
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
