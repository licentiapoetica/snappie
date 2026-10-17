"""Command line. Kept free of heavy imports: hotkeys run `snappie <command>`, and when the
tray app is running that process only parses, forwards over D-Bus and exits."""

import argparse

COMMANDS = ["tray", "shot", "record", "stop", "pause", "draw", "color", "ocr", "scroll",
            "history", "settings", "edit", "pin", "upload", "quit"]


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
    sub.add_parser("ocr", help="pick an area and copy its text")
    sub.add_parser("scroll", help="scrolling capture of a window area")
    sub.add_parser("history", help="show the capture history")
    sub.add_parser("settings", help="open the settings")
    for name, what in (("edit", "open an image in the editor"), ("pin", "pin an image on the screen"),
                       ("upload", "upload a file with the chosen uploader")):
        p = sub.add_parser(name, help=what)
        p.add_argument("file")
    sub.add_parser("quit", help="quit the tray app")
    return ap


def _record_args(r):
    g = r.add_mutually_exclusive_group()
    g.add_argument("--gif", action="store_true", help="make a GIF")
    g.add_argument("--webp", action="store_true", help="make an animated WebP")
    r.add_argument("--last", action="store_true", help="record the same area / window as last time")
    r.add_argument("-o", "--output", help="output file (.mp4/.mkv/.gif/.webp) or directory")
    r.add_argument("-c", "--codec", choices=["hevc", "av1", "h264"],
                   help="video codec, hardware encoded via VAAPI (default: settings, hevc)")
    r.add_argument("-q", "--quality", choices=["high", "good", "small"], help="quality preset")
    r.add_argument("--qp", type=int, help="constant quantizer instead of capped VBR (overrides --quality)")
    r.add_argument("--fps", type=int, help="force a constant frame rate (default: native, variable)")
    r.add_argument("--no-audio", action="store_true", help="don't record desktop audio")
    r.add_argument("--mic", action="store_true", help="also record (mix in) the default microphone")
    r.add_argument("--no-cursor", action="store_true", help="don't record the mouse cursor")
    r.add_argument("--mkv", action="store_true", help="keep MKV instead of remuxing to MP4")
    r.add_argument("--clicks", action="store_true", help="show mouse clicks in the recording")
    r.add_argument("--keys", action="store_true", help="show pressed keys in the recording")
    r.add_argument("--delay", type=float, default=0, help="seconds to wait before recording")
    r.add_argument("--duration", type=float, help="stop automatically after N seconds")


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


def parse(argv):
    """-> argparse.Namespace (cmd set). Raises ArgError; prints help + raises SystemExit for -h."""
    ns = build_parser().parse_args(normalize(argv))
    if not ns.cmd:
        ns.cmd = "tray"
    return ns
