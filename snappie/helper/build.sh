#!/bin/sh
# Compile only: build.sh OUTPUT. Desktop registration is handled at runtime.
set -e
if [ "$#" -ne 1 ]; then
    echo "usage: $0 OUTPUT" >&2
    exit 2
fi
src=$(dirname "$(readlink -f "$0")")
build=$(mktemp -d)
trap 'rm -rf "$build"' EXIT

for proto in zkde-screencast-unstable-v1 fake-input; do
    wayland-scanner client-header "$src/$proto.xml" "$build/$proto-client-protocol.h"
    wayland-scanner private-code "$src/$proto.xml" "$build/$proto-protocol.c"
done
${CC:-cc} ${CPPFLAGS:-} ${CFLAGS:--O2} -Wall -I"$build" $(pkg-config --cflags wayland-client) \
    "$src/snappie-cast.c" "$build/zkde-screencast-unstable-v1-protocol.c" "$build/fake-input-protocol.c" \
    $(pkg-config --libs wayland-client) ${LDFLAGS:-} -o "$1"
