"""Command line. Kept free of heavy imports: hotkeys run `snappie <command>`, and when the
tray app is running that process only parses, forwards over D-Bus and exits."""

import argparse
import os

COMMANDS = ["tray", "shot", "record", "stop", "pause", "draw", "color", "ocr", "scroll",
            "history", "settings", "edit", "pin", "upload", "convert", "quit", "deps"]


class ArgError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # don't sys.exit inside the tray app
        raise ArgError(f"{self.prog}: {message}")


def build_parser():
    ap = _Parser(prog="snappie", description="Screenshots, annotation and screen recording for "
                                               "KDE Plasma (Wayland). Without a command: start the tray app.")
    sub = ap.add_subparsers(dest="cmd", parser_class=_Parser, metavar="command")

    sub.add_parser("tray", help="start the tray app (default)")

    s = sub.add_parser("shot", help="take a screenshot (pick an area)")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--screen", action="store_true", help="the screen under the cursor, no picking")
    g.add_argument("--window", action="store_true", help="the active window, no picking")
    g.add_argument("--last", action="store_true", help="the same area / window as last time")
    s.add_argument("--delay", type=float, default=0, help="seconds to wait first (countdown)")
    s.add_argument("--then", help="skip the editor and run these, comma-separated: "
                                  "copy,save,upload,pin,ocr,edit")
    s.add_argument("-o", "--output", help="save to this file (implies save)")

    r = sub.add_parser("record", help="record an area (when recording: stop)")
    _record_args(r)

    sub.add_parser("stop", help="stop the recording")
    sub.add_parser("pause", help="pause / resume the recording")
    sub.add_parser("draw", help="toggle drawing on the recording")
    sub.add_parser("color", help="pick a color from the screen (copies its hex code)")
    o = sub.add_parser("ocr", help="pick an area and copy its text (or the text of an image file)")
    o.add_argument("file", nargs="?")
    sub.add_parser("scroll", help="scrolling capture of a window area")
    sub.add_parser("history", help="show the capture history")
    sub.add_parser("settings", help="open the settings")
    for name, what in (("edit", "open an image in the editor, or a video in the video editor (cut, crop)"), ("pin", "pin an image on the screen")):
        p = sub.add_parser(name, help=what)
        p.add_argument("file")
    p = sub.add_parser("upload", help="upload files with the chosen uploader (one after another)")
    p.add_argument("files", nargs="+", metavar="file")
    p.add_argument("-u", "--uploader", help="an uploader from the settings (its name or file name), or an "
                                            ".sxcu file (default: the one chosen in settings)")
    c = sub.add_parser("convert", help="convert between videos, GIF and animated WebP, cut, crop, change "
                                        "speed (without a format: opens the convert window)")
    _convert_args(c)
    sub.add_parser("quit", help="quit the tray app")
    sub.add_parser("service")   # no help: started by D-Bus activation, see share_plugin
    sub.add_parser("deps", help="check what Snappie needs from the system")
    return ap


def _record_args(r):
    g = r.add_mutually_exclusive_group()
    g.add_argument("--gif", action="store_true", help="make a GIF")
    g.add_argument("--webp", action="store_true", help="make an animated WebP")
    r.add_argument("--last", action="store_true", help="record the same area / window as last time")
    r.add_argument("-o", "--output", help="output file (.mp4/.mkv/.webm/.gif/.webp) or directory")
    r.add_argument("-c", "--codec", choices=["hevc", "av1", "h264", "vp9", "vp8"],
                   help="video codec (default: settings, hevc); VP8, and VP9 on AMD/NVIDIA, use the CPU")
    r.add_argument("-q", "--quality", choices=["high", "good", "small"], help="quality preset")
    r.add_argument("--qp", type=int, help="constant quantizer instead of capped VBR (overrides --quality)")
    r.add_argument("--fps", type=int, help="force a constant frame rate (default: native, variable)")
    r.add_argument("--no-audio", action="store_true", help="don't record desktop audio")
    r.add_argument("--mic", action="store_true", help="also record (mix in) the default microphone")
    r.add_argument("--no-cursor", action="store_true", help="don't record the mouse cursor")
    r.add_argument("--container", choices=["mp4", "mkv", "webm"],
                   help="file type (default: settings, mp4); WebM records VP9 unless -c is vp8 or av1")
    r.add_argument("--mkv", action="store_true", help="same as --container mkv")
    r.add_argument("--clicks", action="store_true", help="show mouse clicks in the recording")
    r.add_argument("--keys", action="store_true", help="show pressed keys in the recording")
    r.add_argument("--delay", type=float, default=0, help="seconds to wait before recording")
    r.add_argument("--duration", type=float, help="stop automatically after N seconds")


CONVERT_FORMATS = ["gif", "webp", "mp4", "webm"]
CONVERT_VALUES = {"--to", "--fps", "--width", "--quality", "--start", "--end", "--cut", "--crop", "--speed",
                  "--repeat", "--background"}     # options whose value is not a path


