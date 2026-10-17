#!/bin/sh
# Puts `screenrec` on your PATH, adds the launcher entry with its shortcut actions
# (System Settings → Shortcuts → screenrec) and starts it with Plasma.
#   ./install.sh --no-autostart   to skip the autostart entry
set -e
here=$(dirname "$(readlink -f "$0")")
mkdir -p "$HOME/.local/bin"
ln -sf "$here/screenrec.sh" "$HOME/.local/bin/screenrec"
"$here/helper/build.sh" >/dev/null   # capture helper (also built automatically on first run)
PYTHONPATH="$here" python3 -c "from screenrec import hotkeys; hotkeys.install('$HOME/.local/bin/screenrec')"
if [ "$1" != "--no-autostart" ]; then
    PYTHONPATH="$here" python3 -c "from screenrec import app; app._set_autostart(True)" 2>/dev/null || true
fi
echo "installed:"
echo "  screenrec            start the tray app (also starts with Plasma now)"
echo "  screenrec --help     all commands (shot, record, color, ocr, scroll, …)"
echo "  Shortcuts: System Settings → Shortcuts → screenrec (no keys are set by default)"
for tool in ffmpeg tesseract; do
    command -v $tool >/dev/null || echo "  optional: install $tool"
done
