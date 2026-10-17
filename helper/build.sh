#!/bin/sh
# Builds snappie-cast and authorizes it with KWin via a hidden .desktop file.
set -e
src=$(dirname "$(readlink -f "$0")")
dest="${XDG_DATA_HOME:-$HOME/.local/share}/snappie"
apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
build=$(mktemp -d)
trap 'rm -rf "$build"' EXIT

for proto in zkde-screencast-unstable-v1 fake-input; do
    wayland-scanner client-header "$src/$proto.xml" "$build/$proto-client-protocol.h"
    wayland-scanner private-code "$src/$proto.xml" "$build/$proto-protocol.c"
done
mkdir -p "$dest" "$apps"
# build next to the target and rename, so a running helper is never overwritten mid-write
cc -O2 -Wall -I"$build" $(pkg-config --cflags wayland-client) \
    "$src/snappie-cast.c" "$build/zkde-screencast-unstable-v1-protocol.c" "$build/fake-input-protocol.c" \
    $(pkg-config --libs wayland-client) -o "$dest/snappie-cast.new"
mv -f "$dest/snappie-cast.new" "$dest/snappie-cast"

cat > "$apps/snappie-cast.desktop" <<EOD
[Desktop Entry]
Type=Application
Name=snappie capture helper
Exec=$dest/snappie-cast
NoDisplay=true
X-KDE-Wayland-Interfaces=zkde_screencast_unstable_v1,org_kde_kwin_fake_input
EOD
kbuildsycoca6 >/dev/null 2>&1 || true
echo "$dest/snappie-cast"
