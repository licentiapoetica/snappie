"""screenrec — screenshots, annotation and screen recording for KDE Plasma (Wayland)."""

import os
import signal
import sys

SERVICE = "io.github.screenrec"


def _forward(argv):
    """If screenrec is already running, hand it the command line. Returns an exit code, or None
    when nothing is running. Imports only dbus: this is the hotkey path, it must be quick."""
    import dbus
    # a private connection: the shared one must be created later, with the GLib main loop
    bus = None
    try:
        bus = dbus.SessionBus(private=True)
        if not bus.name_has_owner(SERVICE):
            return None
        reply = str(bus.get_object(SERVICE, "/").run(dbus.Array(argv, signature="s"),
                                                     dbus_interface=SERVICE, timeout=10))
    except dbus.DBusException as e:
        print("screenrec:", e.get_dbus_message() or e, file=sys.stderr)
        return 1
    finally:
        if bus is not None:
            bus.close()
    if reply.startswith("error"):
        print(reply, file=sys.stderr)
        return 2
    return 0


def _excepthook(kind, value, tb):
    # PyQt aborts the process on an exception inside a slot; log it and keep going instead
    import traceback
    traceback.print_exception(kind, value, tb)


def main():
    from . import cli
    argv = sys.argv[1:]
    try:
        ns = cli.parse(argv)
    except cli.ArgError as e:
        print(e, file=sys.stderr)
        return 2
    if ns.cmd in ("stop", "pause", "draw", "quit"):
        code = _forward(cli.normalize(argv))
        if code is None:
            print(f"screenrec: nothing is running ({ns.cmd})", file=sys.stderr)
            return 1
        return code
    code = _forward(cli.normalize(argv))
    if code is not None:
        return code

    # -- nothing running: this process becomes the app --
    sys.excepthook = _excepthook
    import dbus
    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib
    DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()
    from . import a11y, cast
    from .app_util import log
    from .kwin import AlreadyRunning, KWin
    try:
        kwin = KWin(bus)
    except AlreadyRunning:  # lost a race with another start: forward after all
        return _forward(cli.normalize(argv)) or 0
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
    qapp.setDesktopFileName("screenrec")
    qapp.setQuitOnLastWindowClosed(False)
    from .app import App
    app = App(bus, kwin, daemon=ns.cmd == "tray")
    try:
        from gi.repository import GLibUnix
        signal_add = GLibUnix.signal_add
    except ImportError:
        signal_add = GLib.unix_signal_add
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal_add(GLib.PRIORITY_DEFAULT, sig, lambda: app.quit() or True)
    if ns.cmd != "tray":
        GLib.idle_add(lambda: app._run(ns) or False)
    try:
        return qapp.exec() or app.exit_code
    finally:
        kwin.cleanup()
        a11y.restore_accessibility(bus, prev_a11y)


if __name__ == "__main__":
    sys.exit(main())
