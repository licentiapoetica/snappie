# screenrec

Screenshots, annotation, screen recording and sharing for KDE Plasma 6 on
Wayland, like ShareX on Windows. Pick a screen, a window, or a part of a
window (a panel, a terminal pane, a browser tab's page area, …), then annotate,
copy, save, upload, pin or record it. There's no screen-share dialog.

```sh
./install.sh          # puts `screenrec` on PATH, launcher + shortcut entries, autostart
screenrec             # starts the tray app
screenrec --help      # all commands
```

## Tray and shortcuts

`screenrec` runs in the tray. Click: screenshot. Middle click: record. Right
click: everything else (delays, last area, GIF, OCR, color picker, scrolling
capture, history, settings, start with Plasma).

Every action is also a command, and a global shortcut you can assign in
**System Settings → Shortcuts → screenrec** (none are set by default):

| command                     | does                                                  |
|-----------------------------|-------------------------------------------------------|
| `screenrec shot`            | screenshot: pick an area                              |
| `shot --screen / --window`  | the screen under the cursor / the active window       |
| `shot --last`               | the same area again (follows the window if it moved)  |
| `shot --delay 5`            | countdown first (for menus and tooltips)             |
| `shot --then copy,save,…`   | skip the editor: copy, save, upload, pin, ocr, edit   |
| `screenrec record`          | record an area; run again to stop                     |
| `record --gif` / `--webp`   | record an animated GIF / WebP                         |
| `record --last`             | record the same area again                            |
| `pause`, `draw`, `stop`     | control the running recording                         |
| `color`                     | pick a color, copies its hex code                     |
| `ocr`                       | pick an area, copies its text (needs `tesseract-ocr`) |
| `scroll`                    | scrolling capture of a window area                    |
| `history`, `settings`       | windows                                               |
| `edit / pin / upload FILE`  | open an image in the editor, pin it, upload it        |

When the tray app is running, commands are handed to it instantly. Otherwise
the command runs on its own and the process exits when done.

## Picking

For screenshots, the screen freezes. For recordings, it dims. The thing under
the cursor is highlighted:

| input             | action                                                     |
|-------------------|------------------------------------------------------------|
| scroll / ↑ ↓      | go deeper (window → content → areas → …) or back up        |
| Tab               | switch inner detection: accessibility tree ↔ from pixels   |
| click / Enter     | take the highlighted area                                  |
| drag              | a free rectangle                                           |
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
the area can still be moved and resized. Enter runs the after-capture actions
(default: copy + save). Esc cancels. The editor button opens a full editor
window with zoom, canvas crop and a frame panel (padding, background, rounded
corners, shadow).

## After capture

Copy, save (`~/Pictures/screenrec`), upload, pin to the screen (always on top;
wheel zooms), and OCR. Pick the defaults in Settings. Every capture lands in the
**history**, which shows thumbnails and lets you open, copy, upload, edit, pin
or delete.

**Uploads** use ShareX custom uploaders: import any `.sxcu` file (Zipline,
XBackBone, catbox, …) in Settings → Uploads. The link is copied when done.

## Recording

While recording, a small bar sits under the area: timer, pause, draw, stop.

- **Pause** leaves the paused time out of the file.
- **Draw** lets you draw on the area (pen; Shift for arrows, R for boxes,
  1–8 colors, wheel width, Z undo, C clear, Esc done). Drawings fade out.
- **Click and key visualizer** (Settings or `--clicks --keys`) shows mouse
  clicks as ripples and pressed keys as a bubble. It reads the input devices,
  so your user must be in the `input` group.

Stop with the bar, the tray icon, Ctrl+C, or `screenrec stop`. Files go to
`~/Videos/screenrec/`.

## Capture

The tool records through KWin's own screen-cast API, the same one Spectacle
uses. A tiny helper binary (`helper/`) is built on first run (needs gcc and
wayland-devel). It's authorized through a hidden `.desktop` entry, so KWin
never shows a dialog. The same helper drives the auto-scrolling for scrolling
capture, through KWin's fake-input protocol.

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
  MP4.
- GIF uses one optimized palette for the whole clip (`gif.fps`, `gif.max_width`
  in Settings). WebP is smaller and has full color.
- `--quality high|good|small` changes the ceiling. `--qp N` uses a constant
  quantizer instead.
- `-c h264` is the most compatible. `-c av1` makes smaller files, but AMD can
  only encode AV1 in 64×16 blocks, so the area is rounded to that.

## Files

Settings: `~/.config/screenrec/settings.json`; uploaders:
`~/.config/screenrec/uploaders/`; history and thumbnails:
`~/.local/share/screenrec/`.
