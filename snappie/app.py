"""The running app: tray icon, commands, and the capture flows.

One process owns the D-Bus name; later `snappie <command>` invocations (hotkeys,
launcher, terminal) forward their command line to it.  Started as `snappie tray`
it stays in the tray; started by any other command it is a one-shot that quits
once nothing is left open (it lingers while it still serves a copied image, since
on Wayland the clipboard data lives in the process that copied it).
"""

import os
import time

from PyQt6.QtCore import QObject, QPoint, QRect, QRectF, QTimer
from PyQt6.QtGui import QAction, QGuiApplication, QIcon, QImage, QPainter
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from . import a11y, cast, cli, settings
from .app_util import Notifier, copy_image, copy_text, log, save_image

ICON = "snappie"
CLIPBOARD_LINGER = 3600   # one-shot: serve a copied image for at most this long


def _icon(*names):
    for n in names:
        ic = QIcon.fromTheme(n)
        if not ic.isNull():
            return ic
    return QIcon()


class App(QObject):
    def __init__(self, bus, kwin, daemon=False):
        super().__init__()
        self.bus, self.kwin, self.daemon = bus, kwin, daemon
        self.qapp = QApplication.instance()
        self.notifier = Notifier(bus)
        self.picker = None
        self.recording = None
        self.windows = set()       # open editors, pins, history, settings, osd (kept alive here)
        self.jobs = 0              # running uploads / OCR / scroll captures
        self.tray = None
        self.exit_code = 0
        self._inspector = None
        self._clip_until = 0
        self._last_bgs = {}
        kwin.on_stop = self.stop_recording
        kwin.on_command = self.command
        QApplication.clipboard().dataChanged.connect(self._clipboard_changed)
        self._idle = QTimer(self, singleShot=True, interval=500, timeout=self._maybe_quit)
        if daemon:
            self.become_daemon()

    # ------------------------------------------------------------------ commands

    def command(self, argv):
        """Run a command line (ours, or forwarded from another `snappie`). Returns reply text."""
        try:
            ns = cli.parse(argv)
        except cli.ArgError as e:
            return f"error: {e}"
        except SystemExit:
            return ""
        QTimer.singleShot(0, lambda: self._run(ns))
        return "ok"

    def _run(self, ns):
        c = ns.cmd
        try:
            if c == "tray":
                if self.daemon:
                    self.show_history()
                else:
                    self.become_daemon()
            elif c == "shot":
                self.screenshot(screen=ns.screen, window=ns.window, last=ns.last, delay=ns.delay,
                                then=ns.then, output=ns.output)
            elif c == "record":
                self.record(ns)
            elif c == "stop":
                self.stop_recording()
            elif c == "pause":
                if self.recording:
                    self.recording.toggle_pause()
            elif c == "draw":
                if self.recording:
                    self.recording.toggle_drawing()
            elif c == "color":
                self.pick(mode="color", on_pick=self._color_picked)
            elif c == "ocr":
                self.pick(mode="ocr", on_pick=self._ocr_picked)
            elif c == "scroll":
                self.pick(mode="scroll", on_pick=self._scroll_picked, frozen=False)
            elif c == "history":
                self.show_history()
            elif c == "settings":
                self.show_settings()
            elif c == "edit":
                self.open_editor(path=os.path.abspath(ns.file))
            elif c == "pin":
                img = QImage(ns.file)
                if img.isNull():
                    raise RuntimeError(f"can't open {ns.file}")
                self.pin(img)
            elif c == "upload":
                self.upload(os.path.abspath(ns.file))
            elif c == "quit":
                self.quit()
                return
        except Exception as e:
            log("error:", e)
            self.notifier.notify("Snappie", str(e), icon="dialog-error")
        self.idle_check()

    # ------------------------------------------------------------------ lifetime

    def become_daemon(self):
        from . import hotkeys
        self.daemon = True
        try:
            hotkeys.install()
        except OSError as e:
            log("could not install the shortcut entries:", e)
        self._show_tray()

    def idle_check(self):
        self._idle.start()

    def _busy(self):
        return bool(self.picker or self.recording or self.windows or self.jobs
                    or self._clip_until > time.monotonic())

    def _maybe_quit(self):
        if self.daemon:
            return
        if self._busy():
            if not (self.picker or self.recording or self.windows or self.jobs):
                self._idle.start(10_000)  # only the clipboard keeps us: check now and then
            return
        self.quit()

    def _clipboard_changed(self):
        cb = QApplication.clipboard()
        try:
            owns = cb.ownsClipboard()
        except Exception:
            owns = False
        if not owns and self._clip_until:
            self._clip_until = 0
            self.idle_check()

    def keep(self, w):
        """Keep a window alive until it closes, then maybe quit."""
        self.windows.add(w)

        def gone(*_):
            self.windows.discard(w)
            self.idle_check()
        for sig in ("closed", "destroyed"):
            if hasattr(w, sig):
                getattr(w, sig).connect(gone)
        return w

    def quit(self):
        if self.recording:
            self.recording.stop()
            QTimer.singleShot(200, self.quit)
            return
        self.kwin.cleanup()
        if self.tray:
            self.tray.hide()
        self.qapp.exit(self.exit_code)

    # ------------------------------------------------------------------ tray

    def _show_tray(self):
        if self.tray or not QSystemTrayIcon.isSystemTrayAvailable():
            self._update_tray()
            return
        from .icons import tray_icon
        self.tray = QSystemTrayIcon(tray_icon())
        self.tray.activated.connect(self._tray_activated)
        self._update_tray()
        self.tray.show()

    def _tray_activated(self, reason):
        R = QSystemTrayIcon.ActivationReason
        if reason == R.Trigger:
            if self.recording:
                self.stop_recording()
            else:
                self._run(cli.parse(["shot"]))
        elif reason == R.MiddleClick:
            if self.recording:
                self.recording.toggle_pause()
            else:
                self._run(cli.parse(["record"]))

    def _update_tray(self):
        if not self.tray and not (self.recording and QSystemTrayIcon.isSystemTrayAvailable()):
            return
        if not self.tray:  # one-shot: tray only while recording
            self.tray = QSystemTrayIcon()
            self.tray.activated.connect(self._tray_activated)
            self.tray.show()
        menu = QMenu()

        def add(label, argv, icon=None, enabled=True):
            a = QAction(_icon(icon) if icon else QIcon(), label, menu)
            a.triggered.connect(lambda: self._run(cli.parse(argv)))
            a.setEnabled(enabled)
            menu.addAction(a)
            return a

        rec = self.recording
        if rec:
            from .icons import tray_icon
            self.tray.setIcon(tray_icon("paused" if rec.paused else "recording"))
            self.tray.setToolTip(f"{rec.describe()} — click to stop")
            add("Stop recording", ["stop"], "media-playback-stop")
            add("Resume" if rec.paused else "Pause", ["pause"],
                "media-playback-start" if rec.paused else "media-playback-pause")
            add("Draw on the recording", ["draw"], "draw-freehand")
            menu.addSeparator()
        else:
            from .icons import tray_icon
            self.tray.setIcon(tray_icon())
            self.tray.setToolTip("Snappie — click: screenshot · middle click: record")
        add("Screenshot…", ["shot"], "camera-photo", not rec)
        add("Screenshot of the screen", ["shot", "--screen"], "view-fullscreen")
        add("Screenshot of the active window", ["shot", "--window"], "window")
        add("Repeat the last area", ["shot", "--last"], "edit-redo",
            bool(settings.get("last.selection")))
        delay = menu.addMenu(_icon("chronometer"), "Screenshot after a delay")
        for sec in (3, 5, 10):
            a = QAction(f"{sec} seconds", delay)
            a.triggered.connect(lambda _=False, s=sec: self._run(cli.parse(["shot", "--delay", str(s)])))
            delay.addAction(a)
        menu.addSeparator()
        add("Record…", ["record"], "media-record", not rec)
        add("Record a GIF…", ["record", "--gif"], "image-gif", not rec)
        add("Record the last area", ["record", "--last"], "media-record",
            not rec and bool(settings.get("last.selection")))
        menu.addSeparator()
        add("Scrolling capture…", ["scroll"], "go-down-skip")
        add("Copy text (OCR)…", ["ocr"], "edit-select-text")
        add("Color picker", ["color"], "color-picker")
        a = QAction(_icon("document-open"), "Open an image in the editor…", menu)
        a.triggered.connect(self._open_image_dialog)
        menu.addAction(a)
        menu.addSeparator()
        add("History", ["history"], "view-history")
        add("Settings", ["settings"], "configure")
        auto = QAction("Start with Plasma", menu, checkable=True, checked=_autostart_enabled())
        auto.toggled.connect(_set_autostart)
        menu.addAction(auto)
        if self.daemon:
            add("Quit", ["quit"], "application-exit")
        self.tray.setContextMenu(menu)
        self._tray_menu = menu  # keep a reference

    def recording_changed(self, rec):
        if self.tray:
            self.tray.setToolTip(f"{rec.describe()} — click to stop")
        if getattr(self, "_tray_state", None) != (bool(rec), rec.paused):
            self._tray_state = (bool(rec), rec.paused)
            self._update_tray()

    def busy_note(self, text):
        log(text)
        if self.tray:
            self.tray.setToolTip(text)

    # ------------------------------------------------------------------ picking

    @property
    def inspector(self):
        if self._inspector is None:
            self._inspector = a11y.Inspector(os.getpid(), self.bus)
        return self._inspector

    def pick(self, mode, on_pick, frozen=True, show_cursor=None):
        """Show the picker. frozen: grab the screens first and show them still (screenshots)."""
        from .picker import Overlay, Picker
        if self.picker:
            return
        self.kwin.skip_taskbar(Overlay.TITLE)  # else Plasma's fullscreen Do Not Disturb kicks in
        windows = self.kwin.list_windows()
        bgs = {}
        magnifier = settings.get("shot.magnifier")
        if frozen or magnifier or mode == "color":
            pointer = cast.POINTER_EMBEDDED if (mode == "shot" and settings.get("shot.cursor")) \
                else cast.POINTER_HIDDEN
            bgs = cast.grab_screens(QGuiApplication.screens(), pointer)
            if not bgs and mode in ("shot", "ocr", "color"):
                raise RuntimeError("Could not capture the screen")
        self._last_bgs = bgs
        if show_cursor is None:
            show_cursor = settings.get("cursor") if mode == "record" else False
        self.picker = Picker(windows, self.inspector, os.getpid(), show_cursor=show_cursor,
                             mode=mode, backgrounds=bgs, magnifier=magnifier,
                             window_style=settings.get("shot.window_mode"))
        if not frozen:
            self.picker.frozen = False
        self._pick_windows = windows
        self.picker.finished.connect(lambda sel: self._picked(sel, on_pick))
        self.picker.show()

    def _picked(self, sel, on_pick):
        pk, self.picker = self.picker, None
        if pk and pk.magnifier != settings.get("shot.magnifier") and pk.mode != "color":
            settings.set("shot.magnifier", pk.magnifier)
        if pk and pk.mode == "shot" and pk.window_style != settings.get("shot.window_mode"):
            settings.set("shot.window_mode", pk.window_style)
        if sel is None:
            log("cancelled")
        else:
            try:
                if sel.front:
                    self._bring_front(sel.front, pk.mode if pk else None)
                on_pick(sel)
            except Exception as e:
                log("error:", e)
                self.notifier.notify("Snappie", str(e), icon="dialog-error")
        self.idle_check()

    def _bring_front(self, front, mode):
        """The picker showed a covered window in front of the others: make the capture match."""
        w, img = front
        if mode in ("record", "scroll"):
            self.kwin.activate(w["id"])  # live: really raise it (scrolling also needs it under the pointer)
            return
        # frozen screens: paint the window's own pixels over them, where the picker showed it
        frame = QRect(*w["frame"])
        bgs = dict(self._last_bgs)
        for scr in QGuiApplication.screens():
            bg, g = bgs.get(scr.name()), scr.geometry()
            if bg is None or not g.intersects(frame):
                continue
            dpr = bg.devicePixelRatio()
            bg = bg.convertToFormat(QImage.Format.Format_RGB32)  # a copy painters handle well
            bg.setDevicePixelRatio(dpr)
            p = QPainter(bg)
            p.drawImage(QRectF(frame.translated(-g.topLeft())), img, QRectF(img.rect()))
            p.end()
            bgs[scr.name()] = bg
        self._last_bgs = bgs

    def remember_selection(self, sel):
        r = sel.rect
        data = {"rect": [r.x(), r.y(), r.width(), r.height()], "kind": sel.kind}
        if sel.win:
            fx, fy = sel.win["frame"][:2]
            data.update(win_id=sel.win["id"], win_cls=sel.win["cls"],
                        rel=[r.x() - fx, r.y() - fy, r.width(), r.height()])
        settings.set("last.selection", data)
        if self.tray:
            self._update_tray()

    def restore_selection(self):
        """The last area again; follows the window if it still exists (also when moved)."""
        from .picker import Selection
        data = settings.get("last.selection")
        if not data:
            raise RuntimeError("No previous area yet — take a screenshot or recording first")
        rect = QRect(*data["rect"])
        win = None
        if data.get("win_id"):
            windows = self.kwin.list_windows()
            win = next((w for w in windows if w["id"] == data["win_id"]), None)
            if win is None:
                win = next((w for w in reversed(windows) if w["cls"] == data.get("win_cls")
                            and w["visible"] and w["frame"][2:] == list(data["rect"][2:])
                            and data.get("kind") == "window"), None)
            if win:
                fx, fy = win["frame"][:2]
                rx, ry, rw, rh = data["rel"]
                rect = QRect(fx + rx, fy + ry, rw, rh)
        sel = Selection(rect, win, "Last area", data.get("kind", "region"))
        return sel

    # ------------------------------------------------------------------ screenshots

    def screenshot(self, screen=False, window=False, last=False, delay=0, then=None, output=None):
        if self.picker:
            return
        opts = {"then": [a.strip() for a in then.split(",") if a.strip()] if then else None,
                "output": output}
        if output and opts["then"] is None:
            opts["then"] = ["save"]
        if delay and delay > 0:
            from .osd import Countdown
            pos = self.kwin.cursor_pos()
            scr = (QGuiApplication.screenAt(QPoint(*pos)) if pos else None) or QGuiApplication.primaryScreen()
            cd = self.keep(Countdown(delay))
            cd.done.connect(lambda: self._shot_now(screen, window, last, opts))
            cd.destroyed.connect(lambda: self.windows.discard(cd))
            cd.start(self.kwin, scr.geometry())
            return
        self._shot_now(screen, window, last, opts)

    def _shot_now(self, screen, window, last, opts):
        from .picker import Selection
        try:
            if not (screen or window or last):
                self.pick(mode="shot", on_pick=lambda sel: self._shot_picked(sel, opts))
                return
            pointer = cast.POINTER_EMBEDDED if settings.get("shot.cursor") else cast.POINTER_HIDDEN
            self._last_bgs = cast.grab_screens(QGuiApplication.screens(), pointer)
            if screen:
                pos = self.kwin.cursor_pos()
                scr = (QGuiApplication.screenAt(QPoint(*pos)) if pos else None) or QGuiApplication.primaryScreen()
                sel = Selection(scr.geometry(), None, f"Screen {scr.name()}", "screen")
            elif window:
                w = next((w for w in self.kwin.list_windows() if w.get("active")), None)
                if not w:
                    raise RuntimeError("No active window")
                sel = Selection(QRect(*w["frame"]), w, w["caption"], "window")
            else:
                sel = self.restore_selection()
            sel.window_style = settings.get("shot.window_mode")
            self._pick_windows = self.kwin.list_windows()
            self._shot_picked(sel, opts, remember=not last)
        except Exception as e:
            log("error:", e)
            self.notifier.notify("Screenshot failed", str(e), icon="dialog-error")
        self.idle_check()

    def _screen_of(self, rect):
        return QGuiApplication.screenAt(rect.center()) or QGuiApplication.primaryScreen()

    def crop_background(self, rect):
        """The frozen pixels of a global logical rect (one screen), device resolution."""
        scr = self._screen_of(rect)
        bg = self._last_bgs.get(scr.name())
        if bg is None:
            raise RuntimeError("The screen could not be captured")
        g, dpr = scr.geometry(), bg.devicePixelRatio()
        r = rect.intersected(g).translated(-g.topLeft())
        src = QRect(round(r.x() * dpr), round(r.y() * dpr), round(r.width() * dpr), round(r.height() * dpr))
        img = bg.copy(src)
        img.setDevicePixelRatio(dpr)
        return img

    def _window_image(self, sel):
        """A whole window with its real transparency, optionally on a soft shadow."""
        img = cast.grab_window(sel.win["id"])
        img.setDevicePixelRatio(img.width() / max(1, sel.win["frame"][2]))
        if sel.window_style == "shadow":
            from .annotate import beautify
            dpr = img.devicePixelRatio()
            img = beautify.apply(img, padding=0, background=None, radius=0, shadow=True)  # grows to fit
            img.setDevicePixelRatio(dpr)
        return img

    def _shot_picked(self, sel, opts, remember=True):
        if remember:
            self.remember_selection(sel)
        then = opts.get("then")
        annotate = settings.get("shot.annotate") and then is None
        if sel.kind == "window" and sel.win and sel.window_style in ("transparent", "shadow"):
            img = self._window_image(sel)
            if annotate:
                self.open_editor(image=img)
            else:
                self.after_capture(img, then or settings.get("shot.after"), origin=sel.rect.topLeft(),
                                   output=opts.get("output"))
            return
        if annotate:
            from .annotate import InPlaceEditor
            scr = self._screen_of(sel.rect)
            bg = self._last_bgs.get(scr.name())
            if bg is None:
                raise RuntimeError("The screen could not be captured")
            frames = [QRect(*w["frame"]) for w in getattr(self, "_pick_windows", [])
                      if w.get("visible") and (w.get("normal") or w.get("dialog"))]
            self.kwin.skip_taskbar(InPlaceEditor.TITLE)  # full screen too
            ed = InPlaceEditor(scr, bg, sel.rect.intersected(scr.geometry()), frames)
            self.keep(ed)
            ed.action.connect(lambda name, doc: self._doc_action(name, doc, ed, inplace=True))
            ed.present()
            return
        img = self.crop_background(sel.rect)
        self.after_capture(img, then or settings.get("shot.after"), origin=sel.rect.topLeft(),
                           output=opts.get("output"))

    def _doc_action(self, name, doc, editor, inplace=False):
        """Buttons of the in-place editor and the editor window."""
        if name in ("cancel", "closed"):
            if inplace:
                self._close_inplace(editor)
            self.windows.discard(editor)
            self.idle_check()
            return
        if name == "edit":
            self._close_inplace(editor)
            self.open_editor(doc=doc)
            return
        img = doc.render()
        origin = getattr(editor, "selection_origin", lambda: None)() if inplace else None
        actions = settings.get("shot.after") if name == "confirm" else [name]
        if name == "save_as":
            if inplace:
                self._close_inplace(editor)
            self._save_as(img, None if inplace else editor)
            return
        # copy while our window still has keyboard focus (Wayland), then close
        path = self.after_capture(img, actions, origin=origin)
        if path and not inplace and hasattr(editor, "mark_saved") and ("save" in actions):
            editor.mark_saved(path)
        if inplace:
            self._close_inplace(editor)

    def _close_inplace(self, ed):
        ed.hide()
        ed.deleteLater()
        self.windows.discard(ed)
        self.idle_check()

    def _save_as(self, img, editor=None):
        from PyQt6.QtWidgets import QFileDialog
        start = settings.screenshot_path()
        path, _ = QFileDialog.getSaveFileName(None, "Save screenshot", start,
                                              "PNG (*.png);;JPEG (*.jpg *.jpeg);;WebP (*.webp)")
        if path:
            if not os.path.splitext(path)[1]:
                path += ".png"
            saved = self.after_capture(img, ["save"], output=path)
            if saved and editor is not None and hasattr(editor, "mark_saved"):
                editor.mark_saved(saved)

    def after_capture(self, img, actions, origin=None, output=None):
        """Run after-capture actions on a finished image: copy, save, upload, pin, ocr, edit.
        Returns the saved path, if any."""
        from . import history
        actions = list(dict.fromkeys(actions or []))
        if not actions:
            actions = ["copy"]
        done, path = [], None
        if "copy" in actions:
            copy_image(img)
            self._clip_until = time.monotonic() + CLIPBOARD_LINGER
            done.append("copied")
        if "save" in actions or "upload" in actions or output:
            path = output or settings.screenshot_path()
            try:
                save_image(img, path)
            except OSError as e:
                self.notifier.notify("Saving failed", str(e), icon="dialog-error")
                path = None
            if path:
                history.add("image", path, width=img.width(), height=img.height())
                if "save" in actions or output:
                    done.append("saved")
        if "pin" in actions:
            self.pin(img, origin)
        if "edit" in actions:
            self.open_editor(image=img, path=path)
        if "ocr" in actions:
            self.ocr(img)
        if "upload" in actions and path:
            self.upload(path)
        if done:
            title = "Screenshot " + " and ".join(done)
            body = os.path.basename(path) if path and "saved" in done else \
                f"{img.width()}×{img.height()} in the clipboard"
            acts = []
            if path:
                acts = [("Open", lambda p=path: _open(p)), ("Edit", lambda p=path: self.open_editor(path=p)),
                        ("Show in folder", lambda p=path: _show_in_folder(p))]
            self.notifier.notify(title, body, path=path if "saved" in done else None,
                                 icon="camera-photo", actions=acts)
        self.idle_check()
        return path

    # ------------------------------------------------------------------ editor, pins, history

    def open_editor(self, doc=None, image=None, path=None):
        from .annotate import Document, EditorWindow
        if doc is None and image is None and path:
            win = EditorWindow.open_file(path)
        else:
            win = EditorWindow(doc or Document(image), path)
        self.keep(win)
        win.action.connect(lambda name, d: self._doc_action(name, d, win))
        win.show()
        win.raise_()
        win.activateWindow()
        return win

    def pin(self, img, origin=None):
        from .pin import PinWindow
        pw = PinWindow(img, self.kwin, origin)
        self.keep(pw)
        pw.action.connect(self._pin_action)
        pw.show()

    def _pin_action(self, name, img):
        if name == "edit":
            self.open_editor(image=img)
        elif name == "ocr":
            self.ocr(img)
        elif name in ("copy", "save", "upload"):
            self.after_capture(img, [name])

    def show_history(self):
        from .history import HistoryWindow
        for w in self.windows:
            if isinstance(w, HistoryWindow):
                w.show()
                w.raise_()
                w.activateWindow()
                return
        hw = HistoryWindow()
        self.keep(hw)
        hw.action.connect(self._history_action)
        hw.show()

    def _history_action(self, name, entry):
        path = entry.get("path") if entry else None
        if name == "new_shot":
            self._run(cli.parse(["shot"]))
        elif name == "new_record":
            self._run(cli.parse(["record"]))
        elif name == "new_gif":
            self._run(cli.parse(["record", "--gif"]))
        elif name == "settings":
            self.show_settings()
        elif name == "open" and path:
            _open(path)
        elif name == "folder" and path:
            _show_in_folder(path)
        elif name == "copy" and path:
            if entry.get("kind") == "image":
                copy_image(QImage(path))
                self._clip_until = time.monotonic() + CLIPBOARD_LINGER
            else:
                _copy_file(path)
        elif name == "copy_path" and path:
            copy_text(self.bus, path)
        elif name == "copy_url" and entry.get("url"):
            copy_text(self.bus, entry["url"])
        elif name == "upload" and path:
            self.upload(path)
        elif name == "edit" and path:
            self.open_editor(path=path)
        elif name == "pin" and path:
            from PyQt6.QtGui import QImage
            self.pin(QImage(path))
        # open / folder / copy_path / copy_url / delete are handled by the window itself

    def show_settings(self):
        from .hotkeys import Hotkeys
        from .settings_ui import SettingsDialog
        for w in self.windows:
            if isinstance(w, SettingsDialog):
                w.show()
                w.raise_()
                w.activateWindow()
                return
        dlg = SettingsDialog(Hotkeys(self.bus))
        self.keep(dlg)
        dlg.finished.connect(lambda _: (self.windows.discard(dlg), self._update_tray(), self.idle_check()))
        dlg.show()

    def _open_image_dialog(self):
        from PyQt6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(None, "Open image", settings.pictures_dir(),
                                              "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif)")
        if path:
            self.open_editor(path=path)

    # ------------------------------------------------------------------ upload / OCR / color

    def upload(self, path):
        from . import history, upload
        if not settings.get("upload.uploader") and not upload.list_uploaders():
            self.notifier.notify("No uploader set up", "Import a ShareX .sxcu uploader in Settings → Uploads",
                                 icon="dialog-information", actions=[("Settings", self.show_settings)])
            return
        up = upload.Uploader()
        self.jobs += 1
        self.busy_note(f"Uploading {os.path.basename(path)}…")

        def done(result, error):
            self.jobs -= 1
            err = getattr(up, "error", None)
            if result is None and isinstance(err, upload.UntrustedCertificate) and err.fingerprint:
                def trust_and_retry():
                    upload.trust_certificate(err.host, err.port, err.fingerprint)
                    self.upload(path)
                self.notifier.notify("Upload blocked: untrusted certificate", str(err), icon="security-low",
                                     actions=[("Trust certificate & retry", trust_and_retry)], timeout=0)
            elif result is None:
                self.notifier.notify("Upload failed", error or "unknown error", icon="dialog-error")
            else:
                if settings.get("upload.copy_url"):
                    copy_text(self.bus, result.url)
                for e in history.entries():
                    if e.get("path") == path:
                        history.update(e, url=result.url, delete_url=result.delete_url)
                        break
                else:
                    history.add("image" if path.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
                                else "video", path, url=result.url, delete_url=result.delete_url)
                self.notifier.notify("Uploaded" + (" — link copied" if settings.get("upload.copy_url") else ""),
                                     result.url, icon="cloud-upload",
                                     actions=[("Open link", lambda: _open(result.url)),
                                              ("Copy link", lambda: copy_text(self.bus, result.url))])
            up.deleteLater()
            self.idle_check()
        up.finished.connect(done)
        up.start(path)
        self._uploads = getattr(self, "_uploads", set()) | {up}

    def ocr(self, img):
        from . import ocr
        if not ocr.available():
            self.notifier.notify("OCR needs Tesseract", ocr.install_hint(), icon="dialog-information")
            return
        job = ocr.Ocr()
        self.jobs += 1

        def done(text, error):
            self.jobs -= 1
            if error and error != "No text found":
                self.notifier.notify("OCR failed", error, icon="dialog-error")
            elif not text.strip():
                self.notifier.notify("No text found", "", icon="dialog-information")
            else:
                copy_text(self.bus, text.strip())
                preview = text.strip()
                self.notifier.notify("Text copied", preview[:300] + ("…" if len(preview) > 300 else ""),
                                     icon="edit-copy")
            job.deleteLater()
            self.idle_check()
        job.finished.connect(done)
        job.start(img)
        self._ocrs = getattr(self, "_ocrs", set()) | {job}

    def _ocr_picked(self, sel):
        self.ocr(self.crop_background(sel.rect))

    def _color_picked(self, sel):
        c = sel.color
        text = c.name().upper()
        copy_text(self.bus, text)
        self.notifier.notify(f"Color {text} copied", f"rgb({c.red()}, {c.green()}, {c.blue()})",
                             icon="color-picker",
                             actions=[("Copy rgb()", lambda: copy_text(self.bus, f"rgb({c.red()}, {c.green()}, {c.blue()})"))])

    def _scroll_picked(self, sel):
        from .osd import ScrollBarOsd
        from .scrollcap import ScrollCapture
        self.remember_selection(sel)
        cap = ScrollCapture(self.kwin, sel)
        bar = ScrollBarOsd()
        self.keep(bar)
        self.jobs += 1
        r = sel.rect
        fx, fy = sel.win["frame"][:2]
        bar.show()
        self.kwin.place(bar.TITLE, (r.x() - fx, r.y() - fy, r.width(), r.height()), sel.win["id"], mode="below")
        bar.stop_clicked.connect(cap.stop)
        cap.progress.connect(bar.set_height)

        def finished(img, error):
            self.jobs -= 1
            self.kwin.unplace(bar.TITLE)
            bar.hide()
            bar.deleteLater()
            self.windows.discard(bar)
            if img is None or img.isNull():
                if error:
                    self.notifier.notify("Scrolling capture failed", error, icon="dialog-error")
                else:
                    log("scrolling capture cancelled")
            elif settings.get("shot.annotate"):
                self.open_editor(image=img)
            else:
                self.after_capture(img, settings.get("shot.after"))
            cap.deleteLater()
            self.idle_check()
        cap.finished.connect(finished)
        self._scroll = cap
        cap.start()
        bar.set_text("Scroll the window yourself, then" if getattr(cap, "manual", False)
                     else "Scrolling… move the mouse or")

    # ------------------------------------------------------------------ recording

    def record(self, ns):
        if self.recording:
            self.recording.stop()
            return
        if self.picker:
            return
        anim = "webp" if ns.webp else "gif" if ns.gif else None
        if ns.gif and settings.get("gif.format") == "webp":
            anim = "webp"  # the "GIF" action follows the animation format chosen in the settings
        opts = {
            "codec": ns.codec or settings.get("rec.codec"),
            "quality": ns.quality or ("high" if anim else settings.get("rec.quality")),
            "qp": ns.qp, "fps": ns.fps or settings.get("rec.fps") or None,
            "audio": not ns.no_audio and settings.get("rec.audio"),
            "mic": ns.mic or settings.get("rec.mic"),
            "mp4": not ns.mkv and settings.get("rec.mp4"),
            "anim": anim, "duration": ns.duration, "output": ns.output, "delay": ns.delay,
            "clicks": ns.clicks or settings.get("rec.clicks"),
            "keys": ns.keys or settings.get("rec.keys"),
        }
        cursor = False if ns.no_cursor else None

        def start(sel, remember=True):
            if remember:
                self.remember_selection(sel)
            if sel.show_cursor != settings.get("cursor") and not ns.no_cursor:
                settings.set("cursor", sel.show_cursor)
            r = sel.rect
            log(f"selected {sel.label!r} at {r.x()},{r.y()} {r.width()}x{r.height()}")
            self.recording = Recording(self, sel, opts, self._recording_ended)
            self.recording.start()

        from .recording import Recording
        if ns.last:
            sel = self.restore_selection()
            sel.show_cursor = settings.get("cursor") if cursor is None else cursor
            start(sel, remember=False)
            return
        self.pick(mode="record", on_pick=start, frozen=False, show_cursor=cursor)

    def stop_recording(self):
        if self.recording:
            self.recording.stop()
        elif self.picker:
            self.picker._finish(None)
        elif not self.daemon:
            self.quit()

    def _recording_ended(self, path, error):
        from . import history
        rec, self.recording = self.recording, None
        self._tray_state = None
        if self.tray and not self.daemon:
            self.tray.hide()
            self.tray = None
        else:
            self._update_tray()
        if path:
            mb = os.path.getsize(path) / 1e6
            kind = "gif" if path.endswith((".gif", ".webp")) else "video"
            history.add(kind, path, width=(rec.size() or [0, 0])[0], height=(rec.size() or [0, 0])[1],
                        duration=rec.elapsed() if rec else None)
            log(f"saved {path} ({mb:.1f} MB)")
            print(path, flush=True)
            title = "Recording stopped by an error" if error else "Recording saved"
            body = (f"{error}\n" if error else "") + f"{os.path.basename(path)} · {mb:.1f} MB"
            self.notifier.notify(title, body, path=path, icon="dialog-warning" if error else "media-record",
                                 actions=[("Open", lambda: _open(path)),
                                          ("Show in folder", lambda: _show_in_folder(path)),
                                          ("Upload", lambda: self.upload(path))])
            after = settings.get("rec.after") or []
            if "copy" in after:
                copy_text(self.bus, path)
            if "upload" in after:
                self.upload(path)
            if error:
                self.exit_code = 1
        elif error:
            log("error:", error)
            self.notifier.notify("Recording failed", error, icon="dialog-error")
            self.exit_code = 1
        self.idle_check()


