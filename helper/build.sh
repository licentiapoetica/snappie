#!/bin/sh
# Compatibility entry point for checkout installs; the sources ship with the package.
set -e
here=$(dirname "$(readlink -f "$0")")
exec sh "$here/../snappie/helper/build.sh" "$@"
