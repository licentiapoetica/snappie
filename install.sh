#!/bin/sh
# Puts `snappie` on your PATH, adds the launcher entry with its shortcut actions
# (System Settings → Shortcuts → snappie) and starts it with Plasma.
#   ./install.sh --no-autostart   to skip the autostart entry
set -e
here=$(dirname "$(readlink -f "$0")")
if ! "$here/snappie.sh" deps; then
    echo "Install the packages above, then run ./install.sh again." >&2
    exit 1
fi
mkdir -p "$HOME/.local/bin"
ln -sf "$here/snappie.sh" "$HOME/.local/bin/snappie"
"$here/helper/build.sh" >/dev/null   # capture helper (also built automatically on first run)
PYTHONPATH="$here" python3 -c "from snappie import hotkeys; hotkeys.install('$HOME/.local/bin/snappie')"
if [ "$1" != "--no-autostart" ]; then
    PYTHONPATH="$here" python3 -c "from snappie import app; app._set_autostart(True)" 2>/dev/null || true
fi
echo "installed:"
echo "  snappie            start the tray app (also starts with Plasma now)"
echo "  snappie --help     all commands (shot, record, color, ocr, scroll, …)"
echo "  Shortcuts: System Settings → Shortcuts → snappie (no keys are set by default)"