# ---------------------------------------------------------------------- desktop helpers

def _open(target):
    import subprocess
    subprocess.Popen(["xdg-open", target], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _show_in_folder(path):
    import subprocess
    import dbus
    try:
        bus = dbus.SessionBus()
        bus.get_object("org.freedesktop.FileManager1", "/org/freedesktop/FileManager1").ShowItems(
            ["file://" + path], "", dbus_interface="org.freedesktop.FileManager1")
    except dbus.DBusException:
        subprocess.Popen(["xdg-open", os.path.dirname(path)], start_new_session=True)


def _copy_file(path):
    from PyQt6.QtCore import QMimeData, QUrl
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(path)])
    QApplication.clipboard().setMimeData(mime)


AUTOSTART = os.path.join(settings.CONFIG_DIR, "..", "autostart", "snappie.desktop")


def _autostart_enabled():
    return os.path.exists(AUTOSTART)


def _set_autostart(on):
    from .hotkeys import launcher
    path = os.path.normpath(AUTOSTART)
    if on:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("[Desktop Entry]\nType=Application\nName=snappie\n"
                    f"Exec={launcher()} tray\nIcon={ICON}\nX-KDE-autostart-phase=2\nNoDisplay=true\n")
    elif os.path.exists(path):
        os.unlink(path)
