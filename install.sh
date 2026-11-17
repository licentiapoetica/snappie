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
# Link the command into ~/.local/bin. Most distros put it on PATH; Arch and some others don't,
# so then add it to the user's shell startup file once (like rustup and pnpm do), not via sudo.
on_path() { case ":$PATH:" in *":$1:"*|*":$1/:"*) return 0 ;; esac; return 1; }
bin="$HOME/.local/bin"
mkdir -p "$bin"
ln -sf "$here/snappie.sh" "$bin/snappie"
rc=
if ! on_path "$bin"; then
    case $(basename "${SHELL:-sh}") in
        zsh)  rc="${ZDOTDIR:-$HOME}/.zshrc"; line='export PATH="$HOME/.local/bin:$PATH"' ;;
        fish) rc="${XDG_CONFIG_HOME:-$HOME/.config}/fish/conf.d/snappie.fish"
              line='fish_add_path -g $HOME/.local/bin' ;;
        *)    rc="$HOME/.bashrc"; line='export PATH="$HOME/.local/bin:$PATH"' ;;
    esac
    if ! grep -qsF "$line" "$rc"; then
        mkdir -p "$(dirname "$rc")"
        printf '\n# added by snappie install.sh: commands in ~/.local/bin\n%s\n' "$line" >> "$rc"
    fi
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
if [ -n "$rc" ]; then
    echo "~/.local/bin is now on your PATH (set in $rc). Open a new terminal to use \`snappie\`."
fi
