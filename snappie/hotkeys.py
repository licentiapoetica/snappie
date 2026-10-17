"""Global shortcuts through KDE's own mechanism.

Every action is a [Desktop Action] in snappie.desktop, linked into
~/.local/share/kglobalaccel/ so it appears under System Settings → Shortcuts →
snappie.  KDE runs the action's command line when its key is pressed, even when
the tray app isn't running; when it is, the command is forwarded to it over
D-Bus (fast path in __main__).  No keys are assigned by default.
"""

import os
import shutil
import subprocess

import dbus

APP_ID = "snappie"
COMPONENT = f"{APP_ID}.desktop"
APPS_DIR = os.path.join(os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")), "applications")
ACCEL_DIR = os.path.join(os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")), "kglobalaccel")
DESKTOP = os.path.join(APPS_DIR, COMPONENT)

# (action id, label, command line arguments)
ACTIONS = [
    ("Screenshot", "Screenshot: pick an area", ["shot"]),
    ("ScreenshotScreen", "Screenshot: screen under the cursor", ["shot", "--screen"]),
    ("ScreenshotWindow", "Screenshot: active window", ["shot", "--window"]),
    ("ScreenshotLast", "Screenshot: repeat the last area", ["shot", "--last"]),
    ("ScreenshotDelay", "Screenshot in 5 seconds", ["shot", "--delay", "5"]),
    ("Record", "Record: pick an area (again: stop)", ["record"]),
    ("RecordGif", "Record a GIF (again: stop)", ["record", "--gif"]),
    ("RecordLast", "Record: repeat the last area (again: stop)", ["record", "--last"]),
    ("Pause", "Pause / resume the recording", ["pause"]),
    ("Draw", "Draw on the recording", ["draw"]),
    ("Stop", "Stop the recording", ["stop"]),
    ("ColorPicker", "Color picker", ["color"]),
    ("Ocr", "Copy text from the screen (OCR)", ["ocr"]),
    ("Scroll", "Scrolling capture", ["scroll"]),
    ("History", "History", ["history"]),
]


def launcher():
    """The command KDE should run: the installed `snappie`, else this checkout's script."""
    found = shutil.which("snappie")
    if found:
        return found
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(here, "snappie.sh")


def desktop_entry(exe):
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        "Name=Snappie",
        "GenericName=Screenshots and Screen Recording",
        "Comment=Capture, annotate, record and share your screen",
        f"Exec={exe}",
        "Icon=snappie",
        "Categories=Utility;Graphics;AudioVideo;Recorder;",
        "Keywords=screenshot;screen;record;capture;gif;annotate;ocr;color;",
        "StartupNotify=false",
        "X-KDE-Shortcuts=",
        "Actions=" + ";".join(a for a, _, _ in ACTIONS) + ";",
        "",
    ]
    for aid, label, args in ACTIONS:
        lines += [f"[Desktop Action {aid}]", f"Name={label}", f"Exec={exe} {' '.join(args)}",
                  "X-KDE-Shortcuts=", ""]
    return "\n".join(lines)


def install(exe=None):
    """Write the .desktop file (launcher + shortcut actions) and register the actions with
    KDE. Returns True if the file changed."""
    text = desktop_entry(exe or launcher())
    try:
        from . import icons
        icons.install()
    except OSError:
        pass
    try:
        with open(DESKTOP) as f:
            if f.read() == text and os.path.islink(os.path.join(ACCEL_DIR, COMPONENT)):
                register()
                return False
    except OSError:
        pass
    os.makedirs(APPS_DIR, exist_ok=True)
    os.makedirs(ACCEL_DIR, exist_ok=True)
    with open(DESKTOP, "w") as f:
        f.write(text)
    link = os.path.join(ACCEL_DIR, COMPONENT)
    if not os.path.islink(link):
        if os.path.exists(link):
            os.unlink(link)
        os.symlink(DESKTOP, link)
    subprocess.run(["kbuildsycoca6"], capture_output=True)
    register()
    return True


def register(bus=None):
    """Tell kglobalaccel about the actions now (it only scans ~/.local/share/kglobalaccel at
    login). Same calls System Settings makes when you add an application: a component named
    after a .desktop file launches that file's actions. Keys the user set are kept."""
    try:
        bus = bus or dbus.SessionBus()
        accel = dbus.Interface(bus.get_object("org.kde.kglobalaccel", "/kglobalaccel"), "org.kde.KGlobalAccel")
        for aid, label in [("_launch", "Snappie")] + [(a, l) for a, l, _ in ACTIONS]:
            action = [COMPONENT, aid, "Snappie", label]
            accel.doRegister(action)
            # SetPresent with autoloading: keys the user stored win over the (empty) default
            accel.setShortcutKeys(action, dbus.Array([], signature="(ai)"), dbus.UInt32(0x2))
    except dbus.DBusException:
        pass


def _keys_text(keys):
    from PyQt6.QtGui import QKeySequence
    out = []
    for seq in keys:
        # a(ai): every shortcut is a struct holding the key codes of its (up to 4) chords
        if len(seq) and isinstance(seq[0], (list, tuple, dbus.Array)):
            seq = seq[0]
        codes = [int(k) for k in seq if int(k)]
        if codes:
            out.append(QKeySequence(*codes).toString(QKeySequence.SequenceFormat.NativeText))
    return ", ".join(out)


class Hotkeys:
    """What the settings dialog shows."""

    def __init__(self, bus=None):
        self.bus = bus or dbus.SessionBus()

    def _accel(self):
        return dbus.Interface(self.bus.get_object("org.kde.kglobalaccel", "/kglobalaccel"),
                              "org.kde.KGlobalAccel")

    def actions(self):
        """[(id, label, current keys as text)]"""
        out = []
        try:
            accel = self._accel()
        except dbus.DBusException:
            accel = None
        for aid, label, _ in ACTIONS:
            keys = ""
            if accel is not None:
                try:
                    keys = _keys_text(accel.shortcutKeys([COMPONENT, aid, "Snappie", label]))
                except dbus.DBusException:
                    pass
            out.append((aid, label, keys))
        return out

    def open_system_settings(self):
        for cmd in (["systemsettings", "kcm_keys", "--args", COMPONENT], ["systemsettings", "kcm_keys"],
                    ["kcmshell6", "kcm_keys"]):
            if shutil.which(cmd[0]):
                subprocess.Popen(cmd, start_new_session=True)
                return
