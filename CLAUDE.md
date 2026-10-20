# Snappie

ShareX-style screenshots, annotation, screen recording and uploads for **KDE Plasma 6 on
Wayland only**. Python 3 + PyQt6, GStreamer for capture/encoding, a tiny C helper to talk
to KWin's private Wayland protocols. Formerly called `screenrec` (renamed in 2153145).
MIT licensed. User-facing docs live in [README.md](README.md); keep it in sync when
features, commands or settings change.

## Running

```sh
./snappie.sh              # start the tray app from the checkout (same as `snappie` once installed)
./snappie.sh shot         # any command; forwarded to the running tray app if there is one
./install.sh              # symlink into ~/.local/bin, build helper, shortcut entries, autostart
```

There is no build step for the Python side and no packaging (no pyproject). `snappie.sh`
just sets `PYTHONPATH` and runs `python3 -m snappie`. If the tray app is already running,
a new invocation only forwards its argv over D-Bus, so **restart the tray app**
(`snappie quit`) to test code changes.

Logs: stderr and `~/.local/state/snappie/snappie.log`.

Runtime deps: python3-pyqt6 (incl. QtSvg), python3-dbus, PyGObject with GStreamer +
GLib, numpy, gstreamer `pipewiresrc` and VA plugins (`vah265enc`/`vah264enc`/`vaav1enc`,
`vapostproc`), gcc + wayland-devel + wayland-scanner (helper build), ffmpeg (GIF/WebP),
optional tesseract (OCR), Noto Color Emoji font. libatspi via gi for the a11y picker.

## Layout

| path | what |
|---|---|
| `snappie/__main__.py` | entry: parse, forward to running instance over D-Bus, else become the app |
| `snappie/cli.py` | argparse; must stay free of heavy imports (hotkey fast path) |
| `snappie/app.py` | `App`: tray, command dispatch (`_run`), capture flows, one-shot vs daemon lifetime |
| `snappie/kwin.py` | KWin scripting over D-Bus (JS strings), owns the `io.github.snappie` service |
| `snappie/cast.py` | runs `snappie-cast`, PipeWire node → GStreamer frames; builds helper on demand |
| `helper/snappie-cast.c` | C helper: `zkde_screencast_unstable_v1` streams + `org_kde_kwin_fake_input` |
| `snappie/recorder.py` | GStreamer pipeline: pipewiresrc → crop → VAAPI encode + Opus audio → MKV |
| `snappie/recording.py` | one recording: segments (pause/reconnect), join, remux MP4 / convert GIF/WebP |
| `snappie/picker.py` | full-screen overlay to pick screen / window / inner area |
| `snappie/a11y.py`, `snappie/visual.py` | inner areas from the AT-SPI tree, or from pixel edges |
| `snappie/annotate/` | annotation: `Document` + `Canvas` engine, two hosts (`inplace.py` overlay, `window.py` editor) |
| `snappie/livedraw.py` | drawing/click ripples/key bubbles on recordings; reads `/dev/input` (evdev) |
| `snappie/scrollcap.py` | scrolling capture; `Stitcher` is plain numpy (testable offline) |
| `snappie/upload.py` | ShareX `.sxcu` custom uploaders (incl. self-signed cert trust) |
| `snappie/ocr.py` | tesseract CLI wrapper with layout heuristics |
| `snappie/history.py` | `history.jsonl` + history window |
| `snappie/settings.py`, `settings_ui.py` | flat dotted keys with `DEFAULTS`; settings dialog |
| `snappie/hotkeys.py` | `.desktop` actions + kglobalaccel (System Settings → Shortcuts) |
| `snappie/migrate.py` | one-time move from `screenrec` names; safe to run every start |
| `snappie/controls.py`, `osd.py`, `pin.py` | recording bar, countdown/scroll bar, pinned images |
| `snappie/style.py`, `icons.py`, `emoji.py` | shared colors, magpie icons, emoji stamps |

## Architecture and constraints

- **Single instance over D-Bus.** The first process owns `io.github.snappie`; later
  `snappie <cmd>` calls forward argv to `run()` and exit. Started as `tray` it's a
  daemon; started by any other command it's a one-shot that quits when idle (but lingers
  while it owns clipboard data, since Wayland clipboard lives in the copying process).
