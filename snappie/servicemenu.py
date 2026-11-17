"""Snappie entries in Dolphin's right-click menu (KIO service menus).

Service menus are .desktop files in ~/.local/share/kio/servicemenus/.  One file can only
match one set of file types, so there is one file per group (any file / images / videos);
they share the submenu name, and Dolphin merges them into one "Snappie" submenu.  KIO only
runs service menus from the user's directory when the file is executable.

The commands go to the running tray app over D-Bus, or run on their own (one-shot).
"""

import os

from .hotkeys import launcher

DIR = os.path.join(os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")), "kio", "servicemenus")
PREFIX = "snappie-"

IMAGES = ["image/png", "image/jpeg", "image/webp", "image/bmp", "image/gif", "image/tiff", "image/avif"]
ANIMATIONS = ["image/gif", "image/webp"]
VIDEOS = ["video/mp4", "video/x-matroska", "video/webm", "video/quicktime", "video/x-msvideo", "video/mpeg"]

# file name -> (mime types, [(action id, label, icon, command line arguments)])
# %F: all selected files in one call; %f: one call per file.  Dolphin sorts a submenu by action id,
# not by file order, so the ids start with their position: Upload first.
MENUS = {
    "upload": (["all/allfiles"], [
        ("a-upload", "Upload", "cloud-upload", ["upload", "%F"]),
    ]),
    "image": (IMAGES, [
        ("b-edit", "Annotate", "edit-image", ["edit", "%f"]),
        ("c-ocr", "Copy Text (OCR)", "edit-select-text", ["ocr", "%f"]),
        ("d-pin", "Pin to Screen", "window-pin", ["pin", "%f"]),
    ]),
    "video": (VIDEOS, [
        ("b-video", "Cut and Crop", "edit-cut", ["edit", "%f"]),
        ("e-convert", "Convert…", "transform-move", ["convert", "%f"]),
        ("f-gif", "Convert to GIF", "image-gif", ["convert", "--gif", "%f"]),
        ("g-webp", "Convert to WebP", "viewimage", ["convert", "--webp", "%f"]),
    ]),
    "animation": (ANIMATIONS, [
        ("e-convert", "Convert…", "transform-move", ["convert", "%f"]),
    ]),
    "to-mp4": ([m for m in VIDEOS if m != "video/mp4"] + ANIMATIONS, [
        ("h-mp4", "Convert to MP4", "video-mp4", ["convert", "--mp4", "%f"]),
    ]),
    "gif": (["image/gif"], [
        ("g-webp", "Convert to WebP", "viewimage", ["convert", "--webp", "%f"]),
    ]),
    "webp": (["image/webp"], [
        ("f-gif", "Convert to GIF", "image-gif", ["convert", "--gif", "%f"]),
    ]),
}


def entry(mimes, actions, exe):
    lines = [
        "[Desktop Entry]",
        "Type=Service",
        "MimeType=" + ";".join(mimes) + ";",
        "Actions=" + ";".join(a for a, _, _, _ in actions) + ";",
        "X-KDE-Submenu=Snappie",
        "X-KDE-Priority=TopLevel",
        "",
    ]
    for aid, label, icon, args in actions:
        lines += [f"[Desktop Action {aid}]", f"Name={label}", f"Icon={icon}", f"Exec={exe} {' '.join(args)}", ""]
    return "\n".join(lines)


def install(exe=None):
    """Write the service menus (only when they changed). Returns True if anything was written."""
    exe = exe or launcher()
    changed = False
    os.makedirs(DIR, exist_ok=True)
    for stale in set(_ours()) - {f"{PREFIX}{name}.desktop" for name in MENUS}:   # from older versions
        os.unlink(os.path.join(DIR, stale))
        changed = True
    for name, (mimes, actions) in MENUS.items():
        path = os.path.join(DIR, f"{PREFIX}{name}.desktop")
        text = entry(mimes, actions, exe)
        try:
            with open(path) as f:
                same = f.read() == text
        except OSError:
            same = False
        if not same:
            with open(path, "w") as f:
                f.write(text)
            changed = True
        if not os.access(path, os.X_OK):
            os.chmod(path, 0o755)
            changed = True
    return changed


def _ours():
    try:
        return [f for f in os.listdir(DIR) if f.startswith(PREFIX) and f.endswith(".desktop")]
    except OSError:
        return []


def uninstall():
    for name in _ours():
        try:
            os.unlink(os.path.join(DIR, name))
        except FileNotFoundError:
            pass
