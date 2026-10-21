"""What Snappie needs from the system, and the packages that provide it.

Standard library only at import time: `snappie deps`, install.sh and the start-up
check run this when the rest of the app can't be imported.  Package names are
known for Arch and openSUSE (and their derivatives); elsewhere the report names
the missing pieces.
"""

import importlib
import shutil
import subprocess
import sys

INSTALL = {"arch": "sudo pacman -S --needed", "suse": "sudo zypper install"}


class Dep:
    __slots__ = ("key", "label", "kind", "names", "packages", "feature")

    def __init__(self, key, label, kind, names, packages, feature=None):
        """kind: python (modules) | typelib | gst (elements) | gst-any (one of them is
        enough) | cmd | pkgconfig. packages: {distro family: [package, ...]}.
        feature: None when required, else what doesn't work without it."""
        self.key, self.label, self.kind, self.names = key, label, kind, names
        self.packages, self.feature = packages, feature


def _p(arch, suse):
    return {"arch": arch.split(), "suse": suse.split()}


DEPS = [
    Dep("pyqt6", "PyQt6", "python", ["PyQt6.QtWidgets", "PyQt6.QtDBus"], _p("python-pyqt6", "python3-PyQt6")),
    Dep("qtsvg", "Qt SVG", "python", ["PyQt6.QtSvg"], _p("qt6-svg", "python3-PyQt6")),
    Dep("dbus", "dbus-python", "python", ["dbus", "dbus.mainloop.glib"], _p("python-dbus", "python3-dbus-python")),
    Dep("gobject", "PyGObject", "python", ["gi"], _p("python-gobject", "python3-gobject")),
    Dep("numpy", "NumPy", "python", ["numpy"], _p("python-numpy", "python3-numpy")),
    Dep("requests", "Requests", "python", ["requests"], _p("python-requests", "python3-requests")),
    Dep("gst", "GStreamer", "typelib", [("Gst", "1.0")], _p("gstreamer", "gstreamer typelib-1_0-Gst-1_0")),
    Dep("gdkpixbuf", "GdkPixbuf", "typelib", [("GdkPixbuf", "2.0")],
        _p("gdk-pixbuf2", "typelib-1_0-GdkPixbuf-2_0")),
    Dep("atspi", "AT-SPI", "typelib", [("Atspi", "2.0")], _p("at-spi2-core", "typelib-1_0-Atspi-2_0")),
    Dep("pipewire", "GStreamer PipeWire plugin", "gst", ["pipewiresrc"],
        _p("gst-plugin-pipewire", "gstreamer-plugin-pipewire")),
    Dep("gst-base", "GStreamer base plugins", "gst",
        ["videoconvert", "videorate", "appsink", "audioconvert", "audioresample", "audiomixer", "opusenc"],
        _p("gst-plugins-base-libs gst-plugins-base", "gstreamer-plugins-base")),
    Dep("gst-good", "GStreamer good plugins", "gst",
        ["videocrop", "gdkpixbufoverlay", "capssetter", "matroskamux", "pulsesrc"],
        _p("gst-plugins-good", "gstreamer-plugins-good")),
    Dep("encoder", "a GStreamer video encoder (VA-API)", "gst-any",
        ["vah265enc", "vah264enc", "vaav1enc", "svtav1enc"], _p("gst-plugin-va", "gstreamer-plugins-bad")),
    Dep("parsers", "GStreamer video parsers", "gst", ["h265parse", "h264parse", "av1parse"],
        _p("gst-plugins-bad-libs", "gstreamer-plugins-bad")),
    Dep("ffmpeg", "ffmpeg", "cmd", ["ffmpeg"], _p("ffmpeg", "ffmpeg")),
    # building the capture helper (helper/build.sh)
    Dep("compiler", "a C compiler", "cmd", ["cc"], _p("gcc", "gcc")),
    Dep("pkg-config", "pkg-config", "cmd", ["pkg-config"], _p("pkgconf", "pkgconf-pkg-config")),
    Dep("wayland", "Wayland client headers", "pkgconfig", ["wayland-client", "wayland-scanner"],
        _p("wayland", "wayland-devel")),
    # optional
    Dep("tesseract", "tesseract", "cmd", ["tesseract"], _p("tesseract", "tesseract-ocr"),
        feature="text recognition (OCR)"),
    Dep("evdev", "python-evdev", "python", ["evdev"], _p("python-evdev", "python3-evdev"),
        feature="showing clicks and keys in recordings"),
]
BY_KEY = {d.key: d for d in DEPS}

