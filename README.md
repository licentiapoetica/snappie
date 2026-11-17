<p align="center"><img src="snappie/icons/snappie.svg" width="128" alt="Snappie logo: a magpie"></p>

# Snappie

*Snap it like a magpie.* Screenshots, annotation, screen recording and sharing for KDE Plasma 6 on
Wayland, like ShareX on Windows. Pick a screen, a window, or a part of a
window (a panel, a terminal pane, a browser tab's page area, …), then annotate,
copy, save, upload, pin or record it. There's no screen-share dialog.

```sh
./install.sh          # puts `snappie` on PATH, launcher + shortcut entries, autostart
snappie               # starts the tray app
snappie --help        # all commands
```

## Requirements

KDE Plasma 6 (Wayland), Python 3 with PyQt6, dbus-python, PyGObject, NumPy and
Requests, GStreamer with the PipeWire, base, good and VA plugins, ffmpeg, and
the Wayland client library. Source installs also need gcc + Wayland headers
to build the capture helper. Optional: tesseract (OCR) and
python-evdev (clicks and keys in recordings).

`snappie deps` checks all of it and prints the install command for your distro;
`install.sh` runs it first. On Arch and its derivatives:

```sh
sudo pacman -S --needed python-pyqt6 qt6-svg python-dbus python-gobject python-numpy \
    python-requests gstreamer gst-plugin-pipewire gst-plugins-base gst-plugins-good \
    gst-plugin-va gst-plugins-bad-libs ffmpeg wayland
sudo pacman -S --needed gcc pkgconf   # source installs (including install.sh)
sudo pacman -S --needed tesseract tesseract-data-eng python-evdev   # optional
```

On openSUSE:

```sh
sudo zypper install python3-PyQt6 python3-dbus-python python3-gobject python3-numpy \
    python3-requests typelib-1_0-Gst-1_0 gstreamer-plugin-pipewire gstreamer-plugins-base \
    gstreamer-plugins-good gstreamer-plugins-bad ffmpeg libwayland-client0
sudo zypper install gcc pkgconf-pkg-config wayland-devel   # source installs
sudo zypper install tesseract-ocr tesseract-ocr-traineddata-english python3-evdev   # optional
```

## Python package installation

Snappie can also be installed as a Python package, independently of the checkout.
Install the system packages above first, then create a virtual environment that
can use your distro's Python libraries:

```sh
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install --no-deps .
.venv/bin/snappie --help
.venv/bin/snappie deps
```

Installing from source builds the C capture helper once during packaging. The
resulting wheel includes the `snappie` command, icons and compiled helper.
Installing that wheel needs no compiler, pkg-config or Wayland headers:

```sh
.venv/bin/python -m pip install --no-deps /path/to/snappie-0.1.0-py3-none-linux_x86_64.whl
```

Use a wheel built for your CPU architecture and a compatible Linux system; the
helper links to the system's Wayland client library and libc. KDE Plasma,
GStreamer plugins, ffmpeg and optional Tesseract remain system dependencies.
This package does not bundle Python or those tools.

To register the launcher and global shortcut actions for this installation:

```sh
.venv/bin/python -c 'from snappie import hotkeys; hotkeys.install()'
```

Start `.venv/bin/snappie` and enable **Start with Plasma** in the tray menu if desired.
Keep the virtual environment at its installed path; the desktop entries point to it.
You can remove the source checkout after installation if the virtual environment
is stored elsewhere. `install.sh` remains the checkout-based alternative.

For development, use `pip install --no-deps -e .` in that environment. The optional
`input` extra installs evdev for click/key visualization; `dev` provides pytest and
the build frontend. To produce a wheel and source archive, install `build` and run
`python -m build` with gcc, pkg-config, wayland-scanner and Wayland headers available.
Builds do not register desktop entries or write to your user data directory.

## Tray and shortcuts

`snappie` runs in the tray. Click: screenshot. Middle click: record. Right
click: everything else (delays, last area, GIF, OCR, color picker, scrolling
capture, history, settings, start with Plasma).

Every action is also a command, and a global shortcut you can assign in
**System Settings → Shortcuts → snappie** (none are set by default):

| command                     | does                                                  |
|-----------------------------|-------------------------------------------------------|
| `snappie shot`            | screenshot: pick an area                              |
| `shot --screen / --window`  | the screen under the cursor / the active window       |
| `shot --last`               | the same area again (follows the window if it moved)  |
| `shot --delay 5`            | countdown first (for menus and tooltips)             |
| `shot --then copy,save,…`   | skip the editor: copy, save, upload, pin, ocr, edit   |
| `snappie record`          | record an area; run again to stop                     |
| `record --gif` / `--webp`   | record an animated GIF / WebP                         |
| `record --last`             | record the same area again                            |
| `pause`, `draw`, `stop`     | control the running recording                         |
| `color`                     | pick a color, copies its hex code                     |
| `ocr`                       | pick an area, copies its text (needs tesseract)       |
| `ocr FILE`                  | copies the text of an image file                      |
| `scroll`                    | scrolling capture of a window area                    |
| `history`, `settings`       | windows                                               |
| `edit FILE`, `pin FILE`     | open an image in the editor, pin it to the screen     |
| `upload FILE…`              | upload files, one after another                       |
| `upload -u NAME FILE…`      | upload with another uploader (name or `.sxcu` path)   |
| `convert --gif/--webp FILE` | turn a video into an animated GIF / WebP next to it   |
| `deps`                      | check what is installed, print the install command    |

When the tray app is running, commands are handed to it instantly. Otherwise
the command runs on its own and the process exits when done.

**Dolphin:** right-click files → **Snappie** to upload them, annotate or pin
an image, copy the text in an image, or turn a video into a GIF or WebP. Turn
this menu off in Settings → Hotkeys.

## Picking

For screenshots, the screen freezes. For recordings, it dims. The smallest
area under the cursor is highlighted, and a click takes it. Scroll up for
bigger ones, or point at the title bar to take the whole window:

| input             | action                                                     |
|-------------------|------------------------------------------------------------|
| scroll / ↑ ↓      | bigger (area → … → content → window → screen) or smaller   |
| Tab               | switch inner detection: accessibility tree ↔ from pixels   |
| click / Enter     | take the highlighted area                                  |
| drag              | a free rectangle                                           |
| F                 | bring a covered window to the front; again: the one behind |
| M                 | magnifier (pixel loupe with the color under the cursor)    |
| W                 | screenshots of a whole window: plain / transparent / shadow |
| C                 | recordings: include the mouse cursor                       |
| Esc / right click | cancel                                                     |

**Transparent** and **shadow** window shots use the window's own pixels:
rounded corners stay transparent, it's correct even when other windows cover
it, and *shadow* adds a soft drop shadow around it.

## Annotating

After picking, a toolbar appears next to the area on the frozen screen:
arrow, line, rectangle, ellipse, text, pen, highlighter, marker, blur,
pixelate, redact, numbered steps, magnifier and crop. Shapes stay editable, and
the area can still be moved and resized. Drag the toolbar by its grip (or any
gap between buttons) to put it anywhere; double-click it to bring it back. Enter runs the after-capture actions
(default: copy + save). Esc cancels. The editor button opens a full editor
window with zoom, canvas crop and a frame panel (padding, background, rounded
corners, shadow).

## After capture

Copy, save (`~/Pictures/Snappie`), upload, pin to the screen (always on top;
wheel zooms), and OCR. Pick the defaults in Settings. Every capture lands in the
**history**, which shows thumbnails and lets you open, copy, upload, edit, pin
or delete.

**Uploads** use ShareX custom uploaders: import any `.sxcu` file (Zipline,
XBackBone, catbox, …) in Settings → Uploads. The link is copied when done.
Several files upload one after another; when they're done, all the links are
copied. While uploading, the tray icon shows the progress, and Plasma shows it
in its notifications with speed, time left and a Cancel button. You can also
cancel from the tray menu.

## Recording

While recording, a small bar sits under the area: timer, pause, draw, stop.

- **Pause** leaves the paused time out of the file.
- **Draw** lets you draw on the area (pen; Shift for arrows, R for boxes,
  1–8 colors, wheel width, Z undo, C clear, Esc done). Drawings fade out.
- **Click and key visualizer** (Settings or `--clicks --keys`) shows mouse
  clicks as ripples and pressed keys as a bubble. It reads the input devices,
  so your user must be in the `input` group.

Stop with the bar, the tray icon, Ctrl+C, or `snappie stop`. Files go to
`~/Videos/Snappie/`.

## Capture

The tool records through KWin's own screen-cast API, the same one Spectacle
uses. A tiny helper binary (sources in `snappie/helper/`) is compiled into wheels.
Source checkouts and editable installs build it on demand and rebuild when its
sources change. On startup, Snappie puts the helper at a stable location in the
user data directory and authorizes it through a hidden `.desktop` entry, so KWin
never shows a dialog. Missing desktop entries are recreated automatically.
The same helper drives the auto-scrolling for scrolling capture, through KWin's
fake-input protocol.

- **Window or anything inside one**: the window's own stream. It stays correct
  when other windows cover it, and it follows the window when you move it.
  Recording stops when the window closes.
- **Screen or a dragged region outside a window**: a region stream of the
  desktop.

## Inner areas

There are two ways to find what's inside a window:

- **From pixels** (default for Chromium, Electron, and anything without an
  accessibility tree): the window is captured once and split along strong
  straight edges.
- **Accessibility tree** (default for KDE/Qt and GTK apps): real widgets with
  names. Chromium has one too if started with `--force-renderer-accessibility`.

## Output

- Native resolution, no scaling. Native frame rate (up to the monitor's
  refresh rate), with real frame timing; `--fps 60` forces a constant rate.
- HEVC through VAAPI, capped VBR: a bitrate ceiling sized to the area keeps
  games and video small, and a quality floor keeps static content crisp.
  Audio is Opus 192 kbps from the desktop (`--mic` mixes in the microphone,
  `--no-audio` turns sound off).
- Recorded as MKV, so a crash won't lose the file, then remuxed losslessly to
  MP4 (or WebM with `--container webm`, or kept as MKV with `--mkv`).
- GIF uses one optimized palette for the whole clip (`gif.fps`, `gif.max_width`
  in Settings). WebP is smaller and has full color.
- `--quality high|good|small` changes the ceiling. `--qp N` uses a constant
  quantizer instead.
- `-c h264` is the most compatible. `-c av1` makes smaller files, but AMD can
  only encode AV1 in 64×16 blocks, so the area is rounded to that.
- `-c vp9` and `-c vp8` are for WebM, which browsers and chat apps play
  directly. Only Intel GPUs encode VP9; elsewhere VP9 and VP8 run on the CPU
  (libvpx, in `gst-plugins-good`), which is heavier for big areas. WebM holds
  only VP9, VP8 and AV1, so other codecs record as VP9 there. VP8 can't go in
  MP4, so it's saved as WebM.

## Files

Settings: `~/.config/snappie/settings.json`; uploaders:
`~/.config/snappie/uploaders/`; history and thumbnails:
`~/.local/share/snappie/`.

Coming from the old name (`screenrec`)? The first start of Snappie moves your
settings, uploaders, history and shortcuts over and removes the old entries.

## License

MIT, see [LICENSE](LICENSE).
