#!/bin/sh
# launcher: works from anywhere, also through a symlink on PATH
here=$(dirname "$(readlink -f "$0")")
PYTHONPATH="$here${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m snappie "$@"
