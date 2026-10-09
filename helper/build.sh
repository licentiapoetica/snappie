#!/bin/sh
# Builds screenrec-cast and authorizes it with KWin via a hidden .desktop file.
set -e
src=$(dirname "$(readlink -f "$0")")
dest="${XDG_DATA_HOME:-$HOME/.local/share}/screenrec"
apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
build=$(mktemp -d)
trap 'rm -rf "$build"' EXIT

proto="$src/zkde-screencast-unstable-v1.xml"
wayland-scanner client-header "$proto" "$build/zkde-screencast-unstable-v1-client-protocol.h"
wayland-scanner private-code "$proto" "$build/zkde-screencast-unstable-v1-protocol.c"
mkdir -p "$dest" "$apps"
cc -O2 -Wall -I"$build" $(pkg-config --cflags wayland-client) \
    "$src/screenrec-cast.c" "$build/zkde-screencast-unstable-v1-protocol.c" \
    $(pkg-config --libs wayland-client) -o "$dest/screenrec-cast"

cat > "$apps/screenrec-cast.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=screenrec capture helper
Exec=$dest/screenrec-cast
NoDisplay=true
X-KDE-Wayland-Interfaces=zkde_screencast_unstable_v1
EOF
kbuildsycoca6 >/dev/null 2>&1 || true
echo "$dest/screenrec-cast"
