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
# Link the command into a folder the shell already searches. Most distros put ~/.local/bin on
# PATH; Arch and some others don't, but /usr/local/bin is on every default PATH (needs root).
on_path() { case ":$PATH:" in *":$1:"*|*":$1/:"*) return 0 ;; esac; return 1; }
bin="$HOME/.local/bin"
if ! on_path "$bin"; then
    if [ -w /usr/local/bin ]; then
        ln -sf "$here/snappie.sh" /usr/local/bin/snappie && bin=/usr/local/bin
    elif command -v sudo >/dev/null 2>&1; then
        echo "~/.local/bin isn't on your PATH; linking snappie into /usr/local/bin (asks for sudo)"
        sudo ln -sf "$here/snappie.sh" /usr/local/bin/snappie && bin=/usr/local/bin
    fi
fi
if [ "$bin" = "$HOME/.local/bin" ]; then
    mkdir -p "$bin"
    ln -sf "$here/snappie.sh" "$bin/snappie"
fi
exe="$bin/snappie"
"$here/helper/build.sh" >/dev/null   # capture helper (also built automatically on first run)
PYTHONPATH="$here" python3 -c "from snappie import hotkeys; hotkeys.install('$exe')"
PYTHONPATH="$here" python3 -c "from snappie import servicemenu; servicemenu.install('$exe')"
share=$(PYTHONPATH="$here" python3 -m snappie.share_plugin "$exe" 2>&1 || true)
if [ "$1" != "--no-autostart" ]; then
    PYTHONPATH="$here" python3 -c "from snappie import app; app._set_autostart(True)" 2>/dev/null || true
fi
echo "installed $exe:"
echo "  snappie            start the tray app (also starts with Plasma now)"
echo "  snappie --help     all commands (shot, record, color, ocr, scroll, …)"
echo "  Shortcuts: System Settings → Shortcuts → snappie (no keys are set by default)"
echo "  Dolphin:   right-click a file → Snappie (upload, annotate, pin, OCR, convert)"
echo "  Share:     $share"
if ! on_path "$bin"; then
    echo "Note: $bin isn't on your PATH, so the shell won't find \`snappie\` yet. Add" >&2
    echo "  export PATH=\"\$HOME/.local/bin:\$PATH\"" >&2
    echo "to ~/.bashrc (or your shell's startup file), or run ./install.sh again with sudo available." >&2
fi
