#!/bin/sh
# Puts `screenrec` on your PATH and adds an app-launcher entry (KDE menu / KRunner).
set -e
here=$(dirname "$(readlink -f "$0")")
mkdir -p "$HOME/.local/bin" "$HOME/.local/share/applications"
ln -sf "$here/screenrec.sh" "$HOME/.local/bin/screenrec"
"$here/helper/build.sh" >/dev/null   # capture helper (also built automatically on first run)
cat > "$HOME/.local/share/applications/screenrec.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=screenrec
GenericName=Screen Recorder
Comment=Pick a window, or a part of one, and record it
Exec=$HOME/.local/bin/screenrec
Icon=media-record
Categories=AudioVideo;Recorder;
Actions=stop;

[Desktop Action stop]
Name=Stop recording
Exec=$HOME/.local/bin/screenrec --stop
EOF
echo "installed: run 'screenrec' or search 'screenrec' in the launcher"
