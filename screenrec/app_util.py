"""Small shared helpers: logging, notifications, clipboard, saving images."""

import os
import sys

import dbus


LOG_FILE = os.path.join(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")),
                        "screenrec", "screenrec.log")


def log(*a):
    """To stderr and to LOG_FILE (kept small), so problems can be looked at afterwards."""
    print("screenrec:", *a, file=sys.stderr, flush=True)
    try:
        import time
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 1_000_000:
            os.replace(LOG_FILE, LOG_FILE + ".1")
        with open(LOG_FILE, "a") as f:
            f.write(time.strftime("%H:%M:%S ") + f"[{os.getpid()}] " + " ".join(str(x) for x in a) + "\n")
    except OSError:
        pass


# -- notifications --

class Notifier:
    """Desktop notifications with action buttons (handled while this process runs)."""

    def __init__(self, bus):
        self.bus = bus
        self._handlers = {}   # notification id -> {action key: callable}
        try:
            self._obj = bus.get_object("org.freedesktop.Notifications", "/org/freedesktop/Notifications")
            bus.add_signal_receiver(self._invoked, "ActionInvoked", "org.freedesktop.Notifications")
            bus.add_signal_receiver(self._closed, "NotificationClosed", "org.freedesktop.Notifications")
        except dbus.DBusException:
            self._obj = None

    def notify(self, title, body="", path=None, icon="media-record", actions=None, timeout=6000):
        """actions: [(label, callable)]; the first one also runs on a click on the notification."""
        if not self._obj:
            return
        hints = {}
        if path:
            hints["x-kde-urls"] = dbus.Array(["file://" + path], signature="s")
        flat, handlers = [], {}
        for i, (label, fn) in enumerate(actions or []):
            key = f"a{i}"
            flat += [key, label]
            handlers[key] = fn
        if actions:
            flat += ["default", ""]
            handlers["default"] = actions[0][1]
        try:
            nid = self._obj.Notify("screenrec", dbus.UInt32(0), icon, title, body,
                                   dbus.Array(flat, signature="s"), dbus.Dictionary(hints, signature="sv"),
                                   timeout, dbus_interface="org.freedesktop.Notifications")
            if handlers:
                self._handlers[int(nid)] = handlers
        except dbus.DBusException:
            pass

    def _invoked(self, nid, key):
        fn = self._handlers.get(int(nid), {}).get(str(key))
        if fn:
            try:
                fn()
            except Exception as e:
                log("notification action failed:", e)

    def _closed(self, nid, reason):
        self._handlers.pop(int(nid), None)

    def pending(self):
        return bool(self._handlers)


# -- clipboard --

def copy_text(bus, text):
    """Klipper can set the clipboard without our window having focus (Wayland needs focus
    otherwise), so prefer it; fall back to Qt."""
    try:
        bus.get_object("org.kde.klipper", "/klipper").setClipboardContents(
            text, dbus_interface="org.kde.klipper.klipper")
        return True
    except dbus.DBusException:
        pass
    from PyQt6.QtWidgets import QApplication
    QApplication.clipboard().setText(text)
    return True


def copy_image(img):
    """Must run while one of our windows has keyboard focus (Wayland). The data stays
    available only while this process runs — the App keeps one-shot processes alive."""
    from PyQt6.QtCore import QMimeData, QBuffer, QByteArray, QIODevice
    from PyQt6.QtWidgets import QApplication
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    mime = QMimeData()
    mime.setData("image/png", data)
    mime.setImageData(img)
    QApplication.clipboard().setMimeData(mime)


def copy_path(bus, path):
    copy_text(bus, path)


# -- images --

def save_image(img, path, quality=None):
    """Save by extension (png / jpg / webp). JPEG has no alpha: flatten onto white."""
    from PyQt6.QtGui import QImage, QPainter, QColor
    from . import settings
    ext = os.path.splitext(path)[1].lower().lstrip(".") or "png"
    fmt = {"jpg": "JPEG", "jpeg": "JPEG", "webp": "WEBP", "png": "PNG", "bmp": "BMP"}.get(ext, "PNG")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if fmt == "JPEG" and img.hasAlphaChannel():
        flat = QImage(img.size(), QImage.Format.Format_RGB32)
        flat.setDevicePixelRatio(img.devicePixelRatio())
        flat.fill(QColor("white"))
        p = QPainter(flat)
        p.drawImage(0, 0, img)
        p.end()
        img = flat
    q = quality if quality is not None else (settings.get("shot.quality") if fmt != "PNG" else -1)
    if not img.save(path, fmt, q):
        raise OSError(f"could not save {path}")
    return path
