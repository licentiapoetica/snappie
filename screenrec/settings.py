"""User settings (JSON in ~/.config/screenrec/settings.json) and standard paths.

Keys are flat, dotted names.  `get` falls back to DEFAULTS, so new keys need no
migration.  Writes are merged into the file, so two processes changing
different keys don't undo each other.
"""

import json
import os
import subprocess
import time

CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "screenrec")
DATA_DIR = os.path.join(os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")), "screenrec")
SETTINGS = os.path.join(CONFIG_DIR, "settings.json")
UPLOADERS_DIR = os.path.join(CONFIG_DIR, "uploaders")    # imported ShareX .sxcu files
HISTORY_FILE = os.path.join(DATA_DIR, "history.jsonl")
THUMBS_DIR = os.path.join(DATA_DIR, "thumbs")

DEFAULTS = {
    # -- screenshots --
    "shot.dir": "",                       # "" = <Pictures>/screenrec
    "shot.filename": "screenshot_%Y-%m-%d_%H-%M-%S",   # strftime pattern, no extension
    "shot.format": "png",                 # png | jpg | webp
    "shot.quality": 92,                   # jpg / webp quality
    "shot.cursor": False,                 # include the mouse cursor
    "shot.annotate": True,                # show the in-place editor after picking
    "shot.after": ["copy", "save"],       # run on Enter in the editor, or right away without it
                                          # (any of: copy, save, upload, pin, ocr, edit)
    "shot.window_mode": "plain",          # plain | transparent | shadow (whole-window shots)
    "shot.magnifier": True,               # pixel loupe next to the cursor while picking

    # -- recording --
    "rec.dir": "",                        # "" = <Videos>/screenrec
    "rec.filename": "screenrec_%Y-%m-%d_%H-%M-%S",
    "rec.codec": "hevc",                  # hevc | av1 | h264
    "rec.quality": "good",                # high | good | small
    "rec.fps": 0,                         # 0 = native / variable
    "rec.audio": True,                    # desktop audio
    "rec.mic": False,
    "cursor": True,                       # record the cursor (key kept from older versions)
    "rec.mp4": True,                      # remux to MP4 (else keep MKV)
    "rec.after": [],                      # extra after recording: copy (path), upload
    "rec.clicks": False,                  # click ripples in the recording
    "rec.keys": False,                    # pressed keys in the recording
    "rec.draw_fade": 3.0,                 # seconds before a live drawing fades out (0 = keep)
    "rec.draw_tool": "pen",               # live drawing: last tool, color, width
    "rec.draw_color": "",                 # "" = annot.color
    "rec.draw_width": 0,                  # 0 = annot.width
    "rec.emoji": "😂",                     # live drawing: emoji stamp

    # -- GIF / WebP --
    "gif.format": "gif",                  # gif | webp
    "gif.fps": 15,
    "gif.max_width": 960,                 # 0 = keep the native width

    # -- uploads --
    "upload.uploader": "",                # file name in UPLOADERS_DIR, "" = none chosen
    "upload.copy_url": True,              # copy the URL after uploading
    "upload.trusted_certs": {},           # "host:port" -> SHA-256 of a trusted self-signed certificate

    # -- annotation defaults (the editor remembers the last used values) --
    "annot.color": "#e93d3d",
    "annot.width": 4,
    "annot.font_size": 18,
    "annot.font": "",                     # "" = system UI font
    "annot.text_bg": False,               # text on a filled box
    "annot.fill": False,                  # rect / ellipse filled
    "annot.tool": "arrow",                # last used tool
    "annot.tool_colors": {},              # tools that keep their own color (highlighter, redact, …)
    "annot.zoom": 2,                      # magnifier tool zoom
    "annot.frame": None,                  # last used editor frame (beautify) options
    "annot.emoji": "😂",                   # emoji stamp tool
    "annot.stamp": 64,                    # its size (logical px)
    "emoji.recent": [],                   # recently picked emojis (recordings and screenshots)

    # -- remembered between captures --
    "last.selection": None,               # see app.remember_selection
    "ocr.lang": "eng",

    # -- scrolling capture --
    "scroll.from_top": True,              # scroll to the top of the page first
    "scroll.max_height": 32000,           # stop once the image is this tall (device px)
    "scroll.step": 0.6,                   # scroll step, as a fraction of the area's height
}


def load():
    try:
        with open(SETTINGS) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def get(key, default=None):
    data = load()
    if key in data:
        return data[key]
    return DEFAULTS.get(key, default) if default is None else default


def save(**changes):
    """save(**{"shot.format": "jpg"}) — merges into the file."""
    data = dict(load(), **changes)
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = SETTINGS + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, SETTINGS)


def set(key, value):  # noqa: A001 - mirrors get()
    save(**{key: value})


def _xdg_dir(kind, fallback):
    try:
        d = subprocess.run(["xdg-user-dir", kind], capture_output=True, text=True).stdout.strip()
    except OSError:
        d = ""
    if not d or d == os.path.expanduser("~"):
        d = os.path.expanduser(fallback)
    return d


def pictures_dir():
    d = get("shot.dir")
    return os.path.expanduser(d) if d else os.path.join(_xdg_dir("PICTURES", "~/Pictures"), "screenrec")


def videos_dir():
    d = get("rec.dir")
    return os.path.expanduser(d) if d else os.path.join(_xdg_dir("VIDEOS", "~/Videos"), "screenrec")


def unique_path(directory, pattern, ext):
    """<directory>/<strftime(pattern)>.<ext>, with -2, -3, … appended if it exists."""
    os.makedirs(directory, exist_ok=True)
    base = os.path.join(directory, time.strftime(pattern))
    path, n = f"{base}.{ext}", 2
    while os.path.exists(path):
        path, n = f"{base}-{n}.{ext}", n + 1
    return path


def screenshot_path(ext=None):
    return unique_path(pictures_dir(), get("shot.filename"), ext or get("shot.format"))
