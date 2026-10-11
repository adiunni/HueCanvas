#!/bin/sh
# Double-click this file in Finder to start HueCanvas in Terminal.
set -eu
PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec /bin/sh "$PROJECT_DIR/start.sh" "$@"
