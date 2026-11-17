"""Event sounds: screenshot taken, recording started / saved, upload finished, errors.

Played in this process with GStreamer's playbin (GStreamer is needed for recording anyway),
so any format GStreamer reads works (WAV, OGG, Opus, FLAC, MP3, …) and no player program is
needed.  The stream is Snappie's own, not tagged as an event sound: Plasma routes event sounds
through its "Notification Sounds" volume, and people who mute system notification sounds still
want the sounds they turned on here (they get a "Snappie" slider in the volume applet instead).

Default sounds come by name from the freedesktop sound theme spec: the KDE sound theme first
(System Settings → Sounds), then Ocean, then freedesktop.  Each event can use a file instead.
"""

import configparser
import os

from . import settings
from .app_util import log

# event -> (label, theme sound names, first match wins)
EVENTS = {
    "shot": ("Screenshot taken", ["camera-shutter", "screen-capture"]),
    "record_start": ("Recording started", ["device-added"]),
    "record_stop": ("Recording saved", ["device-removed"]),
    "upload": ("Upload finished", ["completion-success", "complete"]),
    "error": ("Something failed", ["completion-fail", "dialog-error"]),
}
EXTENSIONS = (".oga", ".ogg", ".wav")
MAX_LENGTH = 5000      # ms; a long custom file is cut off (and never holds up a recording longer)

_playing = set()


def data_dirs():
    home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    rest = (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")
    return [home] + [d for d in rest if d]


def kde_theme():
    path = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "kdeglobals")
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        cp.read(path)
        return cp.get("Sounds", "Theme", fallback="").strip()
    except (configparser.Error, UnicodeDecodeError):
        return ""


def theme_file(names):
    """The first of `names` found in the sound themes, or None."""
    themes = list(dict.fromkeys(t for t in (kde_theme(), "ocean", "freedesktop") if t))
    dirs = data_dirs()
    for theme in themes:
        for name in names:
            for d in dirs:
                for ext in EXTENSIONS:
                    p = os.path.join(d, "sounds", theme, "stereo", name + ext)
                    if os.path.isfile(p):
                        return p
    return None


def file_for(event, custom=None):
    """The file to play for `event`: the chosen file if it exists, else the theme's sound."""
    custom = settings.get(f"sound.{event}.file") if custom is None else custom
    if custom and os.path.isfile(custom):
        return custom
    return theme_file(EVENTS[event][1])


def playing():
    return bool(_playing)


def play(event, then=None):
    """Play the sound for `event` if it is turned on. `then` runs when it has finished (right
    away if nothing plays), so a recording can start after its start sound."""
    path = file_for(event) if settings.get(f"sound.{event}") else None
    if not path or not play_file(path, settings.get("sound.volume"), then):
        if then:
            then()


def play_file(path, volume=70, then=None, on_error=None):
    """Play a file at volume 0..100 (as a volume slider shows it). Returns False if it can't
    start; on_error(message) is called if it fails later (e.g. no decoder for the format)."""
    try:
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import GLib, Gst
        Gst.init(None)
    except (ImportError, ValueError) as e:
        log("no sound:", e)
        return False
    pb = Gst.ElementFactory.make("playbin", None)
    if pb is None:
        log("no sound: GStreamer has no playbin (gst-plugins-base)")
        return False
    pb.set_property("uri", Gst.filename_to_uri(os.path.abspath(path)))
    pb.set_property("flags", 0x2)   # audio only
    pb.set_property("volume", _linear(volume))
    sink = Gst.ElementFactory.make("pulsesink", None)   # PipeWire serves the Pulse API too
    if sink is not None:
        try:
            sink.set_property("stream-properties",
                              Gst.Structure.new_from_string("props, application.name=Snappie, "
                                                            "application.icon_name=snappie"))
            pb.set_property("audio-sink", sink)
        except (TypeError, ValueError):
            pass
    state = {"done": False}

    def finish(*_):
        if state["done"]:
            return False
        state["done"] = True
        pb.set_state(Gst.State.NULL)
        bus.remove_signal_watch()
        _playing.discard(pb)
        if then:
            then()
        return False

    def on_message(_bus, msg):
        if msg.type == Gst.MessageType.ERROR:
            err, _ = msg.parse_error()
            log(f"sound {path}: {err.message}")
            if on_error:
                on_error(err.message)
            finish()
        elif msg.type == Gst.MessageType.EOS:
            finish()

    bus = pb.get_bus()
    bus.add_signal_watch()
    bus.connect("message", on_message)
    _playing.add(pb)
    if pb.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
        state["done"] = True
        pb.set_state(Gst.State.NULL)
        bus.remove_signal_watch()
        _playing.discard(pb)
        return False
    GLib.timeout_add(MAX_LENGTH, finish)
    return True


def _linear(volume):
    # sliders are perceptual (cubic); playbin's volume is linear
    v = max(0, min(100, int(volume if volume is not None else 70))) / 100
    return v ** 3