def _convert_args(c):
    g = c.add_mutually_exclusive_group()
    g.add_argument("--to", choices=CONVERT_FORMATS, help="output format; saved next to the file")
    for f in CONVERT_FORMATS:
        g.add_argument(f"--{f}", dest="to", action="store_const", const=f, help=f"same as --to {f}")
    c.add_argument("--fps", type=_ranged(int, 0, 120), help="frame rate (0 = as the source; "
                                                            "default: settings)")
    c.add_argument("--width", type=_ranged(int, 0, 16384), help="maximum width in pixels (0 = keep)")
    c.add_argument("--quality", type=_ranged(int, 1, 100), help="1–100 (default: settings, 80)")
    c.add_argument("--start", type=parse_time, help="start here (seconds, or M:SS)")
    c.add_argument("--end", type=parse_time, help="end here (seconds, or M:SS)")
    c.add_argument("--cut", type=_span, action="append", default=[], metavar="FROM-TO",
                   help="remove this part, e.g. 0:05-0:12 (repeatable)")
    c.add_argument("--crop", type=_crop, metavar="W:H:X:Y|auto", help="keep this area (auto: remove black bars)")
    c.add_argument("--speed", type=_ranged(float, 0.1, 16), default=1.0, help="play faster (2) or slower (0.5)")
    c.add_argument("--repeat", type=_ranged(int, 1, 50), default=1, help="play it N times (videos)")
    c.add_argument("--background", metavar="COLOR", help="behind transparency in MP4/WebM, e.g. '#000000'")
    c.add_argument("--no-audio", action="store_true", help="drop the sound")
    c.add_argument("--reencode", action="store_true",
                   help="always re-encode (by default a plain container change copies the streams)")
    c.add_argument("-o", "--output", help="output file (default: next to the source, never overwriting)")
    c.add_argument("file")


def parse_time(text):
    """Seconds from "12.5", "1:02.5" or "1:00:02"."""
    try:
        t = 0.0
        for part in str(text).strip().split(":"):
            t = t * 60 + float(part)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a time: {text!r}")
    if t < 0:
        raise argparse.ArgumentTypeError(f"not a time: {text!r}")
    return t


def _span(text):
    a, sep, b = str(text).partition("-")
    if not sep:
        raise argparse.ArgumentTypeError(f"expected FROM-TO, like 0:05-0:12: {text!r}")
    a, b = parse_time(a), parse_time(b)
    if b <= a:
        raise argparse.ArgumentTypeError(f"the end is before the start: {text!r}")
    return a, b


def _crop(text):
    if text == "auto":
        return text
    try:
        w, h, x, y = (int(v) for v in text.split(":"))
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected W:H:X:Y or auto: {text!r}")
    if w < 2 or h < 2 or x < 0 or y < 0:
        raise argparse.ArgumentTypeError(f"not an area: {text!r}")
    return x, y, w, h


def _ranged(kind, lo, hi):
    def check(text):
        try:
            v = kind(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"not a number: {text!r}")
        if not lo <= v <= hi:
            raise argparse.ArgumentTypeError(f"must be {lo}–{hi}")
        return v
    return check


def normalize(argv):
    """Old style `snappie [record options]` and `--stop` still work."""
    argv = list(argv)
    if not argv:
        return ["tray"]
    if argv[0] == "--stop":
        return ["stop"]
    if argv[0].startswith("-") and argv[0] not in ("-h", "--help"):
        return ["record"] + argv
    return argv


def absolute_paths(argv):
    """File arguments as absolute paths: a forwarded command runs in the tray app, whose working
    directory is somewhere else.  argv must be normalized and valid."""
    argv = list(argv)
    if argv and argv[0] in ("edit", "pin", "upload", "ocr", "convert"):
        out, value_of = argv[:1], None
        for a in argv[1:]:
            if value_of in CONVERT_VALUES:
                out.append(a)
                value_of = None
            elif value_of:     # an uploader is a bare name; an .sxcu path is a path
                out.append(os.path.abspath(a) if os.sep in a else a)
                value_of = None
            elif argv[0] == "convert" and a in CONVERT_VALUES:
                out.append(a)
                value_of = a
            elif a in ("-u", "--uploader"):
                out.append(a)
                value_of = a
            elif a.startswith("--uploader="):
                v = a[len("--uploader="):]
                out.append("--uploader=" + (os.path.abspath(v) if os.sep in v else v))
            else:
                out.append(a if a.startswith("-") else os.path.abspath(a))
        return out
    for i, a in enumerate(argv):
        if a in ("-o", "--output") and i + 1 < len(argv):
            argv[i + 1] = os.path.abspath(argv[i + 1])
        elif a.startswith("--output="):
            argv[i] = "--output=" + os.path.abspath(a[len("--output="):])
    return argv


def parse(argv):
    """-> argparse.Namespace (cmd set). Raises ArgError; prints help + raises SystemExit for -h."""
    ns = build_parser().parse_args(normalize(argv))
    if not ns.cmd:
        ns.cmd = "tray"
    return ns