- **Event loops.** Qt runs on the GLib dispatcher; dbus-python uses `DBusGMainLoop`,
  GStreamer and helper I/O use GLib sources. All share one main loop. Qt objects must be
  touched on the GUI thread only; worker threads exist for frame grabs, OCR, uploads,
  thumbnails.
- **No portal, no dialog.** Capture goes through KWin's own screencast protocol via
  `snappie-cast`, authorized by a hidden `snappie-cast.desktop` with
  `X-KDE-Wayland-Interfaces`. It must be a real binary (not a script). `cast.ensure_helper`
  rebuilds it when `snappie-cast.c` is newer than the installed binary.
- **Wayland apps can't place themselves.** Every overlay/bar/pin is positioned by a KWin
  script that finds our window **by pid + exact window title**. So:
  - Window titles (`TITLE = "Snappie — …"`) are load-bearing; don't change them casually.
  - Never call `setApplicationDisplayName` (Qt would append it to every title).
- **Coordinates.** KWin geometry is in logical pixels (rounded to ints); images are device
  pixels with `devicePixelRatio` set. Fractional scaling matters: effects in annotate
  work on device pixels and draw back pixel-aligned. AT-SPI "screen" coords are garbage on
  Wayland; only window-relative coords are used (see `a11y.py` docstring).
- **AT-SPI is fragile.** libatspi `abort()`s the whole process if the a11y bus is dead, so
  `a11y.bus_usable` checks the socket before loading it. Qt's own atspi logging is muted.
- **PyQt aborts on exceptions in slots**; `__main__` installs an excepthook that logs instead.
- **Window vs region streams.** Window recordings use the window's own stream (correct when
  covered, follows moves, overlays not included → `livedraw.render_layer()` is
  composited into the video). Region recordings capture whatever is on screen.
- **Encoding.** Capped VBR (bitrate ceiling from area + QP floor), `target-usage=1`.
  Colorimetry hack in `recorder.GPU_CONVERT` keeps colors exact; don't "simplify" it.
  AV1 on AMD pads to 64×16, so the area is rounded. Records MKV first (crash-safe),
  then lossless remux.
- **Settings** are flat dotted keys; `get` falls back to `DEFAULTS` so new keys need no
  migration; `save` merges into the file (multiple processes are fine).

## Audience

Snappie is meant for **public use by KDE Plasma 6 Wayland users**, not only the dev
machine. Don't assume AMD/VAAPI or openSUSE: handle Intel and NVIDIA, missing optional
tools (ffmpeg, tesseract, VA encoders, a11y), other distros' package names, and fail with a
clear message instead of a crash. X11 and non-KDE desktops are out of scope.

## Testing

Use **pytest** with tests in `tests/`. Write tests for new or changed logic that doesn't
need a live desktop: `scrollcap.Stitcher`, `visual.py`, `upload.py` (.sxcu parsing and
`{json:…}`/`{regex:…}` syntax), `ocr` layout, `settings`, `cli` parsing, `migrate`
(with temp dirs via `XDG_*` env vars). Anything touching KWin, PipeWire or real windows
is verified by running the app on the live Plasma session.

**Never start nested/virtual sessions (`kwin_wayland --virtual`, extra dbus/at-spi) with the
real `XDG_RUNTIME_DIR` (`/run/user/1000`).** A test once replaced the user's AT-SPI socket
and crashed the desktop's a11y. Use a private `XDG_RUNTIME_DIR`, and afterwards check
`/run/user/1000/at-spi/bus_0` still works (`systemctl --user restart
at-spi-dbus-bus.service` fixes it).

## Conventions

- Code style: plain, readable Python; module docstrings explain the *why* (especially
  Wayland/KWin workarounds); comments are short and explain non-obvious constraints.
  ~110-char lines. No type hints, no linter config.
- Keep `cli.py` and the `_forward` path in `__main__` import-light.
- README is user-facing and written in short, plain sentences; update it with features.
- Git: Gitea at `git.lat`, repo `kibi/snappie` (`origin`). The `screenrecorder` remote is
  the obsolete pre-rename repo; never push there.
- Work on a feature branch and open a Gitea PR into `master` (like `sharex-features`);
  don't commit straight to `master`. Commit subjects are short summaries of user-visible
  changes.
- Dev machine: AMD RDNA3 GPU (VAAPI), openSUSE, Plasma 6 Wayland. Only one of the setups
  to support (see Audience).
