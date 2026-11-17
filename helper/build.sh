#!/bin/sh
# Compatibility entry point for checkout installs; the sources ship with the package.
set -e
here=$(dirname "$(readlink -f "$0")")
PYTHONPATH="$here/..${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m snappie.capture_helper "$@"