# tesseract language code -> openSUSE package suffix (tesseract-ocr-traineddata-<name>)
_SUSE_LANGS = {
    "eng": "english", "deu": "german", "fra": "french", "spa": "spanish", "ita": "italian",
    "por": "portuguese", "nld": "dutch", "pol": "polish", "rus": "russian", "ukr": "ukrainian",
    "ces": "czech", "swe": "swedish", "jpn": "japanese", "chi_sim": "chinese_simplified",
    "chi_tra": "chinese_traditional", "kor": "korean", "osd": "orientation_and_script_detection",
}


def family():
    """"arch", "suse" or None, from /etc/os-release (derivatives list their parent in ID_LIKE)."""
    try:
        with open("/etc/os-release") as f:
            info = dict(line.rstrip("\n").split("=", 1) for line in f if "=" in line)
    except OSError:
        return None
    ids = " ".join(info.get(k, "").strip("\"'") for k in ("ID", "ID_LIKE")).split()
    if "arch" in ids:
        return "arch"
    if any(i.startswith(("suse", "opensuse")) for i in ids):
        return "suse"
    return None


# -- checks --

_gst = None   # the Gst module once loaded, False if it can't be


def _load_gst():
    global _gst
    if _gst is None:
        try:
            import gi
            gi.require_version("Gst", "1.0")
            from gi.repository import Gst
            Gst.init(None)
            _gst = Gst
        except (ImportError, ValueError):
            _gst = False
    return _gst


def _has(dep, name):
    kind = dep.kind
    if kind == "python":
        try:
            importlib.import_module(name)
        except Exception:  # noqa: BLE001 - a broken install counts as missing too
            return False
        return True
    if kind == "typelib":
        try:
            import gi
            gi.require_version(*name)
            importlib.import_module("gi.repository." + name[0])
        except (ImportError, ValueError):
            return False
        return True
    if kind in ("gst", "gst-any"):
        gst = _load_gst()
        return bool(gst) and gst.ElementFactory.find(name) is not None
    if kind == "cmd":
        return shutil.which(name) is not None
    if kind == "pkgconfig":
        if name == "wayland-scanner":
            return shutil.which(name) is not None
        try:
            return subprocess.run(["pkg-config", "--exists", name]).returncode == 0
        except OSError:
            return False
    raise ValueError(kind)


def is_missing(dep):
    found = [_has(dep, n) for n in dep.names]
    return not any(found) if dep.kind == "gst-any" else not all(found)


def missing(keys=None, optional=True):
    """The Deps (all, or those named in `keys`) that aren't installed."""
    deps = [BY_KEY[k] for k in keys] if keys else DEPS
    return [d for d in deps if (optional or d.feature is None) and is_missing(d)]


# -- what to install --

def _tesseract_data(lang, fam):
    codes = (lang or "eng").split("+")
    if fam == "arch":
        return [f"tesseract-data-{c}" for c in codes]
    if fam == "suse":
        return [f"tesseract-ocr-traineddata-{_SUSE_LANGS.get(c, c)}" for c in codes]
    return []


def packages(deps, lang=None, fam=None):
    """Package names for `deps` on this distro, in order, without repeats ([] if unknown)."""
    fam = fam or family()
    out = []
    for d in deps:
        if isinstance(d, str):
            d = BY_KEY[d]
        out += d.packages.get(fam, [])
        if d.key == "tesseract":
            out += _tesseract_data(lang, fam)
    return list(dict.fromkeys(out))


def install_hint(deps, lang=None):
    """`sudo pacman -S --needed …` for `deps` (Deps or keys), or "install …" naming them."""
    fam = family()
    pkgs = packages(deps, lang, fam)
    if fam in INSTALL and pkgs:
        return f"{INSTALL[fam]} {' '.join(pkgs)}"
    return "install " + ", ".join(BY_KEY[d].label if isinstance(d, str) else d.label for d in deps)


def tesseract_data_hint(lang):
    fam = family()
    pkgs = _tesseract_data(lang, fam)
    if fam in INSTALL:
        return f"{INSTALL[fam]} {' '.join(pkgs)}"
    return f"install the tesseract language data for {lang}"


# -- report --

def report():
    """(text, everything required is there)"""
    gone = missing()
    required = [d for d in gone if d.feature is None]
    optional = [d for d in gone if d.feature is not None]
    lines = []
    if required:
        lines.append("Snappie needs:")
        lines += [f"  {d.label}" for d in required]
    if optional:
        lines.append("Optional:")
        lines += [f"  {d.label} — for {d.feature}" for d in optional]
    if gone:
        try:
            from . import settings
            lang = settings.get("ocr.lang")
        except ImportError:
            lang = None
        lines += ["", "Install with:", "  " + install_hint(gone, lang)]
        if family() is None:
            lines.append("  (package names are only known for Arch and openSUSE)")
    else:
        lines.append("Everything Snappie needs is installed.")
    return "\n".join(lines), not required


def main():
    text, ok = report()
    print(text, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
