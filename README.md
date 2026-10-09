# screenrec

Pick a screen, a window, or a part of a window (a panel, a terminal pane,
a browser tab's page area, ...) and record it with sound. Made for KDE Plasma 6
on Wayland. There's no screen-share dialog.

```sh
./install.sh          # optional: puts `screenrec` on PATH + launcher entry
screenrec             # or ./screenrec.sh
```

## Picking

The screen dims and the thing under the cursor gets highlighted:

| input             | action                                                     |
|-------------------|------------------------------------------------------------|
| scroll / ↑ ↓      | go deeper (window → content → areas → …) or back up        |
| Tab               | switch inner detection: accessibility tree ↔ from pixels   |
| click / Enter     | record the highlighted area                                |
| drag              | record a free rectangle                                    |
| Esc / right click | cancel                                                     |

Stop with a click on the tray icon, Ctrl+C, or `screenrec --stop` (bind it to a
shortcut in System Settings → Shortcuts). Files go to `~/Videos/screenrec/`.

## Inner areas

There are two ways to find what's inside a window:

- **From pixels** (default for Chromium, Electron, and anything without an
  accessibility tree): the window is captured once and split along strong
  straight edges. For Chromium, the area below the bookmarks bar is the tab's
  page, two scroll steps below the window.
- **Accessibility tree** (default for KDE/Qt and GTK apps): real widgets with
  names. Chromium has one too if started with `--force-renderer-accessibility`.

## Capture

The tool records through KWin's own screen-cast API, the same one Spectacle
uses. A tiny helper binary (`helper/`) is built on first run (needs gcc and
wayland-devel). It's authorized through a hidden `.desktop` entry, so KWin
never shows a dialog.

- **Window or anything inside one**: the window's own stream. It stays correct
  when other windows cover it, and it follows the window when you move it.
  Recording stops when the window closes.
- **Screen or a dragged region outside a window**: a region stream of the
  desktop.

## Output

- Native resolution, no scaling. Native frame rate (up to the monitor's
  refresh rate), with real frame timing; `--fps 60` forces a constant rate.
- HEVC through VAAPI, capped VBR: a bitrate ceiling sized to the area keeps
  games and video small, and a quality floor keeps static content crisp.
  Audio is Opus 192 kbps from the desktop (`--mic` mixes in the microphone,
  `--no-audio` turns sound off).
- Recorded as MKV, so a crash won't lose the file, then remuxed losslessly to
  MP4.
- `--quality high|good|small` changes the ceiling. `--qp N` uses a constant
  quantizer instead.
- `-c h264` is the most compatible. `-c av1` makes smaller files, but AMD can
  only encode AV1 in 64×16 blocks, so the area is rounded to that.
