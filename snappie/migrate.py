"""One-time move from the old name ("screenrec") to Snappie.

Carries over settings, uploaders, trusted certificates, history and thumbnails,
and the global shortcuts the user assigned; removes the old launcher, shortcut
component, autostart entry, capture helper and ~/.local/bin link. Old captures
stay where they are (history keeps pointing at them). Safe to run every start:
it only acts when old files are there.
"""

import os
import shutil

import dbus

from . import settings

OLD = "screenrec"
OLD_SERVICE = "io.github.screenrec"
_HOME = os.path.expanduser("~")
_CONFIG = os.environ.get("XDG_CONFIG_HOME", os.path.join(_HOME, ".config"))
_DATA = os.environ.get("XDG_DATA_HOME", os.path.join(_HOME, ".local", "share"))


def _log(*a):
    from .app_util import log
    log("migrate:", *a)


def _move_dir(old, new):
    """Move old's contents into new (new may already exist); keep anything new already has."""
    if not os.path.isdir(old):
        return False
    os.makedirs(new, exist_ok=True)
    for name in os.listdir(old):
        src, dst = os.path.join(old, name), os.path.join(new, name)
        if os.path.exists(dst):
            if os.path.isdir(src) and os.path.isdir(dst):
                _move_dir(src, dst)
            continue
        shutil.move(src, dst)
    shutil.rmtree(old, ignore_errors=True)
    return True


def _remove(path):
    try:
        if os.path.islink(path) or os.path.isfile(path):
            os.unlink(path)
            return True
    except OSError:
        pass
    return False


def _old_shortcuts(accel):
    """{action id: keys} assigned to the old component."""
    from .hotkeys import ACTIONS
    keys = {}
    for aid, label, _ in ACTIONS:
        try:
            k = accel.shortcutKeys([f"{OLD}.desktop", aid, OLD, label])
        except dbus.DBusException:
            continue
        if any(any(int(c) for c in (seq[0] if len(seq) and isinstance(seq[0], dbus.Array) else seq))
               for seq in k):
            keys[aid] = k
    return keys


def run(bus):
    """Returns True if anything was migrated."""
    did = False
    # an old instance still running (tray): ask it to quit, it finishes a running recording first
    try:
        if bus.name_has_owner(OLD_SERVICE):
            bus.get_object(OLD_SERVICE, "/").run(dbus.Array(["quit"], signature="s"),
                                                 dbus_interface=OLD_SERVICE, timeout=5)
            _log("asked the old screenrec instance to quit")
    except dbus.DBusException:
        pass

    if _move_dir(os.path.join(_CONFIG, OLD), settings.CONFIG_DIR):
        _log("settings and uploaders moved")
        did = True
    old_data = os.path.join(_DATA, OLD)
    if os.path.isdir(old_data):
        _remove(os.path.join(old_data, "screenrec-cast"))     # old helper binary; snappie-cast replaces it
        _move_dir(old_data, settings.DATA_DIR)
        _log("history moved")
        did = True
    from .app_util import LOG_FILE
    _move_dir(os.path.join(os.environ.get("XDG_STATE_HOME", os.path.join(_HOME, ".local", "state")), OLD),
              os.path.dirname(LOG_FILE))

    # global shortcuts: copy the keys over, then drop the old component
    try:
        accel = dbus.Interface(bus.get_object("org.kde.kglobalaccel", "/kglobalaccel"), "org.kde.KGlobalAccel")
        keys = _old_shortcuts(accel)
    except dbus.DBusException:
        accel, keys = None, {}
    old_desktop = os.path.join(_DATA, "applications", f"{OLD}.desktop")
    if os.path.exists(old_desktop) or keys:
        from . import hotkeys
        hotkeys.install()
        if accel is not None:
            for aid, label, _ in hotkeys.ACTIONS:
                try:
                    if aid in keys:
                        # NoAutoloading: set exactly these keys on the new component
                        accel.setShortcutKeys([hotkeys.COMPONENT, aid, "Snappie", label], keys[aid],
                                              dbus.UInt32(0x2 | 0x4))
                    accel.unregister(f"{OLD}.desktop", aid)
                except dbus.DBusException:
                    pass
            for aid in ("_launch",):
                try:
                    accel.unregister(f"{OLD}.desktop", aid)
                except dbus.DBusException:
                    pass
        if keys:
            _log(f"moved {len(keys)} shortcut(s)")
        did = True
    for path in (old_desktop, os.path.join(_DATA, "kglobalaccel", f"{OLD}.desktop"),
                 os.path.join(_DATA, "applications", f"{OLD}-cast.desktop")):
        did |= _remove(path)

    old_auto = os.path.join(_CONFIG, "autostart", f"{OLD}.desktop")
    if os.path.exists(old_auto):
        from .app import _set_autostart
        _remove(old_auto)
        _set_autostart(True)
        did = True

    old_bin = os.path.join(_HOME, ".local", "bin", OLD)
    if os.path.islink(old_bin) and not os.path.exists(old_bin):   # pointed at screenrec.sh, now gone
        _remove(old_bin)
        new_bin = os.path.join(_HOME, ".local", "bin", "snappie")
        if not os.path.exists(new_bin):
            here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            os.symlink(os.path.join(here, "snappie.sh"), new_bin)
        did = True
    if did:
        subprocess_quiet(["kbuildsycoca6"])
    return did


def subprocess_quiet(cmd):
    import subprocess
    try:
        subprocess.run(cmd, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        pass
